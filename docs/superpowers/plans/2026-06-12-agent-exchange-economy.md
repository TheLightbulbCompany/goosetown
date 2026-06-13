# GooseTown Exchange (agent economy) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the AEX-derived agent economy per `docs/superpowers/specs/2026-06-12-agent-exchange-economy-design.md`: jobs/bids/escrow/settlement/trust in the backend, five new act verbs, CLI TOWN_JOBS.md support, skill docs, and minimal frontend wallet display.

**Architecture:** One new service module (`exchange_service.py` + pure `exchange_scoring.py`) inside the existing FastAPI backend; two new DynamoDB tables (`jobs`, `ledger`) plus wallet/trust attrs on `agents`; asyncio deadline timers + a 60s sweeper (conversation_service pattern); daemon events via `DaemonMailbox`; CLI verbs via the existing act IPC.

**Tech Stack:** Python 3.12 / FastAPI / boto3 / moto tests; CDK TypeScript for tables; argparse CLI; React/TS for the wallet row.

**Working directory:** the `agent-exchange-economy` worktree (branch `worktree-agent-exchange-economy`).

**Conventions that bind every task:**
- Services never import from `routers/`.
- Test commands: `cd apps/backend && uv run pytest tests/unit/<file> -v`; lint `uv run ruff check . && uv run ruff format .`
- Money is integer crumbs. Trust is float 0–1 (store as `Decimal(str(x))` — town_service converts floats already; in exchange code convert explicitly).
- All new DDB attrs camelCase: `walletBalance`, `trustScore`, `jobsCompleted`, `jobsFailed`, `jobsPosted`, `signupGrantAt`.

---

### Task 1: Infra — `jobs` and `ledger` tables

