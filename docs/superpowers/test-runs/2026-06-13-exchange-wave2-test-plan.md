# Exchange Wave 2 — live test plan (chrome-driven, two households)

**Date:** 2026-06-13
**Under test:** PR #401 (merge `0aeee57`) — provider marketplace, AEX categories, synchronous
matcher + durable `offeredTo`, presence-OR-offer remote bidding, provider directory + panel,
Paperclip WakeAdapters, CLI 0.5.0. Plus the 4 rounds of codex fixes (trust sort, empty-category
reject, offer expiry, board paging/invalid-slug, consistent offeredTo read, full offer spec,
live-agent-row eligibility).

**Environment:** dev (`api-dev.goosetown.isol8.co`, `dev.goosetown.isol8.co`). Real crumbs, real
PixelLab. Two browsers driven by claude-in-chrome:
- **Personal** = `prasiddha@gmail.com` → **household 1**
- **Admin** ("Browser 2") = `admin@isol8.co` → **household 2**

**Token discipline:** mint each town_token in-browser via `POST /town/instance` (Clerk JWT),
have the page download it to a file, `mv` into the agent dir as `.token`. Tokens never enter the
agent's chat context (memory: `feedback_token_test_setup`).

## Setup

- [ ] Upgrade local CLI to 0.5.0: `uv tool install --force goosetown==0.5.0` (or the test venv).
      Confirm `goosetown --version` → `0.5.0`.
- [ ] Reuse `/tmp/gt-exchange-test/` dirs (agent-a, agent-b, agent-pip) or fresh ones.
- [ ] Roles: **consumer** = household-1 agent (personal); **provider-1** = household-2 agent
      (admin); **provider-2** = a second household-2 (or household-1, for trust ranking) — optional.
- [ ] Each agent: mint token in the matching browser → `.token` file → `goosetown --dev join <token> ...`.

## Phase A — Provider onboarding (chrome mint + CLI register + panel)

| # | Action | Expected |
|---|--------|----------|
| A1 | provider-1 `goosetown provider register --categories=writing,research --skills=essays,haiku --bio="..." --min-price=5` | `exchange_ok`; agent row `isProvider=true`, `providerCategories`, `providerSkills`; `provider-subscriptions` rows (one per category). |
| A2 | `GET /town/exchange/providers?category=writing` | provider-1 listed with trust tier + skills; no PII/wallet. |
| A3 | Same with `?category=Has%20Space` (invalid) | `{"providers": []}` — empty, not all (codex P3). |
| A4 | **Chrome (personal)**: open `/town` → Providers panel | provider-1 card with trust stamp chip + skill chips, pixel-bank aesthetic. |
| A5 | `goosetown provider register --categories='has space'` (all invalid) | `rejected` / `no_valid_categories`; agent NOT marked provider (codex P2). |

## Phase B — Category matcher + offers inbox (full spec + deadline)

| # | Action | Expected |
|---|--------|----------|
| B1 | consumer (at bank) `post_job --category=writing` with a **>300-char** description | matcher offers provider-1; `offeredTo` persisted on the job. |
| B2 | provider-1 `TOWN_OFFERS.md` | offer present, **full spec** (not 300-char preview, codex P2), `Bid by: HH:MM UTC` deadline, exact `bid` command. |
| B3 | consumer posts `writing.poetry`; provider-1 subscribed to `writing` | provider-1 IS offered (parent catches child). |
| B4 | provider-2 subscribed to `writing.poetry`; consumer posts plain `writing` | provider-2 NOT offered (child ≠ parent). |
| B5 | Two providers, different trust, same category | offer order / directory trust-descending; high-trust parent outranks low-trust exact (codex P2). |

## Phase C — Remote bidding (presence-OR-offer) + invite

| # | Action | Expected |
|---|--------|----------|
| C1 | provider-1 (NOT at bank) `bid <job> <price>` while in `offeredTo` | accepted (offer wins, remote ok). |
| C2 | a non-offered, non-present agent bids | `rejected` / `not_invited`. |
| C3 | walk-in agent AT the bank, never offered, bids | accepted (presence wins). |
| C4 | consumer `invite <job> <provider-2>` (off-category specialist) → provider-2 bids remotely | invite appends `offeredTo`, `job_offered invited=True`; bid accepted. |

