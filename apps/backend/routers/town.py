"""GooseTown API endpoints.

Serves two sets of endpoints:
1. Isol8-native endpoints (apartment, instance, registration) -- authenticated
2. AI Town-compatible endpoints (status, state, descriptions, etc.) -- public,
   return plain dicts matching AI Town's TypeScript class constructors
"""

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from core.auth import AuthContext, get_current_user
from core.internal_auth import require_sprite_event_secret, require_ws_lambda_secret
from core.services import exchange_scoring, exchange_service, goals_service, stances_service, town_mood_engine
from core.services.game_server_client import get_game_server_client
from core.services.owner_chat_service import (
    OwnerChatService,
    OwnershipError,
    RateLimitError,
)
from core.services.town_agent_ws import get_town_agent_ws_manager
from core.services.town_mood_engine import mood_label, parse_mood
from core.services.town_service import TownService, versioned_sprite_url
from core.ws_models import WsMessage
from schemas.town import (
    ApartmentAgentState,
    ApartmentResponse,
    ConversationTurn,
    HumanSayRequest,
    HumanSayResponse,
    NicknameRequest,
    ProfileResponse,
    TownConversationResponse,
    TownConversationsListResponse,
    WorldEventEntry,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_TOWN_WS_URL = os.environ.get("TOWN_WS_URL", "wss://ws-dev.goosetown.isol8.co")
_TOWN_API_URL = os.environ.get("TOWN_API_URL", "https://api-dev.goosetown.isol8.co/api/v1")

# Strong references to background sprite tasks to prevent GC
_background_sprite_tasks: set = set()


# ---------------------------------------------------------------------------
# Daemon bridge helpers live in `core.services.daemon_bridge`. Re-exported
# here so call sites (and integration tests patching
# `routers.town._send_to_daemon`) continue to resolve to the same function
# object. Sub-project #3 extraction, 2026-04-22.
# ---------------------------------------------------------------------------
from core.services.daemon_bridge import (  # noqa: E402, F401
    _apply_event_with_sleep_guard,
    _make_agent_event_callback,
    _send_to_daemon,
)

# ---------------------------------------------------------------------------
# Token auth dependency for external OpenClaw agents
# ---------------------------------------------------------------------------


async def get_town_token_user(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> tuple:
    """Validate a town_token from Authorization: Bearer <token>.
    Returns (user_id, token) or raises 401.

    The header is declared Optional deliberately. With ``Header(...)``
    (required) FastAPI treats a missing Authorization as a *body/param
    validation* error and returns **422 before this function ever runs** —
    which reads from the outside exactly like "no auth gate at all" and is
    how #426 came to be filed against /exchange/upload-url. Accepting None
    and raising here keeps the wire contract honest: no credentials → 401.
    """
    from core.town_token import verify_town_token

    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = authorization[7:]

    # Verify HMAC signature before DB lookup
    payload = verify_town_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid token signature")

    service = TownService()
    instance = service.get_instance_by_token(token)
    if not instance or instance.get("isActive") != "true":
        raise HTTPException(status_code=401, detail="Invalid or expired town token")
    return instance.get("userId"), token


# ---------------------------------------------------------------------------
# Map metadata (static -- tile data now loaded from TMJ on the frontend)
# ---------------------------------------------------------------------------


def _load_map_data() -> dict:
    """Return map metadata. Rendering is handled by Godot HTML5 export."""
    return {
        "width": 44,
        "height": 27,
        "tileDim": 32,
        "tileSetUrl": "",
        "mapUrl": "",
    }


# Persistent world ID (single world for now)
WORLD_ID = "world_default"
ENGINE_ID = "engine_default"


# ---------------------------------------------------------------------------
# Helper: build AI Town-format state from DB
# ---------------------------------------------------------------------------


def _build_default_state() -> dict:
    """Build empty AI Town state when no agents exist."""
    now_ms = int(time.time() * 1000)
    return {
        "world": {
            "nextId": 0,
            "players": [],
            "agents": [],
            "conversations": [],
        },
        "engine": {
            "currentTime": now_ms,
            "lastStepTs": now_ms - 16,
        },
        "playerDescriptions": [],
        "agentDescriptions": [],
        "speechBubbles": [],
    }


def _build_ai_town_state() -> dict:
    """Build AI Town-compatible world state from DynamoDB.

    Only includes agents registered via the per-agent registration flow.
    """
    try:
        service = TownService()
        db_states = service.get_town_state()
    except Exception:
        logger.warning("Failed to fetch town state from DynamoDB", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to load town state")

    if not db_states:
        return _build_default_state()

    # Only include agents in the town coordinate space
    db_states = [s for s in db_states if s.get("location_context", "apartment") == "town"]
    if not db_states:
        return _build_default_state()

    now_ms = int(time.time() * 1000)
    players = []
    agents = []
    player_descriptions = []
    agent_descriptions = []

    for i, s in enumerate(db_states):
        player_id = f"p:{i}"
        agent_id = f"a:{i}"

        # Positions come from game server, not DynamoDB — use zeros as placeholders
        players.append(
            {
                "id": player_id,
                "position": {"x": 0, "y": 0},
                "facing": {"dx": 0, "dy": 1},
                "speed": 0,
                "lastInput": now_ms,
            }
        )

        agents.append(
            {
                "id": agent_id,
                "playerId": player_id,
            }
        )

        player_descriptions.append(
            {
                "playerId": player_id,
                "name": s.get("display_name", s.get("agent_name", "Unknown")),
                "description": s.get("personality_summary", ""),
                "character": s.get("character"),
                "spriteUrl": s.get("sprite_url"),
            }
        )

        agent_descriptions.append(
            {
                "agentId": agent_id,
                "identity": s.get("personality_summary", ""),
                "plan": "",
            }
        )

    # Speech bubbles from recent conversations
    try:
        speech_bubbles = service.get_recent_speech(since_seconds=10.0)
    except Exception:
        logger.warning("Failed to fetch recent speech bubbles", exc_info=True)
        speech_bubbles = []

    return {
        "world": {
            "nextId": len(db_states),
            "players": players,
            "agents": agents,
            "conversations": [],
        },
        "engine": {
            "currentTime": now_ms,
            "lastStepTs": now_ms - 16,
        },
        "playerDescriptions": player_descriptions,
        "agentDescriptions": agent_descriptions,
        "speechBubbles": speech_bubbles,
    }


# ===========================================================================
# Skill endpoint -- public, serves SKILL.md + tool bundle for agents
# ===========================================================================

_skill_md_cache: str | None = None
_skill_bundle_cache: bytes | None = None
_SKILL_DIR = Path(__file__).resolve().parent.parent / "data" / "goosetown-skill" / "skills" / "goosetown"


def _load_skill_md() -> str:
    global _skill_md_cache
    if _skill_md_cache is None:
        skill_path = _SKILL_DIR / "SKILL.md"
        if not skill_path.exists():
            raise HTTPException(404, "Skill not found")
        _skill_md_cache = skill_path.read_text()
    return _skill_md_cache


# The daemon CLI is no longer bundled. The single source of truth for the
# resident daemon is the PyPI package (`pip install goosetown` /
# `uv tool install goosetown`) — the bundled `goosetown_cli.py` monolith had
# drifted badly (missing the own-message filter and every CLI 0.1.1–0.1.7 fix)
# and was removed. This endpoint still serves the docs (SKILL.md + references/)
# and the wake-on-status hook so the legacy tarball path keeps working.
_SKILL_BUNDLE_FILES = (
    "SKILL.md",
    "GOOSETOWN.md",
    "TOWN_STATUS.md",
    "TOWN_EVENTS.md",
    "TOWN_LIFE.md",
    "hooks/check-town-status.sh",
    "references/character.md",
    "references/actions.md",
    "references/owner-dms.md",
    "references/troubleshooting.md",
    "references/openclaw.md",
    "references/exchange.md",
)


def _build_skill_bundle() -> bytes:
    """Pack the current skill directory into a tarball for `GET /skill/bundle`.

    Earlier versions packed nonexistent ``tools/`` and ``daemon/`` subdirs
    (left over from a refactor) and shipped an empty 45-byte tarball; the
    explicit allowlist below prevents that class of regression. New skill
    files must be added to ``_SKILL_BUNDLE_FILES``.
    """
    global _skill_bundle_cache
    if _skill_bundle_cache is None:
        import io
        import tarfile

        buf = io.BytesIO()
        included = 0
        # Files whose extracted permissions matter — preserved with the
        # executable bit set explicitly because Docker image layer caching
        # strips +x off `*.sh` files in some build configurations. Without
        # this, the tarball ships a non-executable hook script and any
        # caller's `setup-hooks` writes a `bash <script>` command that
        # silently fails. Surfaced by Jim during the agent-team test on
        # 2026-05-02 (T12 OpenClaw smoke).
        _EXECUTABLE_BUNDLE_FILES = {"hooks/check-town-status.sh"}

        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for filename in _SKILL_BUNDLE_FILES:
                filepath = _SKILL_DIR / filename
                if filepath.is_file():
                    tarinfo = tar.gettarinfo(str(filepath), arcname=f"goosetown/{filename}")
                    if filename in _EXECUTABLE_BUNDLE_FILES:
                        tarinfo.mode = 0o755
                    else:
                        tarinfo.mode = 0o644
                    with open(filepath, "rb") as fh:
                        tar.addfile(tarinfo, fileobj=fh)
                    included += 1
        if included == 0:
            raise RuntimeError(
                "Skill bundle would be empty — missing all expected files in "
                f"{_SKILL_DIR}. Check that the skill directory is shipped with the container."
            )
        _skill_bundle_cache = buf.getvalue()
    return _skill_bundle_cache


@router.get("/skill")
async def get_skill():
    """Serve the GooseTown SKILL.md for agents to read and follow."""
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse(_load_skill_md(), media_type="text/markdown")


@router.get("/skill/bundle")
async def get_skill_bundle():
    """Download the GooseTown skill tools as a tarball.

    DEPRECATED: install via Claude Code plugin marketplace
    (`/plugin marketplace add github.com/Isol8AI/goosetown` then
    `/plugin install goosetown`) or by dropping the skill folder into
    `~/.claude/skills/goosetown/`. This endpoint stays alive through
    subs 2 + 3 to give existing users time to migrate.
    """
    from fastapi.responses import Response

    bundle = _build_skill_bundle()
    return Response(
        content=bundle,
        media_type="application/gzip",
        headers={
            "Content-Disposition": "attachment; filename=goosetown-skill.tar.gz",
            "Deprecation": "true",
            "Link": '<https://github.com/Isol8AI/goosetown#install>; rel="deprecation"',
        },
    )


# ===========================================================================
# AI Town-compatible endpoints (public, no auth required)
# ===========================================================================


@router.get("/status")
async def get_world_status():
    """Return default world status. Game.tsx calls this first."""
    now_ms = int(time.time() * 1000)
    return {
        "worldId": WORLD_ID,
        "engineId": ENGINE_ID,
        "status": "running",
        "lastViewed": now_ms,
        "isDefault": True,
    }


@router.get("/state")
async def get_world_state(
    worldId: Optional[str] = Query(None),
):
    """Return world state in AI Town format.

    Used by useServerGame + useHistoricalTime for PixiJS rendering.
    """
    state = _build_ai_town_state()
    return {
        "world": state["world"],
        "engine": state["engine"],
        "speechBubbles": state.get("speechBubbles", []),
    }


@router.get("/descriptions")
async def get_game_descriptions(
    worldId: Optional[str] = Query(None),
):
    """Return map data + agent/player descriptions.

    Used by useServerGame to construct WorldMap, PlayerDescription,
    AgentDescription objects.
    """
    state = _build_ai_town_state()
    world_map = _load_map_data()

    return {
        "worldMap": world_map,
        "playerDescriptions": state["playerDescriptions"],
        "agentDescriptions": state["agentDescriptions"],
    }


# ===========================================================================
# Agent registration endpoints (town_token auth)
# ===========================================================================


class AgentRegisterRequest(BaseModel):
    agent_name: str = Field(..., min_length=1, max_length=50, pattern="^[a-zA-Z0-9_-]+$")
    display_name: str = Field(..., min_length=1, max_length=100)
    personality: str = Field("", max_length=500)
    appearance: str = Field("", max_length=500)
    traits: str = Field("", max_length=200)


class OwnerMessageRequest(BaseModel):
    text: str


@router.post("/agent/register")
async def register_agent(
    request: AgentRegisterRequest = Body(...),
    token_info: tuple = Depends(get_town_token_user),
):
    """Register a new agent in GooseTown. Authenticated via town_token."""
    user_id, token = token_info

    service = TownService()
    instance = service.get_active_instance(user_id)
    if not instance:
        raise HTTPException(400, "No active instance")

    existing = service.get_agent_by_name(user_id, request.agent_name)
    if existing:
        raise HTTPException(400, f"Agent '{request.agent_name}' already registered")

    # Use opt_in which handles DynamoDB creation
    agent = service.opt_in(
        user_id=user_id,
        agent_name=request.agent_name,
        display_name=request.display_name,
        personality_summary=request.personality[:200] if request.personality else None,
    )

    # One-time signup grant for the Exchange (non-fatal — self-heals on first wallet use).
    try:
        exchange_service.ensure_signup_grant(agent["id"])
    except Exception:
        logger.warning("Signup grant failed for %s (self-heals on first wallet use)", agent.get("id"))

    # Trigger PixelLab sprite generation in background
    logger.info(
        "Sprite check: appearance=%s, appearance_val=%s",
        bool(request.appearance),
        request.appearance[:50] if request.appearance else "empty",
    )
    if request.appearance:
        from core.config import settings
        from core.services.user_service import UserService

        # Sprite-quota check BEFORE kicking off PixelLab — so a rejected
        # request doesn't enqueue work that will never be allowed to run.
        UserService().consume_sprite_quota(user_id)

        logger.info(
            "Sprite settings: api_key=%s, bucket=%s, cdn=%s",
            bool(settings.PIXELLAB_API_KEY),
            settings.SPRITE_S3_BUCKET,
            settings.SPRITE_CDN_URL,
        )
        if settings.PIXELLAB_API_KEY and settings.SPRITE_S3_BUCKET and settings.SPRITE_CDN_URL:
            _agent_id = agent["id"]

            _existing = service.get_town_agent_by_id(_agent_id)
            from core.services.sprite_jobs import IN_PROGRESS_STATUSES

            if _existing and _existing.get("spriteStatus") in IN_PROGRESS_STATUSES:
                logger.info(
                    "Sprite generation in progress for %s (%s)",
                    _agent_id,
                    _existing.get("spriteStatus"),
                )
            else:
                _appearance = request.appearance
                _display_name = request.display_name

                # Persist appearance on the row BEFORE enqueueing.  opt_in does
                # not store appearance; the sprite worker reads the row as the
                # source of truth, so without this write it would hit the
                # "no appearance → warn + return" path and strand the agent at
                # spriteStatus="queued" with no sprite and no claim release.
                service.update_agent_state(_agent_id, appearance=_appearance)

                from core.services import sprite_jobs

                queued = sprite_jobs.enqueue_signup(_agent_id, _display_name, _appearance)
                if queued:
                    _send_to_daemon(
                        _agent_id,
                        {"event": "sprite_progress", "status": "Queued for sprite generation"},
                    )

    # Best-effort SQS queue creation for WorldGM personal events.
    # Do not fail the registration if queue setup fails.
    try:
        from core.services import sqs_drainer

        sqs_drainer.create_agent_queue(agent["id"])
    except Exception as _sqse:
        logger.warning(
            "register: sqs_drainer.create_agent_queue failed for %s (non-fatal): %s",
            agent["id"][:8],
            _sqse,
        )

    return {
        "agent_id": agent["id"],
        "agent_name": agent.get("agentName", request.agent_name),
        "display_name": agent.get("displayName", request.display_name),
        "status": "generating_sprite",
        "ws_url": _TOWN_WS_URL,
        "api_url": _TOWN_API_URL,
        "message": f"Welcome to GooseTown, {request.display_name}!",
    }


# `resume_sprite_generation` lives in `core.services.sprite_pipeline`
# (sub-project #3 final slice, 2026-04-23). Re-imported here so the router
# module remains a one-stop back-compat surface for any external caller.
# Used by the admin recomposite endpoint; the regenerate endpoint now uses
# sprite_jobs.enqueue_signup (Task 7).
from core.services.sprite_pipeline import resume_sprite_generation  # noqa: E402, F401


@router.get("/agent/status")
async def get_agent_status(
    agent_name: str = Query(...),
    token_info: tuple = Depends(get_town_token_user),
):
    """Check agent sprite readiness. Polls PixelLab if sprite not yet downloaded."""
    from core.config import settings

    user_id, _ = token_info
    service = TownService()
    agent = service.get_agent_by_name(user_id, agent_name)
    if not agent:
        raise HTTPException(404, "Agent not found")

    # Already done
    if agent.get("spriteReady") and agent.get("spriteUrl"):
        return {
            "agent_name": agent.get("agentName"),
            "sprite_ready": True,
            "sprite_url": agent.get("spriteUrl"),
        }

    # No PixelLab character queued
    pixellab_char_id = agent.get("pixellabCharacterId")
    if not pixellab_char_id:
        return {
            "agent_name": agent.get("agentName"),
            "sprite_ready": False,
            "sprite_url": None,
        }

    # Check PixelLab and download/upload if ready
    if settings.PIXELLAB_API_KEY and settings.SPRITE_S3_BUCKET:
        try:
            from core.services.pixellab_service import PixelLabService
            from core.services.sprite_storage import (
                composite_animation_spritesheet,
                upload_sprite_to_s3,
            )

            pxl = PixelLabService(api_key=settings.PIXELLAB_API_KEY)
            raw_animations = await pxl.fetch_animation_spritesheets(pixellab_char_id)
            if raw_animations and "walk" in raw_animations:
                result = composite_animation_spritesheet(raw_animations["walk"])
                if result:
                    png_bytes, _frame_count = result
                    s3_key = await asyncio.to_thread(
                        upload_sprite_to_s3,
                        png_bytes,
                        agent["id"],
                        settings.SPRITE_S3_BUCKET,
                        "walk.png",
                    )
                    cdn_url = f"{settings.SPRITE_CDN_URL}/{s3_key}"
                    service.update_agent_state(agent["id"], sprite_ready=True, sprite_url=cdn_url)
                    return {
                        "agent_name": agent.get("agentName"),
                        "sprite_ready": True,
                        "sprite_url": cdn_url,
                    }
        except Exception as e:
            logger.warning("Sprite status check failed: %s", e)

    return {
        "agent_name": agent.get("agentName"),
        "sprite_ready": False,
        "sprite_url": None,
    }


@router.post("/agent/{agent_id}/owner-message")
async def post_owner_message(
    agent_id: str,
    request: OwnerMessageRequest,
    auth: AuthContext = Depends(get_current_user),
):
    """Send a private message from the signed-in owner to one of their agents.

    Auth: Clerk JWT. Rejects 403 if ``agent.userId != auth.user_id``.
    Rate limit: 1 message per 2s per (owner, agent) pair.

    On success: persists to ``owner_chats``, pushes ``owner_message`` event
    to the agent's daemon via the existing _send_to_daemon helper, and returns
    the persisted record.
    """
    if not request.text or not request.text.strip():
        raise HTTPException(status_code=400, detail="text is required")

    service = OwnerChatService()
    try:
        msg = service.persist_message(
            agent_id=agent_id,
            owner_id=auth.user_id,
            direction="owner_to_agent",
            text=request.text,
        )
    except OwnershipError:
        raise HTTPException(status_code=403, detail="not your agent")
    except RateLimitError:
        raise HTTPException(
            status_code=429,
            detail="too many messages — wait a moment",
            headers={"Retry-After": "2"},
        )

    # Daemon's dispatcher reads ``data["event"]`` — see every other
    # backend→daemon payload in core/services/conversation_service.py.
    # The original draft used ``"type":`` which the daemon silently
    # ignored, so owner messages never reached the agent.
    _send_to_daemon(
        agent_id,
        {
            "event": "owner_message",
            "text": msg["text"],
            "message_id": msg["id"],
            "timestamp": msg["createdAt"],
        },
    )

    return {"id": msg["id"], "createdAt": msg["createdAt"]}


@router.post("/agent/{agent_id}/deactivate")
async def deactivate_agent(
    agent_id: str,
    auth: AuthContext = Depends(get_current_user),
):
    """Soft-delete an agent: set ``isActive=false`` and despawn from the world.

    Owner-auth (Clerk JWT). 403 if ``agent.userId != auth.user_id``.
    Idempotent: returning agents already inactive is a 200 with the current
    state (not a 409) so the UI's "Remove" button is safe to retry.

    Soft-delete only — the agent record (TOWN_LIFE.md, sprite, conversation
    history, relationships) is preserved. The slot is freed against the
    per-user quota; a future ``/reactivate`` call brings the agent back.

    Side effects:
      * ``isActive`` flips to ``"false"`` (string, matches existing schema).
      * Active WebSocket connection (if any) is unregistered from the
        in-memory manager so subsequent ``_send_to_daemon`` no-ops.
      * Game-server agent (if connected) is despawned via
        ``disconnect_agent`` so other agents stop seeing them.
    """
    service = TownService()
    agent = service.get_town_agent_by_id(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="agent not found")
    if agent.get("userId") != auth.user_id:
        raise HTTPException(status_code=403, detail="not your agent")

    if agent.get("isActive") != "true":
        # Already inactive — idempotent return.
        return {"id": agent_id, "isActive": False}

    service.set_agent_active(agent_id, False)

    # Best-effort cleanup of in-memory connection state. None of these
    # raise on missing entries — they're idempotent no-ops if the agent
    # wasn't connected.
    try:
        ws_manager = get_town_agent_ws_manager()
        ws_manager.unregister_by_agent_id(agent_id)
    except Exception as e:
        logger.warning("deactivate: ws unregister failed for %s: %s", agent_id, e)

    try:
        gs_client = get_game_server_client()
        await gs_client.disconnect_agent(agent_id)
    except Exception as e:
        logger.warning("deactivate: game server despawn failed for %s: %s", agent_id, e)

    # Best-effort SQS queue + EventBridge rule teardown for WorldGM personal events.
    try:
        from core.services import sqs_drainer

        sqs_drainer.delete_agent_queue(agent_id)
    except Exception as _sqse:
        logger.warning(
            "deactivate: sqs_drainer.delete_agent_queue failed for %s (non-fatal): %s",
            agent_id[:8],
            _sqse,
        )

    return {"id": agent_id, "isActive": False}


@router.post("/agent/{agent_id}/reactivate")
async def reactivate_agent(
    agent_id: str,
    auth: AuthContext = Depends(get_current_user),
):
    """Re-activate a previously deactivated agent.

    Owner-auth. Quota-gated: returns 429 if the user is already at
    ``AGENT_QUOTA`` active agents. Idempotent: returns 200 if already
    active.

    Reactivation does NOT auto-reconnect the agent's daemon — the agent
    must run ``goosetown act idle`` (or any other ``goosetown act ...``)
    in their workspace to re-spawn the daemon, which in turn re-connects
    to the WebSocket and to the game server. This avoids surprising the
    user with a still-living daemon process from a previous session.
    """
    from core.quota import QuotaExceeded

    service = TownService()
    agent = service.get_town_agent_by_id(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="agent not found")
    if agent.get("userId") != auth.user_id:
        raise HTTPException(status_code=403, detail="not your agent")

    if agent.get("isActive") == "true":
        return {"id": agent_id, "isActive": True}

    try:
        service.set_agent_active(agent_id, True)
    except QuotaExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "quota_exceeded",
                "kind": "agents",
                "limit": exc.limit,
                "current": exc.current,
                "message": (
                    f"You have {exc.current} active agents (limit {exc.limit}). Deactivate another agent first."
                ),
            },
        ) from exc
    return {"id": agent_id, "isActive": True}


# Holds references to in-flight re-composite background tasks so the event loop
# doesn't garbage-collect them mid-run (fire-and-forget create_task otherwise risks GC).
_recomposite_tasks: set = set()


async def require_admin_user(auth: AuthContext = Depends(get_current_user)) -> AuthContext:
    """Authenticate an operator for the /town/admin/* ops tools (#430).

    These endpoints used to be guarded ONLY by ``settings.ENVIRONMENT != "dev"``.
    That is a deploy-environment kill switch, not an authentication check: it
    made them unreachable in prod (including for real admins) while leaving them
    **completely anonymous on the public dev ALB** — where ``regenerate-sprite``
    spends real PixelLab credits and ``recomposite-sprites`` fans out across
    every agent in the table.

    Now: a valid Clerk JWT is required, and if ``ADMIN_USER_IDS`` is configured
    the caller must additionally be on that allowlist. Declaring this via
    ``Depends`` also makes the HTTPBearer security scheme show up in the OpenAPI
    spec, so the schema stops advertising these routes as public.
    """
    from core.config import settings

    allowlist = {uid.strip() for uid in settings.ADMIN_USER_IDS.split(",") if uid.strip()}
    if allowlist and auth.user_id not in allowlist:
        logger.warning("Admin endpoint denied for non-admin user %s", auth.user_id)
        raise HTTPException(status_code=403, detail="admin access required")
    return auth


@router.post("/admin/recomposite-sprites", dependencies=[Depends(require_admin_user)])
async def recomposite_sprites():
    """DEV-ONLY: re-composite existing sprites for every agent through the fixed
    gray-strip path (#347).

    Re-downloads the agent's already-generated PixelLab frames (no new generation
    credits — `resume_sprite_generation` at the ``downloading`` phase skips
    create/animate), re-composites them with the gray background stripped, and
    re-uploads to S3. The cache-bust (#354) makes the new sheets show immediately.

    One-off backfill for sprites generated before the gray-strip fix landed.
    Runs sequentially in a background task to avoid hammering the PixelLab API.

    Auth: ``require_admin_user`` (Clerk JWT + optional ``ADMIN_USER_IDS``
    allowlist), THEN the dev-only environment gate. Both layers matter — the
    env gate alone left this anonymous on the public dev ALB (#430).
    """
    from core import dynamo
    from core.config import settings

    if settings.ENVIRONMENT != "dev":
        raise HTTPException(status_code=403, detail="dev-only endpoint")

    resp = dynamo.agents_table().scan(FilterExpression="attribute_exists(pixellabCharacterId)")
    agents = [a for a in resp.get("Items", []) if a.get("pixellabCharacterId")]

    async def _run_all() -> None:
        from core.services.pixellab_service import PixelLabCreditError

        for a in agents:
            try:
                # Per-agent timeout: a hung PixelLab fetch (no timeout of its own)
                # must NOT stall the whole sequential batch — abandon it and move
                # on so the remaining agents still get re-composited.
                await asyncio.wait_for(
                    resume_sprite_generation(
                        a["id"],
                        a["pixellabCharacterId"],
                        "",  # appearance unused at the 'downloading' phase
                        a.get("displayName") or a.get("agentName") or "agent",
                        "downloading",
                    ),
                    timeout=90,
                )
            except TimeoutError:
                logger.warning("recomposite-sprites: timed out for %s, skipping", a.get("id"))
            except PixelLabCreditError:
                logger.error("ALARM: PixelLab credits exhausted during sprite generation for %s", a.get("id"))
            except Exception:
                logger.exception("recomposite-sprites: failed for %s", a.get("id"))

    task = asyncio.create_task(_run_all())
    _recomposite_tasks.add(task)
    task.add_done_callback(_recomposite_tasks.discard)
    return {"queued": len(agents)}


@router.post("/admin/regenerate-sprite/{agent_id}", dependencies=[Depends(require_admin_user)])
async def regenerate_sprite(agent_id: str):
    """DEV-ONLY: full from-scratch sprite regeneration for ONE agent.

    Unlike /recomposite-sprites (which re-downloads existing frames, no credits),
    this creates a BRAND-NEW PixelLab character — it COSTS generation credits.
    Use it when an agent's existing character is broken/hangs (re-composite can't
    fix that). Clears pixellabCharacterId + sets spriteStatus="queued" so the
    SQS sprite worker starts from the ``creating`` phase.

    Single-agent only — not a batch.

    Auth: ``require_admin_user`` (Clerk JWT + optional ``ADMIN_USER_IDS``
    allowlist), THEN the dev-only environment gate. This endpoint SPENDS
    PixelLab credits, so anonymous reachability on the public dev ALB was a
    real cost vector against a PixelLab account shared with prod (#430).
    """
    from core.config import settings

    if settings.ENVIRONMENT != "dev":
        raise HTTPException(status_code=403, detail="dev-only endpoint")

    service = TownService()
    agent = service.get_town_agent_by_id(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="agent not found")

    display_name = agent.get("displayName") or agent.get("agentName") or "agent"

    # Clear existing PixelLab character so the worker creates a fresh one.
    # force=True bypasses the conditional DDB claim — state is already reset.
    service.update_agent_state(agent_id, sprite_status="queued", pixellab_character_id=None)
    from core.services import sprite_jobs

    sent = sprite_jobs.enqueue_signup(agent_id, agent.get("displayName", ""), agent.get("appearance", ""), force=True)
    return {"agent_id": agent_id, "ok": sent, "status": "regenerating", "display_name": display_name}


@router.get("/agent/{agent_id}/owner-chat")
async def get_owner_chat(
    agent_id: str,
    limit: int = 50,
    before: int | None = None,
    auth: AuthContext = Depends(get_current_user),
):
    """Return the owner's chat history with the agent, oldest first.

    Auth: Clerk JWT. Rejects 403 if ``agent.userId != auth.user_id``.
    ``before`` (Unix ms) pages backward — return messages with
    ``createdAt < before``. ``limit`` is capped at 200.
    """
    service = OwnerChatService()
    try:
        history = service.fetch_history(
            agent_id=agent_id,
            owner_id=auth.user_id,
            limit=max(1, min(limit, 200)),
            before_ms=before,
        )
    except OwnershipError:
        raise HTTPException(status_code=403, detail="not your agent")
    return {"messages": history}


def _handle_reply_owner_action(
    *,
    agent_id: str,
    owner_id: str,
    text: str,
    ref_message_id: str | None,
) -> None:
    """Persist an agent's owner-reply and push it to the owner's viewer connections.

    Called from the WS ``town_agent_act`` dispatcher when a daemon emits
    ``{action: "reply_owner", text, ref_message_id?}``. The agent's owner_id is
    derived from the authenticated WS connection's ``AgentConnection.user_id``,
    not from the message body — daemons can't spoof another user's reply.
    """
    if not text or not text.strip():
        logger.warning("reply_owner: empty text from agent %s — dropping", agent_id)
        return

    service = OwnerChatService()
    try:
        msg = service.persist_message(
            agent_id=agent_id,
            owner_id=owner_id,
            direction="agent_to_owner",
            text=text,
        )
    except OwnershipError:
        # Should never happen: ws connection is authenticated per-agent and
        # owner_id comes from the AgentConnection. Log and drop.
        logger.error(
            "reply_owner: ownership mismatch agent=%s owner=%s",
            agent_id,
            owner_id,
        )
        return

    _push_to_owner_viewers(
        user_id=owner_id,
        payload={
            "event": "owner_reply",
            "agent_id": agent_id,
            "text": msg["text"],
            "message_id": msg["id"],
            "timestamp": msg["createdAt"],
            "ref_message_id": ref_message_id,
        },
    )


def _push_to_owner_viewers(*, user_id: str, payload: dict) -> None:
    """Push a payload to every viewer connection associated with ``user_id``.

    Looks up the user's viewer connections in the ``connections`` table and
    sends each one through the API Gateway Management API. No-op if the user
    has no active viewer connections (their reply will be fetched via REST
    history when they reopen the panel). Per-connection failures are logged
    but do not abort the broadcast.
    """
    from core import dynamo
    from core.services.daemon_mailbox import get_daemon_mailbox

    table = dynamo.connections_table()
    scan_kwargs: dict = {
        "FilterExpression": "userId = :uid AND connectionType = :ct",
        "ExpressionAttributeValues": {":uid": user_id, ":ct": "viewer"},
    }
    items: list[dict] = []
    while True:
        try:
            resp = table.scan(**scan_kwargs)
        except Exception as exc:
            logger.warning("push_to_owner_viewers: scan failed: %s", exc)
            return
        items.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key

    if not items:
        return

    mailbox = get_daemon_mailbox()
    for item in items:
        connection_id = item.get("connectionId")
        if not connection_id:
            continue
        mailbox.notify_connection(connection_id, payload)


# ---------------------------------------------------------------------------
# Coercion helpers — guard the WS_STRICT_VALIDATION=false rollback path so
# raw, unvalidated bodies cannot cause a 500 or silently corrupt intent.
# ---------------------------------------------------------------------------


def _coerce_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ===========================================================================
# Isol8-native endpoints (authenticated)
# ===========================================================================


@router.get("/apartment", response_model=ApartmentResponse)
async def get_apartment(
    auth: AuthContext = Depends(get_current_user),
):
    """Get the user's apartment view -- their agents and recent activity."""
    service = TownService()
    # Query the userId-agentName GSI directly instead of scanning every
    # active agent globally then filtering. /apartment polls every 2s per
    # user, and the old scan-then-filter cost scaled with total town
    # population × users online.
    user_agents = service.get_active_agents_for_user(auth.user_id)

    agents = []
    for item in user_agents:
        agents.append(
            ApartmentAgentState(
                agent_id=item["id"],
                agent_name=item.get("agentName", ""),
                display_name=item.get("displayName", ""),
                character=item.get("character"),
                location_context=item.get("locationContext", "apartment"),
                current_location=item.get("currentLocation", "residence"),
                current_activity=item.get("currentActivity"),
                mood=mood_label(parse_mood(item.get("mood"))),
                energy=item.get("energy", 100),
                status_message=item.get("statusMessage"),
                position_x=0.0,
                position_y=0.0,
                speed=0.0,
                facing_x=0.0,
                facing_y=1.0,
                current_spot=item.get("currentLocation"),
                is_active=item.get("isActive") == "true",
                sprite_url=versioned_sprite_url(item.get("spriteUrl"), item) if item.get("spriteReady") else "",
                currentGoal=item.get("currentGoal"),
                wallet_balance=int(item.get("walletBalance", 0) or 0),
                trust_tier=exchange_scoring.trust_tier(
                    float(item.get("trustScore", 0.5) or 0.5),
                    int(item.get("jobsCompleted", 0) or 0),
                ),
            )
        )

    return ApartmentResponse(agents=agents, activity=[])


def _token_uid(town_token: str) -> str | None:
    """Best-effort decode of the 'uid' field from a signed town_token.
    Returns None on malformed input — no signature verification here, the
    caller only uses this to detect uid drift before re-signing.
    """
    import base64
    import json

    try:
        payload_b64 = town_token.split(".", 1)[0]
        pad = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + pad))
        return payload.get("uid")
    except Exception:
        return None


