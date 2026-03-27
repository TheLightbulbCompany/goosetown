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

    # Validate configuration before proceeding
    settings.validate()

    # Start game server client (viewer connection)
    gs_client = get_game_server_client()
    try:
        await gs_client.connect_viewer()
        logger.info("Connected to game server")
    except Exception as e:
        logger.warning("Could not connect to game server: %s (will retry on first agent connect)", e)

    yield

    # Cleanup: disconnect all agents
    for agent_id in list(gs_client._agent_connections.keys()):
        await gs_client.disconnect_agent(agent_id)
    logger.info("GooseTown backend stopping")


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
