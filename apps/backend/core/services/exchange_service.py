"""GooseTown Exchange — jobs, bids, escrow, settlement, trust (ADR-0007).

Domain design ported from open-experiments/agent-exchange (MIT); plumbing
follows conversation_service (timers + sweeper) and town_service (DDB).
Money is integer crumbs; every movement is an append-only ledger row.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable

from boto3.dynamodb.conditions import Key

from core import dynamo
from core.services import exchange_scoring, town_mood_engine
from core.services.daemon_mailbox import get_daemon_mailbox
from core.services.town_agent_ws import get_town_agent_ws_manager
from core.services.town_service import TownService

logger = logging.getLogger(__name__)

SIGNUP_GRANT = int(os.environ.get("EXCHANGE_SIGNUP_GRANT", "100"))
BID_WINDOW_SEC = float(os.environ.get("EXCHANGE_BID_WINDOW_SEC", "600"))
DELIVERY_SEC = float(os.environ.get("EXCHANGE_DELIVERY_SEC", "3600"))
REVIEW_SEC = float(os.environ.get("EXCHANGE_REVIEW_SEC", "1800"))
EXCHANGE_LOCATION = "bank"

TITLE_MAX = 120
DESCRIPTION_MAX = 4096
DELIVERABLE_MAX = 16384
PITCH_MAX = 500

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


def _bump_counter(agent_id: str, attr: str, delta: int = 1) -> None:
    """Atomic counter bump (multi-task safe; same pattern as the wallet)."""
    try:
        dynamo.agents_table().update_item(
            Key={"id": agent_id},
            UpdateExpression="ADD #a :d",
            ConditionExpression="attribute_exists(id)",
            ExpressionAttributeNames={"#a": attr},
            ExpressionAttributeValues={":d": delta},
        )
    except Exception:
        logger.warning("Counter bump failed: %s.%s", agent_id[:8], attr)


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


# ---------------------------------------------------------------------------
# Timers (conversation_service `_inactivity_tasks` pattern)
# ---------------------------------------------------------------------------

# job_id → asyncio.Task that will fire the job's next deadline transition.
# In-memory only — a backend restart drops pending timers; the 60s sweeper
# re-fires any overdue transition from the persisted deadline attrs.
_timers: dict[str, asyncio.Task] = {}


async def _timer_task(job_id: str, delay: float, fn: Callable[[str], Awaitable[None]]) -> None:
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    finally:
        # Only pop the registry entry if THIS task still owns it — a replacement
        # timer (from _schedule) may have already claimed the slot.
        if _timers.get(job_id) is asyncio.current_task():
            _timers.pop(job_id, None)
    try:
        await fn(job_id)
    except Exception:
        logger.exception("Exchange timer transition failed for job %s", job_id)


def _schedule(job_id: str, delay: float, fn: Callable[[str], Awaitable[None]]) -> None:
    """Schedule (or replace) the deadline transition timer for a job."""
    existing = _timers.get(job_id)
    if existing and not existing.done():
        existing.cancel()
    _timers[job_id] = asyncio.create_task(_timer_task(job_id, delay, fn))


def _cancel_timer(job_id: str) -> None:
    """Cancel a pending deadline timer. Idempotent."""
    task = _timers.pop(job_id, None)
    if task and not task.done():
        task.cancel()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _get_job(job_id: str) -> dict | None:
    resp = dynamo.jobs_table().get_item(Key={"id": job_id})
    return resp.get("Item")


def _flip_status(job_id: str, from_status: str, to_status: str, set_extra: dict[str, Any] | None = None) -> bool:
    """Conditionally flip a job's status. Returns False if the job wasn't in
    ``from_status`` (another path — timer, sweeper, explicit verb — won)."""
    table = dynamo.jobs_table()
    names = {"#st": "status"}
    values: dict[str, Any] = {":from": from_status, ":to": to_status, ":now": _now_iso()}
    sets = ["#st = :to", "updatedAt = :now"]
    for i, (attr, val) in enumerate((set_extra or {}).items()):
        names[f"#x{i}"] = attr
        values[f":x{i}"] = val
        sets.append(f"#x{i} = :x{i}")
    try:
        table.update_item(
            Key={"id": job_id},
            UpdateExpression="SET " + ", ".join(sets),
            ConditionExpression="#st = :from",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False
    return True


def _at_exchange(agent_conn: Any) -> bool:
    """Physically at the bank, in the town context (spec: post/bid require presence)."""
    agent = TownService().get_town_agent_by_id(agent_conn.agent_id) or {}
    return agent.get("currentLocation") == EXCHANGE_LOCATION and agent_conn.location_context == "town"


def _notify_outcome(agent_conn: Any, verb: str, result: dict) -> dict:
    """Push the verb outcome to the caller's daemon and return it unchanged."""
    event = "exchange_rejected" if result.get("status") == "rejected" else "exchange_ok"
    get_daemon_mailbox().notify(agent_conn.agent_id, {"event": event, "verb": verb, **result})
    return result