@router.post("/instance")
async def get_or_create_instance(
    auth: AuthContext = Depends(get_current_user),
):
    """Get existing instance or create a new one. Returns town_token."""
    from core.town_token import sign_town_token

    service = TownService()
    instance = service.get_active_instance(auth.user_id)

    if not instance:
        instance = service.create_instance(auth.user_id)
    else:
        # Re-sign the stored token when it's missing, legacy-unsigned, or
        # signed for a different uid (e.g. userId column reassigned since).
        stored = instance.get("townToken", "")
        needs_resign = "." not in stored or _token_uid(stored) != auth.user_id
        if needs_resign:
            instance["townToken"] = sign_town_token(auth.user_id, instance["id"])
            from core import dynamo

            table = dynamo.instances_table()
            table.update_item(
                Key={"id": instance["id"]},
                UpdateExpression="SET townToken = :t",
                ExpressionAttributeValues={":t": instance["townToken"]},
            )

    agents = service.get_instance_agents(instance["id"])

    return {
        "town_token": instance.get("townToken"),
        "apartment_unit": instance.get("apartmentUnit"),
        "agents": [
            {
                "agent_name": a.get("agentName"),
                "display_name": a.get("displayName"),
                "sprite_url": a.get("spriteUrl"),
            }
            for a in agents
        ],
    }


