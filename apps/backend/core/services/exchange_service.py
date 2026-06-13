"""GooseTown Exchange — jobs, bids, escrow, settlement, trust (ADR-0007).

Domain design ported from open-experiments/agent-exchange (MIT); plumbing
follows conversation_service (timers + sweeper) and town_service (DDB).
Money is integer crumbs; every wallet movement commits atomically with its
append-only ledger row (one TransactWriteItems via ``_wallet_transact``).
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable

from boto3.dynamodb.conditions import Key

from core import dynamo
from core.services import exchange_categories, exchange_scoring, town_mood_engine
from core.services.daemon_mailbox import get_daemon_mailbox
from core.services.provider_directory import ProviderDirectory
from core.services.town_agent_ws import get_town_agent_ws_manager
from core.services.town_service import TownService

logger = logging.getLogger(__name__)

SIGNUP_GRANT = int(os.environ.get("EXCHANGE_SIGNUP_GRANT", "100"))
BID_WINDOW_SEC = float(os.environ.get("EXCHANGE_BID_WINDOW_SEC", "600"))
DELIVERY_SEC = float(os.environ.get("EXCHANGE_DELIVERY_SEC", "3600"))
REVIEW_SEC = float(os.environ.get("EXCHANGE_REVIEW_SEC", "1800"))
EXCHANGE_LOCATION = "bank"

# Rate-limit knobs (per agent, per sliding 1-hour window).
RATE_LIMIT_POST = int(os.environ.get("EXCHANGE_RATE_POST", "4"))
RATE_LIMIT_BID = int(os.environ.get("EXCHANGE_RATE_BID", "30"))
RATE_LIMIT_MSG = int(os.environ.get("EXCHANGE_RATE_MSG", "60"))
_RATE_WINDOW_SEC = 3600  # 1 hour

TITLE_MAX = 120
DESCRIPTION_MAX = 4096
DELIVERABLE_MAX = 16384
PITCH_MAX = 500

# Wave 2: job category/tags. Category normalized via exchange_categories
# (defaults to DEFAULT_CATEGORY when absent/invalid); tags are free-form,
# sanitized to lowercase-trimmed slugs, capped per-item and in count.
DEFAULT_CATEGORY = "general"
TAG_MAX_LEN = 40
TAGS_MAX_COUNT = 8

# Synthetic ledger account that accumulates Exchange fees (the crumb sink).
TOWN_ACCOUNT = "TOWN"

# ---------------------------------------------------------------------------
# Artifact delivery (W5) — S3 objects under artifacts/{job_id}/{filename} in
# the sprite bucket. Presigned-only access; 30-day S3 lifecycle expiry.
# ---------------------------------------------------------------------------

ARTIFACT_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
ARTIFACT_UPLOAD_EXPIRES_SEC = 900  # 15 min presigned POST validity
ARTIFACT_DOWNLOAD_EXPIRES_SEC = 86400  # 24 h presigned GET validity

# Allowlist: [A-Za-z0-9._-], 1-128 chars, no leading dot (blocks dotfiles and
# any "../" style traversal even after the basename strip).
_ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}$")


def sanitize_artifact_filename(filename: str) -> str | None:
    """Reduce ``filename`` to a safe basename, or None if it can't be made safe.

    Basename only (both separator flavours stripped), then strict allowlist —
    we reject rather than mangle so the caller-visible key always matches what
    the provider asked for.
    """
    name = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not _ARTIFACT_NAME_RE.match(name):
        return None
    return name


def artifact_key_for(job_id: str, safe_name: str) -> str:
    return f"artifacts/{job_id}/{safe_name}"


def _s3_client():
    import boto3

    return boto3.client("s3")


def presign_artifact_upload(key: str) -> dict:
    """Presigned **POST** (not PUT) for the artifact upload.

    Mechanism choice: a presigned PUT cannot enforce an object-size limit
    (Content-Length is not part of the PUT signature), but a POST policy's
    ``content-length-range`` condition IS enforced by S3 at upload time —
    an oversized upload fails with 400 EntityTooLarge regardless of what the
    client claims. Returns ``{"url": ..., "fields": {...}}``; the client
    uploads multipart/form-data with the policy fields first and the file
    part last.
    """
    from core.config import settings

    return _s3_client().generate_presigned_post(
        Bucket=settings.SPRITE_S3_BUCKET,
        Key=key,
        Conditions=[["content-length-range", 1, ARTIFACT_MAX_BYTES]],
        ExpiresIn=ARTIFACT_UPLOAD_EXPIRES_SEC,
    )


def presign_artifact_download(key: str, version_id: str | None = None) -> str:
    """24 h presigned GET for the consumer to fetch the delivered artifact.

    When ``version_id`` is supplied (captured at deliver time from the S3
    HeadObject response), the presigned URL includes ``VersionId`` in its query
    params so S3 serves that immutable version regardless of later overwrites.

    # Upload presigns outlive delivery (15 min); pin the consumer's GET to the
    # version seen at deliver time so post-delivery overwrites are invisible
    # (bucket is versioned).
    """
    from core.config import settings

    params: dict = {"Bucket": settings.SPRITE_S3_BUCKET, "Key": key}
    if version_id:
        params["VersionId"] = version_id
    return _s3_client().generate_presigned_url(
        "get_object",
        Params=params,
        ExpiresIn=ARTIFACT_DOWNLOAD_EXPIRES_SEC,
    )


def head_artifact(key: str) -> tuple[int, str | None] | None:
    """Return ``(size_bytes, version_id)`` for the artifact, or None if missing/unreadable.

    ``version_id`` is the S3 object version captured at HeadObject time; it
    may be None on unversioned buckets or local test stacks that don't enable
    versioning. Callers that only need the size can index element 0.
    """
    from core.config import settings

    try:
        resp = _s3_client().head_object(Bucket=settings.SPRITE_S3_BUCKET, Key=key)
        return int(resp["ContentLength"]), resp.get("VersionId")
    except Exception:
        return None


class InsufficientFunds(Exception):
    """Raised when an agent's wallet balance cannot cover the requested debit."""


