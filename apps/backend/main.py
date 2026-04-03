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

    yield

    # Cleanup
    await relay.stop()
    gs_client = get_game_server_client()
    for agent_id in list(gs_client._agent_connections.keys()):
        await gs_client.disconnect_agent(agent_id)
    logger.info("GooseTown backend stopping")


async def _reconnect_active_agents() -> None:
    """Reconnect all active agents to the game server on backend startup.

    Queries DynamoDB for agents with spriteReady=true and connects them
    to the game server so the forwarding chain is ready before any daemon
    reconnects.
    """
    from core.services.town_service import TownService
    from routers.town import _make_agent_event_callback

    gs_client = get_game_server_client()
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
        # Skip sleeping agents (have a future wakeAt)
        wake_at = agent.get("wakeAt")
        if wake_at:
            import time
            try:
                if float(wake_at) > time.time():
                    logger.info("Agent %s is sleeping, skipping startup reconnect", agent.get("displayName", agent["id"]))
                    continue
            except (ValueError, TypeError):
                pass

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

        try:
            await gs_client.connect_agent(
                agent_id=agent_id,
                display_name=agent.get("displayName", ""),
                sprite_url=agent.get("spriteUrl", ""),
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
                agent_id, sprite_status, char_id,
            )

            from routers.town import resume_sprite_generation

            asyncio.create_task(
                resume_sprite_generation(agent_id, char_id, appearance, display_name, sprite_status)
            )
            sprite_resumed += 1

    logger.info("Startup: resumed %d interrupted sprite generations", sprite_resumed)


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
