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

_TOWN_WS_URL = os.environ.get("TOWN_WS_URL", "wss://ws-dev.isol8.co")
_TOWN_API_URL = os.environ.get("TOWN_API_URL", "https://api-dev.isol8.co/api/v1")

# Strong references to background sprite tasks to prevent GC
_background_sprite_tasks: set = set()


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
        logger.debug("Town tables not available, using defaults")
        return _build_default_state()

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

        facing_x = s.get("facing_x", 0.0)
        facing_y = s.get("facing_y", 1.0)
        speed = s.get("speed", 0.0)

        players.append(
            {
                "id": player_id,
                "position": {"x": s["position_x"], "y": s["position_y"]},
                "facing": {"dx": facing_x, "dy": facing_y},
                "speed": speed,
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
    from core.apartment_constants import APARTMENT_SPOTS

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

    spawn = APARTMENT_SPOTS["bed_1"]

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

            async def _generate_sprite():
                logger.info("_generate_sprite started for agent %s", _agent_id)
                try:
                    from core.services.pixellab_service import PixelLabService
                    from core.services.sprite_storage import extract_walk_spritesheet, upload_sprite_to_s3

                    pxl = PixelLabService(api_key=settings.PIXELLAB_API_KEY)
                    logger.info("Calling PixelLab create_character for %s", _agent_id)
                    char_id = await pxl.create_character(description=request.appearance)
                    logger.info("PixelLab returned char_id=%s for %s", char_id, _agent_id)

                    # Store character ID
                    service.update_agent_state(_agent_id, pixellab_character_id=char_id)

                    # Queue walk animation
                    job_ids = []
                    try:
                        job_ids = await pxl.animate_character(char_id, "walk")
                        logger.info("PixelLab walk animation queued for %s, jobs=%s", char_id, job_ids)
                    except Exception as anim_err:
                        logger.warning("Animation request failed (continuing to poll): %s", anim_err)

                    # Wait for animation jobs
                    if job_ids:
                        for attempt in range(30):
                            await asyncio.sleep(10)
                            statuses = []
                            for jid in job_ids:
                                try:
                                    job = await pxl.get_job_status(jid)
                                    statuses.append(job.get("status", "unknown"))
                                except Exception as e:
                                    logger.warning("Failed to check job %s: %s", jid, e)
                                    statuses.append("unknown")
                            completed = sum(1 for s in statuses if s == "completed")
                            logger.info(
                                "Animation jobs for %s: %d/%d completed (attempt %d)",
                                char_id,
                                completed,
                                len(job_ids),
                                attempt + 1,
                            )
                            if all(s == "completed" for s in statuses):
                                break
                        else:
                            logger.warning("Animation jobs timed out for %s, proceeding with download anyway", char_id)

                    # Download ZIP and extract spritesheet
                    for attempt in range(6):
                        await asyncio.sleep(10)
                        zip_bytes = await pxl.download_character_zip(char_id)
                        if zip_bytes is None:
                            logger.info("ZIP not ready for %s (attempt %d)", char_id, attempt + 1)
                            continue
                        png_bytes = extract_walk_spritesheet(zip_bytes)
                        if png_bytes:
                            s3_key = await asyncio.to_thread(
                                upload_sprite_to_s3, png_bytes, str(_agent_id), settings.SPRITE_S3_BUCKET
                            )
                            cdn_url = f"{settings.SPRITE_CDN_URL}/{s3_key}"
                            service.update_agent_state(_agent_id, sprite_ready=True, sprite_url=cdn_url)
                            logger.info("Sprite ready for agent %s", _agent_id)
                            return
                        logger.info("ZIP downloaded but no walk frames yet for %s (attempt %d)", char_id, attempt + 1)
                    logger.warning("Sprite generation timed out for agent %s", _agent_id)
                except Exception as e:
                    logger.exception("PixelLab sprite generation failed for %s: %s", _agent_id, e)

            task = asyncio.create_task(_generate_sprite())
            _background_sprite_tasks.add(task)
            task.add_done_callback(_background_sprite_tasks.discard)

    return {
        "agent_id": agent["id"],
        "agent_name": agent.get("agentName", request.agent_name),
        "display_name": agent.get("displayName", request.display_name),
        "position": {"x": float(spawn["x"]), "y": float(spawn["y"])},
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
    from core.apartment_constants import APARTMENT_SPOTS

    service = TownService()
    active_agents = service.get_active_agents()

    # Filter to this user's agents
    user_agents = [a for a in active_agents if a.get("userId") == auth.user_id]

    agents = []
    for item in user_agents:
        # Determine current spot from position
        current_spot = None
        pos_x = item.get("positionX", 0.0)
        pos_y = item.get("positionY", 0.0)
        if pos_x is not None and pos_y is not None:
            for spot_id, spot in APARTMENT_SPOTS.items():
                if abs(pos_x - spot["x"]) < 0.5 and abs(pos_y - spot["y"]) < 0.5:
                    current_spot = spot_id
                    break

        agents.append(
            ApartmentAgentState(
                agent_id=item["id"],
                agent_name=item.get("agentName", ""),
                display_name=item.get("displayName", ""),
                character=item.get("character"),
                location_context=item.get("locationContext", "apartment"),
                current_location=item.get("currentLocation"),
                current_activity=item.get("currentActivity"),
                mood=item.get("mood"),
                energy=item.get("energy", 100),
                status_message=item.get("statusMessage"),
                position_x=float(pos_x) if pos_x is not None else 0.0,
                position_y=float(pos_y) if pos_y is not None else 0.0,
                speed=float(item.get("speed", 0.0)),
                facing_x=float(item.get("facingX", 0.0)),
                facing_y=float(item.get("facingY", 1.0)),
                current_spot=current_spot,
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
    agent_conn = ws_manager.get_by_connection(request.connectionId)

    if not agent_conn:
        return {"statusCode": 400, "error": "unknown_connection"}

    body = request.body
    if "cmd" in body:
        gs_client = get_game_server_client()
        await gs_client.send_command(agent_conn.agent_id, body)

    return {"statusCode": 200}