class AgentNotFound(Exception):
    """Raised when the target agent row does not exist in DynamoDB."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ledger_item(
    agent_id: str,
    entry_type: str,
    amount: int,
    balance_after: int,
    ref_id: str,
    counterparty_id: str | None = None,
) -> dict[str, Any]:
    """Build a ledger row dict (single source of truth for the row schema)."""
    item: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "agentId": agent_id,
        "entryType": entry_type,
        "amount": amount,
        "balanceAfter": balance_after,
        "refId": ref_id,
        "createdAt": _now_iso(),
    }
    if counterparty_id is not None:
        item["counterpartyId"] = counterparty_id
    return item


def _ledger_append(
    agent_id: str,
    entry_type: str,
    amount: int,
    balance_after: int,
    ref_id: str,
    counterparty_id: str | None = None,
) -> None:
    """Append a ledger row with NO paired wallet movement.

    Only used for synthetic-account rows (the TOWN fee sink), where there is
    no wallet balance to move. Real wallet movements go through
    ``_wallet_transact``, which commits the balance update and its ledger row
    in one DynamoDB transaction.
    """
    item = _ledger_item(agent_id, entry_type, amount, balance_after, ref_id, counterparty_id)
    try:
        dynamo.ledger_table().put_item(Item=item)
    except Exception:
        logger.error(
            "Ledger append failed: agent=%s type=%s amount=%d ref=%s",
            agent_id,
            entry_type,
            amount,
            ref_id,
        )
        raise


# CAS attempts for a single wallet movement. Contention on one agent's wallet
# is rare (an agent acts serially); 5 retries is generous headroom.
_WALLET_CAS_ATTEMPTS = 5


def _wallet_transact(
    agent_id: str,
    amount: int,
    entry_type: str,
    ref_id: str,
    *,
    require_funds: bool,
    counterparty_id: str | None = None,
) -> int:
    """Move ``amount`` crumbs (negative = debit) + append the ledger row atomically.

    The wallet update and its ledger row ride in ONE ``TransactWriteItems``
    call, so a crash or a failed ledger put can never move crumbs without an
    audit row (codex P2 #3406959205). ``balanceAfter`` stays exact via a CAS
    loop: read the current balance, condition the wallet update on the value
    we read, and re-read + retry when a concurrent movement cancels the
    transaction.

    Uses the resource-derived client (``table.meta.client``) exactly like
    ``TownService.opt_in`` — boto3 registers the document-interface
    transformations on that client, so plain Python dicts serialize correctly.

    Raises ``AgentNotFound`` when the agent row is missing, ``InsufficientFunds``
    when ``require_funds`` and the balance can't cover the debit (checked
    BEFORE any write), and ``RuntimeError`` after exhausting CAS retries.
    Returns the new balance.
    """
    table = dynamo.agents_table()
    client = table.meta.client
    for _ in range(_WALLET_CAS_ATTEMPTS):
        row = table.get_item(Key={"id": agent_id}).get("Item")
        if row is None:
            raise AgentNotFound(f"agent {agent_id[:8]} not found")
        has_balance = "walletBalance" in row
        current = int(row.get("walletBalance") or 0)
        new_balance = current + amount
        if require_funds and new_balance < 0:
            raise InsufficientFunds(f"agent {agent_id[:8]} cannot cover {-amount} crumbs")
        condition = "attribute_exists(id) AND " + (
            "walletBalance = :cur" if has_balance else "attribute_not_exists(walletBalance)"
        )
        values: dict[str, Any] = {":new": new_balance}
        if has_balance:
            values[":cur"] = current
        try:
            client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": table.name,
                            "Key": {"id": agent_id},
                            "UpdateExpression": "SET walletBalance = :new",
                            "ConditionExpression": condition,
                            "ExpressionAttributeValues": values,
                        }
                    },
                    {
                        "Put": {
                            "TableName": dynamo.ledger_table().name,
                            "Item": _ledger_item(
                                agent_id,
                                entry_type,
                                amount,
                                new_balance,
                                ref_id,
                                counterparty_id,
                            ),
                        }
                    },
                ]
            )
        except client.exceptions.TransactionCanceledException as exc:
            reasons = getattr(exc, "response", {}).get("CancellationReasons", []) or []
            if any(r.get("Code") == "ConditionalCheckFailed" for r in reasons):
                continue  # balance moved under us (or row vanished) — re-read and retry
            raise
        return new_balance
    raise RuntimeError(f"wallet contention: agent {agent_id[:8]} CAS retries exhausted ({entry_type} ref={ref_id})")


def _bump_counter(agent_id: str, attr: str, delta: int = 1) -> None:
    """Atomic counter bump via ADD (multi-task safe)."""
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


def _check_rate(agent_id: str, verb: str, limit: int, window_sec: int = _RATE_WINDOW_SEC) -> bool:
    """Enforce a per-agent windowed rate limit using a DynamoDB map attr.

    Uses the ``exchangeOps`` map on the agent row.  Each verb gets its own
    sub-entry ``{windowStart: epoch_int, count: n}`` stored under
    ``exchangeOps.{verb}``.

    Algorithm (read → conditional SET, multi-task safe):

    1. Initialise the parent ``exchangeOps`` map with ``if_not_exists`` so the
       sub-key SET in step 2 never hits a missing-parent error.
    2. Read the agent row (ConsistentRead) to get the current entry.
    3. Four paths:
       A. No entry → SET {now, 1} guarded on ``attribute_not_exists(exchangeOps.#v)``.
       B. Window expired → SET {now, 1} guarded on ``exchangeOps.#v = :old_entry``
          (the whole sub-map as the optimistic guard, since DynamoDB only supports
          comparisons on map values via the top-level key, not nested sub-attrs
          in ConditionExpression paths).
       C. Count < limit → SET {wstart, count+1} guarded on the same whole-entry
          equality.
       D. Count >= limit → return False (caller rejects).

    The guard in paths B and C conditions on the entire sub-map matching what
    we read; any concurrent modification (reset or increment) will fail the
    condition, so we fall back to a re-read and retry once more before giving up
    (always in favour of the action when there is genuine ambiguity).

    Returns True when the action is allowed (counter incremented), False when
    the agent has hit the limit for this verb in the current window.
    """
    table = dynamo.agents_table()
    now = int(time.time())
    window_start_floor = now - window_sec  # entries older than this are expired

    # Step 1: lazily initialise the parent map so sub-key SET never fails.
    try:
        table.update_item(
            Key={"id": agent_id},
            UpdateExpression="SET exchangeOps = if_not_exists(exchangeOps, :empty)",
            ConditionExpression="attribute_exists(id)",
            ExpressionAttributeValues={":empty": {}},
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        # Agent row missing — caller surfaces this; don't block the action.
        return True

    def _read_entry() -> dict | None:
        row = table.get_item(Key={"id": agent_id}, ConsistentRead=True).get("Item") or {}
        ops: dict = row.get("exchangeOps") or {}
        return ops.get(verb)

    def _try_set(entry_before: dict | None, new_entry: dict) -> bool:
        """Conditional SET guarded on exchangeOps.#v equalling entry_before (or absent).

        DynamoDB supports comparing a map sub-key path (exchangeOps.#v) against a
        whole-value expression attribute value, which lets us use the previously
        read map value as an optimistic lock without relying on nested sub-attribute
        paths in the condition (not universally supported by moto).
        """
        try:
            if entry_before is None:
                table.update_item(
                    Key={"id": agent_id},
                    UpdateExpression="SET exchangeOps.#v = :new",
                    ConditionExpression="attribute_not_exists(exchangeOps.#v)",
                    ExpressionAttributeNames={"#v": verb},
                    ExpressionAttributeValues={":new": new_entry},
                )
            else:
                table.update_item(
                    Key={"id": agent_id},
                    UpdateExpression="SET exchangeOps.#v = :new",
                    ConditionExpression="exchangeOps.#v = :old",
                    ExpressionAttributeNames={"#v": verb},
                    ExpressionAttributeValues={":old": entry_before, ":new": new_entry},
                )
            return True
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    for _attempt in range(3):
        entry = _read_entry()
        if entry is None:
            # Path A: first use — claim the first slot.
            if _try_set(None, {"windowStart": now, "count": 1}):
                return True
            continue  # lost the race to another task; re-read

        w_start = int(entry.get("windowStart", 0))
        count = int(entry.get("count", 0))

        if w_start <= window_start_floor:
            # Path B: window expired — reset.
            if _try_set(entry, {"windowStart": now, "count": 1}):
                return True
            continue  # another task reset; re-read

        if count >= limit:
            # Path D: genuinely rate-limited.
            return False

        # Path C: window live, count < limit — increment.
        if _try_set(entry, {"windowStart": w_start, "count": count + 1}):
            return True
        # Concurrent increment: re-read and retry.

    # Exhausted retries — allow (don't penalise on internal contention).
    logger.warning("_check_rate: retries exhausted for %s.%s — allowing", agent_id[:8], verb)
    return True


def _rate_limit_minutes_left(agent_id: str, verb: str, window_sec: int = _RATE_WINDOW_SEC) -> int:
    """Return minutes until the current rate-limit window resets (for rejection messages)."""
    try:
        table = dynamo.agents_table()
        agent_row = table.get_item(Key={"id": agent_id}, ConsistentRead=True).get("Item") or {}
        ops: dict = agent_row.get("exchangeOps") or {}
        entry: dict | None = ops.get(verb)
        if not entry:
            return 0
        window_start = int(entry.get("windowStart", 0))
        elapsed = int(time.time()) - window_start
        remaining_sec = max(0, window_sec - elapsed)
        return max(1, math.ceil(remaining_sec / 60))
    except Exception:
        return 60  # safe fallback


def ensure_signup_grant(agent_id: str) -> None:
    """Grant the one-time signup crumbs. Idempotent on signupGrantAt.

    The balance bump + signupGrantAt stamp + GRANT ledger row commit in one
    DynamoDB transaction, so the grant can never move crumbs without its
    audit row. Already-granted and agent-missing are both silent returns,
    distinguished by re-reading the row after a cancelled transaction.
    """
    table = dynamo.agents_table()
    client = table.meta.client
    for _ in range(_WALLET_CAS_ATTEMPTS):
        row = table.get_item(Key={"id": agent_id}).get("Item")
        if row is None:
            return  # agent missing — caller surfaces that elsewhere
        if row.get("signupGrantAt"):
            return  # already granted
        has_balance = "walletBalance" in row
        current = int(row.get("walletBalance") or 0)
        new_balance = current + SIGNUP_GRANT
        condition = "attribute_exists(id) AND attribute_not_exists(signupGrantAt) AND " + (
            "walletBalance = :cur" if has_balance else "attribute_not_exists(walletBalance)"
        )
        values: dict[str, Any] = {":new": new_balance, ":now": _now_iso()}
        if has_balance:
            values[":cur"] = current
        try:
            client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": table.name,
                            "Key": {"id": agent_id},
                            "UpdateExpression": "SET walletBalance = :new, signupGrantAt = :now",
                            "ConditionExpression": condition,
                            "ExpressionAttributeValues": values,
                        }
                    },
                    {
                        "Put": {
                            "TableName": dynamo.ledger_table().name,
                            "Item": _ledger_item(agent_id, "GRANT", SIGNUP_GRANT, new_balance, "signup"),
                        }
                    },
                ]
            )
        except client.exceptions.TransactionCanceledException:
            # Cancelled: either another caller granted concurrently (re-read
            # sees signupGrantAt → return), the agent row vanished (re-read
            # sees None → return), or the balance CAS lost — loop re-reads
            # and retries in every case.
            continue
        return
    raise RuntimeError(f"wallet contention: agent {agent_id[:8]} signup-grant CAS retries exhausted")


def debit_wallet(agent_id: str, amount: int, entry_type: str, ref_id: str) -> int:
    """Debit + ledger row in one transaction. Raises InsufficientFunds. Returns new balance."""
    ensure_signup_grant(agent_id)
    return _wallet_transact(agent_id, -amount, entry_type, ref_id, require_funds=True)


def credit_wallet(
    agent_id: str,
    amount: int,
    entry_type: str,
    ref_id: str,
    counterparty_id: str | None = None,
) -> int:
    """Credit + ledger row in one transaction. Returns new balance."""
    ensure_signup_grant(agent_id)
    return _wallet_transact(
        agent_id,
        amount,
        entry_type,
        ref_id,
        require_funds=False,
        counterparty_id=counterparty_id,
    )


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


def _flip_status(
    job_id: str,
    from_status: str,
    to_status: str,
    set_extra: dict[str, Any] | None = None,
    extra_condition: str | None = None,
    extra_values: dict[str, Any] | None = None,
) -> dict | None:
    """Conditionally flip a job's status.

    Returns the post-flip item dict (``ReturnValues="ALL_NEW"``) on success, or
    ``None`` if the condition failed — the job wasn't in ``from_status`` (another
    path — timer, sweeper, explicit verb — won the race) or, when
    ``extra_condition`` is supplied, that condition no longer held.

    Callers that only care about success/failure can use the returned value in a
    boolean context (``None`` is falsy, a dict is truthy), so all existing
    ``if not _flip_status(...)`` call sites remain correct.
    """
    table = dynamo.jobs_table()
    names = {"#st": "status"}
    values: dict[str, Any] = {":from": from_status, ":to": to_status, ":now": _now_iso()}
    sets = ["#st = :to", "updatedAt = :now"]
    for i, (attr, val) in enumerate((set_extra or {}).items()):
        names[f"#x{i}"] = attr
        values[f":x{i}"] = val
        sets.append(f"#x{i} = :x{i}")
    condition = "#st = :from"
    if extra_condition:
        condition += f" AND {extra_condition}"
        values.update(extra_values or {})
    try:
        resp = table.update_item(
            Key={"id": job_id},
            UpdateExpression="SET " + ", ".join(sets),
            ConditionExpression=condition,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return None
    return resp["Attributes"]


def _at_exchange(agent_conn: Any) -> bool:
    """Physically at the bank, in the town context (spec: post/bid require presence).

    Strongly-consistent read: a ``move bank`` arrival commits ``currentLocation``
    to DynamoDB *before* the daemon is told it arrived, but an eventually-
    consistent read can still see the pre-move location and spuriously reject
    a ``post_job``/``bid`` issued immediately after arrival. ``consistent=True``
    closes that read-after-write window.
    """
    agent = TownService().get_town_agent_by_id(agent_conn.agent_id, consistent=True) or {}
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


def _count_recent_same_counterparty_payouts(agent_id: str, counterparty_id: str) -> int:
    """Count PAYOUT ledger rows for *agent_id* whose counterpartyId == *counterparty_id*
    among the most recent 20 entries (reverse-chronological via the GSI).

    Rows without a ``counterpartyId`` attribute (written before Wave 1) are
    treated as diverse counterparties — they do not contribute to the repeat
    count and therefore never trigger dampening on legacy data.
    """
    resp = dynamo.ledger_table().query(
        IndexName="agentId-createdAt-index",
        KeyConditionExpression=Key("agentId").eq(agent_id),
        ScanIndexForward=False,
        Limit=20,
    )
    return sum(
        1
        for row in resp.get("Items", [])
        if row.get("entryType") == "PAYOUT" and row.get("counterpartyId") == counterparty_id
    )


def get_ledger_tail(agent_id: str, limit: int = 15) -> list[dict]:
    """Return the most recent *limit* ledger rows for *agent_id*, newest-first.

    Uses the ``agentId-createdAt-index`` GSI with ``ScanIndexForward=False``
    (same pattern as ``_count_recent_same_counterparty_payouts``).
    Rows are returned as plain dicts with camelCase keys mapped to the
    public shape expected by the ledger endpoint.
    """
    resp = dynamo.ledger_table().query(
        IndexName="agentId-createdAt-index",
        KeyConditionExpression=Key("agentId").eq(agent_id),
        ScanIndexForward=False,
        Limit=limit,
    )
    rows = []
    for item in resp.get("Items", []):
        rows.append(
            {
                "type": item.get("entryType", ""),
                "amount": int(item.get("amount", 0)),
                "balance_after": int(item.get("balanceAfter", 0)),
                "ref_id": item.get("refId", ""),
                "at": item.get("createdAt", ""),
            }
        )
    return rows


def _emit_economy_event(tag: str, summary: str, location: str = "bank", **detail: Any) -> None:
    """Emit a world-feed economy event. Import is lazy (core must not couple to worldgm at module load).

    Never raises — EventBridge absent in tests/local must not break the economy.
    """
    try:
        # Lazy import: core/services must not import worldgm at module level.
        # No existing 'from worldgm' imports exist under core/ — this lazy pattern
        # is the approved approach (see task spec).
        from worldgm import event_publisher

        event_publisher.emit_world_event(
            tag=tag,
            detail=dict(detail),
            location=location,
            summary=summary,
        )
    except Exception:
        logger.debug("_emit_economy_event suppressed error for tag=%s", tag)


def _update_trust(
    agent_id: str,
    outcome: str,
    completed_delta: int,
    failed_delta: int,
    counterparty_id: str | None = None,
) -> None:
    # trustScore is an EMA, so it's inherently read-modify-write. Guard the
    # write with an optimistic ConditionExpression on the value we read and
    # retry on conflict, so two jobs settling concurrently for one agent
    # can't drop an outcome (codex P2 on PR #386). Counters use atomic ADD.

    # Cheap repeat-counterparty damping: if this provider has received ≥3 PAYOUT
    # rows from the same consumer in their recent ledger history, halve the EMA
    # alpha for this update.  This discourages farming (same pair repeatedly
    # transacting to inflate trust) without requiring a full social-graph
    # analysis.  Pre-Wave-1 rows without counterpartyId count as diverse.
    # Revisit with real graph analysis if farming appears in production data.
    alpha = exchange_scoring.TRUST_EMA_ALPHA
    if counterparty_id is not None:
        same_pair_count = _count_recent_same_counterparty_payouts(agent_id, counterparty_id)
        if same_pair_count >= 3:
            alpha = exchange_scoring.TRUST_EMA_ALPHA / 2

    table = dynamo.agents_table()
    for _ in range(3):
        agent = TownService().get_town_agent_by_id(agent_id) or {}
        old_raw = agent.get("trustScore")
        old = float(old_raw) if old_raw is not None else exchange_scoring.INITIAL_TRUST
        new_trust = exchange_scoring.updated_trust(old, outcome, alpha=alpha)
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


def _sanitize_tags(tags: list[str] | None) -> list[str]:
    """Normalize free-form job tags: lowercase, strip, drop empties, cap.

    Wave 2: tags are advisory free-text labels (not validated category slugs).
    Each is lowercased + trimmed + truncated to ``TAG_MAX_LEN``; empties are
    dropped; duplicates removed (first-seen order); the list capped at
    ``TAGS_MAX_COUNT``.
    """
    if not tags:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        if not isinstance(raw, str):
            continue
        tag = raw.strip().lower()[:TAG_MAX_LEN]
        if tag and tag not in seen:
            seen.add(tag)
            out.append(tag)
        if len(out) >= TAGS_MAX_COUNT:
            break
    return out


# ---------------------------------------------------------------------------
# Public verbs (called by the dispatch ladder in routers/town.py)
# ---------------------------------------------------------------------------


async def post_job(
    agent_conn: Any,
    title: str,
    description: str,
    max_price: int,
    strategy: str,
    success_criteria: list[str] | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Post a job at the Exchange: escrow ``max_price``, open the bid window.

    Wave 2 (spec §3.3): the job is categorized (``category`` normalized via
    ``exchange_categories``; defaults to ``"general"``) and, **after the job
    row is committed**, the subscription matcher runs **synchronously in this
    request** — it queries ``provider-subscriptions`` for the category (and its
    wildcard parents), excludes the consumer, and persists the matched provider
    ids onto the job as the durable ``offeredTo`` String Set (codex P1: the
    offer set is authorization-bearing job state, not a fleeting event). Only
    then are the matched providers notified (best-effort ``job_offered``). The
    old all-connections broadcast is gone — offers are targeted; the job still
    lands on the public board regardless, so the pull path is unaffected.
    """
    mailbox = get_daemon_mailbox()
    # Strongly-consistent read so a post_job issued immediately after a `move bank`
    # arrival sees the just-committed currentLocation (read-after-write gate).
    agent = TownService().get_town_agent_by_id(agent_conn.agent_id, consistent=True) or {}
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
    # W2: rate limit — check before any wallet movement.
    if not _check_rate(agent_conn.agent_id, "post_job", RATE_LIMIT_POST):
        mins = _rate_limit_minutes_left(agent_conn.agent_id, "post_job")
        return _notify_outcome(
            agent_conn,
            "post_job",
            {
                "status": "rejected",
                "reason": "rate_limited",
                "message": (
                    f"You've posted the maximum {RATE_LIMIT_POST} jobs per hour. Try again in ~{mins} minute(s)."
                ),
            },
        )
    if strategy not in exchange_scoring.STRATEGY_WEIGHTS:
        strategy = "balanced"
    # W1: denormalize the consumer's userId onto the job so place_bid can enforce
    # the same-owner hard-block without an extra DynamoDB read on every bid.
    consumer_user_id: str = agent.get("userId", "")
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
    # W3: sanitize success_criteria (strip, truncate 200, drop empties, cap 8).
    sanitized_criteria: list[str] | None = None
    if success_criteria:
        sanitized_criteria = [c.strip()[:200] for c in success_criteria if c.strip()][:8]
        if not sanitized_criteria:
            sanitized_criteria = None

    # Wave 2: normalize category (default "general" when None/invalid) + tags.
    job_category = exchange_categories.normalize_category(category) or DEFAULT_CATEGORY
    sanitized_tags = _sanitize_tags(tags)

    now = time.time()
    job: dict[str, Any] = {
        "id": job_id,
        "consumerId": agent_conn.agent_id,
        "consumerName": agent_conn.agent_name,
        "title": title[:TITLE_MAX],
        "description": description[:DESCRIPTION_MAX],
        "maxPrice": max_price,
        "strategy": strategy,
        "status": "OPEN",
        "bids": {},
        "category": job_category,
        "createdAt": _now_iso(),
        "updatedAt": _now_iso(),
        "bidWindowEndsAt": Decimal(str(now + BID_WINDOW_SEC)),
    }
    if sanitized_tags:
        job["tags"] = sanitized_tags
    # W1: store denormalized consumer userId so place_bid can enforce the
    # same-owner hard-block cheaply (no extra agent read per bid).
    if consumer_user_id:
        job["consumerUserId"] = consumer_user_id
    # W3: store success criteria on the job row.
    if sanitized_criteria:
        job["successCriteria"] = sanitized_criteria
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
    _emit_economy_event(
        "economy.job_posted",
        summary=f'{agent_conn.agent_name} posted "{job["title"]}" at the Exchange — up to {max_price} crumbs',
        location=EXCHANGE_LOCATION,
        job_id=job_id,
        consumer_name=agent_conn.agent_name,
        title=job["title"],
        max_price=max_price,
    )

    # Wave 2 (spec §3.3, codex P1): the subscription matcher runs SYNCHRONOUSLY
    # here — it replaces the old all-connections broadcast. Query subscribers for
    # the category (+ wildcard parents), excluding the consumer, and persist the
    # matched ids onto the job as the durable `offeredTo` String Set BEFORE any
    # notification, so a dropped notify is recoverable by reconnect resync /
    # sweeper (the offer set is authorization-bearing state, not a fleeting
    # event). A job with no subscribers leaves `offeredTo` unset and is still on
    # the public board (pull path).
    try:
        matched = ProviderDirectory().subscribers_for_job(job_category, consumer_id=agent_conn.agent_id)
    except Exception:
        # Discovery is best-effort; a directory read failure must not strand the
        # job (it's committed) — fall back to no pushed offers (pull path only).
        logger.exception("post_job: matcher lookup failed for job %s — no offers pushed", job_id)
        matched = []

    if matched:
        # Persist the durable offer set in the SAME item via a String Set ADD —
        # committed here, before notifications, so the offeredTo authz record
        # survives a notify failure (codex P1).
        try:
            dynamo.jobs_table().update_item(
                Key={"id": job_id},
                UpdateExpression="ADD offeredTo :ids SET updatedAt = :now",
                ConditionExpression="#st = :open",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={":ids": set(matched), ":open": "OPEN", ":now": _now_iso()},
            )
        except Exception:
            logger.exception("post_job: offeredTo write failed for job %s", job_id)
            matched = []

    offer_event_base: dict[str, Any] = {
        "event": "job_offered",
        "job_id": job_id,
        "title": job["title"],
        "category": job_category,
        "max_price": max_price,
        "description": job["description"][:300],
        "consumer_name": agent_conn.agent_name,
    }
    if sanitized_criteria:
        offer_event_base["criteria"] = sanitized_criteria
    # Notify each matched provider — best-effort; offeredTo is the durable record.
    for pid in matched:
        try:
            mailbox.notify(pid, dict(offer_event_base))
        except Exception:
            logger.warning("post_job: job_offered notify failed for provider %s job %s", pid[:8], job_id)

    return _notify_outcome(
        agent_conn,
        "post_job",
        {
            "status": "ok",
            "job_id": job_id,
            "category": job_category,
            "offered_count": len(matched),
            "bid_window_sec": int(BID_WINDOW_SEC),
            "escrowed": max_price,
            "balance": get_wallet(agent_conn.agent_id)["balance"],
        },
    )


async def place_bid(agent_conn: Any, job_id: str, price: int, confidence: float, pitch: str) -> dict:
    """Sealed bid on an OPEN job. Per-bidder last-write-wins; atomic vs window close.

    Wave 2 (spec §3.5, Task 5): the Wave-1 at-bank requirement is relaxed to
    **presence OR offer**. A subscribed/invited provider bids *remotely* — it
    appears in the job's durable ``offeredTo`` String Set (the matcher's record,
    or an explicit ``invite_provider``). A walk-in at the bank still bids in the
    casual in-world path even without an offer (presence wins). The check sits
    AFTER the cheap rejections (own_job / own_household) so a same-owner or
    self bid is rejected for the right reason regardless of presence.
    """
    mailbox = get_daemon_mailbox()
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
    # W1: same-owner hard-block (own_household).
    # Use the denormalized consumerUserId when available; fall back to a live
    # agent-row lookup for pre-Wave-1 jobs that don't have the attribute.
    consumer_user_id: str = job.get("consumerUserId") or ""
    if not consumer_user_id:
        svc_tmp = TownService()
        consumer_agent = svc_tmp.get_town_agent_by_id(job["consumerId"]) or {}
        consumer_user_id = consumer_agent.get("userId", "")
    if consumer_user_id:
        bidder_agent = TownService().get_town_agent_by_id(agent_conn.agent_id) or {}
        bidder_user_id = bidder_agent.get("userId", "")
        if bidder_user_id and bidder_user_id == consumer_user_id:
            return _notify_outcome(
                agent_conn,
                "bid",
                {
                    "status": "rejected",
                    "reason": "own_household",
                    "message": "Your household posted this job — agents with the same owner can't bid.",
                },
            )
    # Wave 2 (spec §3.5): presence OR offer. Allow the bid if the bidder is
    # physically at the bank OR appears in the job's durable `offeredTo` set
    # (matched by category at post time, or explicitly invited). A missing
    # `offeredTo` attribute is treated as the empty set — no crash.
    offered_to = job.get("offeredTo") or set()
    if not _at_exchange(agent_conn) and agent_conn.agent_id not in offered_to:
        return _notify_outcome(
            agent_conn,
            "bid",
            {
                "status": "rejected",
                "reason": "not_invited",
                "message": (
                    "You weren't offered this job and aren't at the Exchange — "
                    "move to the bank, or wait to be offered a job in your categories."
                ),
            },
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
    # Deadline guard: enforce the persisted bidWindowEndsAt even when the
    # in-memory timer hasn't fired yet (e.g. after a backend restart).
    bid_deadline = float(job.get("bidWindowEndsAt") or 0)
    if bid_deadline and time.time() > bid_deadline:
        await _close_bid_window(job_id)  # idempotent — settles the overdue window now
        return _notify_outcome(
            agent_conn,
            "bid",
            {"status": "rejected", "reason": "window_closed", "message": "Bidding closed for this job."},
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
    # W2: rate limit — check before any wallet movement or state write.
    if not _check_rate(agent_conn.agent_id, "bid", RATE_LIMIT_BID):
        mins = _rate_limit_minutes_left(agent_conn.agent_id, "bid")
        return _notify_outcome(
            agent_conn,
            "bid",
            {
                "status": "rejected",
                "reason": "rate_limited",
                "message": f"You've placed the maximum {RATE_LIMIT_BID} bids per hour. Try again in ~{mins} minute(s).",
            },
        )

    bid = {
        "price": price,
        "confidence": Decimal(str(confidence)),
        "pitch": (pitch or "")[:PITCH_MAX],
        "bidAt": _now_iso(),
        "agentName": agent_conn.agent_name,
    }
    table = dynamo.jobs_table()
    try:
        # The bidsRev bump rides in the SAME atomic write as the bid, so the
        # window-close path can detect (and re-score) any bid that commits
        # between its job read and its status flip.
        resp = table.update_item(
            Key={"id": job_id},
            UpdateExpression="SET bids.#aid = :bid, updatedAt = :now ADD bidsRev :one",
            ConditionExpression="#st = :open",
            ExpressionAttributeNames={"#aid": agent_conn.agent_id, "#st": "status"},
            ExpressionAttributeValues={":bid": bid, ":open": "OPEN", ":now": _now_iso(), ":one": 1},
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


async def invite_provider(agent_conn: Any, job_id: str, provider_id: str) -> dict:
    """Consumer invites a specific provider to bid on its OPEN job.

    Wave 2 (spec §3.5, Task 5): the directory→invite path. The consumer appends
    *provider_id* to the job's durable ``offeredTo`` String Set (guarded on
    ``status = OPEN``) so that provider can then bid **remotely** — including an
    off-category specialist that the category matcher never auto-offered. The
    ``job_offered`` notify (``invited=True``) is best-effort fan-out on top of
    the durable ``offeredTo`` record (codex P1: the set is the authz state, the
    event is recoverable via resync).
    """
    job = _get_job(job_id)
    if not job:
        return _notify_outcome(
            agent_conn,
            "invite",
            {"status": "rejected", "reason": "not_found", "message": f"No job {job_id} on the board."},
        )
    if job["consumerId"] != agent_conn.agent_id:
        return _notify_outcome(
            agent_conn,
            "invite",
            {"status": "rejected", "reason": "not_consumer", "message": "Only the poster can invite providers."},
        )
    if job.get("status") != "OPEN":
        return _notify_outcome(
            agent_conn,
            "invite",
            {"status": "rejected", "reason": "not_open", "message": "Only OPEN jobs can offer invitations."},
        )

    # Append the provider to the durable offer set, re-checking OPEN at write
    # time so a window-close racing the invite can't resurrect a closed offer.
    try:
        dynamo.jobs_table().update_item(
            Key={"id": job_id},
            UpdateExpression="ADD offeredTo :pid SET updatedAt = :now",
            ConditionExpression="#st = :open",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":pid": {provider_id}, ":open": "OPEN", ":now": _now_iso()},
        )
    except dynamo.jobs_table().meta.client.exceptions.ConditionalCheckFailedException:
        return _notify_outcome(
            agent_conn,
            "invite",
            {"status": "rejected", "reason": "not_open", "message": "Only OPEN jobs can offer invitations."},
        )

    # Best-effort offer notify to the invited provider — same payload shape as
    # the post-time matcher, flagged invited=True.
    offer_event: dict[str, Any] = {
        "event": "job_offered",
        "job_id": job_id,
        "title": job.get("title", ""),
        "category": job.get("category", "general"),
        "max_price": int(job.get("maxPrice", 0)),
        "description": (job.get("description") or "")[:300],
        "consumer_name": agent_conn.agent_name,
        "invited": True,
    }
    success_criteria = job.get("successCriteria")
    if success_criteria:
        offer_event["criteria"] = success_criteria
    try:
        get_daemon_mailbox().notify(provider_id, offer_event)
    except Exception:
        logger.warning("invite_provider: job_offered notify failed for provider %s job %s", provider_id[:8], job_id)

    return _notify_outcome(
        agent_conn,
        "invite",
        {"status": "ok", "job_id": job_id, "provider_id": provider_id},
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
    flipped = _flip_status(job_id, "OPEN", "CANCELLED")
    if not flipped:
        return _notify_outcome(
            agent_conn,
            "cancel_job",
            {"status": "rejected", "reason": "not_open", "message": "Only OPEN jobs can be cancelled."},
        )
    _cancel_timer(job_id)
    balance = credit_wallet(job["consumerId"], int(job["maxPrice"]), "ESCROW_REFUND", job_id)
    mailbox = get_daemon_mailbox()
    # Use the post-flip bids map rather than the pre-read snapshot so that any
    # bid that raced in between our _get_job read and the status flip (and whose
    # status=OPEN condition therefore succeeded) is also notified.  DynamoDB
    # serializes item writes, so every bid whose conditional write committed did
    # so before our flip committed — the ALL_NEW item is the authoritative set.
    post_flip_bids: dict = flipped.get("bids") or {}
    for aid in post_flip_bids:
        mailbox.notify(
            aid,
            {
                "event": "job_cancelled",
                "job_id": job_id,
                "title": job.get("title", ""),
                "consumer_id": job["consumerId"],
            },
        )
    return _notify_outcome(
        agent_conn,
        "cancel_job",
        {"status": "ok", "job_id": job_id, "refunded": int(job["maxPrice"]), "balance": balance},
    )


async def deliver_job(agent_conn: Any, job_id: str, deliverable: str, artifact_key: str | None = None) -> dict:
    """Provider submits the work product; opens the consumer review window.

    W5: ``artifact_key`` references an S3 object the provider uploaded via
    ``POST /town/exchange/upload-url``. The key must live under this job's
    ``artifacts/{job_id}/`` prefix (no cross-job key access) and the object
    must exist and respect the 5 MB cap. The consumer's ``job_delivered``
    event then carries a 24 h presigned GET (``artifact_url``) +
    ``artifact_name`` alongside any inline text deliverable.
    """
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
    # Deadline guard: enforce the persisted deliveryDeadlineAt even when the
    # in-memory timer hasn't fired yet (e.g. after a backend restart).
    delivery_deadline = float(job.get("deliveryDeadlineAt") or 0)
    if delivery_deadline and time.time() > delivery_deadline:
        await _delivery_timeout(job_id)  # idempotent — fails and refunds now
        return _notify_outcome(
            agent_conn,
            "deliver_job",
            {
                "status": "rejected",
                "reason": "deadline_passed",
                "message": "The delivery deadline passed — the job was failed and the poster refunded.",
            },
        )
    deliverable = (deliverable or "")[:DELIVERABLE_MAX]
    # W5: validate the artifact reference before any state flip.
    artifact_name: str | None = None
    artifact_version_id: str | None = None
    if artifact_key:
        expected_prefix = f"artifacts/{job_id}/"
        candidate = artifact_key[len(expected_prefix) :] if artifact_key.startswith(expected_prefix) else ""
        if not candidate or sanitize_artifact_filename(candidate) != candidate:
            return _notify_outcome(
                agent_conn,
                "deliver_job",
                {
                    "status": "rejected",
                    "reason": "bad_artifact",
                    "message": f"artifact_key must be 'artifacts/{job_id}/<safe filename>'.",
                },
            )
        artifact_name = candidate
        head = head_artifact(artifact_key)
        if head is None:
            return _notify_outcome(
                agent_conn,
                "deliver_job",
                {
                    "status": "rejected",
                    "reason": "artifact_missing",
                    "message": "No uploaded artifact found at that key — upload it first via the upload-url flow.",
                },
            )
        size, artifact_version_id = head
        if size > ARTIFACT_MAX_BYTES:
            return _notify_outcome(
                agent_conn,
                "deliver_job",
                {
                    "status": "rejected",
                    "reason": "bad_artifact",
                    "message": f"Artifact is {size} bytes — the cap is {ARTIFACT_MAX_BYTES} bytes (5 MB).",
                },
            )
    flip_extra: dict[str, Any] = {
        "deliverable": deliverable,
        "reviewDeadlineAt": Decimal(str(time.time() + REVIEW_SEC)),
    }
    if artifact_key:
        flip_extra["artifactKey"] = artifact_key
        if artifact_version_id:
            flip_extra["artifactVersionId"] = artifact_version_id
    if not _flip_status(
        job_id,
        "AWARDED",
        "DELIVERED",
        flip_extra,
    ):
        return _notify_outcome(
            agent_conn,
            "deliver_job",
            {"status": "rejected", "reason": "not_awarded", "message": "This job is not awaiting delivery."},
        )
    _schedule(job_id, REVIEW_SEC, _review_timeout)  # replaces the delivery timer
    _apply_mood(agent_conn.agent_id, "job_delivered")
    delivered_event: dict[str, Any] = {
        "event": "job_delivered",
        "job_id": job_id,
        "title": job.get("title", ""),
        "deliverable": deliverable,  # full text — this is how the consumer receives the work
        "provider_id": agent_conn.agent_id,
        "consumer_id": job["consumerId"],
    }
    if artifact_key:
        # 24 h presigned GET — the consumer daemon downloads it into
        # <workspace>/JOBS/{job_id}/. Pin to artifact_version_id when present
        # so post-delivery overwrites via the still-valid upload presign are
        # invisible to the consumer. Presign failure must not strand the
        # delivery (the flip already happened); fall back to key-only.
        try:
            delivered_event["artifact_url"] = presign_artifact_download(artifact_key, version_id=artifact_version_id)
        except Exception:
            logger.exception("Artifact presign failed for job %s key %s", job_id, artifact_key)
        delivered_event["artifact_name"] = artifact_name
    mailbox.notify(job["consumerId"], delivered_event)
    return _notify_outcome(
        agent_conn,
        "deliver_job",
        {"status": "ok", "job_id": job_id, "review_window_sec": int(REVIEW_SEC)},
    )


async def review_job(
    agent_conn: Any,
    job_id: str,
    accept: bool | None,
    reason: str,
    criteria_verdicts: list[bool] | None = None,
) -> dict:
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
    # Deadline guard: enforce the persisted reviewDeadlineAt even when the
    # in-memory timer hasn't fired yet (e.g. after a backend restart).
    review_deadline = float(job.get("reviewDeadlineAt") or 0)
    if review_deadline and time.time() > review_deadline:
        await _review_timeout(job_id)  # idempotent — auto-accepts and pays provider
        return _notify_outcome(
            agent_conn,
            "review_job",
            {
                "status": "rejected",
                "reason": "review_window_closed",
                "message": "The review window passed — the delivery was auto-accepted and the provider paid.",
            },
        )
    # W3: validate criteria_verdicts length when the job has criteria.
    stored_criteria: list[str] = job.get("successCriteria") or []
    if criteria_verdicts is not None and stored_criteria:
        if len(criteria_verdicts) != len(stored_criteria):
            return _notify_outcome(
                agent_conn,
                "review_job",
                {
                    "status": "rejected",
                    "reason": "bad_verdicts",
                    "message": (
                        f"criteria_verdicts length ({len(criteria_verdicts)}) must match "
                        f"the job's success_criteria count ({len(stored_criteria)})."
                    ),
                },
            )
    # W3: determine outcome — accept with any false verdict → SUCCESS_PARTIAL.
    if accept:
        if criteria_verdicts is not None and stored_criteria and not all(criteria_verdicts):
            outcome = "SUCCESS_PARTIAL"
        else:
            outcome = "SUCCESS"
    else:
        outcome = "FAILURE_PROVIDER"
    if not await _settle(
        job,
        outcome,
        reason=reason,
        rejected_by_consumer=not accept,
        criteria_verdicts=criteria_verdicts if stored_criteria else None,
    ):
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


MSG_CAP_PER_SIDE = 20
MSG_TEXT_MAX = 2000


async def job_message(agent_conn: Any, job_id: str, text: str) -> dict:
    """Send a message on an AWARDED or DELIVERED job.

    Sender must be the job's consumer or provider.  Per-side cap: 20 messages.
    Counterparty receives a ``job_message`` daemon event; sender gets
    ``exchange_ok`` via ``_notify_outcome``.

    A2A note: the message shape ``{from, fromName, text, at}`` maps directly to
    an A2A message part — when a non-resident A2A bridge is added in Wave 3,
    these rows can be relayed verbatim via the A2A protocol.
    """
    # Rate limit first — before any DB reads. Note the deliberate asymmetry vs
    # post_job/bid (rate-checked last, so validation rejections are free): a
    # message verb's only cost IS the spam, so even invalid attempts consume budget.
    if not _check_rate(agent_conn.agent_id, "job_msg", RATE_LIMIT_MSG):
        mins = _rate_limit_minutes_left(agent_conn.agent_id, "job_msg")
        return _notify_outcome(
            agent_conn,
            "job_msg",
            {
                "status": "rejected",
                "reason": "rate_limited",
                "message": (
                    f"You've sent the maximum {RATE_LIMIT_MSG} messages per hour. Try again in ~{mins} minute(s)."
                ),
            },
        )
    job = _get_job(job_id)
    if not job:
        return _notify_outcome(
            agent_conn,
            "job_msg",
            {"status": "rejected", "reason": "not_found", "message": f"No job {job_id} found."},
        )

    consumer_id = job.get("consumerId", "")
    provider_id = job.get("providerId", "")
    if agent_conn.agent_id not in (consumer_id, provider_id):
        return _notify_outcome(
            agent_conn,
            "job_msg",
            {
                "status": "rejected",
                "reason": "not_participant",
                "message": "Only the job's consumer or provider can send messages on a job.",
            },
        )

    status = job.get("status", "")
    if status not in ("AWARDED", "DELIVERED"):
        return _notify_outcome(
            agent_conn,
            "job_msg",
            {
                "status": "rejected",
                "reason": "not_active",
                "message": f"Job messages are only allowed on AWARDED or DELIVERED jobs (current status: {status}).",
            },
        )

    # Per-side cap: count the sender's existing messages.
    # Read-then-write race: two concurrent sends at count=19 can both pass the
    # check and produce 21 messages.  Acceptable — the cap is anti-spam, not a
    # hard invariant; a one-message overrun is inconsequential.
    existing_messages: list[dict] = job.get("messages") or []
    sender_count = sum(1 for m in existing_messages if m.get("from") == agent_conn.agent_id)
    if sender_count >= MSG_CAP_PER_SIDE:
        return _notify_outcome(
            agent_conn,
            "job_msg",
            {
                "status": "rejected",
                "reason": "msg_cap",
                "message": f"You have reached the {MSG_CAP_PER_SIDE}-message cap for this job.",
            },
        )

    # Sanitize: strip newlines so a crafted message cannot forge extra event lines
    # or markdown sections in the counterparty's TOWN_EVENTS.md / TOWN_JOBS.md.
    # (Both files use "\n".join(lines) to render, so an embedded newline is an
    # injection vector — low severity because the attacker must be an authenticated
    # participant, but easy to close.)
    sanitized_text = text.replace("\n", " ").replace("\r", " ")
    # Append the message via list_append — conditioned on status still in the two allowed states.
    new_msg: dict[str, Any] = {
        "from": agent_conn.agent_id,
        "fromName": agent_conn.agent_name,
        "text": sanitized_text[:MSG_TEXT_MAX],
        "at": _now_iso(),
    }
    table = dynamo.jobs_table()
    try:
        table.update_item(
            Key={"id": job_id},
            UpdateExpression="SET messages = list_append(if_not_exists(messages, :empty), :m), updatedAt = :now",
            ConditionExpression="#st = :awarded OR #st = :delivered",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":empty": [],
                ":m": [new_msg],
                ":now": _now_iso(),
                ":awarded": "AWARDED",
                ":delivered": "DELIVERED",
            },
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return _notify_outcome(
            agent_conn,
            "job_msg",
            {
                "status": "rejected",
                "reason": "not_active",
                "message": "Job is no longer active (status changed while sending).",
            },
        )

    # Notify the counterparty.
    counterparty_id = provider_id if agent_conn.agent_id == consumer_id else consumer_id
    get_daemon_mailbox().notify(
        counterparty_id,
        {
            "event": "job_message",
            "job_id": job_id,
            "title": job.get("title", ""),
            "from_id": agent_conn.agent_id,
            "from_name": agent_conn.agent_name,
            "text": sanitized_text[:MSG_TEXT_MAX],
        },
    )
    return _notify_outcome(
        agent_conn,
        "job_msg",
        {"status": "ok", "job_id": job_id},
    )


# ---------------------------------------------------------------------------
# Deadline transitions (fired by timers, the sweeper, or review_job)
# ---------------------------------------------------------------------------


_CLOSE_GUARDED_ATTEMPTS = 5


async def _close_bid_window(job_id: str) -> None:
    """Bid window over: award to the scoring winner, or expire with a refund.

    A bid can commit between our job read and the OPEN→AWARDED/EXPIRED flip
    (its ``status=OPEN`` condition passed), and it must participate in the
    award — not just be notified as a loser. ``place_bid`` bumps ``bidsRev``
    atomically with each bid, so we guard the flip on the revision we scored
    and re-read + re-score whenever a late bid moved it.
    """
    for _ in range(_CLOSE_GUARDED_ATTEMPTS):
        if await _close_bid_window_once(job_id, guard_revision=True):
            return
    logger.warning(
        "Bid-window close for job %s lost the revision guard %d times; closing without it (a bid landing "
        "after this final read is notified as a loser but not scored)",
        job_id,
        _CLOSE_GUARDED_ATTEMPTS,
    )
    await _close_bid_window_once(job_id, guard_revision=False)


def _flip_lost(job_id: str) -> bool:
    """After a failed close flip: True if another path already transitioned the
    job (done), False if the job is still OPEN — a late bid bumped ``bidsRev``
    and the caller should re-read and re-score."""
    job = _get_job(job_id)
    return not job or job.get("status") != "OPEN"


async def _close_bid_window_once(job_id: str, guard_revision: bool) -> bool:
    """One read→score→flip attempt. Returns True when the close is finished
    (this attempt won the flip, or another path already transitioned the job);
    False when the revision guard failed and the attempt should be retried."""
    job = _get_job(job_id)
    if not job or job.get("status") != "OPEN":
        return True  # already transitioned (cancel, sweeper/timer race)
    mailbox = get_daemon_mailbox()
    ws = get_town_agent_ws_manager()
    consumer_id = job["consumerId"]
    max_price = int(job.get("maxPrice", 0))
    bids: dict[str, dict] = job.get("bids") or {}

    extra_condition: str | None = None
    extra_values: dict[str, Any] | None = None
    if guard_revision:
        rev = int(job.get("bidsRev", 0))
        if rev > 0:
            extra_condition = "bidsRev = :rev"
            extra_values = {":rev": rev}
        else:
            extra_condition = "attribute_not_exists(bidsRev)"

    # Disqualify bidders that are no longer connected (spec: can't deliver if gone).
    live_bids = {aid: bid for aid, bid in bids.items() if ws.get_by_agent_id(aid) is not None}
    svc = TownService()
    trust_by_agent = {}
    for aid in live_bids:
        bidder = svc.get_town_agent_by_id(aid) or {}
        trust_by_agent[aid] = float(bidder.get("trustScore", exchange_scoring.INITIAL_TRUST))

    winner = exchange_scoring.choose_winner(live_bids, max_price, trust_by_agent, job.get("strategy", "balanced"))

    if winner is None:
        flipped = _flip_status(job_id, "OPEN", "EXPIRED", extra_condition=extra_condition, extra_values=extra_values)
        if not flipped:
            return _flip_lost(job_id)
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
        # Post-flip bids map: under the revision guard it equals the scored
        # snapshot; on the unguarded fallback it also covers any late racer.
        post_flip_bids: dict = flipped.get("bids") or {}
        for aid in post_flip_bids:
            mailbox.notify(aid, {"event": "bid_lost", "job_id": job_id, "title": job.get("title", "")})
            _apply_mood(aid, "bid_lost")
        _emit_economy_event(
            "economy.job_expired",
            summary=f'"{job.get("title", "")}" expired with no takers',
            location=EXCHANGE_LOCATION,
            job_id=job_id,
            consumer_id=consumer_id,
        )
        return True

    agreed = int(live_bids[winner]["price"])
    provider_name = str(live_bids[winner].get("agentName", ""))
    flipped = _flip_status(
        job_id,
        "OPEN",
        "AWARDED",
        {
            "providerId": winner,
            "providerName": provider_name,
            "agreedPrice": agreed,
            "deliveryDeadlineAt": Decimal(str(time.time() + DELIVERY_SEC)),
        },
        extra_condition=extra_condition,
        extra_values=extra_values,
    )
    if not flipped:
        return _flip_lost(job_id)
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
    # W3: include success criteria in the winner's copy (alongside description).
    winner_awarded: dict[str, Any] = {**awarded, "description": job.get("description", "")}
    success_criteria = job.get("successCriteria")
    if success_criteria:
        winner_awarded["success_criteria"] = success_criteria
    delivered = mailbox.notify(winner, winner_awarded)
    if not delivered:
        # AWARDED jobs aren't on the public board, so a winner whose
        # connection went stale at award time has no other way to learn
        # they won. ``resync_agent_jobs`` (called on agent connect) re-sends
        # this exact event when the daemon comes back.
        logger.warning(
            "job_awarded notify failed for winner %s on job %s — will heal on reconnect resync",
            winner[:8],
            job_id,
        )
    # Consumer copy includes updated balance when a partial escrow refund was issued.
    consumer_awarded = (
        awarded if consumer_balance_after_award is None else {**awarded, "balance": consumer_balance_after_award}
    )
    mailbox.notify(consumer_id, consumer_awarded)
    # Post-flip bids map for loser notifications: under the revision guard it
    # equals the scored snapshot; on the unguarded fallback it also covers any
    # late racer (notified as a loser, not scored).
    post_flip_bids = flipped.get("bids") or {}
    for aid in post_flip_bids:
        if aid != winner:
            mailbox.notify(aid, {"event": "bid_lost", "job_id": job_id, "title": job.get("title", "")})
            _apply_mood(aid, "bid_lost")
    _emit_economy_event(
        "economy.job_awarded",
        summary=f'{provider_name} won "{job.get("title", "")}" for {agreed} crumbs',
        location=EXCHANGE_LOCATION,
        job_id=job_id,
        provider_name=provider_name,
        consumer_name=job.get("consumerName", ""),
        agreed_price=agreed,
    )

    _schedule(job_id, DELIVERY_SEC, _delivery_timeout)
    return True


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
    # ESCROW_REFUND: counterparty is the provider (consumer is being refunded due to provider failure)
    consumer_balance = credit_wallet(consumer_id, agreed, "ESCROW_REFUND", job_id, counterparty_id=provider_id)
    _update_trust(provider_id, "FAILURE_PROVIDER", completed_delta=0, failed_delta=1, counterparty_id=consumer_id)
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
    _emit_economy_event(
        "economy.job_failed",
        summary=f'"{job.get("title", "")}" fell through',
        location=EXCHANGE_LOCATION,
        job_id=job_id,
        provider_id=provider_id,
        consumer_id=consumer_id,
    )


async def _review_timeout(job_id: str) -> None:
    """Consumer never reviewed: auto-accept (protects providers, AEX SUCCESS_AUTO)."""
    job = _get_job(job_id)
    if not job or job.get("status") != "DELIVERED":
        return
    await _settle(job, "SUCCESS_AUTO")


async def _settle(
    job: dict,
    outcome: str,
    reason: str = "",
    rejected_by_consumer: bool = False,
    criteria_verdicts: list[bool] | None = None,
) -> bool:
    """Single settlement path for accept / auto-accept / reject.

    Returns True if this call won the DELIVERED→terminal status flip (and
    therefore moved money / updated trust), False if another path (review
    verb vs auto-accept timer) settled the job first.
    """
    job_id = job["id"]
    # W3: SUCCESS_PARTIAL is treated as success for payment (COMPLETED status)
    # but with a lower trust attribution score (0.6 vs 1.0).
    success = outcome in ("SUCCESS", "SUCCESS_AUTO", "SUCCESS_PARTIAL")
    set_extra: dict[str, Any] = {"outcome": outcome}
    # W3: persist criteria_verdicts on the job row when provided.
    if criteria_verdicts is not None:
        set_extra["criteriaVerdicts"] = criteria_verdicts
    if not _flip_status(job_id, "DELIVERED", "COMPLETED" if success else "FAILED", set_extra):
        return False  # another path (review vs auto-accept timer) won the race
    _cancel_timer(job_id)
    mailbox = get_daemon_mailbox()
    agreed = int(job.get("agreedPrice", 0))
    provider_id = job.get("providerId", "")
    consumer_id = job["consumerId"]

    if success:
        fee = exchange_scoring.platform_fee(agreed)
        # PAYOUT: counterparty is the consumer (they commissioned the work)
        provider_balance = credit_wallet(provider_id, agreed - fee, "PAYOUT", job_id, counterparty_id=consumer_id)
        # TOWN has no running balance — balanceAfter 0 by convention.
        _ledger_append(TOWN_ACCOUNT, "FEE", fee, 0, job_id)
        _update_trust(provider_id, outcome, completed_delta=1, failed_delta=0, counterparty_id=consumer_id)
        _apply_mood(provider_id, "job_completed_provider")
        _apply_mood(consumer_id, "job_completed_consumer")
        completed: dict[str, Any] = {
            "event": "job_completed",
            "job_id": job_id,
            "title": job.get("title", ""),
            "payout": agreed - fee,
            "fee": fee,
            "provider_id": provider_id,
            "consumer_id": consumer_id,
        }
        # W3: include criteria_verdicts in settlement events when present.
        if criteria_verdicts is not None:
            completed["criteria_verdicts"] = criteria_verdicts
        # Provider copy includes updated balance; consumer copy does not (consumer wallet
        # didn't move at settle — it moved at award when the partial refund was issued).
        mailbox.notify(provider_id, {**completed, "balance": provider_balance})
        mailbox.notify(consumer_id, completed)
        provider_display = job.get("providerName", provider_id)
        _emit_economy_event(
            "economy.job_completed",
            summary=f'{provider_display} delivered "{job.get("title", "")}" — paid {agreed - fee} crumbs',
            location=EXCHANGE_LOCATION,
            job_id=job_id,
            provider_id=provider_id,
            consumer_id=consumer_id,
            payout=agreed - fee,
        )
    else:
        # ESCROW_REFUND on consumer rejection: counterparty is the provider (who failed)
        consumer_balance = credit_wallet(consumer_id, agreed, "ESCROW_REFUND", job_id, counterparty_id=provider_id)
        _update_trust(provider_id, "FAILURE_PROVIDER", completed_delta=0, failed_delta=1, counterparty_id=consumer_id)
        _apply_mood(provider_id, "job_failed_provider")
        if rejected_by_consumer:
            _bump_counter(consumer_id, "rejectionsIssued")
        failed: dict[str, Any] = {
            "event": "job_failed",
            "job_id": job_id,
            "title": job.get("title", ""),
            "reason": reason or "rejected",
            "provider_id": provider_id,
            "consumer_id": consumer_id,
        }
        # W3: include criteria_verdicts in failed events when present.
        if criteria_verdicts is not None:
            failed["criteria_verdicts"] = criteria_verdicts
        # Consumer copy includes updated balance (they received the refund); provider's does not.
        mailbox.notify(provider_id, failed)
        mailbox.notify(consumer_id, {**failed, "balance": consumer_balance})
        _emit_economy_event(
            "economy.job_failed",
            summary=f'"{job.get("title", "")}" fell through',
            location=EXCHANGE_LOCATION,
            job_id=job_id,
            provider_id=provider_id,
            consumer_id=consumer_id,
        )
    return True


# ---------------------------------------------------------------------------
# Sweeper (restart safety — deadlines are persisted, timers are not)
# ---------------------------------------------------------------------------

_SWEEP_CHECKS: tuple[tuple[str, str, Callable[[str], Awaitable[None]]], ...] = (
    ("OPEN", "bidWindowEndsAt", _close_bid_window),
    ("AWARDED", "deliveryDeadlineAt", _delivery_timeout),
    ("DELIVERED", "reviewDeadlineAt", _review_timeout),
)


def _jobs_with_status(status: str, limit: int | None = None) -> list[dict]:
    """First page of the status GSI for ``status``.

    Shared by the sweeper and connect-time resync. No pagination by design —
    items beyond the first page are picked up on a later pass (sweeper) or
    the next reconnect (resync). Acceptable at town scale where the number
    of concurrently open/awarded/delivered jobs is small.

    When ``limit`` is given, the query is bounded (newest-first via
    ``ScanIndexForward=False``) so callers that only need a recent window
    (e.g. the terminal-outcome resync) don't pull arbitrarily many rows.
    """
    kwargs: dict[str, Any] = {
        "IndexName": "status-createdAt-index",
        "KeyConditionExpression": Key("status").eq(status),
    }
    if limit is not None:
        kwargs["Limit"] = limit
        kwargs["ScanIndexForward"] = False
    resp = dynamo.jobs_table().query(**kwargs)
    return resp.get("Items", [])


async def sweep_overdue_jobs() -> None:
    """Fire any overdue deadline transition found via the status GSI.

    Every transition is a status-conditional flip, so racing an in-memory
    timer (or a concurrent sweep) is safe — exactly one side wins.

    Crash-window note (accepted at current scale):
    (a) This function only retries *deadline transitions* (status flips). Each
        money movement is itself atomic — the wallet update and its ledger row
        ride in one TransactWriteItems via ``_wallet_transact``, so crumbs can
        never move without an audit row. The remaining window is a crash
        between a status flip and its money movement (PAYOUT / ESCROW_REFUND):
        that strands the escrow detectably — the ledger will have no PAYOUT or
        ESCROW_REFUND row whose refId matches the job_id — but the sweep does
        not self-heal that gap. Reconcile-on-sweep is the upgrade path when
        traffic warrants it.
    (b) Each GSI query fetches only the first page (no pagination). Items that
        span multiple pages are handled on a later pass. Acceptable at town scale
        where the number of concurrently open/awarded/delivered jobs is small.
    """
    now = time.time()
    for status, deadline_attr, transition in _SWEEP_CHECKS:
        for item in _jobs_with_status(status):
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
# Resync on connect (missed-notify safety)
# ---------------------------------------------------------------------------


def _try_attach_balance(evt: dict, agent_id: str) -> bool:
    """Best-effort wallet-balance attach for resync events.

    Resync must never fail because the balance read hiccuped — the event is
    still useful without it. Returns True when attached (caller stops trying
    for the rest of the resync pass).
    """
    try:
        evt["balance"] = get_wallet(agent_id)["balance"]
        return True
    except Exception:
        logger.debug("Balance attach failed during resync for %s", agent_id[:8])
        return False


def resync_agent_jobs(agent_id: str) -> int:
    """Re-send action-required job events this agent may have missed.

    ``mailbox.notify`` at award/deliver time is fire-and-forget: if the
    agent's connection went stale at that exact moment, the event is lost —
    and AWARDED jobs aren't on the public board, so a winner that missed
    ``job_awarded`` would eat the delivery-deadline failure + trust hit
    without ever knowing it won. A daemon restart similarly loses the local
    TOWN_JOBS cache. Called from the agent-connect path (after the
    ``connected`` event) to heal both gaps:

    * providerId == agent_id, status AWARDED → re-notify ``job_awarded``
      with the same payload shape as the winner copy at award time, with
      ``deadline_sec`` recomputed against the persisted deadline.
    * consumerId == agent_id, status DELIVERED → re-notify ``job_delivered``
      (regenerating the 24 h presigned artifact GET when present).
    * providerId == agent_id, status DELIVERED → nothing: the provider's
      waiting-on-review state needs no action.
    * agent_id in bids AND providerId != agent_id, status in
      {AWARDED, COMPLETED, FAILED, EXPIRED} → re-notify ``bid_lost`` so a
      bidder that was offline when the window closed clears its stale [BID]
      cache entry.
    * agent_id in bids, status CANCELLED → re-notify ``job_cancelled`` so
      the same offline-bidder gap is covered for consumer-withdrawn jobs.

    Every re-sent event carries ``"resync": True`` so the daemon can update
    its cache without duplicating TOWN_EVENTS lines. Returns the number of
    events delivered. Never raises — resync must never break connect.

    Note: the bid_lost / job_cancelled branches each issue one GSI page query
    per terminal status (4 statuses + CANCELLED = 5 extra queries per connect).
    This is acceptable at GooseTown's town scale where concurrent open jobs
    are in the tens, not thousands.
    """
    try:
        mailbox = get_daemon_mailbox()
        now = time.time()
        sent = 0

        # Wave 2 (spec §3.5, codex P1): reconstruct a lost OPEN-job offer. The
        # `offeredTo` set is the durable authz record; the post-time `job_offered`
        # notify is best-effort, so a provider that was stale at offer time would
        # have a bidding right with no local TOWN_OFFERS entry. Re-send the offer
        # for any OPEN job where this agent is in `offeredTo` (and isn't the
        # consumer — the matcher excludes the consumer, but guard anyway).
        for job in _jobs_with_status("OPEN"):
            offered_to = job.get("offeredTo") or set()
            if agent_id not in offered_to or job.get("consumerId") == agent_id:
                continue
            offer_event: dict[str, Any] = {
                "event": "job_offered",
                "job_id": job["id"],
                "title": job.get("title", ""),
                "category": job.get("category", "general"),
                "max_price": int(job.get("maxPrice", 0)),
                "description": (job.get("description") or "")[:300],
                "consumer_name": job.get("consumerName", ""),
                "resync": True,
            }
            success_criteria = job.get("successCriteria")
            if success_criteria:
                offer_event["criteria"] = success_criteria
            if mailbox.notify(agent_id, offer_event):
                sent += 1

        for job in _jobs_with_status("AWARDED"):
            if job.get("providerId") == agent_id:
                # Winner re-notification — existing path.
                awarded: dict[str, Any] = {
                    "event": "job_awarded",
                    "job_id": job["id"],
                    "title": job.get("title", ""),
                    "agreed_price": int(job.get("agreedPrice", 0)),
                    "deadline_sec": max(0, int(float(job.get("deliveryDeadlineAt") or 0) - now)),
                    "provider_name": job.get("providerName", ""),
                    "consumer_name": job.get("consumerName", ""),
                    "provider_id": agent_id,
                    "consumer_id": job.get("consumerId", ""),
                    "description": job.get("description", ""),
                    "resync": True,
                }
                success_criteria = job.get("successCriteria")
                if success_criteria:
                    awarded["success_criteria"] = success_criteria
                if mailbox.notify(agent_id, awarded):
                    sent += 1
            elif agent_id in (job.get("bids") or {}):
                # Offline loser: bid window closed and someone else won — re-notify bid_lost.
                if mailbox.notify(
                    agent_id,
                    {
                        "event": "bid_lost",
                        "job_id": job["id"],
                        "title": job.get("title", ""),
                        "resync": True,
                    },
                ):
                    sent += 1

        for job in _jobs_with_status("DELIVERED"):
            if job.get("consumerId") != agent_id:
                continue
            delivered_event: dict[str, Any] = {
                "event": "job_delivered",
                "job_id": job["id"],
                "title": job.get("title", ""),
                "deliverable": job.get("deliverable", ""),
                "provider_id": job.get("providerId", ""),
                "consumer_id": agent_id,
                "resync": True,
            }
            success_criteria = job.get("successCriteria")
            if success_criteria:
                delivered_event["success_criteria"] = success_criteria
            artifact_key = job.get("artifactKey")
            if artifact_key:
                # Same shape deliver_job sends: regenerate the 24 h presigned
                # GET, pinned to the version captured at deliver time. Presign
                # failure must not strand the resync — fall back to name-only.
                try:
                    delivered_event["artifact_url"] = presign_artifact_download(
                        artifact_key, version_id=job.get("artifactVersionId")
                    )
                except Exception:
                    logger.exception("Artifact presign failed during resync for job %s", job["id"])
                delivered_event["artifact_name"] = artifact_key.rsplit("/", 1)[-1]
            if mailbox.notify(agent_id, delivered_event):
                sent += 1

        # Terminal-outcome resync for the two principals (provider + consumer).
        # Bounded to the 50 most-recent jobs per status so a busy exchange doesn't
        # produce unbounded resync traffic on every reconnect.
        #
        # Balance is attached ONCE on the first money-relevant resync event per
        # agent (provider job_completed, consumer job_failed/job_expired).  We use
        # get_wallet(agent_id)["balance"] — current truth — rather than trying to
        # reconstruct per-job deltas, which could go wrong if multiple jobs settled
        # while the agent was offline.
        balance_sent = False  # guard: attach balance at most once per resync call

        for terminal_status in ("COMPLETED", "FAILED", "EXPIRED"):
            for job in _jobs_with_status(terminal_status, limit=50):
                job_id = job["id"]
                provider_id = job.get("providerId", "")
                consumer_id = job.get("consumerId", "")

                if provider_id == agent_id:
                    # Provider (winner) side — re-send settlement outcome.
                    if terminal_status == "COMPLETED":
                        agreed = int(job.get("agreedPrice", 0))
                        fee = exchange_scoring.platform_fee(agreed)
                        payout = agreed - fee
                        evt: dict[str, Any] = {
                            "event": "job_completed",
                            "job_id": job_id,
                            "title": job.get("title", ""),
                            "payout": payout,
                            "fee": fee,
                            "provider_id": provider_id,
                            "consumer_id": consumer_id,
                            "resync": True,
                        }
                        verdicts = job.get("criteriaVerdicts")
                        if verdicts is not None:
                            evt["criteria_verdicts"] = verdicts
                        # First money-relevant event for this agent: attach current balance.
                        if not balance_sent:
                            balance_sent = _try_attach_balance(evt, agent_id)
                        if mailbox.notify(agent_id, evt):
                            sent += 1
                    elif terminal_status == "FAILED":
                        evt = {
                            "event": "job_failed",
                            "job_id": job_id,
                            "title": job.get("title", ""),
                            "outcome": job.get("outcome", ""),
                            "provider_id": provider_id,
                            "consumer_id": consumer_id,
                            "resync": True,
                        }
                        verdicts = job.get("criteriaVerdicts")
                        if verdicts is not None:
                            evt["criteria_verdicts"] = verdicts
                        if mailbox.notify(agent_id, evt):
                            sent += 1
                    # EXPIRED with provider == agent_id is a job that expired before
                    # award (no winner), so providerId shouldn't be set; skip.
                    continue

                if consumer_id == agent_id:
                    if terminal_status == "COMPLETED":
                        agreed = int(job.get("agreedPrice", 0))
                        fee = exchange_scoring.platform_fee(agreed)
                        payout = agreed - fee
                        evt = {
                            "event": "job_completed",
                            "job_id": job_id,
                            "title": job.get("title", ""),
                            "payout": payout,
                            "fee": fee,
                            "provider_id": provider_id,
                            "consumer_id": consumer_id,
                            "resync": True,
                        }
                        verdicts = job.get("criteriaVerdicts")
                        if verdicts is not None:
                            evt["criteria_verdicts"] = verdicts
                        if mailbox.notify(agent_id, evt):
                            sent += 1
                    elif terminal_status == "FAILED":
                        evt = {
                            "event": "job_failed",
                            "job_id": job_id,
                            "title": job.get("title", ""),
                            "outcome": job.get("outcome", ""),
                            "provider_id": provider_id,
                            "consumer_id": consumer_id,
                            "resync": True,
                        }
                        verdicts = job.get("criteriaVerdicts")
                        if verdicts is not None:
                            evt["criteria_verdicts"] = verdicts
                        # Consumer receives money on failure (escrow refund) — attach balance.
                        if not balance_sent:
                            balance_sent = _try_attach_balance(evt, agent_id)
                        if mailbox.notify(agent_id, evt):
                            sent += 1
                    elif terminal_status == "EXPIRED":
                        evt = {
                            "event": "job_expired",
                            "job_id": job_id,
                            "title": job.get("title", ""),
                            "provider_id": provider_id,
                            "consumer_id": consumer_id,
                            "resync": True,
                        }
                        # Consumer receives escrow refund on expiry — attach balance.
                        if not balance_sent:
                            balance_sent = _try_attach_balance(evt, agent_id)
                        if mailbox.notify(agent_id, evt):
                            sent += 1
                    continue

                # Neither principal — check if this agent was a losing bidder.
                bids = job.get("bids") or {}
                if agent_id not in bids:
                    continue
                if provider_id == agent_id:
                    # Already handled above (winner); skip bid_lost.
                    continue
                if mailbox.notify(
                    agent_id,
                    {
                        "event": "bid_lost",
                        "job_id": job_id,
                        "title": job.get("title", ""),
                        "resync": True,
                    },
                ):
                    sent += 1

        # Job-cancelled resync: consumer withdrew the OPEN job while the bidder
        # was offline. CANCELLED jobs have no providerId (no winner was ever chosen).
        for job in _jobs_with_status("CANCELLED"):
            bids = job.get("bids") or {}
            if agent_id not in bids:
                continue
            if mailbox.notify(
                agent_id,
                {
                    "event": "job_cancelled",
                    "job_id": job["id"],
                    "title": job.get("title", ""),
                    "consumer_id": job.get("consumerId", ""),
                    "resync": True,
                },
            ):
                sent += 1

        if sent:
            logger.info("[agent:%s] exchange resync re-sent %d job event(s)", agent_id[:8], sent)
        return sent
    except Exception:
        logger.warning("Exchange resync failed for agent %s (non-fatal)", agent_id[:8], exc_info=True)
        return 0


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
            # Epoch seconds; frontend's town-clock strip drains against this.
            "bid_window_ends_at": float(item.get("bidWindowEndsAt") or 0) or None,
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