@router.post("/instance/revoke", status_code=204)
async def revoke_instance_tokens(
    auth: AuthContext = Depends(get_current_user),
):
    """Revoke all GooseTown tokens previously issued to this user.

    Sets the user's tokenRevokedAt timestamp; any town_token whose iat
    is earlier than that timestamp will be rejected by verify_town_token.
    User can mint a fresh one via the existing POST /town/instance.

    Returns 204 No Content on success.
    """
    from core.services.user_service import UserService

    UserService().revoke_tokens(auth.user_id)
    return None


@router.get("/quota")
async def get_quota(
    auth: AuthContext = Depends(get_current_user),
):
    """Per-user quota state for UI display.

    Returns the authenticated user's current agent count and sprite-generation
    usage in the current UTC day (``used_24h`` keeps its name for the UI),
    alongside the corresponding hard limits.  Used by the QuotaIndicator
    component on the agents sidebar.
    """
    from core.services.town_service import TownService
    from core.services.user_service import AGENT_QUOTA, SPRITE_QUOTA_DAILY, UserService

    sprites_used = UserService().sprites_used_today(auth.user_id)

    active_agents = TownService().get_active_agents_for_user(auth.user_id)

    return {
        "agents": {
            "used": len(active_agents),
            "limit": AGENT_QUOTA,
        },
        "sprite_gen": {
            "used_24h": sprites_used,
            "limit": SPRITE_QUOTA_DAILY,
        },
    }


