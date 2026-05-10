"""WorldGM service entrypoint.

Run:
    python -m worldgm          (when cwd is apps/backend — local dev)
    python -m apps.backend.worldgm   (when cwd is repo root — ECS container)

Builds a Concordia interrupt-driven GameMaster, runs an async tick loop:
  1. Sync agent locations + goals + stances from DDB
  2. Decide whether to kick off a new arc
  3. Process pending arc beats
  4. Sleep until next tick

Long-lived single-replica service.  Restart loses in-memory event queue but
durable state (arcs, locations, goals, stances) survives in DDB.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from .bedrock_model import BedrockLanguageModel

TICK_INTERVAL_SECONDS = float(os.environ.get("WORLDGM_TICK_INTERVAL", "30"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

_should_exit = False


def _signal_handler(signum: int, frame: object) -> None:  # noqa: ARG001
    global _should_exit  # noqa: PLW0603
    logger.info("Received signal %d, shutting down…", signum)
    _should_exit = True


signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)


async def _tick(model_lite: BedrockLanguageModel, model_micro: BedrockLanguageModel) -> None:  # noqa: ARG001
    """One tick of the GM loop.

    Phase B: stub only.  Phases C + D will populate:

    TODO(Phase C):
      - sync_locations(gm_state)
      - maybe_kickoff_arc(gm_state)
      - process_arc_beats(gm_state)
    """


async def main() -> None:
    logger.info("Starting WorldGM service (tick=%.1fs)", TICK_INTERVAL_SECONDS)
    model_lite = BedrockLanguageModel(model_id="amazon.nova-lite-v1:0")
    model_micro = BedrockLanguageModel(model_id="amazon.nova-micro-v1:0")

    while not _should_exit:
        try:
            await _tick(model_lite, model_micro)
        except Exception:
            logger.exception("Tick failed; continuing")
        await asyncio.sleep(TICK_INTERVAL_SECONDS)

    logger.info("WorldGM service exiting cleanly")


if __name__ == "__main__":
    asyncio.run(main())