def _apply_mood(agent_id: str, event: str) -> None:
    svc = TownService()
    agent = svc.get_town_agent_by_id(agent_id)
    if not agent:
        return
    energy, mood = town_mood_engine.apply_event(
        event,
        int(agent.get("energy", 100) or 0),
        town_mood_engine.parse_mood(agent.get("mood")),
        agent.get("traits", ""),
        now=time.time(),
        mood_updated_at=town_mood_engine.parse_mood_updated_at(agent.get("moodUpdatedAt")),
    )
    svc.update_agent_mood_energy(agent_id, mood, energy)


def _update_trust(agent_id: str, outcome: str, completed_delta: int, failed_delta: int) -> None:
    # trustScore is an EMA, so it's inherently read-modify-write. Guard the
    # write with an optimistic ConditionExpression on the value we read and
    # retry on conflict, so two jobs settling concurrently for one agent
    # can't drop an outcome (codex P2 on PR #386). Counters use atomic ADD.
    table = dynamo.agents_table()
    for _ in range(3):
        agent = TownService().get_town_agent_by_id(agent_id) or {}
        old_raw = agent.get("trustScore")
        old = float(old_raw) if old_raw is not None else exchange_scoring.INITIAL_TRUST
        new_trust = exchange_scoring.updated_trust(old, outcome)
        condition = "attribute_exists(id) AND " + (
            "attribute_not_exists(trustScore)" if old_raw is None else "trustScore = :old"
        )
        values: dict[str, Any] = {":new": Decimal(str(new_trust))}
        if old_raw is not None:
            values[":old"] = Decimal(str(old_raw))
        try:
            table.update_item(
                Key={"id": agent_id},
                UpdateExpression="SET trustScore = :new",
                ConditionExpression=condition,
                ExpressionAttributeValues=values,
            )
            break
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            continue  # trustScore moved under us (or agent missing) — re-read and retry
    else:
        logger.warning("Trust update dropped after retries: agent=%s outcome=%s", agent_id[:8], outcome)
    if completed_delta:
        _bump_counter(agent_id, "jobsCompleted", completed_delta)
    if failed_delta:
        _bump_counter(agent_id, "jobsFailed", failed_delta)


# ---------------------------------------------------------------------------
# Public verbs (called by the dispatch ladder in routers/town.py)
# ---------------------------------------------------------------------------


