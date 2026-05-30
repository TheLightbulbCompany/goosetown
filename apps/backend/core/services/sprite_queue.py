"""Bounded-concurrency sprite generation queue.

Prevents PixelLab API rate limiting by limiting concurrent jobs.
Phase A+B (character creation + static sprite) runs with bounded concurrency.
Failed jobs retry up to 3 times with exponential backoff.
"""

import asyncio
import logging
from dataclasses import dataclass

from core.services.sprite_pipeline import _sprite_pipeline_task

logger = logging.getLogger(__name__)


@dataclass
class SpriteJob:
    agent_id: str
    display_name: str
    appearance: str
    retries: int = 0
    max_retries: int = 3


class SpriteQueue:
    """Bounded-concurrency queue for sprite generation jobs."""

    def __init__(self, max_concurrent: int = 3):
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._queue: asyncio.Queue[SpriteJob] = asyncio.Queue()
        self._active_jobs: set[str] = set()
        self._worker_task: asyncio.Task | None = None

    def start(self) -> None:
        """Start the queue worker."""
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._worker())
            logger.info("SpriteQueue worker started (max_concurrent=%d)", self._semaphore._value)

    async def stop(self) -> None:
        """Stop the queue worker."""
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None

    def enqueue(self, agent_id: str, display_name: str, appearance: str) -> int:
        """Add a sprite generation job to the queue.

        Returns the queue position (0 = will start immediately, -1 = already queued).
        """
        if agent_id in self._active_jobs:
            logger.info("SpriteQueue: agent %s already queued, skipping", agent_id[:8])
            return -1
        job = SpriteJob(agent_id=agent_id, display_name=display_name, appearance=appearance)
        self._queue.put_nowait(job)
        self._active_jobs.add(agent_id)
        position = self._queue.qsize() - 1
        logger.info("SpriteQueue: enqueued agent %s (position %d)", agent_id[:8], position)
        return position

    async def _worker(self) -> None:
        """Process jobs from the queue with bounded concurrency."""
        while True:
            job = await self._queue.get()
            asyncio.create_task(self._process_job(job))

    async def _process_job(self, job: SpriteJob) -> None:
        """Process a single sprite generation job with semaphore throttling."""
        async with self._semaphore:
            try:
                logger.info(
                    "SpriteQueue: starting job for %s (attempt %d/%d)",
                    job.agent_id[:8],
                    job.retries + 1,
                    job.max_retries,
                )
                await _sprite_pipeline_task(job.agent_id, job.display_name, job.appearance)
                logger.info("SpriteQueue: completed job for %s", job.agent_id[:8])
            except Exception as exc:
                logger.error("SpriteQueue: job failed for %s: %s", job.agent_id[:8], exc)
                if job.retries < job.max_retries:
                    delay = 5 * (2**job.retries)
                    logger.info(
                        "SpriteQueue: retrying %s in %ds (attempt %d/%d)",
                        job.agent_id[:8],
                        delay,
                        job.retries + 1,
                        job.max_retries,
                    )
                    await asyncio.sleep(delay)
                    job.retries += 1
                    self._queue.put_nowait(job)
                    return
                logger.error(
                    "SpriteQueue: giving up on %s after %d retries",
                    job.agent_id[:8],
                    job.max_retries,
                )
            finally:
                self._active_jobs.discard(job.agent_id)


_sprite_queue: SpriteQueue | None = None


def get_sprite_queue() -> SpriteQueue:
    # max_concurrent=1: PixelLab Tier 2 caps at 10 concurrent background jobs total.
    # One in-flight pipeline already issues 4 jobs per animation phase (walk/idle/talking),
    # so allowing >1 pipeline concurrently hits 429s mid-run and silently abandons agents.
    global _sprite_queue
    if _sprite_queue is None:
        _sprite_queue = SpriteQueue(max_concurrent=1)
    return _sprite_queue
