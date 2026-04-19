from dotenv import load_dotenv

load_dotenv()

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.config import settings
from core.services.game_server_client import get_game_server_client
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

    # Start inactivity sweep for dangling conversations
    import asyncio as _asyncio

    sweep_task = _asyncio.create_task(_conversation_inactivity_sweep())

    yield

    # Cleanup
    sweep_task.cancel()
    try:
        await sweep_task
    except _asyncio.CancelledError:
        pass
    await sprite_queue.stop()
    await relay.stop()
    gs_client = get_game_server_client()
    for agent_id in list(gs_client._agent_connections.keys()):
        await gs_client.disconnect_agent(agent_id)
    logger.info("GooseTown backend stopping")


async def _reconnect_active_agents() -> None:
    """Reconnect all active agents to the game server on backend startup.

    Queries DynamoDB for agents with spriteReady=true and connects them
    to the game server so the forwarding chain is ready before any daemon
    reconnects. Also repopulates the in-memory ws_manager from persisted
    daemonConnectionId fields so incoming WebSocket messages from surviving
    API Gateway connections are recognized.
    """
    from core.services.town_agent_ws import get_town_agent_ws_manager
    from core.services.town_service import TownService
    from routers.town import _make_agent_event_callback

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

            from routers.town import resume_sprite_generation

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
            from routers.town import complete_activity_from_startup

            asyncio.create_task(complete_activity_from_startup(agent_id, activity, elapsed))
        else:
            logger.info(
                "Resuming activity %s for agent %s (%.0fs remaining)",
                current_activity_name,
                agent_id,
                remaining,
            )
            from routers.town import restart_activity_timer

            asyncio.create_task(restart_activity_timer(agent_id, activity, remaining))

        activities_resumed += 1

    logger.info("Startup: resumed %d activity timers", activities_resumed)


_CONVERSATION_INACTIVITY_SECONDS = 180  # 3 minutes with no new turn → auto-end
_CONVERSATION_SWEEP_INTERVAL_SECONDS = 30


async def _conversation_inactivity_sweep() -> None:
    """Periodically auto-end conversations that stalled mid-flight.

    A conversation whose ``updatedAt`` has not advanced for
    ``_CONVERSATION_INACTIVITY_SECONDS`` is considered abandoned (one side
    invited, never replied, or both went silent). Left alone, both agents
    stay stuck with ``currentConversationId`` set and cannot start new chats.
    """
    import asyncio

    from core.services.management_api_client import ManagementApiClient
    from core.services.town_agent_ws import get_town_agent_ws_manager
    from core.services.town_service import TownService
    from routers.town import _finalize_conversation

    service = TownService()
    mgmt = ManagementApiClient()
    ws_manager = get_town_agent_ws_manager()

    while True:
        try:
            await asyncio.sleep(_CONVERSATION_SWEEP_INTERVAL_SECONDS)
            stale = service.list_stale_active_conversations(_CONVERSATION_INACTIVITY_SECONDS)
            for conv in stale:
                conv_id = conv.get("id")
                if not conv_id:
                    continue
                logger.info("Inactivity sweep: finalising stale conversation %s", conv_id)
                try:
                    await _finalize_conversation(
                        conv_id,
                        service,
                        mgmt,
                        ws_manager,
                        auto_ended=True,
                        end_reason_override="inactivity_timeout",
                    )
                except Exception:
                    logger.exception("Failed to finalise stale conversation %s", conv_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("conversation_inactivity_sweep iteration failed")


app = FastAPI(title="GooseTown API", lifespan=lifespan)

origins = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(town_router, prefix="/api/v1/town", tags=["town"])


@app.get("/health")
async def health():
    return {"status": "healthy"}
