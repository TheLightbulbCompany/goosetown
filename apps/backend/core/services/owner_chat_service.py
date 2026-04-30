"""Owner ↔ Agent DM service — private messages between an agent's owner
and the agent. Distinct from the public ``conversations`` table.

Storage: ``owner_chats`` DynamoDB table.
PK: ``id`` (UUID per message).
GSI: ``agentId-createdAt-index`` for paginated history per agent.

Authorization: every read/write validates that the caller's owner_id
matches the agent's ``userId`` field. The endpoint layer authenticates
the caller via Clerk JWT before reaching this service.

Rate limit: 1 ``owner_to_agent`` message per 2s per (owner, agent) pair.
``agent_to_owner`` messages are not rate-limited (LLM cadence never
produces them faster than that, and we don't want to 429 the agent's
own daemon).
"""

from __future__ import annotations

import time
import uuid

from boto3.dynamodb.conditions import Key

from core import dynamo
from core.services.town_service import TownService

_RATE_LIMIT_WINDOW_MS = 2_000
_VALID_DIRECTIONS = frozenset({"owner_to_agent", "agent_to_owner"})


class OwnershipError(Exception):
    """Caller is not the owner of the agent."""


class RateLimitError(Exception):
    """Too many messages from this owner to this agent in the rate-limit window."""


def _get_agent(agent_id: str) -> dict | None:
    """Resolve an agent record by id. Indirected for ease of mocking in tests."""
    return TownService().get_town_agent_by_id(agent_id)


class OwnerChatService:
    """CRUD + auth + rate limiting for owner-agent DM messages."""

    def persist_message(
        self,
        *,
        agent_id: str,
        owner_id: str,
        direction: str,
        text: str,
    ) -> dict:
        """Persist a single message. Validates ownership and (for owner→agent)
        rate-limits to 1 message per 2s per pair.

        Raises ValueError if direction is not a known value.
        Raises OwnershipError if owner_id != agent.userId.
        Raises RateLimitError if the rate limit is exceeded.

        Returns the persisted record (dict).
        """
        if direction not in _VALID_DIRECTIONS:
            raise ValueError(f"invalid direction: {direction!r}")

        agent = _get_agent(agent_id)
        if not agent or agent.get("userId") != owner_id:
            raise OwnershipError(f"agent {agent_id} not owned by {owner_id}")

        now_ms = int(time.time() * 1000)

        if direction == "owner_to_agent":
            self._enforce_rate_limit(agent_id=agent_id, owner_id=owner_id, now_ms=now_ms)

        record = {
            "id": str(uuid.uuid4()),
            "agentId": agent_id,
            "ownerId": owner_id,
            "direction": direction,
            "text": text,
            "createdAt": now_ms,
        }
        dynamo.owner_chats_table().put_item(Item=record)
        return record

    def fetch_history(
        self,
        *,
        agent_id: str,
        owner_id: str,
        limit: int = 50,
        before_ms: int | None = None,
    ) -> list[dict]:
        """Fetch up to ``limit`` messages for an agent, oldest first.
        ``before_ms`` (optional) pages backward — return messages with
        ``createdAt < before_ms``.

        Raises OwnershipError if owner_id != agent.userId.
        """
        agent = _get_agent(agent_id)
        if not agent or agent.get("userId") != owner_id:
            raise OwnershipError(f"agent {agent_id} not owned by {owner_id}")

        cond = Key("agentId").eq(agent_id)
        if before_ms is not None:
            cond = cond & Key("createdAt").lt(before_ms)

        resp = dynamo.owner_chats_table().query(
            IndexName="agentId-createdAt-index",
            KeyConditionExpression=cond,
            Limit=limit,
            ScanIndexForward=True,  # oldest first
        )
        return resp.get("Items", [])

    def _enforce_rate_limit(
        self,
        *,
        agent_id: str,
        owner_id: str,
        now_ms: int,
    ) -> None:
        """Reject if the most recent ``owner_to_agent`` message for this
        (owner, agent) pair is within ``_RATE_LIMIT_WINDOW_MS`` of now.
        """
        window_start = now_ms - _RATE_LIMIT_WINDOW_MS
        resp = dynamo.owner_chats_table().query(
            IndexName="agentId-createdAt-index",
            KeyConditionExpression=Key("agentId").eq(agent_id) & Key("createdAt").gte(window_start),
            FilterExpression=("#dir = :dir AND ownerId = :owner"),
            ExpressionAttributeNames={"#dir": "direction"},
            ExpressionAttributeValues={
                ":dir": "owner_to_agent",
                ":owner": owner_id,
            },
        )
        if resp.get("Items"):
            raise RateLimitError(f"rate limit: 1 message per {_RATE_LIMIT_WINDOW_MS}ms per (owner, agent)")
