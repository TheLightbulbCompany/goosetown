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
from pydantic import BaseModel, Field

from core.auth import AuthContext, get_current_user
from core.services.game_server_client import get_game_server_client
from core.services.town_agent_ws import get_town_agent_ws_manager
from core.services.town_mood_engine import mood_label, parse_mood
from core.services.town_service import TownService
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


def _facing_toward(from_state: dict, to_state: dict) -> str:
    """Calculate which direction 'from' should face to look at 'to'."""
    from_pos = from_state.get("position", {})
    to_pos = to_state.get("position", {})
    dx = to_pos.get("x", 0) - from_pos.get("x", 0)
    dy = to_pos.get("y", 0) - from_pos.get("y", 0)
    if abs(dx) > abs(dy):
        return "east" if dx > 0 else "west"
    else:
        return "south" if dy > 0 else "north"


# ---------------------------------------------------------------------------
# Shared per-agent event callback for game server → daemon forwarding
# ---------------------------------------------------------------------------


def _make_agent_event_callback():
    """Create a per-agent callback that forwards game server events to the daemon.

    Uses DynamoDB daemonConnectionId for connection lookup (survives restarts).
    Also updates DynamoDB currentLocation on 'arrived' events.
    """

    async def _on_game_server_event(agent_id: str, event_data: dict) -> None:
        evt_type = event_data.get("event", "")

        # Update DynamoDB location on arrival
        if evt_type == "arrived":
            location = event_data.get("location", "")
            if location:
                try:
                    TownService().update_agent_state(agent_id, current_location=location)
                except Exception:
                    logger.warning("Failed to update location for %s", agent_id)

        # Forward event to daemon via DynamoDB connection lookup
        _send_to_daemon(agent_id, event_data)

    return _on_game_server_event


def _send_to_daemon(agent_id: str, payload: dict) -> bool:
    """Send a message to an agent's daemon using the connection ID from DynamoDB.

    Returns True if sent, False if no connection or connection is gone.
    """
    from core.services.management_api_client import ManagementApiClient

    evt = payload.get("event", payload.get("type", "?"))

    service = TownService()
    agent = service.get_town_agent_by_id(agent_id)
    if not agent:
        logger.warning("[agent:%s] _send_to_daemon: agent not found in DB (event=%s)", agent_id[:8], evt)
        return False

    conn_id = agent.get("daemonConnectionId")
    if not conn_id:
        logger.warning("[agent:%s] _send_to_daemon: no daemonConnectionId (event=%s)", agent_id[:8], evt)
        return False

    # Enrich agent_state events with fields the daemon needs from DynamoDB.
    # The agent record is already in scope here — no extra DB reads required.
    if evt == "agent_state":
        payload = {**payload, "onboardingPhase": agent.get("onboardingPhase", "alive")}
        # Add nearby_conversations for agents not in a conversation
        nearby_agents = payload.get("nearby_agents", [])
        nearby_ids = [a.get("agent_id") for a in nearby_agents if a.get("agent_id")]
        own_conv = agent.get("currentConversationId")
        if nearby_ids and not own_conv:
            nearby_convs = service.get_nearby_conversations(nearby_ids)
            if nearby_convs:
                payload = {**payload, "nearby_conversations": nearby_convs}

    try:
        mgmt = ManagementApiClient()
        sent = mgmt.send_message(conn_id, payload)
        if not sent:
            logger.warning("[agent:%s] _send_to_daemon: connection gone %s (event=%s)", agent_id[:8], conn_id[:12], evt)
        return sent
    except Exception as exc:
        logger.warning("[agent:%s] _send_to_daemon: failed (event=%s): %s", agent_id[:8], evt, exc)
        return False


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
_SKILL_DIR = Path(__file__).resolve().parent.parent / "data" / "goosetown-skill"


def _load_skill_md() -> str:
    global _skill_md_cache
    if _skill_md_cache is None:
        skill_path = _SKILL_DIR / "SKILL.md"
        if not skill_path.exists():
            raise HTTPException(404, "Skill not found")
        _skill_md_cache = skill_path.read_text()
    return _skill_md_cache


def _build_skill_bundle() -> bytes:
    global _skill_bundle_cache
    if _skill_bundle_cache is None:
        import io
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for subdir in ["tools", "daemon"]:
                dirpath = _SKILL_DIR / subdir
                if dirpath.exists():
                    for filepath in dirpath.rglob("*"):
                        if filepath.is_file() and "__pycache__" not in str(filepath):
                            arcname = f"goosetown/{subdir}/{filepath.relative_to(dirpath)}"
                            tar.add(str(filepath), arcname=arcname)
            # Include env.sh
            env_sh = _SKILL_DIR / "env.sh"
            if env_sh.exists():
                tar.add(str(env_sh), arcname="goosetown/env.sh")
        _skill_bundle_cache = buf.getvalue()
    return _skill_bundle_cache


@router.get("/skill")
async def get_skill():
    """Serve the GooseTown SKILL.md for agents to read and follow."""
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse(_load_skill_md(), media_type="text/markdown")


