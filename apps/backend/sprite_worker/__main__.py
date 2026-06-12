"""Sprite worker entrypoint — single-replica drainer of goosetown-{env}-sprite-jobs.

Mirrors worldgm/__main__.py packaging: same backend Docker image, command
override ``uv run python -m sprite_worker``. This is the ONLY process that
calls PixelLab generation, which is what makes the animation semaphore in
pixellab_service a true GLOBAL cap (spec 2026-06-11).

Recovery model: SQS redelivery is primary (visibility lapse -> retry -> DLQ
after 3 receives). The boot sweep is a backstop for rows whose SQS send
failed and for pre-migration stuck rows; a permanently-failing agent costs
one redelivery cycle per worker restart, bounded by the DLQ (alarmed).
"""

from __future__ import annotations

import asyncio
import logging
import signal

import boto3

from core.config import settings
from sprite_worker.backend_sink import HttpSpriteEventSink
from sprite_worker.processor import Outcome, process_message

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

VISIBILITY_SECONDS = 300
HEARTBEAT_SECONDS = 120
CREDIT_PAUSE_SECONDS = 900

_should_exit = False


def _signal_handler(signum: int, frame: object) -> None:  # noqa: ARG001
    global _should_exit  # noqa: PLW0603
    logger.info("Received signal %d, shutting down...", signum)
    _should_exit = True


signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)


def _sqs():
    return boto3.client("sqs", region_name=settings.AWS_REGION)


def _emit_credit_metric() -> None:
    try:
        boto3.client("cloudwatch", region_name=settings.AWS_REGION).put_metric_data(
            Namespace="GooseTown/SpriteWorker",
            MetricData=[{"MetricName": "CreditExhausted", "Value": 1, "Unit": "Count"}],
        )
    except Exception:
        logger.exception("Failed to emit CreditExhausted metric")


def _boot_sweep() -> int:
    """Re-enqueue agents stuck in an in-progress spriteStatus (backstop —
    primary recovery is SQS redelivery; this covers rows whose SQS send
    failed and pre-migration stuck rows). Idempotent: processing is
    resume-aware and skips completed work."""
    from core.services import sprite_jobs
    from core.services.town_service import TownService

    swept = 0
    try:
        agents = TownService().get_all_active_agents()
    except Exception:
        logger.exception("Boot sweep: agent scan failed — continuing without sweep")
        return 0
    for agent in agents:
        if agent.get("spriteStatus") in sprite_jobs.IN_PROGRESS_STATUSES:
            if sprite_jobs.enqueue_signup(
                agent["id"], agent.get("displayName", ""), agent.get("appearance", ""), force=True
            ):
                swept += 1
    logger.info("Boot sweep: re-enqueued %d stuck agent(s)", swept)
    return swept


async def _heartbeat(sqs, queue_url: str, receipt: str, stop: asyncio.Event) -> None:
    """Extend message visibility while its job is in flight, so a long
    pipeline never re-surfaces mid-run but a dead worker's message returns
    within VISIBILITY_SECONDS."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_SECONDS)
        except asyncio.TimeoutError:
            try:
                await asyncio.to_thread(
                    sqs.change_message_visibility,
                    QueueUrl=queue_url,
                    ReceiptHandle=receipt,
                    VisibilityTimeout=VISIBILITY_SECONDS,
                )
            except Exception:
                logger.warning("Heartbeat visibility extension failed", exc_info=True)


def _apply_outcome(sqs, queue_url: str, receipt: str, outcome: Outcome) -> None:
    if outcome is Outcome.DELETE:
        sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
    elif outcome is Outcome.CREDIT_PAUSE:
        _emit_credit_metric()
        sqs.change_message_visibility(QueueUrl=queue_url, ReceiptHandle=receipt, VisibilityTimeout=CREDIT_PAUSE_SECONDS)
    # RELEASE: do nothing — visibility lapses naturally (<=5 min), 3 receives -> DLQ.


async def _handle(sqs, queue_url: str, msg: dict, sink) -> None:
    receipt = msg["ReceiptHandle"]
    stop = asyncio.Event()
    hb = asyncio.create_task(_heartbeat(sqs, queue_url, receipt, stop))
    try:
        outcome = await process_message(msg["Body"], sink=sink)
    finally:
        stop.set()
        await hb
    try:
        await asyncio.to_thread(_apply_outcome, sqs, queue_url, receipt, outcome)
    except Exception:
        logger.exception("Failed to apply outcome %s", outcome)


async def main() -> None:
    logger.info(
        "Sprite worker starting (queue=%s, pipelines=%d, anims=%d)",
        settings.SPRITE_JOBS_QUEUE_NAME,
        settings.SPRITE_WORKER_MAX_CONCURRENT_PIPELINES,
        settings.PIXELLAB_MAX_CONCURRENT_ANIMATIONS,
    )
    for var in ("PIXELLAB_API_KEY", "SPRITE_S3_BUCKET", "SPRITE_CDN_URL", "BACKEND_INTERNAL_URL"):
        if not getattr(settings, var):
            raise RuntimeError(f"sprite_worker: missing required env {var}")

    sqs = _sqs()
    queue_url = sqs.get_queue_url(QueueName=settings.SPRITE_JOBS_QUEUE_NAME)["QueueUrl"]
    sink = HttpSpriteEventSink()
    tasks: set[asyncio.Task] = set()

    await asyncio.to_thread(_boot_sweep)

    while not _should_exit:
        free = settings.SPRITE_WORKER_MAX_CONCURRENT_PIPELINES - len(tasks)
        if free <= 0:
            await asyncio.sleep(1)
            continue
        try:
            resp = await asyncio.to_thread(
                sqs.receive_message,
                QueueUrl=queue_url,
                MaxNumberOfMessages=min(10, free),
                WaitTimeSeconds=20,
            )
        except Exception:
            logger.exception("receive_message failed — backing off")
            await asyncio.sleep(5)
            continue
        for msg in resp.get("Messages", []):
            task = asyncio.create_task(_handle(sqs, queue_url, msg, sink))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

    if tasks:
        logger.info("Draining %d in-flight job(s) before exit (25s budget)...", len(tasks))
        await asyncio.wait(tasks, timeout=25)

    from core.services.pixellab_service import close_pixellab_client

    await close_pixellab_client()
    await sink.aclose()
    logger.info("Sprite worker exiting cleanly")


if __name__ == "__main__":
    asyncio.run(main())
