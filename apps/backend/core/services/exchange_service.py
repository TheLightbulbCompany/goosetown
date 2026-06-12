"""GooseTown Exchange — jobs, bids, escrow, settlement, trust (ADR-0007).

Domain design ported from open-experiments/agent-exchange (MIT); plumbing
follows conversation_service (timers + sweeper) and town_service (DDB).
Money is integer crumbs; every movement is an append-only ledger row.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from core import dynamo
from core.services import exchange_scoring
from core.services.town_service import TownService

logger = logging.getLogger(__name__)

SIGNUP_GRANT = int(os.environ.get("EXCHANGE_SIGNUP_GRANT", "100"))
BID_WINDOW_SEC = float(os.environ.get("EXCHANGE_BID_WINDOW_SEC", "600"))
DELIVERY_SEC = float(os.environ.get("EXCHANGE_DELIVERY_SEC", "3600"))
REVIEW_SEC = float(os.environ.get("EXCHANGE_REVIEW_SEC", "1800"))
EXCHANGE_LOCATION = "bank"

# Synthetic ledger account that accumulates Exchange fees (the crumb sink).
TOWN_ACCOUNT = "TOWN"


class InsufficientFunds(Exception):
    """Raised when an agent's wallet balance cannot cover the requested debit."""


class AgentNotFound(Exception):
    """Raised when the target agent row does not exist in DynamoDB."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ledger_append(agent_id: str, entry_type: str, amount: int, balance_after: int, ref_id: str) -> None:
    try:
        dynamo.ledger_table().put_item(
            Item={
                "id": str(uuid.uuid4()),
                "agentId": agent_id,
                "entryType": entry_type,
                "amount": amount,
                "balanceAfter": balance_after,
                "refId": ref_id,
                "createdAt": _now_iso(),
            }
        )
    except Exception:
        logger.error(
            "Ledger append failed (balance already moved!): agent=%s type=%s amount=%d ref=%s",
            agent_id,
            entry_type,
            amount,
            ref_id,
        )
        raise


def ensure_signup_grant(agent_id: str) -> None:
    """Grant the one-time signup crumbs. Idempotent on signupGrantAt."""
    table = dynamo.agents_table()
    try:
        resp = table.update_item(
            Key={"id": agent_id},
            UpdateExpression=("SET walletBalance = if_not_exists(walletBalance, :z) + :grant, signupGrantAt = :now"),
            ConditionExpression="attribute_exists(id) AND attribute_not_exists(signupGrantAt)",
            ExpressionAttributeValues={":z": 0, ":grant": SIGNUP_GRANT, ":now": _now_iso()},
            ReturnValues="ALL_NEW",
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return  # already granted (or agent missing — caller surfaces that elsewhere)
    _ledger_append(agent_id, "GRANT", SIGNUP_GRANT, int(resp["Attributes"]["walletBalance"]), "signup")


def debit_wallet(agent_id: str, amount: int, entry_type: str, ref_id: str) -> int:
    """Atomic conditional debit. Raises InsufficientFunds. Returns new balance."""
    ensure_signup_grant(agent_id)
    table = dynamo.agents_table()
    try:
        resp = table.update_item(
            Key={"id": agent_id},
            UpdateExpression="ADD walletBalance :neg",
            ConditionExpression="attribute_exists(id) AND walletBalance >= :amt",
            ExpressionAttributeValues={":neg": -amount, ":amt": amount},
            ReturnValues="ALL_NEW",
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException as exc:
        check = table.get_item(Key={"id": agent_id})
        if "Item" not in check:
            raise AgentNotFound(f"agent {agent_id[:8]} not found") from exc
        raise InsufficientFunds(f"agent {agent_id[:8]} cannot cover {amount} crumbs") from exc
    new_balance = int(resp["Attributes"]["walletBalance"])
    _ledger_append(agent_id, entry_type, -amount, new_balance, ref_id)
    return new_balance


def credit_wallet(agent_id: str, amount: int, entry_type: str, ref_id: str) -> int:
    """Atomic credit. Returns new balance."""
    ensure_signup_grant(agent_id)
    table = dynamo.agents_table()
    try:
        resp = table.update_item(
            Key={"id": agent_id},
            UpdateExpression="ADD walletBalance :amt",
            ConditionExpression="attribute_exists(id)",
            ExpressionAttributeValues={":amt": amount},
            ReturnValues="ALL_NEW",
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException as exc:
        raise AgentNotFound(f"agent {agent_id[:8]} not found") from exc
    new_balance = int(resp["Attributes"]["walletBalance"])
    _ledger_append(agent_id, entry_type, amount, new_balance, ref_id)
    return new_balance


def get_wallet(agent_id: str) -> dict[str, Any]:
    """Wallet + reputation snapshot. Self-heals the signup grant."""
    ensure_signup_grant(agent_id)
    agent = TownService().get_town_agent_by_id(agent_id) or {}
    trust = float(agent.get("trustScore", exchange_scoring.INITIAL_TRUST))
    completed = int(agent.get("jobsCompleted", 0))
    return {
        "balance": int(agent.get("walletBalance", 0)),
        "trustScore": trust,
        "trustTier": exchange_scoring.trust_tier(trust, completed),
        "jobsCompleted": completed,
        "jobsFailed": int(agent.get("jobsFailed", 0)),
        "jobsPosted": int(agent.get("jobsPosted", 0)),
    }