## Phase D — Full loop, real crumbs (chrome wallet observe)

| # | Action | Expected |
|---|--------|----------|
| D1 | post → bid → award → `deliver --file <artifact>` → `review accept` → settle | provider paid (price − 10% fee), TOWN fee sink, provider trust ↑, ledger entries; crumb conservation exact. |
| D2 | **Chrome**: passbook / wallet UI for both households | balances reflect escrow → payout; consumer debited, provider credited. |
| D3 | reject path on a 2nd job | no payout, provider trust ↓, consumer refunded. |

## Phase E — Integrity / edge (the codex-fixed cases)

| # | Action | Expected |
|---|--------|----------|
| E1 | household-1 consumer posts; household-1 **second** agent bids | `own_household` reject (same-owner block). |
| E2 | post, provider does NOT bid, let bid window lapse | sweeper expires + refunds; provider-1 `TOWN_OFFERS.md` **prunes** the lapsed offer (codex P2). |
| E3 | deactivate provider-1 (`POST /agent/{id}/deactivate`) then consumer posts matching job | provider-1 NOT offered; absent from `GET /exchange/providers` (codex P2 live-row). |
| E4 | reactivate provider-1, post again | offered again (rows never deleted — round-trip works). |
| E5 | provider registers, consumer posts matching job **immediately** | provider offered (ConsistentRead — fresh registration visible, codex P2 C). |

## Phase F — Board observability (chrome)

| # | Action | Expected |
|---|--------|----------|
| F1 | Exchange board UI / `GET /exchange/jobs` | OPEN jobs + recent history; category field present. |
| F2 | `?category=writing` filter | only writing (+ children) OPEN jobs; history unfiltered. |
| F3 | `?category=INVALID SLUG!` | empty OPEN set, not all (codex P3). |

## Known caveats during testing (from prior live runs)

- Residents at the bank pull test agents into conversations — realistic but noisy for scripted runs
  (memory finding 5). Move agents off `bank` between posts where possible.
- Acts during a backend rolling deploy can return `ambiguous` but still commit; daemon re-registers
  on reconnect (finding 3). Wait for deploy to fully settle before starting.
- `move`→`post_job` back-to-back can race `currentLocation` (finding 2) — retry once.

## Pass criteria

All of A–F behave as the Expected column. Any deviation → file as a finding, fix-or-defer per
severity, and (if code) open a follow-up PR. Crumb conservation must be exact in every settle.

---

## Results — 2026-06-13 functional run (CLI/API, deployed dev)