async def post_job(agent_conn: Any, title: str, description: str, max_price: int, strategy: str) -> dict:
    """Post a job at the Exchange: escrow ``max_price``, open the bid window."""
    mailbox = get_daemon_mailbox()
    agent = TownService().get_town_agent_by_id(agent_conn.agent_id) or {}
    if agent.get("currentLocation") != EXCHANGE_LOCATION or agent_conn.location_context != "town":
        return _notify_outcome(
            agent_conn,
            "post_job",
            {
                "status": "rejected",
                "reason": "not_at_exchange",
                "message": "You must be at the bank (the Exchange) to post a job. `act move bank` first.",
            },
        )
    if max_price < 1:
        return _notify_outcome(
            agent_conn,
            "post_job",
            {"status": "rejected", "reason": "bad_price", "message": "max_price must be at least 1 crumb."},
        )
    if strategy not in exchange_scoring.STRATEGY_WEIGHTS:
        strategy = "balanced"
    job_id = str(uuid.uuid4())
    try:
        debit_wallet(agent_conn.agent_id, max_price, "ESCROW_DEBIT", job_id)
    except InsufficientFunds:
        balance = get_wallet(agent_conn.agent_id)["balance"]
        return _notify_outcome(
            agent_conn,
            "post_job",
            {
                "status": "rejected",
                "reason": "insufficient_funds",
                "message": f"Posting needs {max_price} crumbs in escrow; you have {balance}.",
            },
        )
    now = time.time()
    job = {
        "id": job_id,
        "consumerId": agent_conn.agent_id,
        "consumerName": agent_conn.agent_name,
        "title": title[:TITLE_MAX],
        "description": description[:DESCRIPTION_MAX],
        "maxPrice": max_price,
        "strategy": strategy,
        "status": "OPEN",
        "bids": {},
        "createdAt": _now_iso(),
        "updatedAt": _now_iso(),
        "bidWindowEndsAt": Decimal(str(now + BID_WINDOW_SEC)),
    }
    try:
        dynamo.jobs_table().put_item(Item=job)
    except Exception:
        credit_wallet(agent_conn.agent_id, max_price, "ESCROW_REFUND", job_id)
        logger.exception("post_job: job write failed after escrow debit — refunded %s", job_id)
        return _notify_outcome(
            agent_conn,
            "post_job",
            {
                "status": "rejected",
                "reason": "internal_error",
                "message": "The Exchange clerk fumbled the paperwork — your crumbs were refunded. Try again.",
            },
        )
    _bump_counter(agent_conn.agent_id, "jobsPosted")
    _schedule(job_id, BID_WINDOW_SEC, _close_bid_window)

    broadcast = {
        "event": "job_posted",
        "job_id": job_id,
        "title": job["title"],
        "description": job["description"][:300],
        "max_price": max_price,
        "strategy": strategy,
        "consumer_name": agent_conn.agent_name,
        "bid_window_sec": int(BID_WINDOW_SEC),
    }
    for conn in get_town_agent_ws_manager().connected_agents():
        if conn.agent_id != agent_conn.agent_id:
            mailbox.notify_connection(conn.connection_id, broadcast)

    return _notify_outcome(
        agent_conn,
        "post_job",
        {
            "status": "ok",
            "job_id": job_id,
            "bid_window_sec": int(BID_WINDOW_SEC),
            "escrowed": max_price,
            "balance": get_wallet(agent_conn.agent_id)["balance"],
        },
    )