@router.get("/viewer-token")
async def get_viewer_token(
    auth: AuthContext = Depends(get_current_user),
):
    """Issue a viewer session token for Godot WebSocket connection.

    Returns an HMAC-signed token that the frontend passes to the Godot
    WebGL client so it can open an authenticated WebSocket to the
    API Gateway.  The token uses iid="viewer" to distinguish viewer
    connections from agent connections.
    """
    from core.town_token import sign_town_token

    token = sign_town_token(auth.user_id, "viewer")
    return {"token": token}


@router.get("/anonymous-viewer-token")
async def get_anonymous_viewer_token():
    """Mint a short-lived viewer token for unauthenticated WebSocket access.

    No auth required. Token is HMAC-signed via the same TOWN_TOKEN_SECRET
    as registered agents and signed-in viewers. The Lambda authorizer
    accepts it transparently. Anonymous viewers can subscribe to
    world_state but cannot register, act, or send chat — those endpoints
    enforce a real user_id (Clerk subject) at the router layer.
    """
    from core.town_token import sign_town_token

    # 30-min TTL: short enough that leaked tokens have minimal blast radius,
    # long enough for a casual viewing session before the WS hook needs to
    # refetch on reconnect.
    token = sign_town_token("anonymous", "anonymous-viewer", ttl_seconds=1800)
    return {"token": token}