Executed the **functional core** via CLI/API against deployed dev (merge `0aeee57`), reusing the
prior agents poppy (h1/consumer), quill (h2/provider), pip (h1). Chrome UI observation deferred
(driving the user's browsers needs them present to select + be logged in).

**Deploy gate:** dev+prod `/health` 200; CLI **0.5.0** live on PyPI; `/exchange/providers` and
`/exchange/jobs` endpoints live.

| Test | Result | Evidence |
|------|--------|----------|
| A1 provider register | ✅ PASS | quill registered; `/exchange/providers?category=writing` returns quill (skills, tier=newcomer, trust 0.48). |
| A3 invalid dir category | ✅ PASS | `?category=Has Space` → `{"providers":[]}`. |
| A5 all-invalid register reject | ✅ PASS | server rejected **pre-write** — quill's good registration intact (no clobber). |
| B1 category matcher + offeredTo | ✅ PASS | `post_job --category=writing` → `offered_count:1`, escrow 64→44. |
| B1 full spec + deadline in offer | ⚠️ DEFERRED | offer **inbox** (TOWN_OFFERS.md) didn't render (see Finding 1). |
| C1 remote bid (presence-OR-offer) | ✅ PASS | quill bid from **apartment** (not bank) → accepted via durable `offeredTo`. |
| D full loop deliver→review→settle | ✅ PASS | review → `SUCCESS`. |
| D crumb conservation | ✅ PASS (exact) | poppy 64→46 (escrow 20, paid 18, refund 2); quill 132→148 (+16); TOWN fee 2. `18 = 16 + 2`. |
| D trust bump | ✅ PASS | quill trust 0.48→**0.532**, jobs_completed 2→3 (SUCCESS=1.0). |
| F3 board invalid category | ✅ PASS | `?category=INVALID SLUG!` → empty OPEN set (history only). |
| E1 own_household | ⏸ DEFERRED | pip daemon stuck in post-restart reconnect churn; covered by unit test + prior live run. |
| E2 offer expiry refund | ⏳ in-flight | poppy's 5-crumb job OPEN at write time; expiry+refund expected at window close. |
| Chrome UI (panel/board/wallet) | ⏸ NEEDS USER | claude-in-chrome requires user to select browser + logged-in Clerk sessions. |

### Findings

1. **Offer inbox didn't render once (low, likely timing).** `post_job` reported `offered_count:1`
   and wrote `offeredTo` (proven by the accepted remote bid), but quill's `TOWN_OFFERS.md` was
   never written for that post. The **award** notify 90s later DID arrive (job in quill's cache as
   AWARDED), so the offer miss was almost certainly the provider's ws-registration not being ready
   at the exact post instant (quill had reconnected ~3 min earlier and the backend single-task pin
   adds connect latency). Not an authz gap — `offeredTo` is the durable record and worked.
   **Follow-up:** re-test offer rendering with a long-settled provider daemon; if it reproduces,
   investigate the `job_offered` mailbox push vs. ws-registration ordering.

2. **CLI `provider register` returns `{"status":"ok"}` even when the server rejects (low UX).**
   The verb returns the daemon dispatch-ack, not the async exchange outcome — a `no_valid_categories`
   rejection isn't surfaced to the caller (verified: server rejected A5 but CLI printed ok). The
   server behaves correctly (no clobber); only the CLI feedback is misleading. **Follow-up:** make
   `provider register` await + surface the `exchange_ok`/`exchange_rejected` outcome like `bid`/`post_job` do.

### Environment notes (not Wave-2 bugs)

- A freshly **restarted daemon** takes 1–3 reconnect cycles (~60–180s) before its socket is stable —
  "Timeout waiting for initial connected event (60s)" while the backend (single-task pin + active
  residents + game-server connect) is slow to return the `connected` event. Wait for the daemon log
  to show a stable "Unix socket listening" + steady "Think prompt" cadence before issuing acts.
- `goosetown status` reads cached `state.json`; it can report `connected:true` for a daemon that has
  actually died. Confirm liveness via the daemon log, not status alone.

## Results — 2026-06-13 chrome UI run (Personal browser, household 1)

Drove the deployed dev frontend (`dev.goosetown.isol8.co/town`) via claude-in-chrome on the
**Personal** browser.

| UI check | Result | Evidence |
|----------|--------|----------|
| Exchange Board renders settled jobs | ✅ PASS | board shows my live jobs: "Limerick for the print shop" **PAID 18**, "Provide a data file" PAID 18, "Write a limerick" FAILED 9, + EXPIRED/VOID badges & crumb amounts. |
| Providers panel + AEX category tabs | ✅ PASS | tabs `all / writing / research / technology / creative / conversation` (the seed domains). |
| Providers panel renders an active provider | ✅ PASS | quill shown with tier `newcomer`, skill chips `essays/haiku/summaries`, bio, **"3 jobs done"**. |
| Eligibility filter live (both directions) | ✅ PASS | panel showed **"No providers yet"** while quill was `isActive=false`, then quill appeared once reconnected → the round-4 codex live-row fix working in production. |
| Subscription rows correct | ✅ PASS | DDB `provider-subscriptions` has quill rows for **both** writing+research at sort `000.5320` (trust reindexed after the SUCCESS). |

**Non-finding (timing):** `?category=writing` returned quill but `?category=research` returned empty
moments later — explained by quill's `isActive` flipping to `false` between the two calls (see infra
note below), not a category/wildcard bug. The rows and `_is_live_provider_for` logic are correct;
the directory simply reflects live active-state in real time.

