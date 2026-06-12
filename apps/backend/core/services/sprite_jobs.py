"""SQS producer for durable sprite-generation jobs.

Replaces the in-memory ``SpriteQueue`` (deleted — see spec
docs/superpowers/specs/2026-06-11-durable-sprite-pipeline-design.md). The
queue ``goosetown-{env}-sprite-jobs`` is drained by the dedicated
single-replica sprite worker (``apps/backend/sprite_worker/``); backend
replicas only produce.

Idempotency: ``enqueue_signup`` first claims the job via a conditional
DynamoDB write (``TownService.claim_sprite_job`` sets spriteStatus="queued"),
so concurrent producers (register endpoint + connect auto-enqueue racing) send
at most one message. Write-then-send ordering means a failed send leaves a
``queued`` row that the worker's boot sweep re-enqueues — self-healing, never
job-losing.
"""

from __future__ import annotations

import json
import logging
import time

import boto3

from core.config import settings

logger = logging.getLogger(__name__)

# spriteStatus values that mean "a sprite job is queued or in flight".
# Mirrored by hand inside TownService.claim_sprite_job's ConditionExpression
# (DynamoDB expressions can't import constants) — keep the two in sync.
IN_PROGRESS_STATUSES = (
    "queued",
    "creating",
    "building_static",
    "static_ready",
    "animating",
    "downloading",
)

_sqs_client = None
_queue_url_cache: str | None = None


def _sqs():
    global _sqs_client
    if _sqs_client is None:
        _sqs_client = boto3.client("sqs", region_name=settings.AWS_REGION)
    return _sqs_client


def _queue_url() -> str:
    global _queue_url_cache
    if _queue_url_cache is None:
        _queue_url_cache = _sqs().get_queue_url(QueueName=settings.SPRITE_JOBS_QUEUE_NAME)["QueueUrl"]
    return _queue_url_cache


def _send(body: dict) -> bool:
    """Send a single message to the sprite-jobs queue.

    Returns True on success, False on any error. Never raises — a failed send
    for a signup job is self-healing because the DDB "queued" claim (already
    written) makes the worker boot sweep re-enqueue this agent.
    """
    try:
        _sqs().send_message(QueueUrl=_queue_url(), MessageBody=json.dumps(body))
        return True
    except Exception:
        # Non-fatal by design: for signup jobs the DDB "queued" claim (already
        # written) makes the worker boot sweep re-enqueue this agent. Log loudly.
        logger.exception("sprite_jobs: SQS send failed for %s", body.get("agent_id"))
        return False


def enqueue_signup(agent_id: str, display_name: str, appearance: str, *, force: bool = False) -> bool:
    """Enqueue a full signup pipeline (create -> static -> walk) for an agent.

    Returns True if a message was sent. ``force=True`` skips the conditional
    claim — used by the worker boot sweep (re-enqueueing rows already in an
    in-progress status) and by admin regenerate (which clears state first).
    """
    if not force:
        from core.services.town_service import TownService

        if not TownService().claim_sprite_job(agent_id):
            logger.info("sprite_jobs: %s already queued/in-flight — skipping", agent_id[:8])
            return False
    return _send(
        {
            "schema": 1,
            "kind": "signup",
            "agent_id": agent_id,
            "display_name": display_name,
            "appearance": appearance,
            "enqueued_at": int(time.time()),
        }
    )


def enqueue_animation(agent_id: str, anim_name: str) -> bool:
    """Enqueue lazy generation of ONE animation (all 4 directions).

    Dedup is best-effort at call sites (skip if already in spriteAnimations)
    plus authoritative in the worker (skips if the animation exists by the
    time the message is processed). Duplicate messages are cheap and safe.
    """
    return _send(
        {
            "schema": 1,
            "kind": "animation",
            "agent_id": agent_id,
            "anim_name": anim_name,
            "enqueued_at": int(time.time()),
        }
    )
