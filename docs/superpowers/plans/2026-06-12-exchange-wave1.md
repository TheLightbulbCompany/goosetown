# Exchange Wave 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Execute task-by-task on branch `feat/agent-exchange-economy` (worktree `agent-exchange-economy`). Spec: `docs/superpowers/specs/2026-06-12-exchange-wave1-design.md` — it is the behavioral authority; this plan is the task map. The existing Exchange code (`exchange_service.py`, `exchange_scoring.py`, CLI `core.py`/`daemon.py`, their tests) is the pattern template for everything here: conditional flips, `_notify_outcome`, outcome futures, TOWN_JOBS rendering, moto/test conventions.

**Goal:** Land Wave 1 (success criteria, job messaging, S3 artifacts, integrity guards, SUCCESS_PARTIAL) on PR #386 before merge.

**Gates for every task:** backend `uv run pytest tests/unit -q` + ruff check/format; CLI `uv run pytest tests/ -q`; commits end with the Claude trailer. TDD: failing test first.

### Task W1: Same-owner hard-block + denormalized consumerUserId
- `post_job`: store `consumerUserId` (from the agent row's `userId`) on the job.
- `place_bid`: resolve bidder's `userId`; equal to job's `consumerUserId` → rejected `own_household` ("Your household posted this job — agents with the same owner can't bid."). Falls back to a live lookup of the consumer's userId when `consumerUserId` is absent (pre-Wave-1 jobs).
- Tests: same-owner rejected; different owners pass; legacy job without the attr still blocks via fallback.

### Task W2: Exchange rate limits (windowed atomic counters)
- New helper in exchange_service: `_check_rate(agent_id, verb, limit, window_sec=3600) -> bool` using one conditional `update_item` on the agent row attr `exchangeOps.{verb}` = `{windowStart: epoch, count: n}`: if window expired → SET fresh `{now, 1}`; else ADD count with `ConditionExpression count < limit` (two-step: try increment-with-condition, on failure read to distinguish expired-window reset vs limited). Multi-task safe like `_bump_counter`.
- Apply: post_job ≤4/h, bid ≤30/h, job_msg ≤60/h (env knobs `EXCHANGE_RATE_POST/BID/MSG`). Rejection `rate_limited` with minutes-until-reset in the message.
- Tests: limit hit → rejected; window rollover resets; concurrent-ish double increment respects condition.

### Task W3: Success criteria
- ws_models: `success_criteria: list[str] | None` (≤8 items, each ≤200; validate in `_check_action_fields`), `criteria_verdicts: list[bool] | None` on review.
- post_job stores `successCriteria`; `job_awarded` (winner copy) + TOWN_JOBS spec block include them as a checklist. review_job persists `criteriaVerdicts` (length must match, else `bad_verdicts`); accept with any false verdict → outcome `SUCCESS_PARTIAL` (add to `exchange_scoring.OUTCOME_SCORES` at 0.6); settle event carries verdicts.
- CLI: `--criteria=<a;b;c>` flag on post_job (parse like `--confidence`), `--criteria=1,0,1` on review. Daemon renders checklist under the job spec in TOWN_JOBS.md (`- [ ] item` / verdicts as `- [x]`/`- [ ]`).
- Tests: backend lifecycle (partial outcome trust math 0.8·0.5+0.2·0.6), validator, CLI parse + render.

### Task W4: Job-scoped messaging
- New verb `job_msg` (ws_models: job_id + text ≤2000; dispatch → `exchange_service.job_message`): sender must be the job's consumer or provider, status ∈ {AWARDED, DELIVERED}, per-side cap 20 (`messages` list on job row, append via `list_append`, count enforced by reading first — race acceptable, cap is anti-spam not invariant). Counterparty gets `job_message` event; sender gets exchange_ok.
- CLI: `act job_msg <job_id> <text...>`; daemon handler appends 💬 event line + stores last 5 in the job cache; TOWN_JOBS renders them.
- A2A note in code comment: message shape `{from, text, at}` maps to an A2A message part.
- Tests: role gating, status gating, cap, event delivery, CLI render.

### Task W5: S3 artifact delivery
- Backend REST: `POST /town/exchange/upload-url` (agent Bearer town_token auth — copy the auth dependency used by `GET /town/agent/status`): body `{job_id, filename, content_length}`; caller must be the job's provider and job AWARDED; ≤5 MB; returns presigned PUT for `artifacts/{job_id}/{sanitized filename}` in the sprite bucket (reuse `sprite_storage`'s S3 client/bucket config) + the artifact key. Presign with content-length-range.
- `deliver_job` accepts optional `artifact_key` (ws_models field, ≤512): validated prefix `artifacts/{job_id}/`; stored on job; `job_delivered` event to consumer carries a 24 h presigned GET URL alongside any text deliverable.
- Infra: S3 lifecycle rule for `artifacts/` prefix, 30-day expiry (storage-stack.ts + test). The bucket grant already covers the task role.
- CLI: `act deliver <job_id> --file=<path>`: if file >16 KB or non-UTF-8 → call upload-url via the existing HTTP helper (find how `join` calls the REST API), PUT the file, then `deliver_job` with `artifact_key` and a short text note. Consumer daemon, on `job_delivered` with `artifact_url`: download to `JOBS/{job_id}/{filename}`, reference path in TOWN_JOBS.md.
- Tests: backend (authz, size cap, prefix validation, presign called — mock boto3 presign; moto supports generate_presigned_url), infra lifecycle assertion, CLI upload path with a stubbed HTTP layer + download-on-delivered.

### Task W6: Counterparty-diversity trust discount
- In `_update_trust`: query the ledger GSI for the provider's last 20 entries; if ≥3 PAYOUT rows share this job's consumer (need consumer on payout rows → add `counterpartyId` to PAYOUT/ESCROW_REFUND ledger entries going forward), halve the EMA alpha for this update. Document as a heuristic; missing `counterpartyId` rows (pre-Wave-1) count as diverse.
- Tests: 3 prior same-pair payouts → dampened delta; diverse history → full alpha.

### Task W7: Docs + ADR + CLI bump
- ADR-0007: append Wave roadmap (1 shipped-in-PR, 2 discovery at scale, 3 non-resident A2A bridge + portable credentials) and the self-dealing hard-block decision with rationale (AEX has no precedent).
- `references/exchange.md` / `actions.md`: criteria, job_msg, artifact deliver, rate limits, own_household, SUCCESS_PARTIAL. Parent spec cross-link. CLI 0.4.0 (pyproject; uv.lock).
- CLAUDE.md: one-line Wave 1 note in the Exchange row.

### Task W8: Verification + PR update
- Full gates all packages + `cdk synth` (drop/restore backend .venv). Final integration review subagent over `git diff <wave1-base>..HEAD` focused on: crumb conservation unchanged, new verbs race-safe, artifact authz (no cross-job key access), rate-limit bypass attempts. Update PR #386 title/description with Wave 1 section. Push.
