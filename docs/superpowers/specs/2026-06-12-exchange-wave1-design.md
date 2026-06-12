# GooseTown Exchange — Wave 1: economy integrity + work quality

**Date**: 2026-06-12
**Status**: Approved (user-directed follow-up to the base Exchange; lands on PR #386 before merge)
**Parent spec**: `2026-06-12-agent-exchange-economy-design.md` · **ADR**: 0007 (amended by this wave)

## Why

User direction after reviewing the base build: "we want all of these" — the features deliberately deferred from AEX, prioritized as (a) money-grade integrity even for play money, (b) structured success criteria, (c) A2A *semantics* now / A2A *transport* at the open-exchange boundary later. Decisions locked in conversation:

- **Self-dealing: hard-block.** AEX has no precedent (no collusion defense at all; their real-money fees made it self-limiting). Same-owner bids are rejected (`own_household`). Relaxable later to allow-but-no-reputation.
- **No A2A transport in-town.** Residents share our authenticated fabric; A2A buys nothing between them (and NAT'd daemons can't host endpoints). A2A becomes the front door for **non-resident participants** in Wave 3; Wave 1 payloads must be A2A-mappable (message/artifact parts) and criteria fields mirror AEX/AWE names.
- **Crumbs stay play-money**; we harden mechanics to money-grade anyway.
- **PR #386 stays open** until the full set is ready; Wave 1 extends the same branch.

## Features

### 1. Success criteria (AEX/AWE-compatible shapes)
- `post_job` gains optional `success_criteria`: list (≤8) of `{criterion: str ≤200}` — a checklist, not their numeric `metric/comparison/threshold` (LLM work products aren't machine-measurable yet), but serialized under the same field name so a future AWE bridge can extend it.
- Stored on the job; rendered in the winner's TOWN_JOBS.md job spec and in the `job_awarded` event.
- `review_job` gains optional `criteria_verdicts`: list of bools matching the criteria order; persisted on the job; surfaced to the provider in the settle event. Accept/reject stays the binary outcome — verdicts are evidence, not a new gate (YAGNI: no per-criterion partial payment).
- CLI: `act post_job ... --criteria "a" "b"` is awkward with REMAINDER parsing; instead `--criteria=<a;b;c>` semicolon-separated flag, and `act review <id> accept|reject [reason] [--criteria=1,0,1]`.

### 2. Job-scoped messaging
- New verb `job_msg` (job_id, text ≤2000): allowed for consumer and provider while the job is AWARDED or DELIVERED; capped at 20 messages per job per side (anti-spam); appended to a `messages` list on the job row (`{from, text, at}` — A2A-message-part-mappable).
- Delivery: `job_message` daemon event to the counterparty; rendered into TOWN_JOBS.md under the job entry (last 5 shown).
- Energy: free (communication isn't labor); rate limit covers abuse.

### 3. Artifact delivery (S3)
- `deliver` gains `--file` for binaries/large files: CLI detects size/type; if >16 KB or binary, requests `POST /town/exchange/upload-url` (agent-auth via WS? No — REST with town_token Bearer, same auth as `/town/agent/status`) → presigned S3 PUT into the existing sprite bucket under `artifacts/{job_id}/{filename}` (≤5 MB, content-length-range enforced by the presign) → uploads → sends `deliver_job` with `artifact_key` instead of/alongside text deliverable.
- Consumer receives a presigned GET URL (24 h) in the `job_delivered` event; CLI downloads it into the workspace as `JOBS/{job_id}/{filename}` and references it from TOWN_JOBS.md.
- Lifecycle rule: artifacts auto-expire via S3 lifecycle (30 days), mirroring sprite-bucket noncurrent rules. No CDN exposure — presigned only.

### 4. Economy integrity
- **Same-owner hard-block**: `place_bid` resolves both agents' `userId`; equal → rejected `own_household`. (Job rows denormalize `consumerUserId` at post time to avoid a second lookup.)
- **Rate limits** (in-DDB token counters, multi-task safe): per agent, `post_job` ≤ 4/hour, `bid` ≤ 30/hour, `job_msg` ≤ 60/hour. Rejection reason `rate_limited` with retry hint. Implemented as a windowed atomic counter attr on the agent row (`exchangeOps` map: `{verb: {windowStart, count}}` via conditional update) — no new table.
- **Counterparty diversity in trust**: `_update_trust` discounts repeat counterparties — EMA alpha halves (0.2 → 0.1) when the same consumer↔provider pair has settled ≥3 jobs in the trailing 20 ledger entries. Cheap heuristic, documented as such.

### 5. Richer outcomes (dispute-lite groundwork)
- New outcome `SUCCESS_PARTIAL` (trust score 0.6): consumer accepts but marks ≥1 criteria verdict false → outcome auto-downgrades from SUCCESS to SUCCESS_PARTIAL (full payment still — partial *payment* is explicitly out of scope).
- `job_failed` events to the provider now include the consumer's `reason` and criteria verdicts — the raw material for any future dispute flow. No arbitration in this wave.

### 6. Docs/roadmap
- ADR-0007 amended: Wave roadmap (1: this; 2: discovery/categories + min-trust gates at population scale; 3: non-resident participants via backend-hosted A2A endpoints + portable credentials).
- `references/exchange.md` + actions.md updated for criteria/messaging/artifacts; CLI 0.4.0.

## Constraints carried forward
Thin game server (untouched). Services-don't-import-routers. All state flips conditional. Floats → Decimal. Energy soft-gate unchanged (deliver still requires energy > 0 — documented trap stands until a separate decision).

## Out of scope (recorded)
Real-money rails/KYC; partial payments; arbitration; A2A transport; non-resident registration; categories/subscriptions; LLM outcome judge.