@router.get("/conversations", response_model=TownConversationsListResponse)
async def get_conversations(limit: int = 20):
    """Get recent public conversations.

    Public endpoint — conversations in GooseTown are visible to any viewer,
    like the rest of the town state.
    """
    service = TownService()
    convos = service.get_recent_conversations(limit=limit)

    # Cache display-name lookups across conversations — group chats often share
    # participants, so resolving each id repeatedly per row is wasteful.
    display_name_cache: dict[str, str] = {}

    def _resolve(agent_id: str) -> str:
        if not agent_id:
            return "Unknown"
        if agent_id not in display_name_cache:
            agent = service.get_town_agent_by_id(agent_id)
            display_name_cache[agent_id] = agent.get("displayName", "Unknown") if agent else "Unknown"
        return display_name_cache[agent_id]

    responses = []
    for c in convos:
        # ``participants`` is the canonical list (populated for all new
        # conversations + group chats). Older 2-party rows may only have
        # ``participantAId``/``participantBId`` — fall back to those.
        raw_participants: list[str] = c.get("participants") or []
        if not raw_participants:
            raw_participants = [
                c.get("participantAId", ""),
                c.get("participantBId", ""),
            ]
        # Drop empties + dedupe while preserving order.
        seen: set[str] = set()
        ordered_ids: list[str] = []
        for pid in raw_participants:
            if pid and pid not in seen:
                seen.add(pid)
                ordered_ids.append(pid)

        participant_names = [_resolve(pid) for pid in ordered_ids]

        # Legacy fields keep pointing at the first two participants for
        # backward compatibility. Use "" (not "Unknown") when there's no
        # participant in that slot at all — the frontend's
        # ``[participant_a, participant_b].filter(Boolean)`` fallback path
        # depends on this so a row with no resolvable participants doesn't
        # render as "Unknown & Unknown". The per-participant "Unknown"
        # placeholder emitted by ``_resolve`` for an agent whose record was
        # deleted is still useful in the canonical ``participants`` array
        # ("Rachel & Unknown" tells the user one participant's data is gone).
        participant_a = participant_names[0] if participant_names else ""
        participant_b = participant_names[1] if len(participant_names) > 1 else ""

        public_log = c.get("publicLog") or []

        responses.append(
            TownConversationResponse(
                id=c.get("id", ""),
                participant_a=participant_a,
                participant_b=participant_b,
                participants=participant_names,
                location=c.get("location"),
                started_at=c.get("startedAt"),
                ended_at=c.get("endedAt"),
                turn_count=c.get("turnCount", 0),
                topic_summary=c.get("topicSummary"),
                public_log=[ConversationTurn(speaker=t["speaker"], text=t["text"]) for t in public_log],
            )
        )

    return TownConversationsListResponse(conversations=responses)


@router.get("/exchange/jobs")
async def get_exchange_jobs(category: str | None = Query(default=None)):
    """Public Exchange board: open jobs + recent history (frontend polling).

    ``category`` (optional): when given, OPEN jobs are filtered to those whose
    ``category`` equals the filter or starts with ``filter + "."``.  History
    entries are returned unfiltered.  Normalization happens inside
    ``list_board_jobs``, which distinguishes no-filter from an invalid slug —
    an invalid slug returns an empty OPEN set (no 4xx — history still shows).
    """
    return {"jobs": exchange_service.list_board_jobs(category=category)}


@router.get("/exchange/providers")
async def get_exchange_providers(
    category: str | None = Query(default=None),
    skill: str | None = Query(default=None),
    min_trust: float = Query(default=0.0),
):
    """Public provider directory: agents registered as providers.

    Query params (all optional):
    - ``category``: slug filter — returns providers subscribed to this
      category (or any parent, via wildcard-parent expansion).  Value is
      normalised; invalid slug → empty list (no 4xx).
    - ``skill``: membership filter against ``providerSkills``.
    - ``min_trust``: float [0, 1] (clamped); providers below this trust
      score are excluded.  Values >1 are clamped to 1.0; values <0 to 0.0.

    Returns ``{"providers": [...]}``; each entry is the directory's public
    profile shape — no wallet balance, no PII (see ProviderDirectory._public_profile).
    """
    from core.services.exchange_categories import normalize_category as _norm_cat
    from core.services.provider_directory import ProviderDirectory

    # A caller-supplied category that doesn't normalize is an invalid filter —
    # return no matches rather than silently falling back to the scan-all
    # (no-category) branch, which would dump the whole directory.
    if category is not None:
        norm_cat = _norm_cat(category)
        if norm_cat is None:
            return {"providers": []}
    else:
        norm_cat = None
    clamped_trust = max(0.0, min(1.0, float(min_trust)))
    providers = ProviderDirectory().search(
        category=norm_cat,
        skill=skill if skill else None,
        min_trust=clamped_trust,
    )
    return {"providers": providers}


class ExchangeUploadUrlRequest(BaseModel):
    job_id: str = Field(..., min_length=1, max_length=64)
    filename: str = Field(..., min_length=1, max_length=256)
    content_length: int = Field(..., ge=1, le=exchange_service.ARTIFACT_MAX_BYTES)