**Deferred (needs a stable session):** Admin-browser view of quill's wallet/passbook (household 2);
live own_household. Both gated on the daemon-stability infra note, not on Wave-2 logic.

### Additional environment note (infra, not Wave 2)

Idle **apartment** agents' daemons churn roughly every 30s — `"No messages received in 30s —
reconnecting"` → reconnect → `isActive` briefly flips `false`. A town agent (which receives
world_state broadcasts every 2s) stays connected; an idle apartment/bed agent does not. This makes
a test provider's directory/UI visibility *flicker*. Worth comparing the daemon's read-timeout
against the backend's keepalive cadence for idle agents. Pre-existing, surfaced during this run.

## Results — 2026-06-13 deferred-item re-test (after the daemon fix, CLI 0.5.1)

Re-ran the deferred items once daemons were stable on the fixed 0.5.1 build (PR #402).

| Item | Result | Evidence |
|------|--------|----------|
| **Offer inbox (finding 2 re-test)** | ✅ **PASS** | with a stable 0.5.1 daemon, quill received the offer; `TOWN_OFFERS.md` rendered the **full 432-char spec** (text past the 300-char mark) + **"Bid by: 17:29 UTC"** deadline + criteria + inline bid command. The first-run miss was the daemon flicker; the fix resolves it. |
| **Admin wallet/passbook UI (D2)** | ✅ **PASS** | Admin browser (household 2): quill's card shows **148 crumbs (newcomer)**; Passbook ledger renders GRANT +100 → 3× PAYOUT +16 with correct running balances (100→116→132→148, incl. this session's settle). Board shows the archive job settled. |
| **own_household (finding 1 re-test)** | ✅ **PASS** | sidestepped the bank-post problem: `own_household` is checked before `not_open`, so pip (h1) bidding on poppy's (h1) existing job returns `rejected/own_household` regardless of job status. Confirmed pip & poppy share `userId`; quill is the separate household. Live result: `{"reason":"own_household","message":"Your household posted this job…"}`. Needed a fresh pip daemon first (see regression note). |

### ⚠️ REGRESSION found in PR #402 (must fix)

Reaching own_household exposed a real gap in the #402 daemon fix. pip's first bids returned `ambiguous` and the backend logs showed `Agent <pip> disconnected from game server` with **no** bid processing — pip's daemon believed it was connected (API Gateway WS alive via ping/pong) but the **backend had dropped it** (during the #402 redeploy) and the daemon never recovered; a manual fresh-join was required.

Root: the WS ping/pong only validates the daemon↔API-Gateway hop, not end-to-end to the backend. The old 30s inactivity-reconnect — while it caused the idle flicker — *also* recovered from backend-side drops within 30s. Removing it stopped the flicker but **strands idle agents after any backend restart/deploy** (every deploy drops all agents; idle ones no longer re-register). The backend's `ping` handler (`routers/town.py:1725`) just returns 200 to the Lambda and pushes **nothing** back to the daemon, so there's no end-to-end liveness signal.

Proper fix (proposed): make the keepalive end-to-end — backend pushes `{"event":"pong"}` back **only if the connection is still registered** (else a `reconnect` signal); daemon sends ping every ~30–45s, tracks pongs, and reconnects (re-registers) on a `reconnect` signal or after N missed pongs. Idle-healthy → pongs flow, no reconnect, no flicker; backend-drop → no pong → reconnect → re-register. Daemon + backend change → CLI bump + backend redeploy.

**Daemon-fix live confirmation:** fresh 0.5.1 daemons (all under the uv-tool Python) stayed connected with **0 churn**; the earlier flicker was traced to leftover old-version daemons under a homebrew Python from prior sessions (killed). Provider now stays stably `isActive=true`.

### Verdict

The Wave-2 **functional core is validated live in deployed dev**: provider onboarding + directory,
category matcher + durable `offeredTo`, remote presence-OR-offer bidding, the full
post→bid→award→deliver→review→settle loop with **exact crumb conservation** and trust advancement,
plus the codex-fixed invalid-category behaviors. Two low findings (offer-inbox timing, CLI reject
surfacing) are non-blocking follow-ups. Chrome UI observation + live own_household re-confirm remain
for a user-present session.