@router.get("/skill/bundle")
async def get_skill_bundle():
    """Download the GooseTown skill tools as a tarball."""
    from fastapi.responses import Response

    bundle = _build_skill_bundle()
    return Response(
        content=bundle,
        media_type="application/gzip",
        headers={"Content-Disposition": "attachment; filename=goosetown-skill.tar.gz"},
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


async def _sprite_pipeline_task(agent_id: str, display_name: str, appearance: str) -> None:
    """Progressive sprite pipeline: Phase A (create) → Phase B (static) → Phase C (animations).

    Phase A: Create character on PixelLab and poll until the creation job completes.
    Phase B: Fetch all 4 rotation PNGs, build a static spritesheet, upload to S3, mark
             spriteReady=True and onboardingPhase='static', and connect the agent to the
             game server immediately so they appear in town.
    Phase C: Queue signup animations (walk, idle), download the ZIP, extract all
             animation spritesheets, upload them, update DynamoDB with the full
             spriteAnimations dict, and flip onboardingPhase='alive' once idle lands.
    """
    from core.config import settings
    from core.services.management_api_client import ManagementApiClient
    from core.services.pixellab_service import ANIMATION_REGISTRY, PixelLabService
    from core.services.sprite_storage import (
        build_static_spritesheet_from_rotations,
        composite_animation_spritesheet,
        upload_sprite_to_s3,
    )

    pxl = PixelLabService()
    service = TownService()

    def _send_progress(status: str, **extra) -> None:
        msg = {"event": "sprite_progress", "status": status}
        msg.update(extra)
        ok = _send_to_daemon(str(agent_id), msg)
        logger.info("[KEEPALIVE] agent=%s ok=%s status=%s", str(agent_id)[:8], ok, status[:50])

    try:
        # ---- Phase A: Create character ----
        service.update_agent_state(agent_id, sprite_status="creating")
        _send_progress("Creating your character...")
        try:
            char_id, creation_job_id = await pxl.create_character(description=appearance)
        except Exception:
            logger.exception("Character creation failed for agent %s", agent_id)
            service.update_agent_state(agent_id, sprite_status=None)
            return

        service.update_agent_state(agent_id, pixellab_character_id=char_id)
        logger.info("PixelLab returned char_id=%s job=%s for %s", char_id, creation_job_id, agent_id)

        # Poll creation job until complete (up to 5 min)
        if creation_job_id:
            for attempt in range(30):
                await asyncio.sleep(10)
                try:
                    job = await pxl.get_job_status(creation_job_id)
                    status_val = job.get("status", "unknown")
                    _send_progress(f"Creating your character sprite ({status_val})...")
                    if status_val == "completed":
                        break
                except Exception as e:
                    logger.warning("Failed to check creation job: %s", e)
            else:
                logger.warning("Character creation timed out for %s", char_id)

        # ---- Phase B: Build static spritesheet and mark agent ready ----
        service.update_agent_state(agent_id, sprite_status="building_static")
        _send_progress("Sketching your initial look...")

        rotation_bytes = await pxl.fetch_all_rotations(char_id)
        static_cdn_url = ""
        initial_sprite_animations: dict = {}

        if rotation_bytes:
            try:
                static_png = build_static_spritesheet_from_rotations(rotation_bytes)
                s3_key = await asyncio.to_thread(
                    upload_sprite_to_s3, static_png, str(agent_id), settings.SPRITE_S3_BUCKET
                )
            except Exception:
                logger.exception("Static spritesheet build/upload failed for %s", agent_id)
                s3_key = ""

            if s3_key:
                static_cdn_url = f"{settings.SPRITE_CDN_URL}/{s3_key}"
                initial_sprite_animations = {
                    "walk": {
                        "url": static_cdn_url,
                        "frameCount": 6,
                        "fps": 8,
                    },
                }
                service.update_agent_state(
                    agent_id,
                    sprite_ready=True,
                    sprite_url=static_cdn_url,
                    sprite_animations=initial_sprite_animations,
                    sprite_status="static_ready",
                    onboarding_phase="static",
                )
                logger.info("[agent:%s] Phase B complete: static sprite ready", agent_id)

                # Connect to game server immediately with static sprite
                gs_client = get_game_server_client()
                _ws_mgr = get_town_agent_ws_manager()
                _conn = _ws_mgr.get_by_agent_id(str(agent_id))
                if _conn:
                    try:
                        agent_record = service.get_town_agent_by_id(str(agent_id))
                        await gs_client.connect_agent(
                            agent_id=str(agent_id),
                            display_name=display_name,
                            sprite_url=static_cdn_url,
                            location=agent_record.get("currentLocation", "plaza") if agent_record else "plaza",
                            context=_conn.location_context or "town",
                            user_id=_conn.user_id,
                            sprite_animations=initial_sprite_animations,
                            on_event=_make_agent_event_callback(),
                        )
                        logger.info("[agent:%s] Connected to game server with static sprite", agent_id)
                    except Exception:
                        logger.exception("Failed to connect agent %s with static sprite", agent_id)
        else:
            logger.warning("[agent:%s] No rotations returned — skipping Phase B static sprite", agent_id)

        # ---- Phase C: Generate signup animations (walk, idle, talking) ----
        service.update_agent_state(agent_id, sprite_status="animating")
        _send_progress("Bringing you to life (animating walk, idle, talking)...")

        async def _anim_progress(status_str: str) -> None:
            _send_progress(status_str)

        try:
            await pxl.queue_signup_animations(char_id, on_progress=_anim_progress)
        except Exception:
            logger.exception("Signup animations failed for %s", agent_id)

        # Download and resolve animation frames via API cross-reference
        service.update_agent_state(agent_id, sprite_status="downloading")
        _send_progress("Downloading sprites...")

        raw_animations = await pxl.fetch_animation_spritesheets(char_id)

        if not raw_animations:
            logger.warning("[agent:%s] No animations resolved — agent remains on static sprite", agent_id)
            service.update_agent_state(agent_id, onboarding_phase="alive")
            return

        # Map PixelLab API names to our friendly names
        template_id_to_friendly: dict[str, str] = {
            cfg["template_id"]: name
            for name, cfg in ANIMATION_REGISTRY.items()
            if cfg.get("template_id")
        }

        full_sprite_animations: dict = dict(initial_sprite_animations)

        for pixellab_name, direction_frames in raw_animations.items():
            friendly = template_id_to_friendly.get(pixellab_name, pixellab_name)
            sheet = composite_animation_spritesheet(direction_frames)
            if sheet:
                png_bytes, frame_count = sheet
                s3_key = await asyncio.to_thread(
                    upload_sprite_to_s3, png_bytes, str(agent_id),
                    settings.SPRITE_S3_BUCKET, f"{friendly}.png",
                )
                registry_entry = ANIMATION_REGISTRY.get(friendly, {})
                full_sprite_animations[friendly] = {
                    "url": f"{settings.SPRITE_CDN_URL}/{s3_key}",
                    "frameCount": frame_count,
                    "fps": int(registry_entry.get("fps", 8)),
                }

        # Real walk URL overrides the static placeholder
        real_walk_url = full_sprite_animations.get("walk", {}).get("url", static_cdn_url)

        service.update_agent_state(
            agent_id,
            sprite_ready=True,
            sprite_url=real_walk_url,
            sprite_animations=full_sprite_animations,
            sprite_status="complete",
        )
        logger.info("[agent:%s] Phase C complete: %d animations", agent_id, len(full_sprite_animations))

        # Push updated sprites to game server
        try:
            gs_client_c = get_game_server_client()
            await gs_client_c.update_agent_sprites(str(agent_id), full_sprite_animations)
        except Exception:
            logger.exception("Failed to push sprite update for %s", agent_id)

        # If agent was not connected in Phase B, connect now
        if not static_cdn_url:
            gs_client = get_game_server_client()
            _ws_mgr = get_town_agent_ws_manager()
            _conn = _ws_mgr.get_by_agent_id(str(agent_id))
            if _conn:
                try:
                    agent_record = service.get_town_agent_by_id(str(agent_id))
                    await gs_client.connect_agent(
                        agent_id=str(agent_id),
                        display_name=display_name,
                        sprite_url=real_walk_url,
                        location=agent_record.get("currentLocation", "plaza") if agent_record else "plaza",
                        context=_conn.location_context or "town",
                        user_id=_conn.user_id,
                        sprite_animations=full_sprite_animations,
                        on_event=_make_agent_event_callback(),
                    )
                except Exception:
                    logger.exception("Failed to connect agent %s with animations", agent_id)

        # Flip to alive
        service.update_agent_state(agent_id, onboarding_phase="alive")
        logger.info("[agent:%s] Agent is alive", agent_id)

        # Notify daemon that sprite is fully ready
        try:
            _ws_mgr = get_town_agent_ws_manager()
            _conn = _ws_mgr.get_by_agent_id(str(agent_id))
            if _conn:
                mgmt = ManagementApiClient()
                mgmt.send_message(_conn.connection_id, {
                    "event": "sprite_ready",
                    "agent_id": str(agent_id),
                    "sprite_url": real_walk_url,
                })
        except Exception:
            pass

    except Exception:
        logger.exception("Sprite generation failed for agent %s", agent_id)
        try:
            TownService().update_agent_state(agent_id, sprite_status=None)
        except Exception:
            pass


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
                    _agent_id, _existing.get("spriteStatus"),
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


async def resume_sprite_generation(
    agent_id: str,
    character_id: str | None,
    appearance: str,
    display_name: str,
    current_status: str,
) -> None:
    """Resume an interrupted sprite generation from a specific phase.

    Called from startup recovery when an agent has a non-complete spriteStatus.
    """
    from core.config import settings
    from core.services.pixellab_service import ANIMATION_REGISTRY, PixelLabService
    from core.services.sprite_storage import (
        composite_animation_spritesheet,
        upload_sprite_to_s3,
    )

    pxl = PixelLabService()
    service = TownService()

    # Recover the existing spriteUrl from DynamoDB so we don't overwrite it
    # with "" if Phase C can't find a "walk" key in the animation dict.
    existing_agent = service.get_town_agent_by_id(agent_id)
    existing_sprite_url = (existing_agent or {}).get("spriteUrl", "")

    def _progress(status: str) -> None:
        _send_to_daemon(agent_id, {"event": "sprite_progress", "status": status})

    try:
        async def _anim_progress(status: str) -> None:
            _progress(status)

        char_id = character_id

        # Phase: creating
        if current_status == "creating":
            if not char_id:
                _progress("Recreating character...")
                char_id, creation_job_id = await pxl.create_character(description=appearance)
                service.update_agent_state(agent_id, pixellab_character_id=char_id)
                if creation_job_id:
                    await pxl.wait_for_jobs(
                        [creation_job_id], on_progress=_anim_progress, progress_label="creation"
                    )
            service.update_agent_state(agent_id, sprite_status="animating")
            current_status = "animating"

        # Phase: animating
        if current_status == "animating" and char_id:
            _progress("Resuming animations...")
            await pxl.queue_signup_animations(char_id, on_progress=_anim_progress)
            service.update_agent_state(agent_id, sprite_status="downloading")
            current_status = "downloading"

        # Phase: downloading
        if current_status == "downloading" and char_id:
            _progress("Downloading sprites...")

            raw_animations = await pxl.fetch_animation_spritesheets(char_id)

            template_id_to_friendly: dict[str, str] = {
                cfg["template_id"]: name
                for name, cfg in ANIMATION_REGISTRY.items()
                if cfg.get("template_id")
            }

            sprite_animations: dict = {}
            for pixellab_name, direction_frames in raw_animations.items():
                friendly = template_id_to_friendly.get(pixellab_name, pixellab_name)
                sheet = composite_animation_spritesheet(direction_frames)
                if sheet:
                    png_bytes, frame_count = sheet
                    s3_key = await asyncio.to_thread(
                        upload_sprite_to_s3, png_bytes, agent_id,
                        settings.SPRITE_S3_BUCKET, f"{friendly}.png",
                    )
                    registry_entry = ANIMATION_REGISTRY.get(friendly, {})
                    sprite_animations[friendly] = {
                        "url": f"{settings.SPRITE_CDN_URL}/{s3_key}",
                        "frameCount": frame_count,
                        "fps": int(registry_entry.get("fps", 8)),
                    }

            walk_url = sprite_animations.get("walk", {}).get("url", existing_sprite_url)
            service.update_agent_state(
                agent_id,
                sprite_ready=True,
                sprite_url=walk_url,
                sprite_animations=sprite_animations,
                sprite_status="complete",
                onboarding_phase="alive",
            )
            logger.info("[resume:%s] Complete: %d animations", agent_id, len(sprite_animations))

            # Connect to game server
            gs_client = get_game_server_client()
            agent = service.get_town_agent_by_id(agent_id)
            if agent and agent.get("spriteReady"):
                await gs_client.connect_agent(
                    agent_id=agent_id,
                    display_name=display_name,
                    sprite_url=agent.get("spriteUrl", ""),
                    location=agent.get("currentLocation", "plaza"),
                    context=agent.get("locationContext", "town"),
                    user_id=agent.get("userId", ""),
                    sprite_animations=agent.get("spriteAnimations", {}),
                    on_event=_make_agent_event_callback(),
                )

            _send_to_daemon(agent_id, {
                "event": "sprite_ready",
                "agent_id": agent_id,
                "sprite_url": agent.get("spriteUrl", "") if agent else "",
            })

        logger.info("Sprite recovery completed for agent %s", agent_id)

    except Exception:
        logger.exception("Failed to resume sprite generation for %s", agent_id)
        service.update_agent_state(agent_id, sprite_status=None)


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
                        upload_sprite_to_s3, png_bytes, agent["id"],
                        settings.SPRITE_S3_BUCKET, "walk.png",
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
    active_agents = service.get_active_agents()

    # Filter to this user's agents
    user_agents = [a for a in active_agents if a.get("userId") == auth.user_id]

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
                sprite_url=item.get("spriteUrl") if item.get("spriteReady") else None,
            )
        )

    return ApartmentResponse(agents=agents, activity=[])


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
    elif "." not in instance.get("townToken", ""):
        # Re-sign legacy unsigned tokens
        instance["townToken"] = sign_town_token(auth.user_id, instance["id"])
        # Update in DynamoDB
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

    responses = []
    for c in convos:
        agent_a = service.get_town_agent_by_id(c.get("participantAId", ""))
        agent_b = service.get_town_agent_by_id(c.get("participantBId", ""))

        public_log = c.get("publicLog") or []

        responses.append(
            TownConversationResponse(
                id=c.get("id", ""),
                participant_a=agent_a.get("displayName", "Unknown") if agent_a else "Unknown",
                participant_b=agent_b.get("displayName", "Unknown") if agent_b else "Unknown",
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


@router.post("/ws-disconnect")
async def handle_ws_disconnect(request: WsDisconnectRequest):
    """Handle WebSocket disconnect — clean up agent from game server and DynamoDB."""
    ws_manager = get_town_agent_ws_manager()
    agent_conn = ws_manager.get_by_connection(request.connectionId)

    if agent_conn:
        agent_id = agent_conn.agent_id
        logger.info("[agent:%s] Daemon disconnected (conn=%s)", agent_id[:8], request.connectionId[:12])

        # Remove from game server — the agent is now "sleeping at home" and
        # should not appear in any viewer's world_state broadcast.
        gs_client = get_game_server_client()
        try:
            await gs_client.disconnect_agent(agent_id)
        except Exception as exc:
            logger.warning("[agent:%s] Failed to disconnect from game server: %s", agent_id[:8], exc)

        # Update DynamoDB: clear daemon connection, move to bed (sleeping),
        # drop any in-flight activity. An empty daemonConnectionId is the
        # signal to `_reconnect_active_agents` that this agent is offline
        # and should NOT be auto-reconnected on backend CI/CD restarts.
        service = TownService()
        service.update_agent_state(agent_id, daemon_connection_id="")
        service.update_agent_context(agent_id, "apartment", "bed")
        service.update_agent_activity(agent_id, None, None)

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

    ws_manager = get_town_agent_ws_manager()
    body = request.body
    msg_type = body.get("type", "")

    # Handle viewer subscription — register with ViewerRelay for world_state broadcasts
    if msg_type == "town_subscribe":
        from core.services.viewer_relay import get_viewer_relay

        relay = get_viewer_relay()
        relay.register_viewer(request.connectionId)
        return {"statusCode": 200}

    # Handle agent connection handshake
    if msg_type == "town_agent_connect":
        return await _handle_agent_connect(request, ws_manager)

    # Handle agent action commands
    if msg_type == "town_agent_act":
        agent_conn = ws_manager.get_by_connection(request.connectionId)
        if not agent_conn:
            return {"statusCode": 400, "error": "unknown_connection"}

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
        action = body.get("action", "")
        logger.debug(
            "town_agent_act: action=%s agent=%s ctx=%s",
            action, agent_conn.agent_name, agent_conn.location_context,
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
                    transition_dest = "exit" if current_context == "apartment" else "residence"
                    transitioned = await _handle_context_transition(
                        request.connectionId, transition_dest, agent_conn
                    )
                    if not transitioned:
                        return {"statusCode": 200}
                    # _handle_context_transition updates the ws_manager in-place,
                    # so agent_conn.location_context now reflects the new context.
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
        elif action != "idle":
            # Treat any unrecognised action as an activity name
            await _handle_agent_activity(request.connectionId, action, agent_conn)
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
# Conversation system
# ===========================================================================

_MAX_CONVERSATION_TURNS = 20


async def _handle_chat(connection_id: str, body: dict, agent_conn) -> None:
    """Handle a ``chat`` action — initiate a new conversation.

    Looks up the target agent by ``agentName``, validates neither party is
    already in a conversation, creates an active conversation record in
    DynamoDB, and delivers a ``conversation_invite`` event to the target.

    Args:
        connection_id: API Gateway connection ID of the initiating agent.
        body: Parsed WebSocket message body containing ``target`` (agentName)
            and ``message`` (opening line).
        agent_conn: Connection record for the initiating agent.
    """
    from core.services.management_api_client import ManagementApiClient

    mgmt = ManagementApiClient()
    service = TownService()
    ws_manager = get_town_agent_ws_manager()
    initiator_id = agent_conn.agent_id

    def _reject(reason: str) -> None:
        try:
            mgmt.send_message(
                connection_id,
                {"event": "chat_rejected", "reason": reason},
            )
        except Exception as exc:
            logger.warning("Failed to send chat_rejected to %s: %s", initiator_id, exc)

    target_agent_name = body.get("target", "").strip()
    opening_message = body.get("message", "").strip()

    if not target_agent_name or not opening_message:
        _reject("missing_target_or_message")
        return

    # Load initiator from DB
    initiator = service.get_town_agent_by_id(initiator_id)
    if not initiator:
        _reject("initiator_not_found")
        return

    # Validate initiator is not already in a conversation
    if initiator.get("currentConversationId"):
        _reject("already_in_conversation")
        return

    # Resolve the target agent by machine name
    target = service.get_agent_by_agent_name_active(target_agent_name)
    if not target:
        _reject("target_not_found")
        return

    target_id = target["id"]
    if target_id == initiator_id:
        _reject("cannot_chat_with_self")
        return

    # Validate target is not already in a conversation
    if target.get("currentConversationId"):
        _reject("target_busy")
        return

    # Validate agents are within proximity (3 tiles / 96px — same as game server)
    gs_client = get_game_server_client()
    init_state = gs_client.get_cached_agent_state(initiator_id)
    target_state = gs_client.get_cached_agent_state(target_id)
    if init_state and target_state:
        import math
        dx = float(init_state.get("x", 0)) - float(target_state.get("x", 0))
        dy = float(init_state.get("y", 0)) - float(target_state.get("y", 0))
        dist = math.sqrt(dx * dx + dy * dy)
        if dist > 96.0:  # PROXIMITY_THRESHOLD from agent_controller.gd
            _reject("not_nearby")
            return
    elif not init_state or not target_state:
        # No cached position — fall back to location check
        initiator_location = initiator.get("currentLocation", "")
        target_location = target.get("currentLocation", "")
        if initiator_location != target_location:
            _reject("not_nearby")
            return

    # Create the conversation record
    location = initiator.get("currentLocation", "residence")
    initiator_display = initiator.get("displayName", agent_conn.agent_name)
    conv = service.create_active_conversation(
        initiator_id=initiator_id,
        target_id=target_id,
        location=location,
        opening_message=opening_message,
        initiator_display_name=initiator_display,
    )
    conv_id = conv["id"]

    # Deliver invite to target agent if they are connected
    target_conn = ws_manager.get_by_agent_id(target_id)
    if target_conn:
        try:
            mgmt.send_message(
                target_conn.connection_id,
                {
                    "event": "conversation_invite",
                    "conv_id": conv_id,
                    "from": initiator_display,
                    "from_agent_name": agent_conn.agent_name,
                    "message": opening_message,
                    "location": location,
                },
            )
        except Exception as exc:
            logger.warning("Failed to deliver conversation_invite to %s: %s", target_id, exc)
    else:
        logger.info("Target agent %s not connected — invite stored in conversation record", target_id)

    # Acknowledge to initiator with the conversation ID
    try:
        mgmt.send_message(
            connection_id,
            {
                "event": "act_ok",
                "action": "chat",
                "conv_id": conv_id,
                "target": target_agent_name,
            },
        )
    except Exception as exc:
        logger.warning("Failed to send chat act_ok to %s: %s", initiator_id, exc)

    # Send speech bubble to game server for viewer display
    try:
        gs_client = get_game_server_client()
        await gs_client.send_command(initiator_id, {"cmd": "set_speech", "text": opening_message})
    except Exception:
        pass  # Speech bubble is cosmetic, don't fail the conversation

    # Face agents toward each other and start talking animation
    try:
        gs_client = get_game_server_client()
        initiator_state = gs_client.get_cached_agent_state(initiator_id)
        target_state = gs_client.get_cached_agent_state(target_id)
        if initiator_state and target_state:
            init_facing = _facing_toward(initiator_state, target_state)
            target_facing = _facing_toward(target_state, initiator_state)
            await gs_client.send_command(initiator_id, {"cmd": "set_facing", "direction": init_facing})
            await gs_client.send_command(target_id, {"cmd": "set_facing", "direction": target_facing})
        await gs_client.send_command(initiator_id, {"cmd": "set_activity", "activity": "talking"})
    except Exception:
        pass  # Cosmetic, don't fail conversation

    logger.info(
        "Conversation %s started between %s and %s at %s",
        conv_id,
        initiator_id,
        target_id,
        location,
    )


async def _handle_say(connection_id: str, body: dict, agent_conn) -> None:
    """Handle a ``say`` action — append a message to an active conversation.

    Validates that the conversation exists, is active, and the speaker is a
    participant. Appends the message and forwards it to the other participant.
    Auto-ends the conversation when the turn limit is reached.

    Args:
        connection_id: API Gateway connection ID of the speaking agent.
        body: Parsed message body with ``conv_id`` and ``message``.
        agent_conn: Connection record for the speaking agent.
    """
    from core.services.management_api_client import ManagementApiClient

    mgmt = ManagementApiClient()
    service = TownService()
    ws_manager = get_town_agent_ws_manager()
    speaker_id = agent_conn.agent_id

    def _reject(reason: str) -> None:
        try:
            mgmt.send_message(
                connection_id,
                {"event": "say_rejected", "reason": reason},
            )
        except Exception as exc:
            logger.warning("Failed to send say_rejected to %s: %s", speaker_id, exc)

    conv_id = body.get("conv_id", "").strip()
    message = body.get("message", "").strip()

    if not conv_id or not message:
        _reject("missing_conv_id_or_message")
        return

    # Validate the conversation
    conv = service.get_conversation_by_id(conv_id)
    if not conv:
        _reject("conversation_not_found")
        return

    if conv.get("status") != "active":
        _reject("conversation_not_active")
        return

    participants: list[str] = conv.get("participants") or [
        conv.get("participantAId"), conv.get("participantBId"),
    ]
    participants = [p for p in participants if p]
    if speaker_id not in participants:
        _reject("not_a_participant")
        return

    # Enforce "not twice in a row" for group conversations (3+ participants)
    if len(participants) > 2 and conv.get("lastSpeakerId") == speaker_id:
        _reject("must_let_others_speak")
        return

    # Append the message
    speaker = service.get_town_agent_by_id(speaker_id)
    speaker_display = speaker.get("displayName", agent_conn.agent_name) if speaker else agent_conn.agent_name

    updated = service.append_message_to_conversation(conv_id, speaker_id, speaker_display, message)
    if not updated:
        _reject("conversation_not_active")
        return

    turn_count = updated.get("turnCount", 0)

    # Forward message to ALL other participants
    for pid in participants:
        if pid == speaker_id:
            continue
        other_conn = ws_manager.get_by_agent_id(pid)
        if not other_conn:
            continue
        try:
            mgmt.send_message(
                other_conn.connection_id,
                {
                    "event": "conversation_message",
                    "conv_id": conv_id,
                    "from": speaker_display,
                    "from_agent_name": agent_conn.agent_name,
                    "text": message,
                    "turn": turn_count,
                },
            )
        except Exception as exc:
            logger.warning("Failed to forward message to %s: %s", pid, exc)

    # Send speech bubble to game server for viewer display
    try:
        gs_client = get_game_server_client()
        await gs_client.send_command(speaker_id, {"cmd": "set_speech", "text": message})
    except Exception:
        pass  # Speech bubble is cosmetic, don't fail the conversation

    # Set talking activity on the speaker
    try:
        gs_client = get_game_server_client()
        await gs_client.send_command(speaker_id, {"cmd": "set_activity", "activity": "talking"})
    except Exception:
        pass

    logger.info("Conversation %s turn %d: %s said %d chars", conv_id, turn_count, speaker_id, len(message))

    # Auto-end at turn limit
    if turn_count >= _MAX_CONVERSATION_TURNS:
        logger.info("Conversation %s reached turn limit (%d), auto-ending", conv_id, _MAX_CONVERSATION_TURNS)
        await _finalize_conversation(conv_id, service, mgmt, ws_manager, auto_ended=True)


async def _handle_join_conversation(connection_id: str, body: dict, agent_conn) -> None:
    """Handle a join_conversation action — agent joins an existing active conversation."""
    from core.services.management_api_client import ManagementApiClient

    mgmt = ManagementApiClient()
    service = TownService()
    ws_manager = get_town_agent_ws_manager()
    joiner_id = agent_conn.agent_id

    def _reject(reason: str) -> None:
        try:
            mgmt.send_message(
                connection_id,
                {"event": "join_rejected", "reason": reason},
            )
        except Exception as exc:
            logger.warning("Failed to send join_rejected to %s: %s", joiner_id, exc)

    conv_id = body.get("conv_id", "")
    if not conv_id:
        _reject("missing_conv_id")
        return

    joiner = service.get_town_agent_by_id(joiner_id)
    if not joiner:
        _reject("joiner_not_found")
        return

    if joiner.get("currentConversationId"):
        _reject("already_in_conversation")
        return

    updated = service.join_conversation(conv_id, joiner_id)
    if updated is None:
        _reject("conversation_not_joinable")
        return

    joiner_display = joiner.get("displayName", agent_conn.agent_name)
    participants: list[str] = updated.get("participants", [])

    # Notify all other participants
    for pid in participants:
        if pid == joiner_id:
            continue
        conn = ws_manager.get_by_agent_id(pid)
        if not conn:
            continue
        try:
            mgmt.send_message(
                conn.connection_id,
                {
                    "event": "participant_joined",
                    "conv_id": conv_id,
                    "agent_id": joiner_id,
                    "display_name": joiner_display,
                },
            )
        except Exception as exc:
            logger.warning("Failed to send participant_joined to %s: %s", pid, exc)

    # Acknowledge to the joiner
    try:
        mgmt.send_message(
            connection_id,
            {"event": "act_ok", "action": "join_conversation", "conv_id": conv_id},
        )
    except Exception as exc:
        logger.warning("Failed to send join act_ok to %s: %s", joiner_id, exc)

    logger.info("Agent %s joined conversation %s (%d participants)", joiner_id, conv_id, len(participants))


async def _handle_end_conversation(connection_id: str, body: dict, agent_conn) -> None:
    """Handle an ``end_conversation`` action — agent LEAVES the conversation.

    Validates the requester is a participant, marks the conversation ended,
    applies mood effects to both agents, updates the relationship affinity,
    and notifies both participants.

    Args:
        connection_id: API Gateway connection ID of the ending agent.
        body: Parsed message body with ``conv_id``.
        agent_conn: Connection record for the ending agent.
    """
    from core.services.management_api_client import ManagementApiClient

    mgmt = ManagementApiClient()
    service = TownService()
    ws_manager = get_town_agent_ws_manager()
    ender_id = agent_conn.agent_id

    def _reject(reason: str) -> None:
        try:
            mgmt.send_message(
                connection_id,
                {"event": "end_rejected", "reason": reason},
            )
        except Exception as exc:
            logger.warning("Failed to send end_rejected to %s: %s", ender_id, exc)

    conv_id = body.get("conv_id", "").strip()
    if not conv_id:
        _reject("missing_conv_id")
        return

    conv = service.get_conversation_by_id(conv_id)
    if not conv:
        _reject("conversation_not_found")
        return

    participants: list[str] = conv.get("participants") or [
        conv.get("participantAId"), conv.get("participantBId"),
    ]
    participants = [p for p in participants if p]
    if ender_id not in participants:
        _reject("not_a_participant")
        return

    if conv.get("status") != "active":
        _reject("conversation_not_active")
        return

    # Leave semantics: remove agent from participants
    remaining = service.leave_conversation(conv_id, ender_id)

    # Notify remaining participants that someone left
    leaver_display = agent_conn.agent_name
    for pid in participants:
        if pid == ender_id:
            continue
        conn = ws_manager.get_by_agent_id(pid)
        if not conn:
            continue
        try:
            mgmt.send_message(
                conn.connection_id,
                {
                    "event": "participant_left",
                    "conv_id": conv_id,
                    "agent_id": ender_id,
                    "display_name": leaver_display,
                    "remaining": remaining,
                },
            )
        except Exception as exc:
            logger.warning("Failed to send participant_left to %s: %s", pid, exc)

    # Acknowledge leaver
    try:
        mgmt.send_message(
            connection_id,
            {"event": "act_ok", "action": "end_conversation", "conv_id": conv_id},
        )
    except Exception:
        pass

    # If fewer than 2 remain, finalize the conversation entirely
    if remaining < 2:
        await _finalize_conversation(conv_id, service, mgmt, ws_manager, auto_ended=False)


async def _finalize_conversation(
    conv_id: str,
    service: TownService,
    mgmt,
    ws_manager,
    auto_ended: bool,
    end_reason_override: str | None = None,
) -> None:
    """End a conversation, apply mood/relationship effects, and notify both agents.

    Extracted from ``_handle_end_conversation`` so the auto-end path on turn
    limit can share the same logic.

    Args:
        conv_id: Conversation UUID to finalise.
        service: ``TownService`` instance (caller's, to avoid re-instantiation).
        mgmt: ``ManagementApiClient`` instance.
        ws_manager: Active WebSocket manager.
        auto_ended: Whether the conversation ended by hitting the turn limit
            (``True``) or by explicit agent request (``False``).
    """
    from core.services import town_mood_engine

    conv = service.end_conversation(conv_id)
    if not conv:
        logger.warning("end_conversation returned None for conv_id %s", conv_id)
        return

    participants: list[str] = conv.get("participants") or [
        conv.get("participantAId"), conv.get("participantBId"),
    ]
    participants = [p for p in participants if p]
    turn_count = conv.get("turnCount", 0)
    location = conv.get("location", "unknown")

    if end_reason_override:
        end_reason = end_reason_override
    else:
        end_reason = "turn_limit_reached" if auto_ended else "ended_by_participant"

    # Apply mood/energy effects for each participant
    for agent_id in participants:
        agent = service.get_town_agent_by_id(agent_id)
        if not agent:
            continue

        current_energy = int(agent.get("energy", 100))
        current_mood_raw = agent.get("mood", 0)
        current_mood = town_mood_engine.parse_mood(current_mood_raw)
        traits = agent.get("traits", "")

        new_energy, new_mood = town_mood_engine.apply_event(
            "conversation_completed", current_energy, current_mood, traits
        )
        service.update_agent_mood_energy(agent_id, new_mood, new_energy)

    # Update relationship affinity for all participant pairs
    if len(participants) >= 2:
        for i in range(len(participants)):
            for j in range(i + 1, len(participants)):
                try:
                    service.get_or_create_relationship(participants[i], participants[j])
                    service.update_relationship(participants[i], participants[j], affinity_delta=5)
                except Exception as exc:
                    logger.warning("Failed to update relationship: %s", exc)

    # Clear talking activity and speech bubbles for all participants
    gs_client = get_game_server_client()
    for agent_id in participants:
        try:
            await gs_client.send_command(agent_id, {"cmd": "set_activity", "activity": ""})
            await gs_client.send_command(agent_id, {"cmd": "set_speech", "text": ""})
        except Exception:
            pass

    # Notify all connected participants
    public_log = conv.get("publicLog") or []
    log_summary = _build_log_summary(public_log)

    for agent_id in participants:
        conn = ws_manager.get_by_agent_id(agent_id)
        if not conn:
            continue
        try:
            mgmt.send_message(
                conn.connection_id,
                {
                    "event": "conversation_ended",
                    "conv_id": conv_id,
                    "turn_count": turn_count,
                    "location": location,
                    "reason": end_reason,
                    "log_summary": log_summary,
                },
            )
        except Exception as exc:
            logger.warning("Failed to send conversation_ended to %s: %s", agent_id, exc)

    logger.info(
        "Conversation %s finalised (%s, %d turns, location=%s, %d participants)",
        conv_id, end_reason, turn_count, location, len(participants),
    )


def _build_log_summary(public_log: list, max_turns: int = 5) -> str:
    """Build a short plaintext summary of the last N turns for the ended event.

    Included in ``conversation_ended`` so agents can log to TOWN_LIFE.md
    without re-fetching the full log.

    Args:
        public_log: List of ``{speaker, text}`` dicts from DynamoDB.
        max_turns: How many recent turns to include (default: last 5).

    Returns:
        Multi-line string, or empty string if the log is empty.
    """
    if not public_log:
        return ""
    recent = public_log[-max_turns:]
    return "\n".join(f"{t.get('speaker', '?')}: {t.get('text', '')}" for t in recent)


async def _handle_agent_connect(request: WsMessageRequest, ws_manager):
    """Process a town_agent_connect message: verify token, register connection, send connected event."""
    from core.services.management_api_client import ManagementApiClient
    from core.town_token import verify_town_token

    body = request.body
    token = body.get("token", "")
    agent_name = body.get("agent_name", "")

    if not token or not agent_name:
        logger.warning("Agent connect missing token or agent_name")
        return {"statusCode": 400, "error": "missing_token_or_agent_name"}

    # Verify town_token
    payload = verify_town_token(token)
    if not payload:
        logger.warning("Invalid town_token for agent connect: %s", agent_name)
        return {"statusCode": 401, "error": "invalid_token"}

    user_id = payload["uid"]
    instance_id = payload["iid"]

    # Look up the agent
    service = TownService()
    agent = service.get_agent_by_name(user_id, agent_name)
    if not agent:
        logger.warning("Agent not found: %s for user %s", agent_name, user_id)
        return {"statusCode": 404, "error": "agent_not_found"}

    agent_id = agent["id"]

    # Register connection in ws_manager
    ws_manager.register(
        connection_id=request.connectionId,
        user_id=user_id,
        agent_name=agent_name,
        agent_id=agent_id,
        instance_id=instance_id,
    )

    # Persist the daemon's connection ID to DynamoDB so it survives restarts
    logger.info("[agent:%s] daemonConnectionId updated: %s", agent_id[:8], request.connectionId[:12])
    service.update_agent_state(agent_id, daemon_connection_id=request.connectionId)

    # Wake up in bed. On every daemon reconnect the agent "wakes up at home"
    # — they can walk themselves out of the apartment from there.
    location_context = "apartment"
    current_location = "bed"
    service.update_agent_context(agent_id, location_context, current_location)

    # Set in-memory context on the connection record
    ws_manager.update_context(agent_id, location_context)

    # Connect agent to game server (shadow sprite fallback if no custom sprite)
    gs_client = get_game_server_client()
    try:
        await gs_client.connect_agent(
            agent_id=agent_id,
            display_name=agent.get("displayName", agent_name),
            sprite_url=agent.get("spriteUrl", ""),
            location=current_location,
            context=location_context,
            user_id=user_id,
            sprite_animations=agent.get("spriteAnimations", {}),
            on_event=_make_agent_event_callback(),
        )
        logger.info(
            "Agent %s connected to game server (context=%s, location=%s, userId=%s)",
            agent_name, location_context, current_location, user_id[:12],
        )

    except Exception as e:
        logger.warning("Failed to connect agent %s to game server: %s", agent_name, e)

    # Send connected event back to the agent via Management API
    try:
        mgmt = ManagementApiClient()
        mgmt.send_message(
            request.connectionId,
            {
                "event": "connected",
                "agent": {
                    "id": agent_id,
                    "name": agent.get("displayName", agent_name),
                    "location": current_location,
                    "locationContext": location_context,
                    "mood": agent.get("mood", 0),
                    "energy": agent.get("energy", 100),
                    "spriteReady": agent.get("spriteReady", False),
                },
            },
        )
        logger.info("Sent connected event to agent %s", agent_name)
    except Exception as e:
        logger.error("Failed to send connected event to %s: %s", agent_name, e)

    return {"statusCode": 200}


# ===========================================================================
# Activity system
# ===========================================================================

# Strong references to activity timer tasks to prevent GC
_activity_tasks: set = set()
_custom_anim_in_progress: set[tuple[str, str]] = set()  # (agent_id, animation_name)


async def _handle_agent_activity(connection_id: str, activity_name: str, agent_conn) -> None:
    """Validate and start a timed activity for a connected agent.

    Sends an ``activity_rejected`` event back to the agent if validation
    fails, or an ``act_ok`` followed by an asyncio background timer that
    sends ``activity_complete`` (or ``activity_interrupted``) when the
    duration elapses.

    For apartment activities that have a ``spot`` field the agent is first
    navigated to that furniture marker via the game server; the activity
    timer starts only after the ``arrived`` event for that spot.

    Args:
        connection_id: API Gateway connection ID for the acting agent.
        activity_name: The ``town_act`` argument (e.g. ``"read"``).
        agent_conn: Connection record from ``TownAgentWsManager``.
    """
    from core.services.activity_registry import (
        canonical_location,
        get_activity,
        get_context_mismatch_message,
    )
    from core.services.management_api_client import ManagementApiClient

    mgmt = ManagementApiClient()
    service = TownService()
    agent_id = agent_conn.agent_id

    def _reject(reason: str, message: Optional[str] = None) -> None:
        payload: dict = {"event": "activity_rejected", "activity": activity_name, "reason": reason}
        if message:
            payload["message"] = message
        try:
            mgmt.send_message(connection_id, payload)
        except Exception as exc:
            logger.warning("Failed to send activity_rejected to %s: %s", agent_id, exc)

    # --- Look up activity ---
    activity = get_activity(activity_name)
    if activity is None:
        _reject("unknown_activity")
        return

    # --- Load agent from DB ---
    agent = service.get_town_agent_by_id(agent_id)
    if not agent:
        _reject("agent_not_found")
        return

    # --- Validate location context (town vs apartment) ---
    # This must happen before the fine-grained location check so the agent
    # receives a useful navigation hint rather than a bare "wrong_location".
    agent_location_context = agent.get("locationContext", "town")
    mismatch_msg = get_context_mismatch_message(activity, agent_location_context)
    if mismatch_msg:
        _reject("wrong_context", mismatch_msg)
        return

    # --- Validate fine-grained location within the correct context ---
    current_location = agent.get("currentLocation", "")
    if canonical_location(current_location) != activity.location:
        _reject("wrong_location")
        return

    # --- Validate not already busy ---
    if agent.get("currentActivity"):
        _reject("already_busy")
        return

    # --- Mark agent as busy in DB ---
    import time as _time

    logger.info("[agent:%s] Activity START: %s (conn=%s)", agent_id[:8], activity_name, connection_id[:12])
    service.update_agent_activity(agent_id, activity_name, _time.time())

    # Notify game server of activity state change
    try:
        gs_client = get_game_server_client()
        await gs_client.send_command(agent_id, {
            "cmd": "set_activity",
            "activity": activity_name,
        })
    except Exception as exc:
        logger.warning("Failed to send set_activity to game server for %s: %s", agent_id, exc)

    # --- Acknowledge to the agent ---
    try:
        mgmt.send_message(
            connection_id,
            {
                "event": "act_ok",
                "activity": activity_name,
                "duration_seconds": activity.duration_seconds,
            },
        )
    except Exception as exc:
        logger.warning("Failed to send act_ok to %s: %s", agent_id, exc)

    # --- Trigger lazy custom animation generation if needed ---
    from core.services.pixellab_service import CUSTOM_ANIMATIONS

    activity_anim = activity.animation
    if activity_anim in CUSTOM_ANIMATIONS:
        agent_animations = agent.get("spriteAnimations") or {}
        anim_key = (agent_id, activity_anim)
        char_id = agent.get("pixellabCharacterId") or agent.get("pixellab_character_id")
        logger.info(
            "Custom animation check: anim=%s, already_have=%s, in_progress=%s, char_id=%s",
            activity_anim,
            activity_anim in agent_animations,
            anim_key in _custom_anim_in_progress,
            char_id or "MISSING",
        )
        if activity_anim not in agent_animations and anim_key not in _custom_anim_in_progress:
            if char_id:
                _custom_anim_in_progress.add(anim_key)
                logger.info(
                    "Triggering lazy animation generation: %s for agent %s (char %s)",
                    activity_anim, agent_id, char_id,
                )
                gen_task = asyncio.create_task(
                    _generate_custom_animation(agent_id, char_id, activity_anim)
                )
                _activity_tasks.add(gen_task)
                gen_task.add_done_callback(_activity_tasks.discard)
            else:
                logger.warning(
                    "Cannot trigger custom animation %s for agent %s: no pixellabCharacterId in DB",
                    activity_anim, agent_id,
                )

    # --- Schedule the activity timer ---
    task = asyncio.create_task(_activity_timer(connection_id, agent_id, activity, agent))
    _activity_tasks.add(task)

    def _on_timer_done(t: asyncio.Task) -> None:
        _activity_tasks.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            logger.error(
                "[agent:%s] Activity timer CRASHED for %s: %s",
                agent_id[:8], activity.name, exc, exc_info=exc,
            )
            # Best-effort cleanup: clear the stuck activity in DB
            try:
                TownService().update_agent_activity(agent_id, None, None)
                logger.info("[agent:%s] Cleared stuck activity %s after timer crash", agent_id[:8], activity.name)
            except Exception:
                logger.warning("[agent:%s] Failed to clear stuck activity %s", agent_id[:8], activity.name)

    task.add_done_callback(_on_timer_done)


async def _generate_custom_animation(agent_id: str, character_id: str, animation_name: str) -> None:
    """Background task: generate a custom animation via animate-with-text-v2.

    Uses the PixelLab V2 pro endpoint which takes the character's rotation
    images and an action description. Generates one direction at a time,
    collects frames, composites into a spritesheet, uploads to S3.
    """
    from core.config import settings
    from core.services.pixellab_service import ANIMATION_REGISTRY, CUSTOM_ANIMATIONS, PixelLabService
    from core.services.sprite_storage import composite_v2_animation, upload_sprites_to_s3

    config = CUSTOM_ANIMATIONS.get(animation_name)
    if not config:
        return

    pxl = PixelLabService()
    service = TownService()

    try:
        # Queue custom animation (one V2 job per direction)
        job_ids = await pxl.queue_custom_animation(
            character_id=character_id,
            animation_name=animation_name,
            action_description=config["prompt"],
        )
        if not job_ids:
            logger.warning("No job IDs returned for custom animation %s", animation_name)
            return

        logger.info(
            "Custom animation %s queued for agent %s (%d jobs), waiting...",
            animation_name, agent_id, len(job_ids),
        )

        # Wait for all V2 jobs to complete
        await pxl.wait_for_jobs(
            job_ids,
            progress_label=f"custom:{animation_name}",
        )

        # Collect frames from each completed job
        # Jobs are ordered: south, west, east, north (matching queue order)
        directions = ["south", "west", "east", "north"]
        direction_frames: dict[str, list[bytes]] = {}
        for i, job_id in enumerate(job_ids):
            direction = directions[i] if i < len(directions) else f"dir{i}"
            frames = await pxl.get_v2_animation_frames(job_id)
            if frames:
                direction_frames[direction] = frames
                logger.info("Got %d frames for %s/%s", len(frames), animation_name, direction)
            else:
                logger.warning("No frames for %s/%s (job %s)", animation_name, direction, job_id)

        if not direction_frames:
            logger.warning("No frames from any direction for %s", animation_name)
            return

        # Composite into spritesheet
        result = composite_v2_animation(direction_frames)
        if result is None:
            logger.warning("Failed to composite %s for agent %s", animation_name, agent_id)
            return

        png_bytes, frame_count = result
        all_sheets = {animation_name: (png_bytes, frame_count)}

        # Upload spritesheet to S3
        uploaded = await asyncio.to_thread(
            upload_sprites_to_s3, all_sheets, agent_id, settings.SPRITE_S3_BUCKET
        )

        # Merge into existing spriteAnimations
        agent = service.get_town_agent_by_id(agent_id)
        existing = (agent.get("spriteAnimations") or {}) if agent else {}

        for anim_name, info in uploaded.items():
            registry_entry = ANIMATION_REGISTRY.get(anim_name, {})
            existing[anim_name] = {
                "url": f"{settings.SPRITE_CDN_URL}/{info['key']}",
                "frameCount": info["frame_count"],
                "fps": int(registry_entry.get("fps", 8)),
            }

        service.update_agent_state(agent_id, sprite_animations=existing)
        logger.info("Custom animation %s ready for agent %s", animation_name, agent_id)

        # Push sprites_updated to daemon and viewers
        try:
            from core.services.management_api_client import ManagementApiClient

            ws_mgr = get_town_agent_ws_manager()
            conn = ws_mgr.get_by_agent_id(agent_id)
            if conn:
                mgmt = ManagementApiClient()
                mgmt.send_message(conn.connection_id, {
                    "event": "sprites_updated",
                    "agent_id": agent_id,
                    "animations": existing,
                })
        except Exception:
            pass

    except Exception:
        logger.exception("Custom animation generation failed: %s for agent %s", animation_name, agent_id)
    finally:
        _custom_anim_in_progress.discard((agent_id, animation_name))


async def _activity_timer(
    connection_id: str,
    agent_id: str,
    activity,
    agent_snapshot: dict,
) -> None:
    """Background timer that completes a timed activity.

    For apartment activities with a ``spot`` the agent is first navigated
    to the furniture marker.  The activity duration begins only after the
    game server confirms arrival at the spot (``arrived`` event).

    Args:
        connection_id: API Gateway connection ID — used to push events back.
        agent_id: Internal agent UUID.
        activity: The ``Activity`` being performed.
        agent_snapshot: Agent DB record captured at activity start (used for
            initial energy/mood; fresh values are re-fetched before applying
            effects so concurrent updates are handled correctly).
    """
    from core.services.activity_registry import apply_activity_effect
    from core.services.management_api_client import ManagementApiClient

    mgmt = ManagementApiClient()
    service = TownService()
    gs_client = get_game_server_client()

    import time as _time

    start_time = _time.time()

    # --- For apartment activities, move to the furniture spot first ---
    if activity.spot:
        try:
            # Register arrival waiter BEFORE sending move to avoid race condition
            arrival_event = gs_client.expect_arrival(agent_id)
            await gs_client.send_command(
                agent_id,
                {"cmd": "move", "destination": activity.spot},
            )
            await asyncio.wait_for(
                gs_client.wait_for_arrival(agent_id, arrival_event),
                timeout=60.0,
            )
            # Reset start time: timer begins from when agent arrives at spot
            start_time = _time.time()
        except asyncio.TimeoutError:
            logger.warning(
                "Agent %s timed out navigating to spot %s for activity %s",
                agent_id,
                activity.spot,
                activity.name,
            )
            _clear_activity_and_notify(service, mgmt, connection_id, agent_id, activity.name, "navigation_timeout")
            try:
                await gs_client.send_command(agent_id, {"cmd": "set_activity", "activity": ""})
            except Exception:
                pass
            return
        except Exception as exc:
            logger.warning("Navigation error for agent %s activity %s: %s", agent_id, activity.name, exc)
            _clear_activity_and_notify(service, mgmt, connection_id, agent_id, activity.name, "navigation_error")
            try:
                await gs_client.send_command(agent_id, {"cmd": "set_activity", "activity": ""})
            except Exception:
                pass
            return

    # --- Run the activity timer ---
    await asyncio.sleep(activity.duration_seconds)

    elapsed = _time.time() - start_time

    # --- Fetch fresh agent state before applying effects ---
    agent = service.get_town_agent_by_id(agent_id)
    if not agent:
        logger.warning("Agent %s not found when completing activity %s", agent_id, activity.name)
        return

    current_energy = int(agent.get("energy", 100))
    current_mood = parse_mood(agent.get("mood"))
    traits = agent.get("traits", "")

    new_energy, new_mood, interrupted = apply_activity_effect(activity, current_energy, current_mood, traits, elapsed)

    # --- Persist updated mood/energy and clear activity ---
    service.update_agent_mood_energy(agent_id, new_mood, new_energy)
    service.update_agent_activity(agent_id, None, None)
    logger.info(
        "[agent:%s] Activity COMPLETE: %s (mood %d→%d, energy %d→%d, conn=%s)",
        agent_id[:8], activity.name, current_mood, new_mood, current_energy, new_energy, connection_id[:12],
    )

    # Clear activity state in game server
    try:
        await gs_client.send_command(agent_id, {
            "cmd": "set_activity",
            "activity": "",
        })
    except Exception as exc:
        logger.warning("Failed to clear activity in game server for %s: %s", agent_id, exc)

    # --- Send outcome event to agent ---
    event_name = "activity_interrupted" if interrupted else "activity_complete"
    try:
        mgmt.send_message(
            connection_id,
            {
                "event": event_name,
                "activity": activity.name,
                "mood_delta": new_mood - current_mood,
                "energy_delta": new_energy - current_energy,
                "new_mood": new_mood,
                "new_energy": new_energy,
                "interrupted": interrupted,
            },
        )
        logger.info(
            "Activity %s %s for agent %s (mood %d→%d, energy %d→%d)",
            activity.name,
            "interrupted" if interrupted else "complete",
            agent_id,
            current_mood,
            new_mood,
            current_energy,
            new_energy,
        )
    except Exception as exc:
        logger.warning("Failed to send %s to %s: %s", event_name, agent_id, exc)


def _clear_activity_and_notify(
    service: TownService,
    mgmt,
    connection_id: str,
    agent_id: str,
    activity_name: str,
    reason: str,
) -> None:
    """Clear the agent's busy state and send an activity_rejected event.

    Used when navigation fails before the activity timer even starts.
    """
    try:
        service.update_agent_activity(agent_id, None, None)
    except Exception as exc:
        logger.warning("Failed to clear activity for %s: %s", agent_id, exc)
    try:
        mgmt.send_message(
            connection_id,
            {"event": "activity_rejected", "activity": activity_name, "reason": reason},
        )
    except Exception as exc:
        logger.warning("Failed to send activity_rejected for navigation failure %s: %s", agent_id, exc)


async def complete_activity_from_startup(agent_id: str, activity, elapsed: float) -> None:
    """Complete an activity that should have finished while the backend was down."""
    from core.services.activity_registry import apply_activity_effect

    service = TownService()
    agent = service.get_town_agent_by_id(agent_id)
    if not agent:
        return

    current_energy = int(agent.get("energy", 100))
    current_mood = parse_mood(agent.get("mood"))
    traits = agent.get("traits", "")

    new_energy, new_mood, interrupted = apply_activity_effect(
        activity, current_energy, current_mood, traits, elapsed
    )

    service.update_agent_mood_energy(agent_id, new_mood, new_energy)
    service.update_agent_activity(agent_id, None, None)

    # Clear activity in game server
    gs_client = get_game_server_client()
    try:
        await gs_client.send_command(agent_id, {"cmd": "set_activity", "activity": ""})
    except Exception:
        pass

    # Notify daemon
    event_name = "activity_interrupted" if interrupted else "activity_complete"
    _send_to_daemon(agent_id, {
        "event": event_name,
        "activity": activity.name,
        "mood_delta": new_mood - current_mood,
        "energy_delta": new_energy - current_energy,
        "new_mood": new_mood,
        "new_energy": new_energy,
        "interrupted": interrupted,
    })

    logger.info("Startup: completed overdue activity %s for agent %s", activity.name, agent_id)


async def restart_activity_timer(agent_id: str, activity, remaining: float) -> None:
    """Restart an activity timer for the remaining duration after backend restart."""
    import asyncio

    from core.services.activity_registry import apply_activity_effect

    await asyncio.sleep(remaining)

    service = TownService()
    agent = service.get_town_agent_by_id(agent_id)
    if not agent or agent.get("currentActivity") != activity.name:
        return  # Activity was cancelled or changed

    current_energy = int(agent.get("energy", 100))
    current_mood = parse_mood(agent.get("mood"))
    traits = agent.get("traits", "")

    new_energy, new_mood, interrupted = apply_activity_effect(
        activity, current_energy, current_mood, traits, activity.duration_seconds
    )

    service.update_agent_mood_energy(agent_id, new_mood, new_energy)
    service.update_agent_activity(agent_id, None, None)

    # Clear activity in game server
    gs_client = get_game_server_client()
    try:
        await gs_client.send_command(agent_id, {"cmd": "set_activity", "activity": ""})
    except Exception:
        pass

    # Notify daemon
    event_name = "activity_interrupted" if interrupted else "activity_complete"
    _send_to_daemon(agent_id, {
        "event": event_name,
        "activity": activity.name,
        "mood_delta": new_mood - current_mood,
        "energy_delta": new_energy - current_energy,
        "new_mood": new_mood,
        "new_energy": new_energy,
        "interrupted": interrupted,
    })

    logger.info("Startup: timer completed activity %s for agent %s", activity.name, agent_id)


# ---------------------------------------------------------------------------
# Context transition handler (town ↔ apartment)
# ---------------------------------------------------------------------------

# Destination → expected source context and resulting (dest_context, dest_location)
_TRANSITION_MAP: dict[str, tuple[str, str, str]] = {
    # destination: (required_source_context, dest_context, dest_location)
    "residence": ("town", "apartment", "exit"),
    "exit": ("apartment", "town", "residence"),
}

# Which context each known destination belongs to. Used by the move handler
# to decide whether a cross-context transition needs to happen before the
# agent can navigate to a given location.
_TOWN_LOCATIONS: set[str] = {
    "plaza",
    "library",
    "cafe",
    "activity_center",
    "bank",
    "residence",
    "player_spawn",
}


def _context_for_location(location: str) -> str | None:
    """Return ``"town"`` or ``"apartment"`` for a known destination, else ``None``."""
    if location in _TOWN_LOCATIONS:
        return "town"
    try:
        from core.apartment_constants import APARTMENT_ROOMS
    except Exception:
        return None
    if location in APARTMENT_ROOMS or location == "exit":
        return "apartment"
    return None


async def _handle_context_transition(
    connection_id: str,
    destination: str,
    agent_conn,
) -> bool:
    """Handle a context-crossing move command (``residence`` or ``exit``).

    Two-phase protocol:
    1. Validate source context and busy state.
    2. Set ``locationContext: "transitioning"`` in DynamoDB and in-memory
       ws manager to block concurrent commands.
    3. Send ``transition_agent`` to the game server (handles remove+spawn
       atomically for both directions).
    4. On success: update DynamoDB to the destination context/location,
       update ws manager, push ``context_changed`` to the daemon.
    5. On failure or timeout: roll back DynamoDB and ws manager to the
       original context, push ``move_rejected`` to the daemon.

    Args:
        connection_id: API Gateway connection ID for the acting agent.
        destination: ``"residence"`` (enter apartment) or ``"exit"`` (return
            to town).
        agent_conn: Connection record from ``TownAgentWsManager``.
    """
    from core.services.management_api_client import ManagementApiClient

    mgmt = ManagementApiClient()
    service = TownService()
    ws_manager = get_town_agent_ws_manager()
    gs_client = get_game_server_client()
    agent_id = agent_conn.agent_id

    logger.info("[agent:%s] Transition START: %s (conn=%s)", agent_id[:8], destination, connection_id[:12])

    def _reject(reason: str) -> None:
        logger.warning("[agent:%s] Transition REJECTED: %s → %s", agent_id[:8], destination, reason)
        try:
            mgmt.send_message(
                connection_id,
                {"event": "move_rejected", "destination": destination, "reason": reason},
            )
        except Exception as exc:
            logger.warning("Failed to send move_rejected to %s: %s", agent_id, exc)

    # --- Load agent from DB ---
    agent = service.get_town_agent_by_id(agent_id)
    if not agent:
        _reject("agent_not_found")
        return False

    # --- Validate context: reject if already in the destination context ---
    required_source, dest_context, dest_location = _TRANSITION_MAP[destination]
    current_context = agent.get("locationContext", "town")
    if current_context != required_source:
        reason = "already_in_apartment" if destination == "residence" else "not_in_apartment"
        _reject(reason)
        return False

    # --- Validate not busy ---
    if agent.get("currentActivity"):
        _reject("busy")
        return False

    # --- Phase 1: Enter transitioning state ---
    transitioning = service.update_agent_context(agent_id, "transitioning", current_context)
    if not transitioning:
        logger.error("Failed to set transitioning state for agent %s", agent_id)
        _reject("internal_error")
        return False
    ws_manager.update_context(agent_id, "transitioning")

    # --- Phase 2: Instruct game server to move the agent across contexts ---
    agent_data = {
        "agent_id": agent_id,
        "name": agent.get("displayName", agent_conn.agent_name),
        "sprite_url": agent.get("spriteUrl", ""),
        "location": dest_location,
    }
    try:
        await asyncio.wait_for(
            gs_client.transition_agent(
                agent_id,
                to_context=dest_context,
                user_id=agent_conn.user_id,
                agent_data=agent_data,
            ),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Game server transition timed out for agent %s (%s → %s)",
            agent_id,
            current_context,
            dest_context,
        )
        service.update_agent_context(agent_id, current_context, agent.get("currentLocation", ""))
        ws_manager.update_context(agent_id, current_context)
        _reject("navigation_timeout")
        return False
    except Exception as exc:
        logger.warning("Game server transition failed for agent %s: %s", agent_id, exc)
        service.update_agent_context(agent_id, current_context, agent.get("currentLocation", ""))
        ws_manager.update_context(agent_id, current_context)
        _reject("navigation_error")
        return False

    # --- Phase 3: Commit destination state ---
    updated = service.update_agent_context(agent_id, dest_context, dest_location)
    if not updated:
        logger.error("update_agent_context returned None for agent %s after transition", agent_id)
        service.update_agent_context(agent_id, current_context, agent.get("currentLocation", ""))
        ws_manager.update_context(agent_id, current_context)
        _reject("internal_error")
        return False

    ws_manager.update_context(agent_id, dest_context)
    logger.info(
        "Context transition complete: agent %s %s → %s (location=%s)",
        agent_id,
        current_context,
        dest_context,
        dest_location,
    )

    # Notify the daemon
    try:
        mgmt.send_message(
            connection_id,
            {
                "event": "context_changed",
                "context": dest_context,
                "location": dest_location,
            },
        )
    except Exception as exc:
        logger.warning("Failed to send context_changed to %s: %s", agent_id, exc)

    # Notify browser viewers so they switch scenes
    try:
        from core.services.viewer_relay import get_viewer_relay

        relay = get_viewer_relay()
        for viewer_conn_id in list(relay._viewer_connections):
            try:
                mgmt.send_message(
                    viewer_conn_id,
                    {
                        "event": "agent_context_changed",
                        "agent_id": agent_id,
                        "context": dest_context,
                        "userId": agent_conn.user_id,
                    },
                )
            except Exception:
                pass
    except Exception as exc:
        logger.debug("Failed to notify viewers of context change: %s", exc)

    return True


async def _transition_to_apartment(
    connection_id: str,
    agent_id: str,
    agent_conn,
) -> None:
    """Deprecated shim kept for backward-compatibility with existing tests.

    The transition logic has been consolidated into ``_handle_context_transition``.
    New code should call that function directly.

    Args:
        connection_id: API Gateway connection ID for the acting agent.
        agent_id: Internal agent UUID.
        agent_conn: Connection record from ``TownAgentWsManager``.
    """
    await _handle_context_transition(connection_id, "residence", agent_conn)
