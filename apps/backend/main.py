from dotenv import load_dotenv

load_dotenv()

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.config import settings
from core.services.town_simulation import TownSimulation
from routers.town import router as town_router
from routers.ws_messages import router as ws_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

_simulation: TownSimulation | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _simulation
    logger.info("GooseTown backend starting")

    # Start simulation loop
    _simulation = TownSimulation()
    await _simulation.start()
    logger.info("TownSimulation started")

    yield

    # Shutdown simulation loop
    if _simulation:
        await _simulation.stop()
        logger.info("TownSimulation stopped")
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
app.include_router(ws_router, prefix="/api/v1/town", tags=["ws"])


@app.get("/health")
async def health():
    return {"status": "healthy"}