async def place_bid(agent_conn: Any, job_id: str, price: int, confidence: float, pitch: str) -> dict:
    """Sealed bid on an OPEN job. Per-bidder last-write-wins; atomic vs window close."""
    mailbox = get_daemon_mailbox()
    if not _at_exchange(agent_conn):
        return _notify_outcome(
            agent_conn,
            "bid",
            {
                "status": "rejected",
                "reason": "not_at_exchange",
                "message": "You must be at the bank (the Exchange) to bid. `act move bank` first.",
            },
        )
    job = _get_job(job_id)
    if not job:
        return _notify_outcome(
            agent_conn,
            "bid",
            {"status": "rejected", "reason": "not_found", "message": f"No job {job_id} on the board."},
        )
    if job["consumerId"] == agent_conn.agent_id:
        return _notify_outcome(
            agent_conn,
            "bid",
            {"status": "rejected", "reason": "own_job", "message": "You can't bid on your own job."},
        )
    max_price = int(job.get("maxPrice", 0))
    if price < 0 or price > max_price:
        return _notify_outcome(
            agent_conn,
            "bid",
            {
                "status": "rejected",
                "reason": "bad_price",
                "message": f"Bid must be between 0 and the job's max price ({max_price}).",
            },
        )
    if job.get("status") != "OPEN":
        return _notify_outcome(
            agent_conn,
            "bid",
            {"status": "rejected", "reason": "not_open", "message": "Bidding on this job has closed."},
        )
    if not math.isfinite(confidence):
        return _notify_outcome(
            agent_conn,
            "bid",
            {
                "status": "rejected",
                "reason": "bad_confidence",
                "message": "confidence must be a finite number between 0 and 1.",
            },
        )
    confidence = max(0.0, min(1.0, confidence))

    bid = {
        "price": price,
        "confidence": Decimal(str(confidence)),
        "pitch": (pitch or "")[:PITCH_MAX],
        "bidAt": _now_iso(),
        "agentName": agent_conn.agent_name,
    }
    table = dynamo.jobs_table()
    try:
        resp = table.update_item(
            Key={"id": job_id},
            UpdateExpression="SET bids.#aid = :bid, updatedAt = :now",
            ConditionExpression="#st = :open",
            ExpressionAttributeNames={"#aid": agent_conn.agent_id, "#st": "status"},
            ExpressionAttributeValues={":bid": bid, ":open": "OPEN", ":now": _now_iso()},
            ReturnValues="ALL_NEW",
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return _notify_outcome(
            agent_conn,
            "bid",
            {"status": "rejected", "reason": "not_open", "message": "Bidding on this job has closed."},
        )

    # Sealed-bid: the consumer learns a bid arrived, never the amount.
    bid_count = len(resp["Attributes"].get("bids", {}))
    mailbox.notify(
        job["consumerId"],
        {"event": "bid_received", "job_id": job_id, "title": job.get("title", ""), "bid_count": bid_count},
    )
    return _notify_outcome(
        agent_conn,
        "bid",
        {"status": "ok", "job_id": job_id, "price": price, "confidence": confidence, "bid_count": bid_count},
    )


async def cancel_job(agent_conn: Any, job_id: str) -> dict:
    """Consumer withdraws an OPEN job: full escrow refund."""
    job = _get_job(job_id)
    if not job:
        return _notify_outcome(
            agent_conn,
            "cancel_job",
            {"status": "rejected", "reason": "not_found", "message": f"No job {job_id} on the board."},
        )
    if job["consumerId"] != agent_conn.agent_id:
        return _notify_outcome(
            agent_conn,
            "cancel_job",
            {"status": "rejected", "reason": "not_consumer", "message": "Only the poster can cancel a job."},
        )
    if not _flip_status(job_id, "OPEN", "CANCELLED"):
        return _notify_outcome(
            agent_conn,
            "cancel_job",
            {"status": "rejected", "reason": "not_open", "message": "Only OPEN jobs can be cancelled."},
        )
    _cancel_timer(job_id)
    balance = credit_wallet(job["consumerId"], int(job["maxPrice"]), "ESCROW_REFUND", job_id)
    return _notify_outcome(
        agent_conn,
        "cancel_job",
        {"status": "ok", "job_id": job_id, "refunded": int(job["maxPrice"]), "balance": balance},
    )


async def deliver_job(agent_conn: Any, job_id: str, deliverable: str) -> dict:
    """Provider submits the work product; opens the consumer review window."""
    mailbox = get_daemon_mailbox()
    job = _get_job(job_id)
    if not job:
        return _notify_outcome(
            agent_conn,
            "deliver_job",
            {"status": "rejected", "reason": "not_found", "message": f"No job {job_id} on the board."},
        )
    if job.get("providerId") != agent_conn.agent_id:
        return _notify_outcome(
            agent_conn,
            "deliver_job",
            {"status": "rejected", "reason": "not_provider", "message": "Only the awarded provider can deliver."},
        )
    if job.get("status") != "AWARDED":
        return _notify_outcome(
            agent_conn,
            "deliver_job",
            {"status": "rejected", "reason": "not_awarded", "message": "This job is not awaiting delivery."},
        )
    deliverable = (deliverable or "")[:DELIVERABLE_MAX]
    if not _flip_status(
        job_id,
        "AWARDED",
        "DELIVERED",
        {"deliverable": deliverable, "reviewDeadlineAt": Decimal(str(time.time() + REVIEW_SEC))},
    ):
        return _notify_outcome(
            agent_conn,
            "deliver_job",
            {"status": "rejected", "reason": "not_awarded", "message": "This job is not awaiting delivery."},
        )
    _schedule(job_id, REVIEW_SEC, _review_timeout)  # replaces the delivery timer
    _apply_mood(agent_conn.agent_id, "job_delivered")
    mailbox.notify(
        job["consumerId"],
        {
            "event": "job_delivered",
            "job_id": job_id,
            "title": job.get("title", ""),
            "deliverable": deliverable,  # full text — this is how the consumer receives the work
            "provider_id": agent_conn.agent_id,
            "consumer_id": job["consumerId"],
        },
    )
    return _notify_outcome(
        agent_conn,
        "deliver_job",
        {"status": "ok", "job_id": job_id, "review_window_sec": int(REVIEW_SEC)},
    )


async def review_job(agent_conn: Any, job_id: str, accept: bool | None, reason: str) -> dict:
    """Consumer accepts (settle + payout) or rejects (refund + provider trust hit)."""
    if accept is None:
        return _notify_outcome(
            agent_conn,
            "review_job",
            {
                "status": "rejected",
                "reason": "bad_accept",
                "message": "review_job requires accept=true or accept=false.",
            },
        )
    job = _get_job(job_id)
    if not job:
        return _notify_outcome(
            agent_conn,
            "review_job",
            {"status": "rejected", "reason": "not_found", "message": f"No job {job_id} on the board."},
        )
    if job["consumerId"] != agent_conn.agent_id:
        return _notify_outcome(
            agent_conn,
            "review_job",
            {"status": "rejected", "reason": "not_consumer", "message": "Only the poster can review a delivery."},
        )
    if job.get("status") != "DELIVERED":
        return _notify_outcome(
            agent_conn,
            "review_job",
            {"status": "rejected", "reason": "not_delivered", "message": "There is no delivery to review yet."},
        )
    outcome = "SUCCESS" if accept else "FAILURE_PROVIDER"
    if not await _settle(job, outcome, reason=reason, rejected_by_consumer=not accept):
        # Lost the race against the auto-accept timer (or a concurrent review):
        # the job was already settled between our status read and the flip.
        return _notify_outcome(
            agent_conn,
            "review_job",
            {
                "status": "rejected",
                "reason": "already_settled",
                "message": "This job was already settled (auto-accept may have fired first).",
            },
        )
    return _notify_outcome(
        agent_conn,
        "review_job",
        {"status": "ok", "job_id": job_id, "outcome": outcome},
    )


# ---------------------------------------------------------------------------
# Deadline transitions (fired by timers, the sweeper, or review_job)
# ---------------------------------------------------------------------------


async def _close_bid_window(job_id: str) -> None:
    """Bid window over: award to the scoring winner, or expire with a refund."""
    job = _get_job(job_id)
    if not job or job.get("status") != "OPEN":
        return  # already transitioned (cancel, sweeper/timer race)
    mailbox = get_daemon_mailbox()
    ws = get_town_agent_ws_manager()
    consumer_id = job["consumerId"]
    max_price = int(job.get("maxPrice", 0))
    bids: dict[str, dict] = job.get("bids") or {}

    # Disqualify bidders that are no longer connected (spec: can't deliver if gone).
    live_bids = {aid: bid for aid, bid in bids.items() if ws.get_by_agent_id(aid) is not None}
    svc = TownService()
    trust_by_agent = {}
    for aid in live_bids:
        bidder = svc.get_town_agent_by_id(aid) or {}
        trust_by_agent[aid] = float(bidder.get("trustScore", exchange_scoring.INITIAL_TRUST))

    winner = exchange_scoring.choose_winner(live_bids, max_price, trust_by_agent, job.get("strategy", "balanced"))

    if winner is None:
        if not _flip_status(job_id, "OPEN", "EXPIRED"):
            return
        _cancel_timer(job_id)
        consumer_balance = credit_wallet(consumer_id, max_price, "ESCROW_REFUND", job_id)
        _apply_mood(consumer_id, "job_expired_consumer")
        mailbox.notify(
            consumer_id,
            {
                "event": "job_expired",
                "job_id": job_id,
                "title": job.get("title", ""),
                "refunded": max_price,
                "consumer_id": consumer_id,
                "balance": consumer_balance,
            },
        )
        return

    agreed = int(live_bids[winner]["price"])
    provider_name = str(live_bids[winner].get("agentName", ""))
    if not _flip_status(
        job_id,
        "OPEN",
        "AWARDED",
        {
            "providerId": winner,
            "providerName": provider_name,
            "agreedPrice": agreed,
            "deliveryDeadlineAt": Decimal(str(time.time() + DELIVERY_SEC)),
        },
    ):
        return
    consumer_balance_after_award: int | None = None
    if max_price - agreed > 0:
        consumer_balance_after_award = credit_wallet(consumer_id, max_price - agreed, "ESCROW_REFUND", job_id)

    _apply_mood(winner, "job_won")
    awarded = {
        "event": "job_awarded",
        "job_id": job_id,
        "title": job.get("title", ""),
        "agreed_price": agreed,
        "deadline_sec": int(DELIVERY_SEC),
        "provider_name": provider_name,
        "consumer_name": job.get("consumerName", ""),
        "provider_id": winner,
        "consumer_id": consumer_id,
    }
    mailbox.notify(winner, {**awarded, "description": job.get("description", "")})
    # Consumer copy includes updated balance when a partial escrow refund was issued.
    consumer_awarded = (
        awarded if consumer_balance_after_award is None else {**awarded, "balance": consumer_balance_after_award}
    )
    mailbox.notify(consumer_id, consumer_awarded)
    for aid in bids:
        if aid != winner:
            mailbox.notify(aid, {"event": "bid_lost", "job_id": job_id, "title": job.get("title", "")})
            _apply_mood(aid, "bid_lost")

    _schedule(job_id, DELIVERY_SEC, _delivery_timeout)


async def _delivery_timeout(job_id: str) -> None:
    """Provider missed the delivery deadline: fail, refund, trust hit."""
    job = _get_job(job_id)
    if not job or job.get("status") != "AWARDED":
        return
    if not _flip_status(job_id, "AWARDED", "FAILED", {"outcome": "FAILURE_PROVIDER"}):
        return
    _cancel_timer(job_id)
    mailbox = get_daemon_mailbox()
    agreed = int(job.get("agreedPrice", 0))
    provider_id = job.get("providerId", "")
    consumer_id = job["consumerId"]
    consumer_balance = credit_wallet(consumer_id, agreed, "ESCROW_REFUND", job_id)
    _update_trust(provider_id, "FAILURE_PROVIDER", completed_delta=0, failed_delta=1)
    _apply_mood(provider_id, "job_failed_provider")
    failed = {
        "event": "job_failed",
        "job_id": job_id,
        "title": job.get("title", ""),
        "reason": "delivery_timeout",
        "provider_id": provider_id,
        "consumer_id": consumer_id,
    }
    mailbox.notify(provider_id, failed)
    # Consumer copy includes updated balance after the delivery-timeout refund.
    mailbox.notify(consumer_id, {**failed, "balance": consumer_balance})


async def _review_timeout(job_id: str) -> None:
    """Consumer never reviewed: auto-accept (protects providers, AEX SUCCESS_AUTO)."""
    job = _get_job(job_id)
    if not job or job.get("status") != "DELIVERED":
        return
    await _settle(job, "SUCCESS_AUTO")


async def _settle(job: dict, outcome: str, reason: str = "", rejected_by_consumer: bool = False) -> bool:
    """Single settlement path for accept / auto-accept / reject.

    Returns True if this call won the DELIVERED→terminal status flip (and
    therefore moved money / updated trust), False if another path (review
    verb vs auto-accept timer) settled the job first.
    """
    job_id = job["id"]
    success = outcome in ("SUCCESS", "SUCCESS_AUTO")
    if not _flip_status(job_id, "DELIVERED", "COMPLETED" if success else "FAILED", {"outcome": outcome}):
        return False  # another path (review vs auto-accept timer) won the race
    _cancel_timer(job_id)
    mailbox = get_daemon_mailbox()
    agreed = int(job.get("agreedPrice", 0))
    provider_id = job.get("providerId", "")
    consumer_id = job["consumerId"]

    if success:
        fee = exchange_scoring.platform_fee(agreed)
        provider_balance = credit_wallet(provider_id, agreed - fee, "PAYOUT", job_id)
        # TOWN has no running balance — balanceAfter 0 by convention.
        _ledger_append(TOWN_ACCOUNT, "FEE", fee, 0, job_id)
        _update_trust(provider_id, outcome, completed_delta=1, failed_delta=0)
        _apply_mood(provider_id, "job_completed_provider")
        _apply_mood(consumer_id, "job_completed_consumer")
        completed = {
            "event": "job_completed",
            "job_id": job_id,
            "title": job.get("title", ""),
            "payout": agreed - fee,
            "fee": fee,
            "provider_id": provider_id,
            "consumer_id": consumer_id,
        }
        # Provider copy includes updated balance; consumer copy does not (consumer wallet
        # didn't move at settle — it moved at award when the partial refund was issued).
        mailbox.notify(provider_id, {**completed, "balance": provider_balance})
        mailbox.notify(consumer_id, completed)
    else:
        consumer_balance = credit_wallet(consumer_id, agreed, "ESCROW_REFUND", job_id)
        _update_trust(provider_id, "FAILURE_PROVIDER", completed_delta=0, failed_delta=1)
        _apply_mood(provider_id, "job_failed_provider")
        if rejected_by_consumer:
            _bump_counter(consumer_id, "rejectionsIssued")
        failed = {
            "event": "job_failed",
            "job_id": job_id,
            "title": job.get("title", ""),
            "reason": reason or "rejected",
            "provider_id": provider_id,
            "consumer_id": consumer_id,
        }
        # Consumer copy includes updated balance (they received the refund); provider's does not.
        mailbox.notify(provider_id, failed)
        mailbox.notify(consumer_id, {**failed, "balance": consumer_balance})
    return True


# ---------------------------------------------------------------------------
# Sweeper (restart safety — deadlines are persisted, timers are not)
# ---------------------------------------------------------------------------

_SWEEP_CHECKS: tuple[tuple[str, str, Callable[[str], Awaitable[None]]], ...] = (
    ("OPEN", "bidWindowEndsAt", _close_bid_window),
    ("AWARDED", "deliveryDeadlineAt", _delivery_timeout),
    ("DELIVERED", "reviewDeadlineAt", _review_timeout),
)


async def sweep_overdue_jobs() -> None:
    """Fire any overdue deadline transition found via the status GSI.

    Every transition is a status-conditional flip, so racing an in-memory
    timer (or a concurrent sweep) is safe — exactly one side wins.

    Crash-window note (accepted at current scale):
    (a) This function only retries *deadline transitions* (status flips). A
        crash between a status flip and its money movement (PAYOUT / ESCROW_REFUND)
        strands the escrow detectably — the ledger will have no PAYOUT or
        ESCROW_REFUND row whose refId matches the job_id — but the sweep does
        not self-heal that gap. Reconcile-on-sweep is the upgrade path when
        traffic warrants it.
    (b) Each GSI query fetches only the first page (no pagination). Items that
        span multiple pages are handled on a later pass. Acceptable at town scale
        where the number of concurrently open/awarded/delivered jobs is small.
    """
    now = time.time()
    table = dynamo.jobs_table()
    for status, deadline_attr, transition in _SWEEP_CHECKS:
        resp = table.query(
            IndexName="status-createdAt-index",
            KeyConditionExpression=Key("status").eq(status),
        )
        for item in resp.get("Items", []):
            deadline = item.get(deadline_attr)
            if deadline is not None and float(deadline) <= now:
                await transition(item["id"])


async def exchange_sweeper_loop() -> None:
    """Background task started from main.py lifespan (Task 10)."""
    while True:
        await asyncio.sleep(60)
        try:
            await sweep_overdue_jobs()
        except Exception:
            logger.exception("Exchange sweeper pass failed")


# ---------------------------------------------------------------------------
# Public board (REST read, Task 9)
# ---------------------------------------------------------------------------


def list_board_jobs() -> list[dict]:
    """Exchange board: OPEN jobs newest-first + recent settled history.

    Public payload — no deliverables, no bid amounts (sealed-bid).
    """
    table = dynamo.jobs_table()

    def _entry(item: dict) -> dict:
        return {
            "job_id": item["id"],
            "title": item.get("title", ""),
            "description": (item.get("description") or "")[:200],
            "max_price": int(item.get("maxPrice", 0)),
            "agreed_price": int(item.get("agreedPrice") or 0),
            "status": item.get("status", ""),
            "consumer_name": item.get("consumerName", ""),
            "provider_name": item.get("providerName") or "",
            "created_at": item.get("createdAt", ""),
            "bid_count": len(item.get("bids") or {}),
        }

    out: list[dict] = []
    resp = table.query(
        IndexName="status-createdAt-index",
        KeyConditionExpression=Key("status").eq("OPEN"),
        ScanIndexForward=False,
        Limit=50,
    )
    out.extend(_entry(i) for i in resp.get("Items", []))
    for status in ("COMPLETED", "FAILED", "EXPIRED", "CANCELLED"):
        resp = table.query(
            IndexName="status-createdAt-index",
            KeyConditionExpression=Key("status").eq(status),
            ScanIndexForward=False,
            Limit=10,
        )
        out.extend(_entry(i) for i in resp.get("Items", []))
    return out
