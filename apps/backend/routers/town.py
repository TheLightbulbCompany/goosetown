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
from typing import Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from core.auth import AuthContext, get_current_user
from core.services import town_mood_engine
from core.services.game_server_client import get_game_server_client
from core.services.town_agent_ws import get_town_agent_ws_manager
from core.services.town_mood_engine import mood_label, parse_mood
from core.services.town_service import TownService, versioned_sprite_url
from core.ws_models import WsMessage
from schemas.town import (
    ApartmentAgentState,
    ApartmentResponse,
    ConversationTurn,
    TownConversationResponse,
    TownConversationsListResponse,
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
    authorization: str = Header(..., alias="Authorization"),
) -> tuple:
    """Validate a town_token from Authorization: Bearer <token>.
    Returns (user_id, token) or raises 401.
    """
    from core.town_token import verify_town_token

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


_SKILL_BUNDLE_FILES = (
    "SKILL.md",
    "goosetown_cli.py",
    "GOOSETOWN.md",
    "TOWN_STATUS.md",
    "TOWN_EVENTS.md",
    "TOWN_LIFE.md",
    "hooks/check-town-status.sh",
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
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for filename in _SKILL_BUNDLE_FILES:
                filepath = _SKILL_DIR / filename
                if filepath.is_file():
                    tar.add(str(filepath), arcname=f"goosetown/{filename}")
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


# `_sprite_pipeline_task` lives in `core/services/sprite_pipeline.py` — moved
# out to break the circular import with `sprite_queue`. Re-exported here so
# callers inside this module (and external patches like the test suite) can
# still reference `routers.town._sprite_pipeline_task` during the grace period
# while we migrate off the old path.
from core.services.sprite_pipeline import _sprite_pipeline_task  # noqa: E402, F401


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

    # Trigger PixelLab sprite generation in background
    logger.info(
        "Sprite check: appearance=%s, appearance_val=%s",
        bool(request.appearance),
        request.appearance[:50] if request.appearance else "empty",
    )
    if request.appearance:
        from core.config import settings

        logger.info(
            "Sprite settings: api_key=%s, bucket=%s, cdn=%s",
            bool(settings.PIXELLAB_API_KEY),
            settings.SPRITE_S3_BUCKET,
            settings.SPRITE_CDN_URL,
        )
        if settings.PIXELLAB_API_KEY and settings.SPRITE_S3_BUCKET and settings.SPRITE_CDN_URL:
            _agent_id = agent["id"]

            _existing = service.get_town_agent_by_id(_agent_id)
            _in_progress = ("creating", "building_static", "static_ready", "animating", "downloading")
            if _existing and _existing.get("spriteStatus") in _in_progress:
                logger.info(
                    "Sprite generation in progress for %s (%s)",
                    _agent_id,
                    _existing.get("spriteStatus"),
                )
            else:
                _appearance = request.appearance
                _display_name = request.display_name

                from core.services.sprite_queue import get_sprite_queue

                queue = get_sprite_queue()
                position = queue.enqueue(_agent_id, _display_name, _appearance)
                if position > 0:
                    _send_to_daemon(
                        _agent_id,
                        {"event": "sprite_progress", "status": f"Queued for sprite generation (position {position})"},
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
# alongside `_sprite_pipeline_task` (sub-project #3 final slice,
# 2026-04-23). Re-imported here so the router module remains a one-stop
# back-compat surface for any external caller.
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


@router.post("/ws-disconnect")
async def handle_ws_disconnect(request: WsDisconnectRequest):
    """Handle WebSocket disconnect — clean up agent from game server and DynamoDB."""
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
        if clean_leave:
            service.update_agent_context(agent_id, "apartment", "bed")
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


@router.post("/ws-message")
async def handle_ws_message(request: WsMessageRequest, raw_request: Request):
    """Receive forwarded WebSocket messages from API Gateway $default Lambda."""
    from core.config import settings

    # In non-dev environments, require shared secret from API Gateway Lambda.
    # In dev, skip if WS_LAMBDA_SECRET is unset (local dev ergonomics).
    if settings.ENVIRONMENT != "dev" or settings.WS_LAMBDA_SECRET:
        provided = raw_request.headers.get("x-ws-lambda-secret", "")
        if provided != settings.WS_LAMBDA_SECRET:
            raise HTTPException(status_code=403, detail="Invalid or missing WS lambda secret")

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

    # Keepalive frame from the daemon — no action needed, the frame itself
    # resets API Gateway's 10-minute idle clock. Swallow silently.
    if msg_type == "ping":
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
                from core.services.management_api_client import ManagementApiClient

                mgmt = ManagementApiClient()
                try:
                    mgmt.send_message(
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
                except Exception as exc:
                    logger.warning(
                        "Failed to send exhausted event to %s: %s",
                        agent_conn.agent_id,
                        exc,
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
                    try:
                        mgmt.close_connection(request.connectionId)
                    except Exception as exc:
                        logger.warning(
                            "Failed to force-close exhausted connection %s: %s",
                            request.connectionId,
                            exc,
                        )
                return {"statusCode": 200, "rejected": "exhausted"}

        # Reject ALL actions while a context transition is in progress.
        if agent_conn.location_context == "transitioning":
            from core.services.management_api_client import ManagementApiClient

            mgmt = ManagementApiClient()
            try:
                mgmt.send_message(
                    request.connectionId,
                    {"event": "busy", "reason": "transitioning"},
                )
            except Exception as _exc:
                logger.warning(
                    "Failed to send busy:transitioning to %s: %s",
                    agent_conn.agent_id,
                    _exc,
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
                # not forwarded to the game server directly.
                await _handle_context_transition(request.connectionId, destination, agent_conn)
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
    _custom_anim_in_progress,
    _generate_custom_animation,
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
