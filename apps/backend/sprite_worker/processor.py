"""Resume-aware processing of sprite-job SQS messages."""

from __future__ import annotations

import enum
import json
import logging

from core.services.pixellab_service import PixelLabCreditError
from core.services.sprite_pipeline import SpriteEventSink, run_animation_job, run_signup_pipeline

logger = logging.getLogger(__name__)


class Outcome(enum.Enum):
    DELETE = "delete"  # done (or permanently un-processable) — remove from queue
    RELEASE = "release"  # transient failure / duplicate-in-flight — let visibility lapse
    CREDIT_PAUSE = "credit_pause"  # 402 — long visibility + metric; queue effectively pauses


# Per-process serialization: one job per agent at a time. SQS standard queues
# can deliver duplicates; a duplicate for an in-flight agent is RELEASEd (it
# re-polls later and finds the work complete -> DELETE).
_active: set[str] = set()


async def process_message(raw_body: str, *, sink: SpriteEventSink) -> Outcome:
    try:
        body = json.loads(raw_body)
    except (json.JSONDecodeError, TypeError):
        logger.error("sprite-worker: malformed message dropped: %.200s", raw_body)
        return Outcome.DELETE
    if not isinstance(body, dict):
        logger.error("sprite-worker: non-object message dropped: %.200s", raw_body)
        return Outcome.DELETE

    kind = body.get("kind")
    agent_id = body.get("agent_id")
    if not agent_id or kind not in ("signup", "animation"):
        logger.error("sprite-worker: unknown message dropped: %.200s", raw_body)
        return Outcome.DELETE

    if agent_id in _active:
        logger.info("sprite-worker: %s already in flight — releasing duplicate", agent_id[:8])
        return Outcome.RELEASE

    _active.add(agent_id)
    try:
        if kind == "signup":
            await run_signup_pipeline(
                agent_id,
                sink=sink,
                fallback_display_name=body.get("display_name", ""),
                fallback_appearance=body.get("appearance", ""),
            )
        else:
            await run_animation_job(agent_id, body.get("anim_name", ""), sink=sink)
        return Outcome.DELETE
    except PixelLabCreditError:
        logger.error("sprite-worker: PixelLab credits exhausted (agent %s)", agent_id[:8])
        return Outcome.CREDIT_PAUSE
    except Exception:
        logger.exception("sprite-worker: job failed for %s — releasing for retry", agent_id[:8])
        return Outcome.RELEASE
    finally:
        _active.discard(agent_id)