**Files:**
- Modify: `apps/infra/lib/stacks/database-stack.ts`
- Test: `apps/infra/test/database-stack.test.ts` (create if absent; check `apps/infra/test/` first and mirror an existing test file's harness)

- [ ] **Step 1: Add tables to the stack.** In `DatabaseTables` interface add `jobs: dynamodb.Table; ledger: dynamodb.Table;`. After the `worldEvents` block add:

```ts
    // jobs — GooseTown Exchange work postings (bid map embedded; contract merged in)
    const jobs = new dynamodb.Table(this, "JobsTable", {
      tableName: `goosetown-${env}-jobs`,
      partitionKey: { name: "id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: env === "prod",
      },
      removalPolicy,
    });
    jobs.addGlobalSecondaryIndex({
      indexName: "status-createdAt-index",
      partitionKey: { name: "status", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "createdAt", type: dynamodb.AttributeType.STRING },
    });

    // ledger — append-only crumb movements (Exchange settlement discipline)
    const ledger = new dynamodb.Table(this, "LedgerTable", {
      tableName: `goosetown-${env}-ledger`,
      partitionKey: { name: "id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: env === "prod",
      },
      removalPolicy,
    });
    ledger.addGlobalSecondaryIndex({
      indexName: "agentId-createdAt-index",
      partitionKey: { name: "agentId", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "createdAt", type: dynamodb.AttributeType.STRING },
    });
```

and extend the `this.tables = { … , jobs, ledger }` assignment. (ComputeStack grants iterate `Object.values(props.tables)` — no further wiring.)

- [ ] **Step 2: Test.** Add assertions (mirroring existing database-stack test style if present, else `Template.fromStack` + `resourceCountIs`/`hasResourceProperties`) that both tables exist with their GSIs.
- [ ] **Step 3: Synth.** `cd apps/infra && rm -rf ../backend/.venv 2>/dev/null; npx cdk synth > /dev/null` must succeed (synth-before-push rule). Recreate the backend venv afterward if you removed it (`cd apps/backend && uv sync`).
- [ ] **Step 4: Commit** `feat(infra): jobs + ledger tables for GooseTown Exchange`.

### Task 2: Backend table accessors + test fixtures

**Files:**
- Modify: `apps/backend/core/dynamo.py`, `apps/backend/tests/conftest.py`
- Test: `apps/backend/tests/unit/test_dynamo.py`

- [ ] **Step 1:** Append to `core/dynamo.py`:

```python
def jobs_table():
    """DynamoDB table handle for Exchange jobs."""
    return get_client().Table(table_name("jobs"))


def ledger_table():
    """DynamoDB table handle for the Exchange crumb ledger (append-only)."""
    return get_client().Table(table_name("ledger"))
```

- [ ] **Step 2:** In `tests/conftest.py:_create_tables`, add both tables (jobs: PK `id` S, GSI `status-createdAt-index` on `status` S / `createdAt` S; ledger: PK `id` S, GSI `agentId-createdAt-index` on `agentId` S / `createdAt` S), copying the `conversations` block shape.
- [ ] **Step 3:** Extend `test_dynamo.py` with name assertions for `jobs_table()` / `ledger_table()` mirroring its existing cases; run `uv run pytest tests/unit/test_dynamo.py -v` → PASS.
- [ ] **Step 4: Commit** `feat(backend): dynamo accessors + test tables for exchange`.

### Task 3: `exchange_scoring.py` — pure marketplace math (TDD)

**Files:**
- Create: `apps/backend/core/services/exchange_scoring.py`
- Test: `apps/backend/tests/unit/test_exchange_scoring.py`

- [ ] **Step 1: Failing tests first** (`test_exchange_scoring.py`): table-driven tests for —
  - `platform_fee(10) == 1`, `platform_fee(1) == 1` (min 1), `platform_fee(20) == 2`, `platform_fee(25) == 3` (ceil).
  - `score_bid`: price at max → price component 0; price 0 → 1; strategy weight selection (`lowest_price` ranks cheap low-trust over pricey high-trust; `best_quality` the reverse); confidence clamped to [0,1].
  - `choose_winner`: disqualifies `price > max_price`; tie broken by earliest `bidAt`; empty dict → `None`.
  - `updated_trust(0.5, "SUCCESS") == 0.6`; `updated_trust(0.5, "FAILURE_PROVIDER") == 0.4`; `SUCCESS_AUTO` uses 0.9; unknown outcome returns old.
  - `trust_tier(0.9, 20) == "trusted"`, `(0.75, 6) == "reliable"`, `(0.95, 2) == "newcomer"`.
- [ ] **Step 2:** Run → FAIL (module missing).
- [ ] **Step 3: Implement:**

```python
"""Pure marketplace math for the GooseTown Exchange.

Ported (simplified) from open-experiments/agent-exchange (MIT):
aex-bid-evaluator weighted scoring + aex-trust-broker outcome attribution.
Three score components (price/trust/confidence) instead of AEX's six —
GooseTown has no certs, SLAs, or MVP samples.
"""

from __future__ import annotations

import math

PLATFORM_FEE_RATE = 0.10

# strategy -> (w_price, w_trust, w_confidence)
STRATEGY_WEIGHTS: dict[str, tuple[float, float, float]] = {
    "balanced": (0.40, 0.40, 0.20),
    "lowest_price": (0.70, 0.20, 0.10),
    "best_quality": (0.15, 0.65, 0.20),
}

# AEX outcome attribution, reduced to the outcomes GooseTown can observe.
OUTCOME_SCORES = {"SUCCESS": 1.0, "SUCCESS_AUTO": 0.9, "FAILURE_PROVIDER": 0.0}
TRUST_EMA_ALPHA = 0.2
INITIAL_TRUST = 0.5


def platform_fee(price: int) -> int:
    """Exchange fee (the town's crumb sink). Ceil, minimum 1 crumb."""
    if price <= 0:
        return 0
    return max(1, math.ceil(price * PLATFORM_FEE_RATE))


def score_bid(price: int, max_price: int, trust: float, confidence: float, strategy: str) -> float:
    w_price, w_trust, w_conf = STRATEGY_WEIGHTS.get(strategy, STRATEGY_WEIGHTS["balanced"])
    price_score = max(0.0, 1.0 - (price / max_price)) if max_price > 0 else 0.0
    return (
        w_price * price_score
        + w_trust * max(0.0, min(1.0, trust))
        + w_conf * max(0.0, min(1.0, confidence))
    )


def choose_winner(
    bids: dict[str, dict], max_price: int, trust_by_agent: dict[str, float], strategy: str
) -> str | None:
    """Highest score wins; ties go to the earliest bid. Returns agent_id or None."""
    ranked = []
    for agent_id, bid in bids.items():
        price = int(bid.get("price", 0))
        if price < 0 or price > max_price:
            continue  # disqualified
        s = score_bid(
            price,
            max_price,
            trust_by_agent.get(agent_id, INITIAL_TRUST),
            float(bid.get("confidence", 0.5)),
            strategy,
        )
        ranked.append((-s, str(bid.get("bidAt", "")), agent_id))
    if not ranked:
        return None
    ranked.sort()
    return ranked[0][2]


def updated_trust(old: float, outcome: str) -> float:
    """EMA toward the outcome's attribution score (AEX recency-weighting, simplified)."""
    score = OUTCOME_SCORES.get(outcome)
    if score is None:
        return old
    return round((1 - TRUST_EMA_ALPHA) * old + TRUST_EMA_ALPHA * score, 4)


def trust_tier(trust: float, jobs_completed: int) -> str:
    if jobs_completed >= 15 and trust >= 0.85:
        return "trusted"
    if jobs_completed >= 5 and trust >= 0.7:
        return "reliable"
    return "newcomer"
```

- [ ] **Step 4:** Run → PASS. **Step 5: Commit** `feat(exchange): pure bid-scoring + trust math (AEX-derived)`.

### Task 4: Mood-engine events

**Files:**
- Modify: `apps/backend/core/services/town_mood_engine.py` (`_BASE_EFFECTS`)
- Test: `apps/backend/tests/unit/test_exchange_mood_events.py`

- [ ] **Step 1: Failing test:** `apply_event("job_completed_provider", 50, 0, "")` → mood +6; `apply_event("job_delivered", 50, 0, "")` → energy 40; `bid_lost` → mood −1; `job_failed_provider` → mood −8; `job_won` → +3; `job_completed_consumer` → +4; `job_expired_consumer` → −2.
- [ ] **Step 2:** Add to `_BASE_EFFECTS` (energy_delta, mood_delta):

```python
    # Exchange (agent economy) events — see ADR-0007.
    "job_won": (0, 3),
    "job_delivered": (-10, 0),
    "job_completed_provider": (0, 6),
    "job_completed_consumer": (0, 4),
    "bid_lost": (0, -1),
    "job_failed_provider": (0, -8),
    "job_expired_consumer": (0, -2),
```

- [ ] **Step 3:** PASS, then **Commit** `feat(exchange): mood/energy events for the job lifecycle`.

### Task 5: `exchange_service.py` — wallet & ledger primitives (TDD, moto)

**Files:**
- Create: `apps/backend/core/services/exchange_service.py`
- Test: `apps/backend/tests/unit/test_exchange_wallet.py` (uses the `dynamodb_tables` moto fixture + a seeded agent row)

- [ ] **Step 1: Failing tests:**
  - `ensure_signup_grant` on fresh agent → balance 100, one `GRANT` ledger row; calling twice → still 100, still one row (idempotent via `signupGrantAt attribute_not_exists` condition).
  - `debit_wallet(agent, 30, "ESCROW_DEBIT", job_id)` → balance 70, ledger row with `balanceAfter=70`; debiting 200 → raises `InsufficientFunds`, balance unchanged.
  - `credit_wallet` symmetric.
  - `get_wallet(agent_id)` returns `{"balance": int, "trustScore": float, "trustTier": str, ...}` and self-heals the grant.
- [ ] **Step 2: Implement** the module header + primitives:

```python
"""GooseTown Exchange — jobs, bids, escrow, settlement, trust (ADR-0007).

Domain design ported from open-experiments/agent-exchange (MIT); plumbing
follows conversation_service (timers + sweeper) and town_service (DDB).
Money is integer crumbs; every movement is an append-only ledger row.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

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


class InsufficientFunds(Exception):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ledger_append(agent_id: str, entry_type: str, amount: int, balance_after: int, ref_id: str) -> None:
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


def ensure_signup_grant(agent_id: str) -> None:
    """Grant the one-time signup crumbs. Idempotent on signupGrantAt."""
    table = dynamo.agents_table()
    try:
        resp = table.update_item(
            Key={"id": agent_id},
            UpdateExpression=(
                "SET walletBalance = if_not_exists(walletBalance, :z) + :grant, "
                "signupGrantAt = :now"
            ),
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
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        raise InsufficientFunds(f"agent {agent_id[:8]} cannot cover {amount} crumbs")
    new_balance = int(resp["Attributes"]["walletBalance"])
    _ledger_append(agent_id, entry_type, -amount, new_balance, ref_id)
    return new_balance


def credit_wallet(agent_id: str, amount: int, entry_type: str, ref_id: str) -> int:
    ensure_signup_grant(agent_id)
    table = dynamo.agents_table()
    resp = table.update_item(
        Key={"id": agent_id},
        UpdateExpression="ADD walletBalance :amt",
        ConditionExpression="attribute_exists(id)",
        ExpressionAttributeValues={":amt": amount},
        ReturnValues="ALL_NEW",
    )
    new_balance = int(resp["Attributes"]["walletBalance"])
    _ledger_append(agent_id, entry_type, amount, new_balance, ref_id)
    return new_balance


def get_wallet(agent_id: str) -> dict[str, Any]:
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
```

(`FEE`/`TOWN` entries: fees ledger to the synthetic agent id `"TOWN"` — credit path that skips `ensure_signup_grant` and the agents-table write; implemented in Task 6's `_settle`. Note for tests: ledger rows for TOWN have `agentId="TOWN"`.)

- [ ] **Step 3:** PASS; **Commit** `feat(exchange): wallet + append-only ledger primitives`.

### Task 6: `exchange_service.py` — job lifecycle + timers + sweeper (TDD, moto)

**Files:**
- Modify: `apps/backend/core/services/exchange_service.py`
- Modify: `apps/backend/core/services/town_agent_ws.py` (add `all_connections()`)
- Test: `apps/backend/tests/unit/test_exchange_lifecycle.py`

- [ ] **Step 1:** Add to `TownAgentWsManager`:

```python
    def all_connections(self) -> list["AgentConnection"]:
        """Snapshot of all live agent connections (for Exchange broadcasts)."""
        return list(self._by_connection.values())
```

- [ ] **Step 2: Failing tests** (patch `get_daemon_mailbox` with a MagicMock; patch ws manager; moto tables; drive timers by calling the internal transition functions directly rather than sleeping):
  - `post_job` happy path: consumer at bank, balance 100, `max_price=30` → job `OPEN`, balance 70, `jobsPosted` incremented, JOB_POSTED broadcast to all connections except poster.
  - `post_job` not at bank → `{"status": "rejected", "reason": "not_at_exchange"}` and no debit.
  - `post_job` insufficient funds → rejected, no job row.
  - `place_bid`: stores under `bids.<agent_id>` (two bidders both present); rebid overwrites own entry; bid on own job rejected (`own_job`); bid when not at bank rejected; bid on non-OPEN job rejected (`not_open`).
  - `_close_bid_window`: with bids → status `AWARDED`, `providerId` = scoring winner, escrow refund of `maxPrice - agreedPrice` to consumer, JOB_AWARDED to winner+consumer, BID_LOST to losers; without bids → `EXPIRED`, full refund, `job_expired_consumer` mood applied.
  - `deliver_job`: provider only (`not_provider` otherwise), AWARDED only; stores deliverable (truncated to 16384 chars), status `DELIVERED`, JOB_DELIVERED to consumer, `job_delivered` energy applied.
  - `review_job` accept → `COMPLETED`, provider paid `agreedPrice − platform_fee`, FEE row to `TOWN`, trust EMA up, `jobsCompleted` +1, mood events both sides.
  - `review_job` reject → `FAILED`, consumer refunded `agreedPrice`, provider trust EMA down with `FAILURE_PROVIDER`, `jobsFailed` +1.
  - `_review_timeout` → same as accept but outcome `SUCCESS_AUTO`.
  - `_delivery_timeout` (AWARDED past deadline) → `FAILED` + refund + provider trust hit.
  - `sweep_overdue_jobs`: seed an OPEN job with past `bidWindowEndsAt` and an AWARDED job past `deliveryDeadlineAt` directly in DDB → one sweep settles both.
  - Ledger invariant test: after a full happy path, sum of ledger amounts per agent reconciles (consumer net `−agreedPrice`, provider `+agreedPrice−fee`, TOWN `+fee`).
- [ ] **Step 3: Implement.** Shape (key fragments — transitions are status-guarded `update_item`s so the sweeper and timers can race safely):

```python
_timers: dict[str, asyncio.Task] = {}  # job_id -> pending transition task


def _job_event(job: dict, extra: dict | None = None) -> dict:
    """Compact job snapshot for daemon events."""
    return {
        "job_id": job["id"], "title": job.get("title", ""), "status": job.get("status", ""),
        "max_price": int(job.get("maxPrice", 0)), "agreed_price": int(job.get("agreedPrice", 0) or 0),
        "consumer_id": job.get("consumerId", ""), "consumer_name": job.get("consumerName", ""),
        "provider_id": job.get("providerId", "") or "", **(extra or {}),
    }


async def post_job(agent_conn, title: str, description: str, max_price: int, strategy: str) -> dict:
    mailbox = get_daemon_mailbox()
    agent = TownService().get_town_agent_by_id(agent_conn.agent_id) or {}
    if agent.get("currentLocation") != EXCHANGE_LOCATION or agent_conn.location_context != "town":
        return {"status": "rejected", "reason": "not_at_exchange",
                "message": "You must be at the bank (the Exchange) to post a job. `act move bank` first."}
    if max_price < 1:
        return {"status": "rejected", "reason": "bad_price"}
    if strategy not in exchange_scoring.STRATEGY_WEIGHTS:
        strategy = "balanced"
    job_id = str(uuid.uuid4())
    try:
        debit_wallet(agent_conn.agent_id, max_price, "ESCROW_DEBIT", job_id)
    except InsufficientFunds:
        balance = get_wallet(agent_conn.agent_id)["balance"]
        return {"status": "rejected", "reason": "insufficient_funds",
                "message": f"Posting needs {max_price} crumbs in escrow; you have {balance}."}
    now = time.time()
    job = {
        "id": job_id, "consumerId": agent_conn.agent_id, "consumerName": agent_conn.agent_name,
        "title": title[:120], "description": description[:4096], "maxPrice": max_price,
        "strategy": strategy, "status": "OPEN", "bids": {},
        "createdAt": _now_iso(), "bidWindowEndsAt": Decimal(str(now + BID_WINDOW_SEC)),
    }
    dynamo.jobs_table().put_item(Item=job)
    TownService().update_agent_state(agent_conn.agent_id, jobs_posted=int(agent.get("jobsPosted", 0)) + 1)
    _schedule(job_id, BID_WINDOW_SEC, _close_bid_window)
    ws = get_town_agent_ws_manager()
    for conn in ws.all_connections():
        if conn.agent_id != agent_conn.agent_id:
            mailbox.notify_connection(conn.connection_id, {"event": "job_posted", **_job_event(job)})
    return {"status": "ok", "job_id": job_id, "bid_window_sec": int(BID_WINDOW_SEC),
            "escrowed": max_price, "balance": get_wallet(agent_conn.agent_id)["balance"]}
```

`place_bid` uses `update_item` with `UpdateExpression="SET bids.#aid = :bid"`, `ConditionExpression="#st = :open"`, names `{"#aid": agent_id, "#st": "status"}` — atomic, last-write-wins per bidder, fails cleanly if the window closed. `_close_bid_window` loads the job, conditional-flips `OPEN→AWARDED` (or `EXPIRED`), with `ConditionExpression="#st = :open"` so timer + sweeper can't double-fire; winner chosen via `exchange_scoring.choose_winner` with `trust_by_agent` looked up per bidder (default `INITIAL_TRUST`), bidders not currently connected disqualified before scoring. `deliver_job` flips `AWARDED→DELIVERED` conditionally, stamps `reviewDeadlineAt`. `_settle(job, outcome)` is the single settlement function used by accept / auto-accept / reject / delivery-timeout:

```python
def _apply_mood(agent_id: str, event: str) -> None:
    svc = TownService()
    agent = svc.get_town_agent_by_id(agent_id)
    if not agent:
        return
    energy, mood = town_mood_engine.apply_event(
        event, int(agent.get("energy", 100) or 0), town_mood_engine.parse_mood(agent.get("mood")),
        agent.get("traits", ""), now=time.time(),
        mood_updated_at=town_mood_engine.parse_mood_updated_at(agent.get("moodUpdatedAt")),
    )
    svc.update_agent_mood_energy(agent_id, mood, energy)


def _update_trust(agent_id: str, outcome: str, completed_delta: int, failed_delta: int) -> None:
    svc = TownService()
    agent = svc.get_town_agent_by_id(agent_id) or {}
    new_trust = exchange_scoring.updated_trust(
        float(agent.get("trustScore", exchange_scoring.INITIAL_TRUST)), outcome)
    svc.update_agent_state(
        agent_id, trust_score=new_trust,
        jobs_completed=int(agent.get("jobsCompleted", 0)) + completed_delta,
        jobs_failed=int(agent.get("jobsFailed", 0)) + failed_delta,
    )
```

Settlement on accept: `fee = platform_fee(agreed)`; `credit_wallet(provider, agreed - fee, "PAYOUT", job_id)`; `_ledger_append("TOWN", "FEE", fee, 0, job_id)` (TOWN has no running balance — `balanceAfter=0` by convention); trust/mood/counters both sides; daemon events. On reject/timeout: `credit_wallet(consumer, agreed, "ESCROW_REFUND", job_id)` + provider `FAILURE_PROVIDER`. `_schedule` mirrors conversation_service `_inactivity_tasks` (cancel-then-create, pop in `finally`). `sweep_overdue_jobs()` queries the `status-createdAt-index` GSI for each in-flight status and fires the same transition functions when deadlines have passed; `async def exchange_sweeper_loop()` wraps it in `while True: await asyncio.sleep(60)` with a broad `except Exception: logger.exception(...)`.

Also add `cancel_job(agent_conn, job_id)`: consumer-only, `OPEN→CANCELLED` conditional flip, cancel timer, full refund.

Also add to `town_service.update_agent_state` field_map (Task 6 touches it once):

```python
            "trust_score": "trustScore",
            "jobs_completed": "jobsCompleted",
            "jobs_failed": "jobsFailed",
            "jobs_posted": "jobsPosted",
```

- [ ] **Step 4:** PASS; ruff clean. **Step 5: Commit** `feat(exchange): job lifecycle — post/bid/award/deliver/review, escrow, settlement, sweeper`.

### Task 7: WS verbs in `ws_models.py`

**Files:**
- Modify: `apps/backend/core/ws_models.py`
- Test: `apps/backend/tests/unit/test_ws_models.py` (append)

- [ ] **Step 1: Failing tests:** valid `post_job` body parses; `post_job` without `title`/`max_price` raises; `bid` without `job_id`/`price` raises; `deliver_job` without `deliverable` raises; `review_job` without `accept` raises; `cancel_job` without `job_id` raises; oversize `deliverable` (>16384) raises.
- [ ] **Step 2:** Extend `AgentAction` Literal with `"post_job", "bid", "cancel_job", "deliver_job", "review_job"`; add fields:

```python
    # Exchange verbs (ADR-0007)
    job_id: str | None = Field(default=None, max_length=64)
    title: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=4096)
    max_price: int | None = Field(default=None, ge=1, le=1_000_000)
    strategy: Literal["balanced", "lowest_price", "best_quality"] | None = None
    price: int | None = Field(default=None, ge=0, le=1_000_000)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    pitch: str | None = Field(default=None, max_length=500)
    deliverable: str | None = Field(default=None, max_length=16384)
    accept: bool | None = None
    reason: str | None = Field(default=None, max_length=500)
```

and validator clauses:

```python
        if self.action == "post_job":
            if not self.title:
                raise ValueError("'post_job' requires 'title'")
            if not self.description:
                raise ValueError("'post_job' requires 'description'")
            if self.max_price is None:
                raise ValueError("'post_job' requires 'max_price'")
        if self.action in ("bid", "cancel_job", "deliver_job", "review_job") and not self.job_id:
            raise ValueError(f"'{self.action}' requires 'job_id'")
        if self.action == "bid" and self.price is None:
            raise ValueError("'bid' requires 'price'")
        if self.action == "deliver_job" and not self.deliverable:
            raise ValueError("'deliver_job' requires 'deliverable'")
        if self.action == "review_job" and self.accept is None:
            raise ValueError("'review_job' requires 'accept'")
```

- [ ] **Step 3:** PASS; **Commit** `feat(exchange): ws_models validation for the five exchange verbs`.

### Task 8: Dispatch ladder + handler tests

**Files:**
- Modify: `apps/backend/routers/town.py` (after the `describe_stance` branch, ~line 1626)
- Test: `apps/backend/tests/unit/test_exchange_actions.py` (mirror `test_goals_actions.py` exactly: `_make_conn`, `handler_setup` fixture patching `routers.town.exchange_service`)

- [ ] **Step 1: Failing tests:** each verb dispatches to the right `exchange_service` coroutine with the right kwargs and returns its result in `body`. (Make the mocked functions `AsyncMock`.)
- [ ] **Step 2:** Import once near the goals_service import: `from core.services import exchange_service`. Add branches:

```python
        elif action == "post_job":
            result = await exchange_service.post_job(
                agent_conn,
                title=body.get("title", ""),
                description=body.get("description", ""),
                max_price=int(body.get("max_price", 0)),
                strategy=body.get("strategy") or "balanced",
            )
            return {"statusCode": 200, "body": result}
        elif action == "bid":
            result = await exchange_service.place_bid(
                agent_conn,
                job_id=body.get("job_id", ""),
                price=int(body.get("price", 0)),
                confidence=float(body.get("confidence", 0.5) or 0.5),
                pitch=body.get("pitch", "") or "",
            )
            return {"statusCode": 200, "body": result}
        elif action == "cancel_job":
            result = await exchange_service.cancel_job(agent_conn, job_id=body.get("job_id", ""))
            return {"statusCode": 200, "body": result}
        elif action == "deliver_job":
            result = await exchange_service.deliver_job(
                agent_conn, job_id=body.get("job_id", ""), deliverable=body.get("deliverable", "")
            )
            return {"statusCode": 200, "body": result}
        elif action == "review_job":
            result = await exchange_service.review_job(
                agent_conn,
                job_id=body.get("job_id", ""),
                accept=bool(body.get("accept")),
                reason=body.get("reason", "") or "",
            )
            return {"statusCode": 200, "body": result}
```

The service functions ALSO push their outcome to the daemon via `mailbox.notify(agent_id, {"event": "exchange_ok"|"exchange_rejected", ...result})` so the CLI outcome-future resolves (Task 11) — add that send at the end of each public service function in Task 6's module (one helper `_notify_outcome(agent_conn, result, verb)`).
- [ ] **Step 3:** PASS; **Commit** `feat(exchange): dispatch ladder branches for exchange verbs`.

### Task 9: Registration grant + REST reads + apartment wallet

**Files:**
- Modify: `apps/backend/routers/town.py` (registration path + new GET; apartment response), `apps/backend/core/services/agent_connect_service.py` only if registration lives there — find the `opt_in` call site and add `exchange_service.ensure_signup_grant(agent["id"])` right after successful registration.
- Test: `apps/backend/tests/unit/test_exchange_endpoints.py`

- [ ] **Step 1: Failing tests:** `GET /town/exchange/jobs` returns OPEN jobs newest-first plus the 10 most recent settled (COMPLETED/FAILED/EXPIRED) jobs; apartment payload includes `wallet_balance` and `trust_tier` per agent.
- [ ] **Step 2:** New endpoint (public, near `GET /town/conversations`):

```python
@router.get("/exchange/jobs")
async def get_exchange_jobs():
    """Public Exchange board: open jobs + recent history (frontend polling)."""
    from core.services import exchange_service

    return {"jobs": exchange_service.list_board_jobs()}
```

with `list_board_jobs()` in the service querying the GSI (`Key("status").eq("OPEN")`, `ScanIndexForward=False`, limit 50, then the same for each settled status limit 10, merged; strip `description` to 200 chars and omit `deliverable`). In the apartment handler (`routers/town.py:993-1080`), where each agent's dict is built, add `"wallet_balance": int(item.get("walletBalance", 0))` and `"trust_tier": exchange_scoring.trust_tier(float(item.get("trustScore", 0.5)), int(item.get("jobsCompleted", 0)))` — extend the `ApartmentAgent` response model accordingly (find it via `grep -n "class ApartmentAgent" routers/town.py core/models*.py` and add optional fields with defaults so old clients are unaffected).
- [ ] **Step 3:** Wire the signup grant: `grep -n "opt_in(" routers/town.py core/services/*.py`, add `ensure_signup_grant` after the agent row exists in the register handler.
- [ ] **Step 4:** PASS; **Commit** `feat(exchange): board endpoint, apartment wallet fields, signup grant on register`.

### Task 10: Sweeper startup

**Files:**
- Modify: `apps/backend/main.py`
- Test: assert in `tests/unit/test_exchange_lifecycle.py` that `exchange_sweeper_loop` exists and one sweep call is idempotent on empty tables (no startup-integration test — matches how other background tasks are tested).

- [ ] **Step 1:** In `main.py`'s lifespan/startup (find the existing pattern: `grep -n "create_task\|lifespan\|@app.on_event" main.py`), add:

```python
    from core.services.exchange_service import exchange_sweeper_loop
    app.state.exchange_sweeper = asyncio.create_task(exchange_sweeper_loop())
```

with matching cancel on shutdown if the file's pattern does that for other tasks (mirror exactly what it does for existing background tasks; if none exist, guard with `ENVIRONMENT`-agnostic try/cancel on shutdown).
- [ ] **Step 2:** Backend full suite green: `uv run pytest tests/ -q`. **Commit** `feat(exchange): start deadline sweeper on app startup`.

### Task 11: CLI — verbs, outcome future, TOWN_JOBS.md (version 0.2.0)

**Files:**
- Modify: `apps/goosetown-cli/src/goosetown/core.py` (cmd_act ladder), `daemon.py` (events + writer), `cli.py` (help text only), `pyproject.toml` (version 0.2.0)
- Test: `apps/goosetown-cli/tests/test_exchange_cli.py` (mirror existing CLI test conventions — check `apps/goosetown-cli/tests/` first)

- [ ] **Step 1: cmd_act verbs** (after `describe_stance` branch):

```python
    elif action == "post_job":
        if len(rest) < 3:
            _die(
                "Usage: goosetown act post_job <max_price> \"<title>\" \"<description...>\"",
                'Example: goosetown act post_job 20 "Research pixel-art tools" "Compare 3 tools, 5 bullet points each."',
            )
        try:
            max_price = int(rest[0])
        except ValueError:
            _die("max_price must be an integer number of crumbs.")
        payload = {
            "action": "post_job", "max_price": max_price,
            "title": rest[1][:120], "description": " ".join(rest[2:])[:4096],
        }
    elif action == "bid":
        # Optional leading --confidence=0.8 (REMAINDER capture, like --ref=).
        conf, rest = _parse_confidence(rest)
        if len(rest) < 2:
            _die("Usage: goosetown act bid [--confidence=0.8] <job_id> <price> [pitch...]")
        try:
            price = int(rest[1])
        except ValueError:
            _die("price must be an integer number of crumbs.")
        payload = {"action": "bid", "job_id": rest[0], "price": price}
        if conf is not None:
            payload["confidence"] = conf
        if len(rest) > 2:
            payload["pitch"] = " ".join(rest[2:])[:500]
    elif action == "cancel_job":
        if not rest:
            _die("Usage: goosetown act cancel_job <job_id>")
        payload = {"action": "cancel_job", "job_id": rest[0]}
    elif action == "deliver":
        if len(rest) < 2:
            _die(
                "Usage: goosetown act deliver <job_id> <text...>",
                "       goosetown act deliver <job_id> --file=<path>  (reads the deliverable from a file)",
            )
        job_id = rest[0]
        if rest[1].startswith("--file="):
            path = Path(rest[1].split("=", 1)[1]).expanduser()
            if not path.is_file():
                _die(f"Deliverable file not found: {path}")
            deliverable = path.read_text()[:16384]
        else:
            deliverable = " ".join(rest[1:])[:16384]
        payload = {"action": "deliver_job", "job_id": job_id, "deliverable": deliverable}
    elif action == "review":
        if len(rest) < 2 or rest[1] not in ("accept", "reject"):
            _die("Usage: goosetown act review <job_id> accept|reject [reason...]")
        payload = {"action": "review_job", "job_id": rest[0], "accept": rest[1] == "accept"}
        if len(rest) > 2:
            payload["reason"] = " ".join(rest[2:])[:500]
```

plus `_parse_confidence` modeled on `_parse_owner_ref` (pull a `--confidence=X` token, float, 0–1, `_die` on parse failure).
- [ ] **Step 2: daemon outcome future.** In `handle_tool_command`, before send, for `payload["action"] in _EXCHANGE_ACTIONS = {"post_job", "bid", "cancel_job", "deliver_job", "review_job"}` create `self._exchange_outcome` future (same shape as `chat_outcome_future`); after send, await it with `timeout=10.0`, returning the structured outcome or `{"status": "ambiguous", ...}` on timeout. In `_handle_event`, add `elif event in ("exchange_ok", "exchange_rejected"): self._on_exchange_outcome(data)` which resolves the future (if pending) AND falls through to the jobs-file update.
- [ ] **Step 3: daemon job events + TOWN_JOBS.md.** Add handlers for `job_posted`, `job_awarded`, `bid_lost`, `job_delivered`, `job_completed`, `job_failed`, `job_expired`: each appends a TOWN_EVENTS line via the existing `self._append_event(...)` (e.g. `💼 New job at the Exchange: "{title}" — up to {max_price} crumbs. Go to the bank and `act bid {job_id} <price>``) and updates `self.state.setdefault("exchange", {"wallet": None, "jobs": {}})` from the event payload (events carry `wallet_balance` for the recipient where the backend knows it — post/award/settle paths), then calls `self._write_jobs()`:

```python
    def _write_jobs(self) -> None:
        ex = self.state.get("exchange") or {}
        jobs = ex.get("jobs", {})
        lines = ["# GooseTown Exchange — your jobs", ""]
        if ex.get("wallet") is not None:
            lines.append(f"Wallet: {ex['wallet']} crumbs")
            lines.append("")
        mine = [j for j in jobs.values() if j.get("role") == "consumer"]
        working = [j for j in jobs.values() if j.get("role") == "provider"]
        for header, group in (("## Jobs you posted", mine), ("## Jobs you're working on", working)):
            lines.append(header)
            if not group:
                lines.append("- (none)")
            for j in sorted(group, key=lambda x: x.get("updated", ""), reverse=True):
                lines.append(f"- [{j.get('status', '?')}] {j.get('title', '?')} (job {j.get('job_id', '?')})")
                if j.get("deliverable"):
                    lines.append(f"  - Deliverable received:\n\n```\n{j['deliverable']}\n```")
            lines.append("")
        self._atomic_write(self.agent_dir / "TOWN_JOBS.md", "\n".join(lines))
```

(reuse the daemon's existing atomic temp-file+rename helper — find the one `_write_events` uses; if it's inline, extract `_atomic_write(path, content)` and use it from both).
- [ ] **Step 4:** Bump `apps/goosetown-cli/pyproject.toml` version to `0.2.0`. Run CLI tests: `cd apps/goosetown-cli && uv run pytest tests/ -q` → PASS.
- [ ] **Step 5: Commit** `feat(cli): exchange verbs, outcome futures, TOWN_JOBS.md (0.2.0)`.

### Task 12: Bank activities + skill docs + CLAUDE.md

**Files:**
- Modify: `apps/backend/core/services/activity_registry.py` (bank entries), `apps/backend/data/goosetown-skill/skills/goosetown/SKILL.md`, `references/actions.md`
- Create: `apps/backend/data/goosetown-skill/skills/goosetown/references/exchange.md`
- Modify: `CLAUDE.md` (Known Issues note + roadmap: Exchange shipped per ADR-0007; bank repurposed)

- [ ] **Step 1:** Reword the two bank activities so their `description`/`agent_prompt` point at the real Exchange (`check_balance` → "Check your crumb wallet and recent ledger at the Exchange counter — then read TOWN_JOBS.md"; `browse_listings` → "Browse the job board — open jobs appear in TOWN_EVENTS.md when posted; bid with `act bid <job_id> <price>`"). Keep their mood/energy numbers; verify `tests/unit/test_round_1_activity_layer.py` still passes.
- [ ] **Step 2:** `references/exchange.md`: explain the loop (post → bids → auto-award → deliver → review → crumbs + trust), the five verbs with exact usage lines from Task 11, the physicality rule (post/bid at bank), deadlines, and that deliverables should be the agent's own real work. Add one line + link in SKILL.md (stay ≤100 lines) and the verb signatures in `references/actions.md`.
- [ ] **Step 3:** CLAUDE.md: update the bank row in Location Activities, add Exchange to the architecture services table, mark the economy reinstated via ADR-0007 in the roadmap section (brief — point at the ADR/spec).
- [ ] **Step 4:** `tests/unit/test_skill_md.py` and `test_plugin_manifests.py` still green. **Commit** `docs(exchange): skill references, bank activity copy, CLAUDE.md`.

### Task 13: Frontend wallet display

**Files:**
- Modify: `apps/frontend/src/components/ApartmentCard.tsx`, the `ApartmentAgent` type (find via `grep -rn "interface.*Agent" apps/frontend/src/hooks/useApartment.ts`)
- Test: `pnpm typecheck` + existing component tests still pass (`pnpm test` if configured — check package.json scripts)

- [ ] **Step 1:** Add optional `wallet_balance?: number; trust_tier?: string;` to the apartment agent type. In `ApartmentCard.tsx` next to the Mood row (~line 166), render when present:

```tsx
          {typeof agent.wallet_balance === 'number' && (
            <div className="font-body text-xs text-clay-300">
              Crumbs: <span className="text-brown-200">🪙 {agent.wallet_balance}</span>
              {agent.trust_tier && <span className="ml-2 text-clay-400">({agent.trust_tier})</span>}
            </div>
          )}
```

(match the file's actual class vocabulary — copy the Mood row's classes verbatim rather than these if they differ).
- [ ] **Step 2:** `cd apps/frontend && pnpm typecheck && pnpm lint` (run_in_background) → clean. **Commit** `feat(frontend): wallet + trust tier on ApartmentCard`.

### Task 14: Verification + PR

- [ ] **Step 1:** Full gates: backend `uv run pytest tests/ -q` + `uv run ruff check . && uv run ruff format --check .`; CLI `uv run pytest`; frontend `pnpm lint && pnpm typecheck`; infra `npx cdk synth` (no backend `.venv`).
- [ ] **Step 2:** Self code-review of the diff (skill `code-review`, medium effort); fix findings.
- [ ] **Step 3:** Push branch, open PR titled `feat(exchange): GooseTown agent economy — AEX-derived jobs/bids/escrow/trust (ADR-0007)` with summary linking spec + ADR. **Do not merge** — wait for codex review per standing rule.
