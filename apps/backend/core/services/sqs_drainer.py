"""Per-agent SQS queue drainer.

Each registered agent gets a dedicated SQS queue named ``goosetown-{env}-agent-{agent_id}``.
The WorldGM service publishes events to the world bus; an EventBridge rule per agent
routes ``personal.<agent_id>.*`` events to that agent's queue. When the agent's daemon
connects (or as a periodic poll), the backend drains the queue and writes events into
TOWN_EVENTS.md / sets think=true.

Lifecycle:
  - Agent register: create_agent_queue(agent_id) — also creates the EventBridge rule
  - Agent hard-leave (deactivate or delete): delete_agent_queue(agent_id) — removes both
  - Daemon connects: drain_for_agent(agent_id) called from agent_connect_service
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_ENV = os.environ.get("ENVIRONMENT", "dev")
_REGION = os.environ.get("AWS_REGION", "us-east-1")
_BUS_NAME = os.environ.get("WORLD_BUS_NAME", f"goosetown-{_ENV}-world")


def _sqs():
    return boto3.client("sqs", region_name=_REGION)


def _events():
    return boto3.client("events", region_name=_REGION)


def _queue_name(agent_id: str) -> str:
    # SQS queue names: ≤80 chars, alphanumeric + - + _ only.
    # GooseTown agent_ids are UUIDs (36 chars). Total: 16 + 4 + 36 + 6 = 62 chars max.
    safe_id = agent_id.replace(":", "-")
    return f"goosetown-{_ENV}-agent-{safe_id}"


def _rule_name(agent_id: str) -> str:
    safe_id = agent_id.replace(":", "-")
    return f"goosetown-{_ENV}-agent-{safe_id}-route"


def create_agent_queue(agent_id: str) -> str:
    """Create SQS queue + EventBridge rule for an agent. Idempotent.

    Returns the queue URL.
    """
    sqs = _sqs()
    eb = _events()

    qname = _queue_name(agent_id)
    rname = _rule_name(agent_id)

    # Create queue (idempotent — CreateQueue with same name returns existing URL)
    try:
        resp = sqs.create_queue(
            QueueName=qname,
            Attributes={
                "MessageRetentionPeriod": str(14 * 24 * 3600),  # 14 days
                "VisibilityTimeout": "60",
            },
        )
        queue_url = resp["QueueUrl"]
    except ClientError:
        logger.exception("create_queue failed for %s", qname)
        raise

    # Get queue ARN for the rule target
    attrs = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])
    queue_arn = attrs["Attributes"]["QueueArn"]

    # Allow EventBridge to send to the queue
    policy = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "events.amazonaws.com"},
                    "Action": "sqs:SendMessage",
                    "Resource": queue_arn,
                }
            ],
        }
    )
    sqs.set_queue_attributes(QueueUrl=queue_url, Attributes={"Policy": policy})

    # Create EventBridge rule routing personal.<agent_id>.* to this queue
    eb.put_rule(
        Name=rname,
        EventBusName=_BUS_NAME,
        EventPattern=json.dumps(
            {
                "source": ["goosetown.worldgm"],
                "detail-type": [{"prefix": f"personal.{agent_id}."}],
            }
        ),
        State="ENABLED",
    )
    eb.put_targets(
        Rule=rname,
        EventBusName=_BUS_NAME,
        Targets=[{"Id": "Queue", "Arn": queue_arn}],
    )

    logger.info("Created agent queue %s + rule %s for %s", qname, rname, agent_id[:8])
    return queue_url


def delete_agent_queue(agent_id: str) -> None:
    """Delete SQS queue + EventBridge rule for an agent. Idempotent."""
    sqs = _sqs()
    eb = _events()

    qname = _queue_name(agent_id)
    rname = _rule_name(agent_id)

    # Remove targets and rule first (rule must have no targets to delete)
    try:
        eb.remove_targets(Rule=rname, EventBusName=_BUS_NAME, Ids=["Queue"])
    except ClientError as e:
        if "ResourceNotFoundException" not in str(e):
            logger.warning("remove_targets failed for %s: %s", rname, e)

    try:
        eb.delete_rule(Name=rname, EventBusName=_BUS_NAME)
    except ClientError as e:
        if "ResourceNotFoundException" not in str(e):
            logger.warning("delete_rule failed for %s: %s", rname, e)

    # Delete the queue
    try:
        url = sqs.get_queue_url(QueueName=qname)["QueueUrl"]
        sqs.delete_queue(QueueUrl=url)
    except ClientError as e:
        if "QueueDoesNotExist" not in str(e) and "AWS.SimpleQueueService.NonExistentQueue" not in str(e):
            logger.warning("delete_queue failed for %s: %s", qname, e)

    logger.info("Deleted agent queue %s + rule %s for %s", qname, rname, agent_id[:8])


async def drain_for_agent(agent_id: str, max_messages: int = 100) -> int:
    """Drain the agent's SQS queue. Returns number of events drained.

    Each event is forwarded to the daemon via the existing daemon_bridge mechanism.
    LOG events append to TOWN_EVENTS.md; WAKE events also set think=true.

    Idempotent: messages have receipt handles; we delete after processing.
    """
    sqs = _sqs()
    qname = _queue_name(agent_id)
    try:
        url = sqs.get_queue_url(QueueName=qname)["QueueUrl"]
    except ClientError as e:
        if "QueueDoesNotExist" in str(e) or "NonExistentQueue" in str(e):
            logger.debug("No queue for %s — skipping drain", agent_id[:8])
            return 0
        raise

    drained = 0
    while drained < max_messages:
        resp = sqs.receive_message(
            QueueUrl=url,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=0,  # short-poll
        )
        messages = resp.get("Messages", [])
        if not messages:
            break

        for msg in messages:
            try:
                body = json.loads(msg["Body"])
                # EventBridge wraps the event — actual detail is at body["detail"]
                event = body.get("detail", body)
                await _apply_event_to_agent(agent_id, event)
            except Exception:
                logger.exception(
                    "Error processing event for %s; deleting message anyway",
                    agent_id[:8],
                )
            sqs.delete_message(QueueUrl=url, ReceiptHandle=msg["ReceiptHandle"])
            drained += 1

    if drained:
        logger.info("Drained %d events for %s", drained, agent_id[:8])
    return drained


async def _apply_event_to_agent(agent_id: str, event: dict) -> None:
    """Push the event into the daemon's stream + set think=true if wake.

    Uses ``_send_to_daemon`` from daemon_bridge — same path as game-server events.
    The daemon receives a ``world_event`` envelope; the ``wake`` flag tells it
    whether to set ``think=true`` in TOWN_STATUS.md.

    If the agent has no live daemon connection the event is already persisted in
    the world_events DDB table (written by event_publisher in Phase C) and will
    be visible on the next drain after the daemon reconnects — so this is a
    best-effort live push only.
    """
    from core.services.daemon_bridge import _send_to_daemon
    from core.services.town_agent_ws import get_town_agent_ws_manager

    ws_manager = get_town_agent_ws_manager()
    agent_conn = ws_manager.get_by_agent_id(agent_id)
    if not agent_conn:
        logger.debug(
            "No active daemon connection for %s — event already in DDB world_events",
            agent_id[:8],
        )
        return

    # Wrap as a daemon event so the daemon's event handler can pattern-match on it
    daemon_event: dict[str, Any] = {
        "event": "world_event",
        "tag": event.get("tag", ""),
        "description": event.get("description", ""),
        "wake": event.get("wake", False),
        "payload": event,
    }
    _send_to_daemon(agent_id, daemon_event)
