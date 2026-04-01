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

        # Relay world_state from game server to all browser viewer connections
        async def _relay_world_state(_agent_id: str | None, data: dict) -> None:
            from core.services.management_api_client import ManagementApiClient

            try:
                mgmt = ManagementApiClient()
                # Get all viewer connections from DynamoDB
                import core.dynamo as dynamo

                table = dynamo.connections_table()
                resp = table.scan(
                    FilterExpression="connectionType = :t",
                    ExpressionAttributeValues={":t": "viewer"},
                )
                for item in resp.get("Items", []):
                    conn_id = item.get("connectionId")
                    if conn_id:
                        try:
                            mgmt.send_message(conn_id, data)
                        except Exception:
                            pass  # Stale connection, ignore
            except Exception as relay_err:
                logger.debug("world_state relay error: %s", relay_err)

        gs_client.on_event("world_state", _relay_world_state)

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
