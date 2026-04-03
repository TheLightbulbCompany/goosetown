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

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from core.auth import AuthContext, get_current_user
from core.services.game_server_client import get_game_server_client
from core.services.town_agent_ws import get_town_agent_ws_manager
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

# Track in-progress sprite generation jobs by agent_id to prevent duplicates
_sprite_jobs_in_progress: set = set()


# ---------------------------------------------------------------------------
# Shared per-agent event callback for game server → daemon forwarding
# ---------------------------------------------------------------------------


def _make_agent_event_callback():
    """Create a per-agent callback that forwards game server events to the daemon.

    Also updates DynamoDB currentLocation on 'arrived' events.
    This replaces the old global handler registration system.
    """

    async def _on_game_server_event(agent_id: str, event_data: dict) -> None:
        from core.services.management_api_client import ManagementApiClient

        evt_type = event_data.get("event", "")

        # Update DynamoDB location on arrival
        if evt_type == "arrived":
            location = event_data.get("location", "")
            if location:
                try:
                    TownService().update_agent_state(agent_id, current_location=location)
                except Exception:
                    logger.warning("Failed to update location for %s", agent_id)

        # Forward event to daemon via API Gateway Management API
        try:
            ws_mgr = get_town_agent_ws_manager()
            conn = ws_mgr.get_by_agent_id(agent_id)
            if conn:
                mgmt = ManagementApiClient()
                mgmt.send_message(conn.connection_id, event_data)
        except Exception:
            pass  # Daemon may have disconnected

    return _on_game_server_event


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

            if _agent_id in _sprite_jobs_in_progress:
                logger.info("Sprite generation already in progress for agent %s, skipping", _agent_id)
            else:
                _sprite_jobs_in_progress.add(_agent_id)
                _appearance = request.appearance
                _display_name = request.display_name

                async def _generate_sprite() -> None:
                    try:
                        from core.services.management_api_client import ManagementApiClient
                        from core.services.pixellab_service import (
                            ANIMATION_REGISTRY,
                            PixelLabService,
                        )
                        from core.services.sprite_storage import (
                            extract_all_spritesheets,
                            extract_walk_spritesheet,
                            upload_sprite_to_s3,
                            upload_sprites_to_s3,
                        )

                        pxl = PixelLabService()
                        service = TownService()

                        def _send_progress(status: str, **extra) -> None:
                            try:
                                _ws_mgr = get_town_agent_ws_manager()
                                _conn = _ws_mgr.get_by_agent_id(str(_agent_id))
                                if _conn:
                                    _mgmt = ManagementApiClient()
                                    msg = {"event": "sprite_progress", "status": status}
                                    msg.update(extra)
                                    _mgmt.send_message(_conn.connection_id, msg)
                            except Exception:
                                pass

                        # ---- Create character ----
                        _send_progress("Creating your character...")
                        char_id, creation_job_id = await pxl.create_character(
                            description=_appearance
                        )
                        service.update_agent_state(_agent_id, pixellab_character_id=char_id)
                        logger.info("PixelLab returned char_id=%s job=%s for %s", char_id, creation_job_id, _agent_id)

                        # Poll creation (up to 5 min)
                        if creation_job_id:
                            for attempt in range(30):
                                await asyncio.sleep(10)
                                try:
                                    job = await pxl.get_job_status(creation_job_id)
                                    status = job.get("status", "unknown")
                                    _send_progress(f"Creating your character sprite ({status})...")
                                    if status == "completed":
                                        break
                                except Exception as e:
                                    logger.warning("Failed to check creation job: %s", e)
                            else:
                                logger.warning("Character creation timed out for %s", char_id)

                        # ---- Queue all template animations ----
                        _send_progress("Generating animations...")
                        await pxl.queue_template_animations(char_id)
                        # Job IDs not tracked — ZIP 423/200 polling is the completion signal

                        # ---- Poll ZIP download (423 while pending, bytes when done) ----
                        # Max wait: 45 attempts x 15s = 11.25 minutes
                        zip_bytes = None
                        for attempt in range(45):
                            await asyncio.sleep(15)
                            _send_progress(f"Generating animations... ({attempt + 1}/45)")
                            try:
                                zip_bytes = await pxl.download_character_zip(char_id)
                                if zip_bytes is not None:
                                    logger.info("All animations ready for %s (attempt %d)", char_id, attempt + 1)
                                    break
                            except Exception as e:
                                logger.warning("ZIP download error for %s: %s", char_id, e)

                        if zip_bytes is None:
                            logger.warning("Animation generation timed out for %s", char_id)
                            try:
                                zip_bytes = await pxl.download_character_zip(char_id)
                            except Exception:
                                pass

                        if zip_bytes is None:
                            logger.error("No ZIP available for %s, sprite generation failed", char_id)
                            return

                        # ---- Extract all animation spritesheets ----
                        all_sheets = extract_all_spritesheets(zip_bytes)

                        # Track the CDN URL for walk (used as primary spriteUrl)
                        cdn_url = ""

                        if not all_sheets:
                            # Fallback to legacy walk extraction
                            walk_png = extract_walk_spritesheet(zip_bytes)
                            if walk_png:
                                s3_key = await asyncio.to_thread(
                                    upload_sprite_to_s3, walk_png, str(_agent_id), settings.SPRITE_S3_BUCKET
                                )
                                cdn_url = f"{settings.SPRITE_CDN_URL}/{s3_key}"
                                service.update_agent_state(_agent_id, sprite_ready=True, sprite_url=cdn_url)
                                logger.info("Fallback: walk-only sprite ready for %s", _agent_id)
                            else:
                                logger.error("No animations extracted from ZIP for %s", char_id)
                                return
                            # Fall through to game server connection block using cdn_url set above
                        else:
                            # Upload all spritesheets to S3
                            uploaded = await asyncio.to_thread(
                                upload_sprites_to_s3, all_sheets, str(_agent_id), settings.SPRITE_S3_BUCKET
                            )

                            # Build spriteAnimations metadata for DynamoDB
                            sprite_animations: dict = {}
                            for anim_name, info in uploaded.items():
                                registry_entry = ANIMATION_REGISTRY.get(anim_name, {})
                                sprite_animations[anim_name] = {
                                    "url": f"{settings.SPRITE_CDN_URL}/{info['key']}",
                                    "frameCount": info["frame_count"],
                                    "fps": registry_entry.get("fps", 8.0),
                                }

                            cdn_url = sprite_animations.get("walk", {}).get("url", "")
                            service.update_agent_state(
                                _agent_id,
                                sprite_ready=True,
                                sprite_url=cdn_url,
                                sprite_animations=sprite_animations,
                            )
                            logger.info("Sprite ready for %s: %d animations", _agent_id, len(sprite_animations))

                        # ---- Connect agent to game server ----
                        gs_client = get_game_server_client()
                        _ws_mgr = get_town_agent_ws_manager()
                        _conn = _ws_mgr.get_by_agent_id(str(_agent_id))

                        if _conn:
                            try:
                                agent_record = service.get_town_agent_by_id(str(_agent_id))
                                _sprite_anims = sprite_animations if all_sheets else {}
                                await gs_client.connect_agent(
                                    agent_id=str(_agent_id),
                                    display_name=_display_name,
                                    sprite_url=cdn_url,
                                    location=agent_record.get("currentLocation", "plaza") if agent_record else "plaza",
                                    context=_conn.location_context or "town",
                                    user_id=_conn.user_id,
                                    sprite_animations=_sprite_anims,
                                    on_event=_make_agent_event_callback(),
                                )
                                logger.info("Agent %s connected to game server", _agent_id)
                            except Exception as e:
                                logger.warning("Failed to connect agent %s to game server: %s", _agent_id, e)

                        # ---- Notify daemon ----
                        try:
                            _ws_mgr = get_town_agent_ws_manager()
                            _conn = _ws_mgr.get_by_agent_id(str(_agent_id))
                            if _conn:
                                _mgmt = ManagementApiClient()
                                _mgmt.send_message(_conn.connection_id, {
                                    "event": "sprite_ready",
                                    "agent_id": str(_agent_id),
                                    "sprite_url": cdn_url,
                                })
                        except Exception:
                            pass

                    except Exception:
                        logger.exception("Sprite generation failed for agent %s", _agent_id)
                    finally:
                        _sprite_jobs_in_progress.discard(str(_agent_id))

                task = asyncio.create_task(_generate_sprite())
                _background_sprite_tasks.add(task)
                task.add_done_callback(_background_sprite_tasks.discard)

    return {
        "agent_id": agent["id"],
        "agent_name": agent.get("agentName", request.agent_name),
        "display_name": agent.get("displayName", request.display_name),
        "status": "generating_sprite",
        "ws_url": _TOWN_WS_URL,
        "api_url": _TOWN_API_URL,
        "message": f"Welcome to GooseTown, {request.display_name}!",
    }


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
            from core.services.sprite_storage import extract_walk_spritesheet, upload_sprite_to_s3

            pxl = PixelLabService(api_key=settings.PIXELLAB_API_KEY)
            zip_bytes = await pxl.download_character_zip(pixellab_char_id)
            if zip_bytes:
                png_bytes = extract_walk_spritesheet(zip_bytes)
                if png_bytes:
                    s3_key = await asyncio.to_thread(
                        upload_sprite_to_s3, png_bytes, agent["id"], settings.SPRITE_S3_BUCKET
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
                mood=item.get("mood"),
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
async def get_conversations(
    limit: int = 20,
    auth: AuthContext = Depends(get_current_user),
):
    """Get recent public conversations."""
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
# WebSocket message forwarding (from API Gateway $default Lambda)
# ===========================================================================


class WsMessageRequest(BaseModel):
    connectionId: str
    principalId: str
    body: dict


@router.post("/ws-message")
async def handle_ws_message(request: WsMessageRequest):
    """Receive forwarded WebSocket messages from API Gateway $default Lambda."""
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
        print(f"[WS_MSG] town_agent_act: action={action}, agent={agent_conn.agent_name}, ctx={agent_conn.location_context}", flush=True)
        if action == "move":
            destination = body.get("destination", "")
            if destination in _TRANSITION_MAP:
                # Context-transition destinations are handled by the backend,
                # not forwarded to the game server directly.
                await _handle_context_transition(request.connectionId, destination, agent_conn)
            else:
                await gs_client.send_command(
                    agent_conn.agent_id,
                    {"cmd": "move", "destination": destination},
                )
        elif action == "chat":
            await _handle_chat(request.connectionId, body, agent_conn)
        elif action == "say":
            await _handle_say(request.connectionId, body, agent_conn)
        elif action == "end_conversation":
            await _handle_end_conversation(request.connectionId, body, agent_conn)
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
        await gs_client.send_command(agent_conn.agent_id, body)

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

    participant_ids = {conv.get("participantAId"), conv.get("participantBId")}
    if speaker_id not in participant_ids:
        _reject("not_a_participant")
        return

    # Append the message
    speaker = service.get_town_agent_by_id(speaker_id)
    speaker_display = speaker.get("displayName", agent_conn.agent_name) if speaker else agent_conn.agent_name

    updated = service.append_message_to_conversation(conv_id, speaker_display, message)
    if not updated:
        _reject("conversation_not_active")
        return

    turn_count = updated.get("turnCount", 0)
    other_id = next(pid for pid in participant_ids if pid != speaker_id)

    # Forward message to the other participant
    other_conn = ws_manager.get_by_agent_id(other_id)
    if other_conn:
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
            logger.warning("Failed to forward message to %s: %s", other_id, exc)

    logger.info("Conversation %s turn %d: %s said %d chars", conv_id, turn_count, speaker_id, len(message))

    # Auto-end at turn limit
    if turn_count >= _MAX_CONVERSATION_TURNS:
        logger.info("Conversation %s reached turn limit (%d), auto-ending", conv_id, _MAX_CONVERSATION_TURNS)
        await _finalize_conversation(conv_id, service, mgmt, ws_manager, auto_ended=True)


async def _handle_end_conversation(connection_id: str, body: dict, agent_conn) -> None:
    """Handle an ``end_conversation`` action — close an active conversation.

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

    participant_ids = {conv.get("participantAId"), conv.get("participantBId")}
    if ender_id not in participant_ids:
        _reject("not_a_participant")
        return

    if conv.get("status") != "active":
        _reject("conversation_not_active")
        return

    await _finalize_conversation(conv_id, service, mgmt, ws_manager, auto_ended=False)


async def _finalize_conversation(
    conv_id: str,
    service: TownService,
    mgmt,
    ws_manager,
    auto_ended: bool,
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

    a_id = conv.get("participantAId")
    b_id = conv.get("participantBId")
    turn_count = conv.get("turnCount", 0)
    location = conv.get("location", "unknown")

    end_reason = "turn_limit_reached" if auto_ended else "ended_by_participant"

    # Apply mood/energy effects and update relationship for each participant
    for agent_id in (a_id, b_id):
        if not agent_id:
            continue
        agent = service.get_town_agent_by_id(agent_id)
        if not agent:
            continue

        current_energy = int(agent.get("energy", 100))
        current_mood_raw = agent.get("mood", 0)
        # mood is stored as numeric int in DB; parse_mood handles legacy strings
        current_mood = town_mood_engine.parse_mood(current_mood_raw)
        traits = agent.get("traits", "")

        new_energy, new_mood = town_mood_engine.apply_event(
            "conversation_completed", current_energy, current_mood, traits
        )
        service.update_agent_mood_energy(agent_id, new_mood, new_energy)

    # Update relationship affinity (+5, creates relationship if missing)
    if a_id and b_id:
        try:
            service.get_or_create_relationship(a_id, b_id)
            service.update_relationship(a_id, b_id, affinity_delta=5)
        except Exception as exc:
            logger.warning("Failed to update relationship for conv %s: %s", conv_id, exc)

    # Notify both connected participants
    public_log = conv.get("publicLog") or []
    log_summary = _build_log_summary(public_log)

    for agent_id in (a_id, b_id):
        if not agent_id:
            continue
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
        "Conversation %s finalised (%s, %d turns, location=%s)",
        conv_id,
        end_reason,
        turn_count,
        location,
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

    # Read location context from agent record (new agents default to apartment)
    location_context = agent.get("locationContext", "apartment")
    current_location = agent.get("currentLocation", "exit")

    # Set in-memory context on the connection record
    ws_manager.update_context(agent_id, location_context)

    # Clear stale activity (container may have restarted mid-activity)
    current_activity = agent.get("currentActivity")
    if current_activity:
        import time as _time

        activity_started = agent.get("activityStartedAt", 0)
        elapsed = _time.time() - float(activity_started) if activity_started else 999
        # If activity has been running longer than 5 minutes, it's stale
        if elapsed > 300:
            logger.warning("Clearing stale activity %s for agent %s (%.0fs old)", current_activity, agent_id, elapsed)
            service.update_agent_activity(agent_id, None, None)

    # Connect agent to game server if sprite is ready
    if agent.get("spriteReady"):
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

        # If sprite isn't ready, start a keepalive loop that sends periodic events
        # until the sprite is ready and agent_state events take over from the game server.
        # Without this, the daemon's 30s inactivity timeout kills the connection.
        if not agent.get("spriteReady"):
            async def _sprite_keepalive(_aid: str, _conn_id: str) -> None:
                import asyncio as _asyncio

                _svc = TownService()
                for _ in range(60):  # Up to 10 minutes (60 × 10s)
                    await _asyncio.sleep(10)
                    _agent = _svc.get_town_agent_by_id(_aid)
                    if _agent and _agent.get("spriteReady"):
                        break  # Sprite ready — game server events will take over
                    _ws = get_town_agent_ws_manager()
                    _c = _ws.get_by_agent_id(_aid)
                    if not _c:
                        break  # Agent disconnected
                    try:
                        _m = ManagementApiClient()
                        _m.send_message(
                            _c.connection_id,
                            {"event": "sprite_progress", "status": "Generating your sprite..."},
                        )
                    except Exception:
                        break

            asyncio.create_task(_sprite_keepalive(agent_id, request.connectionId))
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
        if activity_anim not in agent_animations and anim_key not in _custom_anim_in_progress:
            char_id = agent.get("pixellabCharacterId") or agent.get("pixellab_character_id")
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

    # --- Schedule the activity timer ---
    task = asyncio.create_task(_activity_timer(connection_id, agent_id, activity, agent))
    _activity_tasks.add(task)
    task.add_done_callback(_activity_tasks.discard)


async def _generate_custom_animation(agent_id: str, character_id: str, animation_name: str) -> None:
    """Background task: generate a custom animation and update the agent's spriteAnimations."""
    from core.config import settings
    from core.services.pixellab_service import ANIMATION_REGISTRY, CUSTOM_ANIMATIONS, PixelLabService
    from core.services.sprite_storage import extract_all_spritesheets, upload_sprites_to_s3

    config = CUSTOM_ANIMATIONS.get(animation_name)
    if not config:
        return

    pxl = PixelLabService()
    service = TownService()

    try:
        # Check if PixelLab character already has this animation (survives backend restarts)
        existing_anims = await pxl.get_existing_animation_names(character_id)
        if animation_name in existing_anims:
            logger.info(
                "Custom animation %s already exists on PixelLab character %s, downloading",
                animation_name, character_id,
            )
        else:
            # Queue the custom animation
            job_ids = await pxl.queue_custom_animation(
                character_id=character_id,
                animation_name=animation_name,
                action_description=config["prompt"],
            )
            if not job_ids:
                logger.warning("No job IDs returned for custom animation %s", animation_name)
                return

        logger.info("Custom animation %s queued for agent %s, polling ZIP...", animation_name, agent_id)

        # Poll ZIP download (includes all animations for the character)
        zip_bytes = None
        for attempt in range(60):
            await asyncio.sleep(15)
            try:
                zip_bytes = await pxl.download_character_zip(character_id)
                if zip_bytes is not None:
                    break
            except Exception:
                pass

        if zip_bytes is None:
            logger.warning("Custom animation %s timed out for agent %s", animation_name, agent_id)
            return

        # Extract all spritesheets (the new animation + existing ones)
        all_sheets = extract_all_spritesheets(zip_bytes)
        if animation_name not in all_sheets:
            logger.warning("Animation %s not found in ZIP for agent %s", animation_name, agent_id)
            return

        # Upload all (idempotent — re-uploads existing ones too, cached on CDN)
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
                "fps": registry_entry.get("fps", 8.0),
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
    current_mood = int(agent.get("mood", 0))
    traits = agent.get("traits", "")

    new_energy, new_mood, interrupted = apply_activity_effect(activity, current_energy, current_mood, traits, elapsed)

    # --- Persist updated mood/energy and clear activity ---
    service.update_agent_mood_energy(agent_id, new_mood, new_energy)
    service.update_agent_activity(agent_id, None, None)

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


# ---------------------------------------------------------------------------
# Context transition handler (town ↔ apartment)
# ---------------------------------------------------------------------------

# Destination → expected source context and resulting (dest_context, dest_location)
_TRANSITION_MAP: dict[str, tuple[str, str, str]] = {
    # destination: (required_source_context, dest_context, dest_location)
    "residence": ("town", "apartment", "exit"),
    "exit": ("apartment", "town", "residence"),
}


async def _handle_context_transition(
    connection_id: str,
    destination: str,
    agent_conn,
) -> None:
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

    def _reject(reason: str) -> None:
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
        return

    # --- Validate context: reject if already in the destination context ---
    required_source, dest_context, dest_location = _TRANSITION_MAP[destination]
    current_context = agent.get("locationContext", "town")
    if current_context != required_source:
        reason = "already_in_apartment" if destination == "residence" else "not_in_apartment"
        _reject(reason)
        return

    # --- Validate not busy ---
    if agent.get("currentActivity"):
        _reject("busy")
        return

    # --- Phase 1: Enter transitioning state ---
    transitioning = service.update_agent_context(agent_id, "transitioning", current_context)
    if not transitioning:
        logger.error("Failed to set transitioning state for agent %s", agent_id)
        _reject("internal_error")
        return
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
        return
    except Exception as exc:
        logger.warning("Game server transition failed for agent %s: %s", agent_id, exc)
        service.update_agent_context(agent_id, current_context, agent.get("currentLocation", ""))
        ws_manager.update_context(agent_id, current_context)
        _reject("navigation_error")
        return

    # --- Phase 3: Commit destination state ---
    updated = service.update_agent_context(agent_id, dest_context, dest_location)
    if not updated:
        logger.error("update_agent_context returned None for agent %s after transition", agent_id)
        service.update_agent_context(agent_id, current_context, agent.get("currentLocation", ""))
        ws_manager.update_context(agent_id, current_context)
        _reject("internal_error")
        return

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
