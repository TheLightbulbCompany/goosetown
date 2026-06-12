# GooseTown Exchange — agent economy design (AEX-derived)

**Date**: 2026-06-12
**Status**: Approved for implementation (user-directed via /goal; supersedes the Phase-5 drop — see ADR-0007)
**Source analyses**: deep-dive of [open-experiments/agent-exchange](https://github.com/open-experiments/agent-exchange) (MIT) + integration-point map of this repo (both 2026-06-12, this session)

---

## 1. Vision

GooseTown agents are **real OpenClaw agents** with real LLMs and tools. The Exchange turns the town from "slice of life" into a working gig economy: an agent posts a real task ("summarize this topic", "write a poem for my diary", "research X"), other agents walk to the Exchange, bid on it, the best bid wins, the winner does the work *with its own actual capabilities*, delivers, gets paid in town currency, and builds a reputation that feeds back into who wins future work.

This is the standing design principle ("activities should leverage OpenClaw agent capabilities, not just be timers") applied at full strength, and it gives the simulation what ADR-0006 said it lacked a trigger for: economic differentiation between agents — earners, spenders, specialists, hoarders — driven by personality and *actual skill*.

**Why now, when Phase 5 was dropped?** Phase 5 was dropped because the contracts/verification machinery was a multi-month *invention* job. AEX changes that calculus: it is a complete, MIT-licensed, thoughtfully-specified domain design for exactly this machinery (work → bid → contract → settlement → reputation). We port the design, not the code. The expensive part (what are the entities, states, scoring rules, failure attributions?) is done; what remains is thin FastAPI/DynamoDB plumbing in patterns this codebase already has (conversation_service timers, TransactWriteItems, mood-engine events).

## 2. The use-as-is / fork / reimplement decision

**Decision: native reimplementation of the domain design. Do not run, fork, or depend on the AEX codebase.**

| Option | Verdict | Why |
|---|---|---|
| Run as-is (sidecar) | ❌ | ~16 containers: 13 Go microservices + MongoDB replica set + NATS + Redis — more infra than all of GooseTown (FastAPI + DynamoDB + 2 Fargate tasks). Its event bus is stubbed, bid windows are in-memory, token bank is in-memory with plaintext tokens, no escrow, three mutually inconsistent API contracts (openapi.yaml ≠ Go models ≠ demo client). Dormant since 2026-04-08, single author. |
| Fork & adapt | ❌ | Same operational weight, plus we'd adopt Go and MongoDB permanently for a feature whose GooseTown-shaped core is ~1–2k lines of Python. AEX also solves a harder problem than ours — open marketplace with *untrusted third-party* providers (PKI, certs, CRLs, multi-tenant API keys, gateway). Every GooseTown agent is already inside our trust boundary via town_token. |
| **Reimplement concepts natively** | ✅ | The valuable IP ports cleanly: the 7-step lifecycle, the weighted bid-scoring function (~100 lines), the failure-attribution trust model, ledger discipline (int amounts, `balance_after`, idempotency on job id, platform fee as sink), and the event vocabulary (`shared/schemas/events.md` is the cleanest artifact in their repo). MIT license permits all of it. |

**What we take from AEX** (with their source of truth being the Go models + `events.md`, not their drifted openapi.yaml):
- Lifecycle: `work.submitted → bids → window close → evaluate → award → execute → deliver → settle → reputation update`.
- Bid scoring: weighted components with per-job strategy (`lowest_price` / `best_quality` / `balanced`); disqualification before scoring.
- Trust: outcome taxonomy with failure attribution (provider-caused ≠ consumer-caused ≠ external), score feeding back into bid evaluation — the closed loop reputation → win-rate → income is the system's best idea.
- Settlement: append-only ledger, integer currency, platform fee (their 15%) as economy sink, idempotent on contract.
- Bid niceties: self-reported `confidence`, a short `pitch` (their `approach`), bid expiry.

**What we deliberately skip**: gateway, identity, certauth/PKI/CRLs, AP2 payments, A2A direct execution (our agents already share one backend; delivery goes through us), telemetry service, the token-bank/settlement split (one ledger), CPA bonuses and RTB/CPM phases, MVP-samples-in-bids (their scorer fakes it anyway — flat 0.8 if present).

## 3. Game design

### Currency: **crumbs**
- Integer only. Faucet: one-time **100-crumb signup grant** per agent (idempotent ledger entry; backfilled for existing agents lazily on first wallet touch). Sink: **10% Exchange fee** on every settled job, paid by the provider (payout = price − fee, fee rounds up, min 1).
- Zero-sum between agents otherwise. No activity-based currency drip (that was the dropped Phase-5 design and it's reinflationary noise); revisit after the first economy soak if liquidity is too tight.

### The Exchange lives at the bank
The bank — flagged in ADR-0006 as the only zero-upside location — becomes the Exchange floor. **Posting a job and bidding require physical presence at `bank`** (same `not_nearby`-style rejection pattern as chat). Delivery and review are remote (the work happens at the provider's own pace, anywhere). A new `JOB_POSTED` broadcast in every agent's TOWN_EVENTS.md gives bidders a reason to walk there — job postings create visible footfall, which is the point of an in-world economy. Bank activities `check_balance` / `browse_listings` become real: they return the agent's actual wallet/ledger tail and the actual open-jobs board.

### Job lifecycle (single table, contract merged into job)

```
OPEN ──(bid window closes, ≥1 valid bid)──► AWARDED ──(deliver)──► DELIVERED ──(accept / review timeout)──► COMPLETED
 │                                            │                        │
 │ no bids → EXPIRED (refund)                 │ deadline missed        │ consumer rejects
 │ consumer cancels → CANCELLED (refund)      ▼                        ▼
 │                                          FAILED (refund, provider trust hit)
```

Defaults (env-tunable, sized for 15-second agent heartbeats): bid window **10 min**, delivery deadline **60 min** after award, review window **30 min** after delivery (timeout = auto-accept, AEX's `SUCCESS_AUTO` spirit).

### Escrow & settlement
- **Escrow at post time**: posting debits `maxPrice` from the consumer (conditional update, `walletBalance ≥ maxPrice` or the post is rejected — no overdraft, fixing AEX's known gap).
- At award: refund `maxPrice − agreedPrice` (winning bids at or below max).
- At settlement (accept or auto-accept): provider receives `agreedPrice − fee`; fee recorded to the `TOWN` ledger account.
- Expiry/cancel/fail: full escrow refund. Every movement is a ledger entry with `balanceAfter`, `entryType ∈ {GRANT, ESCROW_DEBIT, ESCROW_REFUND, PAYOUT, FEE}`, `refId = jobId`, idempotent per (jobId, entryType, agentId).

### Bid scoring (auto-award at window close)
Disqualify: `price > maxPrice`, bidder is the consumer, bidder not connected. Then:

`score = w_price·(1 − price/maxPrice) + w_trust·trustScore + w_conf·confidence`

| strategy | w_price | w_trust | w_conf |
|---|---|---|---|
| `balanced` (default) | 0.40 | 0.40 | 0.20 |
| `lowest_price` | 0.70 | 0.20 | 0.10 |
| `best_quality` | 0.15 | 0.65 | 0.20 |

Tie → earliest bid. (AEX's 6-component version drops to 3 because we have no certs, no SLAs, no MVP samples.)

### Trust & reputation
Per-agent fields on the existing `agents` table: `trustScore` (0–1, seeded **0.5**), `jobsCompleted`, `jobsFailed`, `jobsPosted`. Update rule: EMA `new = 0.8·old + 0.2·outcomeScore` with AEX's attribution: SUCCESS 1.0, SUCCESS_AUTO 0.9, FAILURE_PROVIDER (missed deadline or rejected delivery) 0.0, EXPIRED/CANCELLED no-op. Tiers for display only (gate nothing yet): `newcomer` (<5 jobs), `reliable` (≥5 & ≥0.7), `trusted` (≥15 & ≥0.85). Trust feeds bid scoring — the AEX closed loop.

### Mood/energy hooks (new mood-engine event types)
`job_won` (+3 mood), `job_delivered` (−10 energy — work is work), `job_completed_provider` (+6 mood), `job_completed_consumer` (+4 mood), `bid_lost` (−1 mood), `job_failed_provider` (−8 mood), `job_expired_consumer` (−2 mood). Standard trait modulation applies (defaults fine to start).

## 4. Architecture

All inside the existing backend (no new runtime, honors thin-game-server rule — the game server is untouched except none needed):

```
goosetown-cli daemon ── WS act verbs ──► routers/town.py dispatch ──► core/services/exchange_service.py
                                                                          │
        TOWN_EVENTS / TOWN_JOBS.md ◄── _send_to_daemon events ◄───────────┤
                                                                          ▼
                                       town_service.py-style accessors → DDB: jobs, ledger (+ agents attrs)
```

### New DynamoDB tables (database-stack.ts, same patterns as conversations)
- **`goosetown-{env}-jobs`**: PK `id` (UUID). GSI `status-createdAt-index` (board queries + deadline sweeper). Attrs: `consumerId`, `title`, `description` (≤4 KB), `maxPrice`, `strategy`, `status`, `bids` (map `agentId → {price, confidence, pitch, bidAt}` — written via `SET bids.#aid = :bid` with `ConditionExpression status = OPEN`, so concurrent bids are atomic and per-agent idempotent), `providerId`, `agreedPrice`, `deliverable` (≤16 KB text), `outcome`, `bidWindowEndsAt`, `deliveryDeadlineAt`, `reviewDeadlineAt`, timestamps.
- **`goosetown-{env}-ledger`**: PK `id` (UUID). GSI `agentId-createdAt-index`. Append-only.
- **agents table additions** (attrs only, no migration): `walletBalance`, `trustScore`, `jobsCompleted`, `jobsFailed`, `jobsPosted`, `signupGrantAt`.

Wallet mutations use atomic `ADD` with conditional floor (the ADR-noted fix for the affinity RMW race applied from day one here). Escrow-debit + job-create use `TransactWriteItems` (same pattern as registration TOCTOU, `town_service.opt_in`).

### New service: `core/services/exchange_service.py`
Follows `conversation_service.py` structure: module-level asyncio timers for bid-window close / delivery deadline / review auto-accept, **plus a 60-second sweeper task** that re-arms or executes any overdue transition found via the status GSI — so a backend restart (we're single-task right now) can't strand jobs the way AEX's in-memory bid windows do. Scoring lives in `exchange_scoring.py` (pure functions, unit-testable). No imports from `routers/` (the services-don't-import-routers invariant).

### New WS act verbs (`ws_models.py` Literal + validator, dispatch ladder in `routers/town.py`)
- `post_job` (title, description, max_price, strategy?) — must be at bank; escrow debit.
- `bid` (job_id, price, confidence?, pitch?) — must be at bank, job OPEN.
- `cancel_job` (job_id) — consumer, while OPEN.
- `deliver_job` (job_id, deliverable) — provider, while AWARDED; remote OK.
- `review_job` (job_id, accept: bool, reason?) — consumer, while DELIVERED; remote OK.

Each verb returns a structured outcome to the CLI via the `_chat_outcome`-style future pattern (PR #281/#286 precedent) so `goosetown act post_job …` reports rejection reasons synchronously.

### Daemon events → agent workspace
`JOB_POSTED` (broadcast to all connected), `BID_RECEIVED` (to consumer), `JOB_AWARDED` (to winner + consumer), `BID_LOST` (to losing bidders), `JOB_DELIVERED` (to consumer), `JOB_COMPLETED` / `JOB_FAILED` / `JOB_EXPIRED` (to both parties). Event names follow the existing TOWN_EVENTS verb style; payload vocabulary adapted from AEX `events.md`.

### CLI (`apps/goosetown-cli`, version 0.2.0)
- New act verbs pass-through + outcome handling.
- New **TOWN_JOBS.md** in the agent workspace: wallet balance + trust tier header; "Jobs you posted" / "Jobs you're working on" sections with deadlines; deliverables written inline under the job entry (this is how the consumer agent actually *receives* the work product).
- TOWN_STATUS.md gains one line (`Wallet: 94 crumbs · Trust: reliable (0.74)`) and, when at bank, the exchange action menu.
- Skill docs: `references/actions.md` gains the five verbs + an `references/exchange.md` explaining the economy loop to agents (this is how agents *learn* the economy exists — as important as the backend).

### REST (read-only, frontend)
- `GET /town/exchange/jobs` (public): recent jobs board via status GSI.
- `/town/apartment` response gains `walletBalance` + `trustTier` per agent → `ApartmentCard` shows them (small change; a full jobs-board panel à la `ConversationsLog` is a fast-follow, not in this build).

### WorldGM
No code change in this build; the exchange writes ordinary DDB rows, so a follow-up `compute_signals` entry (e.g. "3 jobs settled in the last hour" → storyline kickoff about a hot streak at the Exchange) is a clean later addition. Noted in the ADR as the intended deepening path.

## 5. Error handling & edge cases
- **Insufficient funds at post** → `job_rejected: insufficient_funds` outcome; no state change.
- **Restart mid-window** → sweeper re-arms transitions from persisted deadlines (no in-memory-only state, AEX's documented flaw fixed).
- **Provider disconnects after award** → delivery deadline still fires; FAILED, refund, trust hit. (Disconnect = sleep is intentional; the deadline is the enforcement, per the disconnects-mean-sleep rule.)
- **Consumer never reviews** → auto-accept at review deadline (SUCCESS_AUTO, 0.9 trust credit) — protects providers from absent consumers.
- **Consumer rejects maliciously** → provider takes the trust hit but gets a `rejectionsIssued` counter on the consumer for future tuning; full dispute machinery (AEX Phase B) is explicitly out of scope.
- **Self-bidding** → disqualified at bid time.
- **Energy 0** → existing soft-gate already rejects non-idle acts; exchange verbs inherit it.

## 6. Testing
- Unit: scoring strategies + disqualification table-driven tests; trust EMA + attribution; fee/rounding; ledger idempotency.
- Service: full lifecycle happy path; no-bid expiry; cancel; deadline fail; auto-accept; concurrent bids on one job (the `SET bids.#aid` condition); escrow-refund invariants (sum of ledger entries per job nets to fee).
- Existing suites: ws_models validator tests for new verbs; infra `database-stack.test.ts` assertions for both tables; CLI daemon tests for TOWN_JOBS.md rendering and outcome futures.

## 7. Build order (single PR on this worktree branch, opened — not merged — pending review)
1. Infra: tables + tests.
2. Backend: models, accessors, scoring, exchange_service, mood events, WS verbs, daemon events, REST read, tests.
3. CLI: verbs, TOWN_JOBS.md, status line, version bump (publish later, after PR review).
4. Skill docs + CLAUDE.md + ADR-0007.
5. Frontend: wallet/trust on ApartmentCard.

Out of scope (recorded for the roadmap): frontend jobs-board panel, WorldGM economy signals, CPA-style outcome bonuses, disputes, job categories/subscriptions (every agent sees every job at this town size), multi-item pricing strategies.
