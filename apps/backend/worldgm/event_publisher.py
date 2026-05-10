"""EventBridge publisher with tag taxonomy.

Publishes world events to the goosetown-{env}-world bus. Per-witness events
get their own DetailType (`personal.<witness_id>.observed`) so per-agent SQS
subscriptions can route by pattern.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any

import boto3

from core import dynamo

logger = logging.getLogger(__name__)


_BUS_NAME = os.environ.get("WORLD_BUS_NAME", "goosetown-dev-world")
_ENV = os.environ.get("ENVIRONMENT", "dev")


_WAKE_DEFAULTS = {
    "personal.": True,
    "conversation.": True,
    "owner.": True,
    "weather.": False,
    "ambient.": False,
    "arc.": False,
    "social.": False,
    "system.": False,
}


def _infer_wake_default(tag: str) -> bool:
    for prefix, wake in _WAKE_DEFAULTS.items():
        if tag.startswith(prefix):
            return wake
    return False


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _events_client():
    return boto3.client("events")


def emit_world_event(
    *,
    tag: str,
    detail: dict[str, Any],
    witnesses: list[str] | None = None,
    wake: bool | None = None,
    location: str | None = None,
    summary: str | None = None,
) -> str:
    """Publish a world event to the bus + persist to world_events table.

    Returns the event_id.

    Per-witness events get their own ``personal.<witness_id>.observed`` DetailType
    for per-agent SQS subscription routing.
    """
    event_id = str(uuid.uuid4())
    timestamp = _now_iso()
    if wake is None:
        wake = _infer_wake_default(tag)
    witnesses = witnesses or []

    eb = _events_client()

    base_detail = {
        "eventId": event_id,
        "tag": tag,
        "timestamp": timestamp,
        "location": location,
        "summary": summary,
        "witnesses": witnesses,
        "wake": wake,
        **detail,
    }

    entries = [
        {
            "EventBusName": _BUS_NAME,
            "Source": "goosetown.worldgm",
            "DetailType": tag,
            "Detail": json.dumps(base_detail),
        }
    ]
    for w in witnesses:
        entries.append(
            {
                "EventBusName": _BUS_NAME,
                "Source": "goosetown.worldgm",
                "DetailType": f"personal.{w}.observed",
                "Detail": json.dumps({**base_detail, "witness": w, "baseTag": tag}),
            }
        )

    # Batch put — EventBridge accepts up to 10 entries per call
    for i in range(0, len(entries), 10):
        eb.put_events(Entries=entries[i : i + 10])

    # Persist to world_events table
    _persist_to_table(event_id, tag, timestamp, location, summary, witnesses, wake, detail)

    logger.info(
        "Emitted event %s tag=%s witnesses=%d wake=%s",
        event_id[:8],
        tag,
        len(witnesses),
        wake,
    )
    return event_id


def _persist_to_table(event_id, tag, timestamp, location, summary, witnesses, wake, detail):
    table = dynamo.world_events_table()
    table.put_item(
        Item={
            "eventId": event_id,
            "envBucket": _ENV,  # PK for byTimestamp GSI
            "timestamp": timestamp,
            "tag": tag,
            "location": location,
            "summary": summary,
            "witnesses": witnesses,
            "wake": wake,
            "detail": detail,
        }
    )
