from dotenv import load_dotenv

load_dotenv()

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from core.config import settings
from core.services.game_server_client import get_game_server_client
from routers import landing as landing_router
from routers.town import router as town_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("GooseTown backend starting")

    settings.validate()

    # Start viewer relay (game server → browser viewers)
    from core.services.viewer_relay import get_viewer_relay

    relay = get_viewer_relay()
    await relay.start()

    # Reconnect active agents to game server on startup
    await _reconnect_active_agents()

    # Start sprite generation queue
    from core.services.sprite_queue import get_sprite_queue

    sprite_queue = get_sprite_queue()
    sprite_queue.start()

    # Start Exchange deadline sweeper
    import asyncio

    from core.services.exchange_service import exchange_sweeper_loop

    app.state.exchange_sweeper = asyncio.create_task(exchange_sweeper_loop())

    yield

    # Cleanup
    app.state.exchange_sweeper.cancel()
    await sprite_queue.stop()
    await relay.stop()
    gs_client = get_game_server_client()
    for agent_id in list(gs_client._agent_connections.keys()):
        await gs_client.disconnect_agent(agent_id)
    from core.services.pixellab_service import close_pixellab_client

    await close_pixellab_client()
    logger.info("GooseTown backend stopping")


async def _reconnect_active_agents() -> None:
    """Reconnect all active agents to the game server on backend startup.

    Queries DynamoDB for agents with spriteReady=true and connects them
    to the game server so the forwarding chain is ready before any daemon
    reconnects. Also repopulates the in-memory ws_manager from persisted
    daemonConnectionId fields so incoming WebSocket messages from surviving
    API Gateway connections are recognized.
    """
    from core.services.daemon_bridge import _make_agent_event_callback
    from core.services.town_agent_ws import get_town_agent_ws_manager
    from core.services.town_service import TownService

    gs_client = get_game_server_client()
    ws_manager = get_town_agent_ws_manager()
    service = TownService()

    try:
        agents = service.get_all_active_agents()
    except Exception:
        logger.warning("Failed to query active agents on startup", exc_info=True)
        return

    connected = 0
    for agent in agents:
        if not agent.get("spriteReady"):
            continue
        # Skip agents whose daemon is NOT currently connected. Empty
        # daemonConnectionId means their daemon disconnected earlier and
        # they are "sleeping at home" — we must not force-reconnect them
        # to the game server on CI/CD restarts. Their next daemon handshake
        # will wake them up.
        if not agent.get("daemonConnectionId"):
            continue

        agent_id = agent["id"]
        location_context = agent.get("locationContext", "town")
        current_location = agent.get("currentLocation", "plaza")

        # Recover stuck transitioning state
        if location_context == "transitioning":
            if current_location in ("exit", "desk", "bed", "couch", "kitchen"):
                location_context = "apartment"
            else:
                location_context = "town"
            service.update_agent_context(agent_id, location_context, current_location)

        # Repopulate in-memory ws_manager so incoming WebSocket messages
        # from the daemon (which survived the backend restart via API Gateway)
        # are recognized. Without this, every town_agent_act silently fails
        # with "unknown_connection" after a CI/CD deploy.
        daemon_conn_id = agent.get("daemonConnectionId", "")
        if daemon_conn_id:
            ws_manager.register(
                connection_id=daemon_conn_id,
                user_id=agent.get("userId", ""),
                agent_name=agent.get("agentName", ""),
                agent_id=agent_id,
                instance_id=agent.get("instanceId", ""),
            )
            ws_manager.update_context(agent_id, location_context)
            # Seed hot-path cache for _send_to_daemon (see town_agent_ws.py).
            # restore_phase_from_db (not advance_phase) — startup recovery
            # always trusts DDB even if it appears to "regress" a stale
            # cache from before the restart.
            from core.services.phase_lattice import restore_phase_from_db

            restore_phase_from_db(agent_id, agent.get("onboardingPhase") or "alive")
            ws_manager.update_conversation(agent_id, agent.get("currentConversationId") or "")

        try:
            await gs_client.connect_agent(
                agent_id=agent_id,
                display_name=agent.get("displayName", ""),
                sprite_url=agent.get("spriteUrl") or "",
                location=current_location,
                context=location_context,
                user_id=agent.get("userId", ""),
                sprite_animations=agent.get("spriteAnimations", {}),
                on_event=_make_agent_event_callback(),
            )
            connected += 1
        except Exception:
            logger.warning("Failed to reconnect agent %s on startup", agent_id, exc_info=True)

    logger.info("Startup: reconnected %d/%d active agents to game server", connected, len(agents))

    # Resume interrupted sprite generation
    import asyncio

    sprite_resumed = 0
    for agent in agents:
        sprite_status = agent.get("spriteStatus")
        if sprite_status in ("creating", "animating", "downloading"):
            agent_id = agent["id"]
            char_id = agent.get("pixellabCharacterId") or agent.get("pixellab_character_id")
            appearance = agent.get("appearance", "")
            display_name = agent.get("displayName", "")

            logger.info(
                "Resuming sprite generation for %s (status=%s, char=%s)",
                agent_id,
                sprite_status,
                char_id,
            )

            from core.services.sprite_pipeline import resume_sprite_generation

            asyncio.create_task(resume_sprite_generation(agent_id, char_id, appearance, display_name, sprite_status))
            sprite_resumed += 1

    logger.info("Startup: resumed %d interrupted sprite generations", sprite_resumed)

    # Resume interrupted activity timers
    import time as _time

    from core.services.activity_registry import get_activity

    activities_resumed = 0
    for agent in agents:
        current_activity_name = agent.get("currentActivity")
        if not current_activity_name:
            continue

        agent_id = agent["id"]
        activity = get_activity(current_activity_name)
        if not activity:
            # Unknown activity — clear it
            service.update_agent_activity(agent_id, None, None)
            continue

        started_at = float(agent.get("activityStartedAt", 0))
        elapsed = _time.time() - started_at if started_at else 999
        remaining = activity.duration_seconds - elapsed

        if remaining <= 0:
            logger.info(
                "Completing overdue activity %s for agent %s (%.0fs overdue)",
                current_activity_name,
                agent_id,
                -remaining,
            )
            from core.services.activity_service import complete_activity_from_startup

            asyncio.create_task(complete_activity_from_startup(agent_id, activity, elapsed))
        else:
            logger.info(
                "Resuming activity %s for agent %s (%.0fs remaining)",
                current_activity_name,
                agent_id,
                remaining,
            )
            from core.services.activity_service import restart_activity_timer

            asyncio.create_task(restart_activity_timer(agent_id, activity, remaining))

        activities_resumed += 1

    logger.info("Startup: resumed %d activity timers", activities_resumed)


app = FastAPI(title="GooseTown API", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Global exception handlers
# ---------------------------------------------------------------------------


from core.quota import QuotaExceeded  # noqa: E402


@app.exception_handler(QuotaExceeded)
async def quota_exceeded_handler(request: Request, exc: QuotaExceeded) -> JSONResponse:
    """Map QuotaExceeded → 429 with a structured payload.

    The daemon reads ``kind``, ``limit``, and ``current`` to surface a
    human-readable error to the agent owner.
    """
    return JSONResponse(
        status_code=429,
        content={
            "error": "quota_exceeded",
            "kind": exc.kind,
            "limit": exc.limit,
            "current": exc.current,
            "message": str(exc),
        },
    )


origins = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(town_router, prefix="/api/v1/town", tags=["town"])
app.include_router(landing_router.router, prefix="/api/v1")


@app.get("/health")
async def health():
    return {"status": "healthy"}