@router.post("/exchange/upload-url")
async def exchange_upload_url(
    request: ExchangeUploadUrlRequest = Body(...),
    token_info: tuple = Depends(get_town_token_user),
):
    """Presigned S3 upload for a job artifact (W5, town_token Bearer auth).

    Authz: the town_token is instance/user-scoped, so the caller is whoever
    holds the token — we authorize by ownership: the job's awarded provider
    agent must belong to the token's user (a user's token authorizes its own
    agents, never another household's).

    Mechanism: presigned **POST** with a ``content-length-range`` policy
    condition, not presigned PUT — a presigned PUT cannot enforce a size cap
    (Content-Length isn't part of the PUT signature), while the POST policy
    is enforced by S3 itself at upload time. Response shape::

        {"method": "POST", "upload": {"url", "fields"}, "artifact_key", ...}

    The CLI (`goosetown act deliver --file=...`) uploads multipart/form-data
    to ``upload.url`` with every ``upload.fields`` entry first and the file
    part last, then sends ``deliver_job`` with ``artifact_key``.
    """
    from core.config import settings

    user_id, _ = token_info
    if not settings.SPRITE_S3_BUCKET:
        raise HTTPException(503, "Artifact storage is not configured")

    safe_name = exchange_service.sanitize_artifact_filename(request.filename)
    if safe_name is None:
        raise HTTPException(
            400,
            "Invalid filename: use 1-128 chars of [A-Za-z0-9._-], no leading dot.",
        )

    job = exchange_service._get_job(request.job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.get("status") != "AWARDED":
        raise HTTPException(409, "Job is not awaiting delivery")

    provider_id = job.get("providerId") or ""
    provider = TownService().get_town_agent_by_id(provider_id) if provider_id else None
    if not provider or provider.get("userId") != user_id:
        raise HTTPException(403, "Your token does not own this job's provider agent")

    artifact_key = exchange_service.artifact_key_for(request.job_id, safe_name)
    upload = exchange_service.presign_artifact_upload(artifact_key)
    return {
        "method": "POST",
        "upload": upload,
        "artifact_key": artifact_key,
        "expires_in": exchange_service.ARTIFACT_UPLOAD_EXPIRES_SEC,
        "max_bytes": exchange_service.ARTIFACT_MAX_BYTES,
    }


@router.get("/agent/{agent_id}/ledger")
async def get_agent_ledger(
    agent_id: str,
    auth: AuthContext = Depends(get_current_user),
):
    """Return the owner's ledger passbook for one of their agents.

    Auth: Clerk JWT. Returns 403 if ``agent.userId != auth.user_id``,
    404 if the agent does not exist.

    Response shape::

        {
          "balance": int,
          "trust_tier": str,
          "trust_score": float,
          "entries": [{"type", "amount", "balance_after", "ref_id", "at"}, ...]
        }

    Entries are the agent's most recent 15 ledger rows, newest-first.
    The signup grant is self-healed via ``get_wallet`` if it hasn't fired yet.
    """
    service = TownService()
    agent = service.get_town_agent_by_id(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="agent not found")
    if agent.get("userId") != auth.user_id:
        raise HTTPException(status_code=403, detail="not your agent")

    wallet = exchange_service.get_wallet(agent_id)
    entries = exchange_service.get_ledger_tail(agent_id, limit=15)
    return {
        "balance": wallet["balance"],
        "trust_tier": wallet["trustTier"],
        "trust_score": wallet["trustScore"],
        "entries": entries,
    }


@router.get("/profile", response_model=ProfileResponse)
async def get_profile(
    auth: AuthContext = Depends(get_current_user),
):
    """Return the current user's town profile.

    ``nickname`` is ``None`` until the user picks a public-safe handle. The
    frontend reads this once to decide whether to prompt before letting the
    user speak in a conversation.
    """
    nickname = TownService().get_nickname(auth.user_id)
    return ProfileResponse(nickname=nickname)


@router.put("/profile/nickname", response_model=ProfileResponse)
async def put_profile_nickname(
    body: NicknameRequest,
    auth: AuthContext = Depends(get_current_user),
):
    """Set the current user's public-safe town nickname.

    The handle labels the user's turns in the public conversation log, so it's
    validated against ``validate_nickname`` (2–20 chars, letters/digits/spaces/
    ``_-`` only, no leading/trailing space, no email-like strings). Stored
    server-side keyed by the authenticated Clerk user_id — never taken from a
    conversation body — so it can't be spoofed.

    Status codes:
        * 200 — stored; returns the nickname.
        * 422 — nickname violates the allowed shape.
    """
    from core.services.town_service import validate_nickname

    valid = validate_nickname(body.nickname)
    if valid is None:
        raise HTTPException(status_code=422, detail="invalid_nickname")

    stored = TownService().set_nickname(auth.user_id, valid)
    return ProfileResponse(nickname=stored)


def _visitor_display_name(user_id: str) -> str:
    """Friendly, non-spoofable label for a logged-in visitor's conversation turn.

    If the user has set a town nickname (``PUT /town/profile/nickname``) we use
    it — it's already validated to a public-safe shape and stored server-side
    keyed by the authenticated Clerk id, so it can't be spoofed via the say
    body. Otherwise we fall back to a stable ``Visitor-<short>`` label: the
    short suffix is derived from the user id so two different users dropping
    into the same conversation are distinguishable in the public log, while
    never exposing the raw Clerk id. Crucially the result is always derived
    from the *authenticated* identity, not the request body, so a user cannot
    present themselves as a resident.
    """
    nickname = TownService().get_nickname(user_id)
    if nickname:
        return nickname
    suffix = "".join(ch for ch in user_id if ch.isalnum())[-4:].lower()
    return f"Visitor-{suffix}" if suffix else "Visitor"


@router.post(
    "/conversations/{conv_id}/say",
    response_model=HumanSayResponse,
)
async def post_conversation_say(
    conv_id: str,
    body: HumanSayRequest,
    auth: AuthContext = Depends(get_current_user),
):
    """Let a logged-in user drop a message into an ongoing resident conversation.

    The resident participants' LLM brains see the visitor's line (it lands in
    each resident daemon's ``pending_messages`` inbox via the same
    ``conversation_message`` broadcast residents use for each other) and reply
    on their next tick.

    The speaker name is derived from the authenticated user, NOT from the body
    — there is no ``speaker`` field on :class:`HumanSayRequest`. The visitor's
    turn is tagged ``speakerType: "human"`` in the conversation's public log and
    counted in a separate ``humanTurnCount`` so it never advances the agent
    turn-cap that auto-ends the conversation.

    Status codes:
        * 200 — appended and broadcast.
        * 404 — conversation does not exist.
        * 409 — conversation has ended.
        * 422 — empty/blank or over-length message (pydantic + service guard).
        * 429 — too many messages into this conversation too quickly.
    """
    from core.services.conversation_service import broadcast_human_message

    result = await broadcast_human_message(
        conv_id=conv_id,
        user_id=auth.user_id,
        speaker_display=_visitor_display_name(auth.user_id),
        message=body.message,
    )

    if result.ok:
        return HumanSayResponse(ok=True, conv_id=conv_id, turn=result.turn_index or 0)

    error_status = {
        "conversation_not_found": 404,
        "conversation_not_active": 409,
        "rate_limited": 429,
        "empty_message": 422,
        "message_too_long": 422,
    }
    status_code = error_status.get(result.error or "", 400)
    raise HTTPException(status_code=status_code, detail=result.error)


@router.get("/world-events", response_model=list[WorldEventEntry])
async def list_world_events(limit: int = 50):
    """Return the most recent N public world events.

    Filters out ``personal.*`` events (those are private to each agent) and
    returns in reverse-chronological order (newest first). Limit capped at 200.

    Public endpoint — no auth required, matches the ``/conversations`` pattern.
    """
    from core import dynamo

    limit = min(max(limit, 1), 200)
    env = os.environ.get("ENVIRONMENT", "dev")
    table = dynamo.world_events_table()
    resp = table.query(
        IndexName="byTimestamp",
        KeyConditionExpression="envBucket = :env",
        ExpressionAttributeValues={":env": env},
        ScanIndexForward=False,  # newest first
        Limit=limit * 2,  # over-fetch since we filter personal.*
    )
    items = resp.get("Items", [])
    public = [
        WorldEventEntry(
            eventId=i["eventId"],
            timestamp=i["timestamp"],
            tag=i["tag"],
            summary=i.get("summary"),
            location=i.get("location"),
        )
        for i in items
        if not i["tag"].startswith("personal.")
    ]
    return public[:limit]


# ===========================================================================
# WebSocket disconnect handling (from API Gateway $disconnect Lambda)
# ===========================================================================


class WsDisconnectRequest(BaseModel):
    connectionId: str


# ---------------------------------------------------------------------------
# Sleep grace window + agent-connect handler live in
# ``core.services.agent_connect_service``. Re-exported here so call sites
# (tests, the `handle_ws_disconnect` route below) keep working. See the
# service module for the invariant commentary. Sub-project #3 fifth slice,
# 2026-04-22.
# ---------------------------------------------------------------------------
from core.services.agent_connect_service import (  # noqa: E402, F401
    _SLEEP_GRACE_SECONDS,
    _cancel_sleep_grace,
    _delayed_sleep,
    _handle_agent_connect,
    _pending_sleep_tasks,
    _schedule_sleep_grace,
)


@router.post("/ws-disconnect", dependencies=[Depends(require_ws_lambda_secret)])
async def handle_ws_disconnect(request: WsDisconnectRequest):
    """Handle WebSocket disconnect — clean up agent from game server and DynamoDB.

    Auth: shared-secret gate (``x-ws-lambda-secret``), only the API Gateway
    $disconnect Lambda holds it. Before #427 this route had **no gate at all**:
    a bare ``{"connectionId": ...}`` POST from anywhere on the internet would
    evict that agent from the game server, clear its activity, and (paired with
    a forged ``town_agent_sleep``) mark it inactive and move it to bed.
    """
    ws_manager = get_town_agent_ws_manager()
    agent_conn = ws_manager.get_by_connection(request.connectionId)

    if agent_conn:
        agent_id = agent_conn.agent_id
        clean_leave = agent_conn.leaving_cleanly
        logger.info(
            "[agent:%s] Daemon disconnected (conn=%s, clean=%s)",
            agent_id[:8],
            request.connectionId[:12],
            clean_leave,
        )

        # Remove from game server — the agent is no longer being controlled
        # by a live daemon, so the game server should drop them from the sim
        # regardless of whether this was a clean leave or a drop.
        gs_client = get_game_server_client()
        try:
            await gs_client.disconnect_agent(agent_id)
        except Exception as exc:
            logger.warning("[agent:%s] Failed to disconnect from game server: %s", agent_id[:8], exc)

        service = TownService()
        service.update_agent_state(agent_id, daemon_connection_id="")
        service.update_agent_activity(agent_id, None, None)

        # Move to apartment/bed IMMEDIATELY on clean leave (user explicitly
        # asked to sleep). On a server-initiated drop schedule a grace window
        # instead — a 9-second network flap with an auto-reconnect should
        # NOT teleport the agent home. If the daemon doesn't come back within
        # the grace, the delayed task moves them to bed then. See
        # _delayed_sleep / _schedule_sleep_grace below.
        #
        # Why also flip ``isActive=false`` here:
        #   ``isActive`` gates the per-user agent quota
        #   (``set_agent_active`` moves the ``activeAgents`` counter). If we
        #   leave it "true" after the daemon goes away, sleeping agents
        #   permanently occupy quota slots and re-registration eventually
        #   fails with HTTP 429. Re-register flips it back to "true" via
        #   ``opt_in`` so this is symmetric.
        if clean_leave:
            service.update_agent_context(agent_id, "apartment", "bed")
            service.set_agent_active(agent_id, False)
        else:
            _schedule_sleep_grace(agent_id)

        # Unregister from ws_manager
        ws_manager.unregister(request.connectionId)
    else:
        # Might be a viewer disconnect
        from core.services.viewer_relay import get_viewer_relay

        relay = get_viewer_relay()
        relay.unregister_viewer(request.connectionId)

    return {"statusCode": 200}


# ===========================================================================
# WebSocket message forwarding (from API Gateway $default Lambda)
# ===========================================================================


class WsMessageRequest(BaseModel):
    connectionId: str
    principalId: str
    body: dict


_ws_message_adapter: TypeAdapter[WsMessage] = TypeAdapter(WsMessage)


@router.post("/ws-message", dependencies=[Depends(require_ws_lambda_secret)])
async def handle_ws_message(request: WsMessageRequest, raw_request: Request):
    """Receive forwarded WebSocket messages from API Gateway $default Lambda.

    Auth: shared-secret gate (``x-ws-lambda-secret``) as a dependency, so it
    runs *before* body validation. The previous inline check was vacuous —
    ``WS_LAMBDA_SECRET`` was never wired through CDK, so it compared
    ``"" != ""`` and admitted every anonymous caller (#427).
    """
    from core.config import settings

    # Boundary validation (sub-project #1 CRITICAL fix): reject malformed
    # bodies at the request boundary instead of forwarding to the game
    # server. Flag-gated for 48h rollback post-deploy; B3→Sub-project #3
    # will remove the flag when handle_ws_message itself is split.
    if settings.WS_STRICT_VALIDATION:
        try:
            _ws_message_adapter.validate_python(request.body)
        except ValidationError as exc:
            # include_context=False strips the raw Python exception objects
            # (from model_validator ValueErrors) that aren't JSON-serializable.
            raise HTTPException(
                status_code=422,
                detail={"validation_errors": exc.errors(include_url=False, include_context=False)},
            )

    ws_manager = get_town_agent_ws_manager()
    body = request.body
    msg_type = body.get("type", "")

    # Handle viewer subscription — register with ViewerRelay for world_state broadcasts
    if msg_type == "town_subscribe":
        from core.services.viewer_relay import get_viewer_relay

        relay = get_viewer_relay()
        relay.register_viewer(request.connectionId)
        return {"statusCode": 200}

    # Keepalive frame from the daemon. The frame itself resets API Gateway's
    # 10-minute idle clock, but we also answer it so the daemon has an
    # END-TO-END liveness signal (not just the daemon↔API-Gateway hop that WS
    # ping/pong covers):
    #   - registered connection  → push {"event":"pong"}. The daemon resets its
    #     pong deadline and stays connected (no needless reconnect → no isActive
    #     flicker for idle agents).
    #   - UNregistered connection → push {"event":"reconnect"}. This happens when
    #     a new backend task (after a deploy) lost the in-memory ws_manager
    #     registry: the daemon's WS to API Gateway still looks alive, but its
    #     acts go into the void. Telling it to reconnect makes it re-send
    #     town_agent_connect and re-register. Older daemons ignore these events.
    if msg_type == "ping":
        from core.services.daemon_mailbox import get_daemon_mailbox

        registered = ws_manager.get_by_connection(request.connectionId) is not None
        get_daemon_mailbox().notify_connection(
            request.connectionId,
            {"event": "pong" if registered else "reconnect"},
        )
        return {"statusCode": 200}

    # Clean-leave signal from the daemon before it closes the socket.
    # Sets `leaving_cleanly` on the in-memory AgentConnection so the
    # subsequent $disconnect Lambda knows this is "agent went to sleep"
    # rather than "network dropped the connection". F12.
    if msg_type == "town_agent_sleep":
        agent_conn = ws_manager.get_by_connection(request.connectionId)
        if agent_conn:
            agent_conn.leaving_cleanly = True
            logger.info(
                "[agent:%s] Clean-leave signal received (wake_time=%s)",
                agent_conn.agent_id[:8],
                body.get("wake_time", ""),
            )
        return {"statusCode": 200}

    # Handle agent connection handshake
    if msg_type == "town_agent_connect":
        return await _handle_agent_connect(request, ws_manager)

    # Handle agent action commands
    if msg_type == "town_agent_act":
        agent_conn = ws_manager.get_by_connection(request.connectionId)
        if not agent_conn:
            return {"statusCode": 400, "error": "unknown_connection"}

        action = body.get("action", "")

        # Energy soft-gate: an exhausted agent (energy=0) can only idle or
        # disconnect. Every other act is rejected with an explicit event so the
        # agent's LLM sees it needs to sleep. After repeated abuse we hard-close
        # the WS to cap costs.
        if action not in ("idle",):
            service = TownService()
            agent = service.get_town_agent_by_id(agent_conn.agent_id)
            if agent is not None and int(agent.get("energy", 100) or 0) <= 0:
                agent_conn.exhausted_rejections += 1
                from core.services.daemon_mailbox import get_daemon_mailbox

                mailbox = get_daemon_mailbox()
                mailbox.notify_connection(
                    request.connectionId,
                    {
                        "event": "exhausted",
                        "action": action,
                        "rejections": agent_conn.exhausted_rejections,
                        "message": (
                            "You are exhausted (energy=0). Use "
                            "`town_disconnect <HH:MM>` to sleep. All other "
                            "actions will be rejected until you do."
                        ),
                    },
                )
                if agent_conn.exhausted_rejections >= 10:
                    logger.warning(
                        "Force-closing exhausted agent %s after %d rejections",
                        agent_conn.agent_name,
                        agent_conn.exhausted_rejections,
                    )
                    # Apply the sleep event so the agent wakes refreshed on
                    # reconnect. Without this, the agent comes back with
                    # energy=0 and immediately hits the same rejection loop.
                    try:
                        current_mood = town_mood_engine.parse_mood(agent.get("mood"))
                        traits = agent.get("traits", "")
                        new_energy, new_mood = town_mood_engine.apply_event("sleep", 0, current_mood, traits)
                        service.update_agent_mood_energy(agent_conn.agent_id, new_mood, new_energy)
                    except Exception as exc:
                        logger.warning(
                            "Failed to apply sleep event for exhausted agent %s: %s",
                            agent_conn.agent_id,
                            exc,
                        )
                    mailbox.disconnect(request.connectionId)
                return {"statusCode": 200, "rejected": "exhausted"}

        # Reject ALL actions while a context transition is in progress.
        if agent_conn.location_context == "transitioning":
            from core.services.daemon_mailbox import get_daemon_mailbox

            get_daemon_mailbox().notify_connection(
                request.connectionId,
                {"event": "busy", "reason": "transitioning"},
            )
            return {"statusCode": 200}

        gs_client = get_game_server_client()
        logger.debug(
            "town_agent_act: action=%s agent=%s ctx=%s",
            action,
            agent_conn.agent_name,
            agent_conn.location_context,
        )
        if action == "move":
            destination = body.get("destination", "")
            if destination in _TRANSITION_MAP:
                # Context-transition destinations are handled by the backend,
                # not forwarded to the game server directly. Cross-interior moves
                # (e.g. apartment → library) can't go direct — you can only enter
                # an interior from town — so _transition_path expands them into a
                # two-step exit-then-enter, making a single ``move <interior>``
                # verb work from anywhere (mirrors the town-location auto-exit
                # below). A genuine already-there case stays a single step and is
                # correctly rejected with ``already_in_X``.
                for _step in _transition_path(agent_conn.location_context, destination):
                    if not await _handle_context_transition(request.connectionId, _step, agent_conn):
                        break
            else:
                # If the destination lives in a different context than the
                # agent is currently in, transparently transition first and
                # then forward the real move command to the new context's
                # game server. This keeps cross-context routing deterministic
                # in the backend so the skill/CLI only need a single
                # ``move <place>`` verb.
                dest_context = _context_for_location(destination)
                current_context = agent_conn.location_context
                if dest_context is not None and dest_context != current_context:
                    # Pick the right transition trigger based on (source, dest).
                    # Both apartment and library exit to town via "exit".
                    # Town enters apartment via "residence" and library via "library".
                    if current_context in ("apartment", "library"):
                        transition_dest = "exit"
                    elif dest_context == "library":
                        transition_dest = "library"
                    else:  # dest_context == "apartment"
                        transition_dest = "residence"
                    transitioned = await _handle_context_transition(request.connectionId, transition_dest, agent_conn)
                    if not transitioned:
                        return {"statusCode": 200}
                    # _handle_context_transition updates the ws_manager in-place,
                    # so agent_conn.location_context now reflects the new context.
                    # NOTE: cross-interior moves (e.g. apartment → library spot)
                    # only do a single step here; the agent ends up in town and
                    # the forwarded move command may fail. Two-step moves are
                    # not yet supported and the agent must issue them manually.
                try:
                    await gs_client.send_command(
                        agent_conn.agent_id,
                        {"cmd": "move", "destination": destination},
                    )
                except Exception as exc:
                    logger.warning("[agent:%s] Move command failed: %s", agent_conn.agent_id[:8], exc)
        elif action == "chat":
            await _handle_chat(request.connectionId, body, agent_conn)
        elif action == "say":
            await _handle_say(request.connectionId, body, agent_conn)
        elif action == "end_conversation":
            await _handle_end_conversation(request.connectionId, body, agent_conn)
        elif action == "join_conversation":
            await _handle_join_conversation(request.connectionId, body, agent_conn)
        elif action == "activity":
            # ``{action: "activity", activity_id: <name>}`` is the validator-
            # enforced shape (``core/ws_models.py``). The activity name comes
            # from ``activity_id`` — using ``action`` would always look up the
            # literal string "activity" and fail.
            activity_name = body.get("activity_id", "")
            if activity_name:
                await _handle_agent_activity(request.connectionId, activity_name, agent_conn)
        elif action == "reply_owner":
            text = body.get("text", "")
            ref_message_id = body.get("ref_message_id")
            _handle_reply_owner_action(
                agent_id=agent_conn.agent_id,
                owner_id=agent_conn.user_id,
                text=text,
                ref_message_id=ref_message_id,
            )
        elif action == "set_goal":
            text = body.get("text", "")
            result = goals_service.set_goal(agent_id=agent_conn.agent_id, text=text)
            return {"statusCode": 200, "body": result}
        elif action == "complete_goal":
            reflection = body.get("reflection", "")
            result = goals_service.complete_goal(agent_id=agent_conn.agent_id, reflection=reflection)
            return {"statusCode": 200, "body": result}
        elif action == "describe_stance":
            target_agent_id = body.get("target_agent_id", "")
            text = body.get("text", "")
            result = stances_service.describe_stance(
                agent_id=agent_conn.agent_id,
                target_agent_id=target_agent_id,
                text=text,
            )
            return {"statusCode": 200, "body": result}
        elif action == "post_job":
            result = await exchange_service.post_job(
                agent_conn,
                title=body.get("title", ""),
                description=body.get("description", ""),
                max_price=_coerce_int(body.get("max_price")),
                strategy=body.get("strategy") or "balanced",
                success_criteria=body.get("success_criteria") or None,
                category=body.get("category"),
                tags=body.get("tags"),
            )
            return {"statusCode": 200, "body": result}
        # CLI verb "bid" → service place_bid (the one non-1:1 name).
        elif action == "bid":
            result = await exchange_service.place_bid(
                agent_conn,
                job_id=body.get("job_id", ""),
                price=_coerce_int(body.get("price")),
                confidence=_coerce_float(body.get("confidence"), 0.5),
                pitch=body.get("pitch", "") or "",
            )
            return {"statusCode": 200, "body": result}
        elif action == "cancel_job":
            result = await exchange_service.cancel_job(agent_conn, job_id=body.get("job_id", ""))
            return {"statusCode": 200, "body": result}
        elif action == "deliver_job":
            result = await exchange_service.deliver_job(
                agent_conn,
                job_id=body.get("job_id", ""),
                deliverable=body.get("deliverable", ""),
                artifact_key=body.get("artifact_key") or None,
            )
            return {"statusCode": 200, "body": result}
        elif action == "review_job":
            _raw_accept = body.get("accept")
            result = await exchange_service.review_job(
                agent_conn,
                job_id=body.get("job_id", ""),
                accept=_raw_accept if isinstance(_raw_accept, bool) else None,
                reason=body.get("reason", "") or "",
                criteria_verdicts=body.get("criteria_verdicts") or None,
            )
            return {"statusCode": 200, "body": result}
        elif action == "job_msg":
            result = await exchange_service.job_message(
                agent_conn,
                job_id=body.get("job_id", ""),
                text=body.get("msg_text", "") or "",
            )
            return {"statusCode": 200, "body": result}
        elif action == "invite":
            result = await exchange_service.invite_provider(
                agent_conn,
                job_id=body.get("job_id", ""),
                provider_id=body.get("provider_id", ""),
            )
            return {"statusCode": 200, "body": result}
        elif action == "provider_register":
            result = await exchange_service.register_provider(
                agent_conn,
                skills=body.get("skills") or [],
                categories=body.get("categories") or [],
                bio=body.get("bio", "") or "",
                min_price=_coerce_int(body.get("min_price")),
            )
            return {"statusCode": 200, "body": result}
        elif action == "provider_off":
            result = await exchange_service.unregister_provider(agent_conn)
            return {"statusCode": 200, "body": result}
        # ``idle`` and ``context_transition`` fall through with no extra work:
        # ``idle`` exists only to bypass the energy-exhaustion gate, and
        # ``context_transition`` is handled inside the ``move`` branch when the
        # destination implies a context change.
        return {"statusCode": 200}

    # Legacy: forward raw commands
    agent_conn = ws_manager.get_by_connection(request.connectionId)
    if not agent_conn:
        return {"statusCode": 400, "error": "unknown_connection"}
    if "cmd" in body:
        gs_client = get_game_server_client()
        try:
            await gs_client.send_command(agent_conn.agent_id, body)
        except Exception as exc:
            logger.warning("[agent:%s] Raw command forward failed: %s", agent_conn.agent_id[:8], exc)

    return {"statusCode": 200}


# ===========================================================================
# Internal seam: sprite worker → backend
# ===========================================================================


class SpriteEventRequest(BaseModel):
    agent_id: str
    type: Literal["progress", "static_ready", "animations_updated"]
    status: str | None = None


@router.post("/internal/sprite-event", dependencies=[Depends(require_sprite_event_secret)])
async def internal_sprite_event(request: SpriteEventRequest):
    """Sprite-worker → backend seam (spec 2026-06-11 §4).

    The sprite worker owns PixelLab API calls and DynamoDB writes
    (spriteStatus / spriteUrl / spriteAnimations / onboardingPhase). This
    endpoint applies the backend-process side effects the worker cannot reach:
    updating the in-memory phase-lattice cache, pushing sprites to the game
    server, and notifying the daemon.

    Auth: dedicated ``SPRITE_EVENT_SECRET`` shared-secret gate (header
    ``x-sprite-event-secret``), wired end-to-end through CDK into both the
    backend and the sprite worker. Fails closed outside dev.

    The gate is a **dependency**, not an inline check (#428): dependencies
    resolve before request-body validation, so an unauthenticated caller gets
    403 rather than a Pydantic 422 that never reaches the auth code at all.
    The behaviour was always correct — only the ordering made it look absent
    to external probing.
    """
    if request.type == "progress":
        _send_to_daemon(
            request.agent_id,
            {"event": "sprite_progress", "status": request.status or ""},
        )
    else:
        from core.services.sprite_update_apply import apply_sprite_update

        await apply_sprite_update(request.agent_id, request.type)

    return {"ok": True}


# ---------------------------------------------------------------------------
# Conversation handlers live in `core.services.conversation_service`. These
# imports keep the dispatch ladder above calling its old names AND preserve
# `from routers.town import _handle_chat` for tests and any external caller.
# Sub-project #3 extraction, 2026-04-22.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Activity handlers live in `core.services.activity_service`. Re-exported
# here so the dispatch ladder, `main.py:_reconnect_active_agents` startup
# code, and tests that still reference `routers.town.<name>` continue to
# work unchanged. Sub-project #3 extraction, 2026-04-22.
# ---------------------------------------------------------------------------
from core.services.activity_service import (  # noqa: E402, F401
    _activity_tasks,
    _activity_timer,
    _clear_activity_and_notify,
    _handle_agent_activity,
    complete_activity_from_startup,
    restart_activity_timer,
)

# ---------------------------------------------------------------------------
# Context transition helpers live in
# ``core.services.context_transition_service``. Re-exported here so the
# move dispatch ladder above + existing tests that patch
# ``routers.town._handle_context_transition`` keep resolving to the same
# function object. Sub-project #3 sixth slice, 2026-04-23.
# ---------------------------------------------------------------------------
from core.services.context_transition_service import (  # noqa: E402, F401
    _TOWN_LOCATIONS,
    _TRANSITION_MAP,
    _context_for_location,
    _handle_context_transition,
    _transition_path,
    _transition_to_apartment,
)
from core.services.conversation_service import (  # noqa: E402, F401
    _MAX_CONVERSATION_TURNS,
    _build_log_summary,
    _finalize_conversation,
    _handle_chat,
    _handle_end_conversation,
    _handle_join_conversation,
    _handle_say,
)
