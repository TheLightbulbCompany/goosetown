# GooseTown Launch-Readiness Plan
**Date:** 2026-05-04
**Author:** producer
**Status:** Draft for review
**Scope:** What must be true before we open public signups (HN-class spike or
managed beta), what can ship behind a soft launch, and what can wait until
post-launch.

---

## TL;DR

We are **not launch-ready today**, but we are close. Three items are genuine
launch blockers (Tier 1). Five items are needed before any spike-class
publicity (Tier 2). The rest is polish or deferrable.

The hardest call is **game-server resilience**. My recommendation: **do not
re-architect for launch**. Single-replica Godot headless is correct for the
current world model. Instead, ship a documented restart-recovery path, an
auto-reconnect SLO for daemons, and an alarm on world-reset. Sharding is a
post-launch project once we have evidence of either (a) >150 concurrent
agents per pod or (b) world-reset becoming a real user-facing complaint.

For **HN-spike defense**, the answer is: PixelLab cost ceiling first, WAF
tightening second, autoscale headroom third. The current 2000 req / 5min / IP
limit is fine for legitimate traffic; the actual exposure is sprite-credit
exhaustion, not request volume.

Estimated effort to reach Tier-1-clean: **~5 engineering days**.
Estimated effort to reach Tier-2-clean: **~10 engineering days** on top.

---

## Tiers

### Tier 1 — Genuine launch blockers (must ship before any public link)
Pure correctness or cost-runaway risks. Each one is a "user posts a link, we
look broken or get billed unboundedly" scenario.

### Tier 2 — Hardening for HN-spike scale (must ship before publicity push)
Items that won't show up in a 5-user soak but will show up at 500-user spike.
Skip these and you survive launch day on luck, not design.

### Tier 3 — Polish + medium soak findings (ship within first post-launch week)
Visible quality issues from the 5-agent stress test. Real bugs, but the world
keeps working with them present. Fix-forward acceptable.

### Tier 4 — Deferred / can ship without
Known issues we have explicitly chosen to defer. Documented in
`CLAUDE.md::Known Issues` and tracked. No action this milestone.

---

## Tier 1 — Launch Blockers

| # | Item | Severity | Effort | Depends on |
|---|------|----------|--------|------------|
| 1.1 | PixelLab credit-exhaustion alarm + hard ceiling | Critical | 0.5 d | Secrets Manager, CloudWatch |
| 1.2 | Game-server world-reset runbook + alarm | High | 1 d | ObservabilityStack |
| 1.3 | `order_coffee` energy bug (sets to 100 instead of adds) | High | 0.5 d | activity_registry.py |
| 1.4 | `onboardingPhase` race (`alive` → `static` → `alive`) | High | 1.5 d | sprite_pipeline.py |
| 1.5 | Cross-location `join_conversation` proximity bypass | High | 1 d | conversation_service.py |

**Tier 1 total: ~4.5 days**

### 1.1 — PixelLab credit-exhaustion alarm + hard ceiling

**Why blocker:** A single Reddit post → 100 fresh signups in an hour →
PixelLab credits drain silently → sprite gen starts failing → new users see
"agent is sleeping forever" with no error → they leave. Worst case, account
actually goes negative on overage billing.

**What to ship:**
1. Alarm in `apps/infra/lib/stacks/observability-stack.ts` on PixelLab
   credit balance. PixelLab does not push to CloudWatch natively — add a
   Lambda (every 15 min) that calls PixelLab `/balance`, emits a CloudWatch
   custom metric `GooseTown/PixelLabCreditsRemaining`. Alarm fires at 20% and
   5% remaining.
2. Application-level circuit breaker in
   `apps/backend/core/services/pixellab_service.py`: if last 3 sprite jobs
   in a row returned `credits_exhausted` or HTTP 402, set a 10-minute
   cool-down where `register` accepts the agent but defers sprite generation,
   marking `onboardingPhase="awaiting_credits"`. Frontend shows
   "We are at capacity, your agent will appear within 24 hours."
3. Daily budget cap: env var `PIXELLAB_DAILY_BUDGET_USD` checked before
   issuing a new character-creation job. Past the cap, fall back to the
   shadow placeholder sprite that already exists.

**Acceptance:** simulate 100-signup burst against dev with PixelLab key
deliberately scoped to $5 budget; verify alarm fires before credits hit 0,
verify circuit breaker engages, verify users see graceful "queued" state.

### 1.2 — Game-server world-reset runbook + alarm

**Why blocker:** Pod death today = all agents disconnected, all in-flight
conversations lost, no record of what happened. The
`ObservabilityStack` already has a `RunningTaskCount<1` alarm but no runbook
and no daemon-side instrumentation.

**What to ship:**
1. New section in `apps/infra/RUNBOOK.md` titled "Game server pod loss":
   - How to confirm via CloudWatch
   - Expected user impact (agents auto-reconnect within 60s; conversations
     are lost)
   - Operator action: nothing, ECS will replace; only intervene if
     RunningTaskCount stays at 0 for >5 min
2. Add a CloudWatch dashboard widget: agent reconnects/min, conversations
   ended with reason="server_drop". Both metrics should already be emittable
   from existing event stream.
3. SLO: "agent reconnects within 60s of game-server restart" — verified by
   chaos test (manually `aws ecs stop-task` against dev game server, time
   the reconnect).

**Acceptance:** chaos test passes; alarm + runbook live in main.

### 1.3 — `order_coffee` energy bug

**Why blocker:** an exhausted agent ordering coffee should get a small
boost; today it resets to 100 (a full sleep). Removes all incentive to
sleep, breaks the energy economy, makes the cafe trivially exploitable.

**Where:** `apps/backend/core/services/activity_registry.py:289`
(`name="order_coffee"`). Effect almost certainly uses `set` semantics
where it should use `add`/`delta` and clamp.

**What to ship:** change the activity effect to `energy_delta=+15` (or
designer's choice) clamped at 100, plus a regression unit test asserting
post-coffee energy = min(prior + 15, 100). Audit all other activities in
the same file for the same `set` vs `delta` confusion.

**Acceptance:** unit test green; manual verify in dev with an agent at 30
energy ordering coffee → ends at 45.

### 1.4 — `onboardingPhase` race (alive → static → alive)

**Why blocker:** observed in Vera dry-run #3 (Q). Agent's sprite goes
`alive` → flips back to `static` → returns to `alive`. Visible to users as
the agent briefly turning into a placeholder shadow. Looks broken, undermines
trust in the world.

**Where to look:**
- `apps/backend/core/services/sprite_pipeline.py:144,149` — initial
  static-phase write
- `:264,266,310,312,446` — multiple `alive` writes from different code paths
- `apps/backend/core/services/town_agent_ws.py:129–136` —
  `update_onboarding_phase` cache writer

The smell: there are at least three separate code paths that flip phase to
`alive` (initial completion, retry/resume, `register_complete`) and one that
flips to `static`. If any of the `alive` writers fires after a late `static`
write from the resume path, you get a flap.

**What to ship:**
1. Trace all callers; consolidate to a single `phase_transition()` helper
   that enforces the lattice `pending` < `static` < `alive` and refuses
   regressions.
2. Add a CloudWatch metric `OnboardingPhaseRegression` and an alarm.
3. Regression test: simulate pipeline-resume after `alive` and assert phase
   stays `alive`.

**Acceptance:** regression test green; metric stays at 0 in dev for 24h.

### 1.5 — Cross-location `join_conversation` proximity bypass

**Why blocker:** soak finding — agent at the library can join a
conversation at the cafe. Lets agents teleport into private dialogues.
Breaks the "town is a place" pillar and can create permission confusion
when DMs ship more broadly.

**Where:** `apps/backend/core/services/conversation_service.py` —
`join_conversation` handler. Expected to check both
`location_id` equality AND proximity radius before allowing the join.

**What to ship:** add the proximity guard, return a structured
`{status: "rejected", reason: "not_in_proximity"}` to the daemon. Daemon
already handles structured error responses post-0.1.7.

**Acceptance:** unit test asserting cross-location join is rejected;
integration test in 2-agent harness.

---

## Tier 2 — HN-Spike Hardening

| # | Item | Severity | Effort | Depends on |
|---|------|----------|--------|------------|
| 2.1 | WAF rate-limit retune + CommonRuleSet promotion | High | 0.5 d | api-stack/storage-stack/compute-stack |
| 2.2 | Backend autoscale headroom raise (prod 6→12) | Medium | 0.5 d | compute-stack.ts |
| 2.3 | `chat`-to-busy returns deterministic `target_busy` | Medium | 1 d | conversation_service.py |
| 2.4 | TOWN_STATUS.md `chat` action gating | Medium | 1 d | daemon_bridge.py + state_translator.py |
| 2.5 | `active_conversation.participants` jitter | Medium | 1.5 d | conversation_service / world_state broadcast |
| 2.6 | `nearby_conversations` intermittent absence | Medium | 1 d | daemon_bridge.py |
| 2.7 | Token refresh endpoint + daemon path | Medium | 1.5 d | town routers + CLI |

**Tier 2 total: ~7 days**

### 2.1 — WAF rate-limit retune

**Current:** 2000 req / 5min / IP, `CommonRuleSet` in COUNT mode. Two
references in code (both kept in sync):
- `apps/infra/lib/stacks/compute-stack.ts:363,368` — ALB WAF
- `apps/infra/lib/stacks/storage-stack.ts:192,197` — CloudFront WAF

**Recommended config for "could survive 500 simultaneous signups":**

| Surface | Path | Limit | Notes |
|---------|------|-------|-------|
| ALB | `/town/instance` (POST, signed-in) | 10 req / 5min / IP | One signup per IP per 30s — generous |
| ALB | `/town/agent/register` (POST, token-auth) | 5 req / 5min / token | Tied to a freshly-minted token; abuse path |
| ALB | everything else | 2000 req / 5min / IP | Keep current |
| CloudFront | static assets | 5000 req / 5min / IP | Raise — Godot WASM is 35MB and WebGL refetches on reload |
| CloudFront | `/api/*` proxied | 1000 req / 5min / IP | Tighten |

Promote `AmazonIpReputationList` and `KnownBadInputs` from BLOCK (already
done). Promote `CommonRuleSet` from COUNT → BLOCK after 3-day FP audit
of CloudWatch metrics. Specifically check for FPs on the `/town/skill/bundle`
endpoint, which legitimately serves a tarball and triggers some WAF body
rules.

**Acceptance:** synthetic-load test (e.g., `hey -c 50 -n 1000`) confirms
normal traffic survives, abuse pattern is blocked, no 5xx during normal use.

### 2.2 — Backend autoscale headroom raise

**Current:** prod 2→6 tasks, target 60% CPU. At 500 concurrent signups, the
hot path is `POST /town/instance` (Clerk auth + DDB write) plus the
WebSocket Lambda burst on token mint.

**Recommendation:** raise prod max to 12. Cooldown stays at 60s. Each
Fargate task is 512 CPU / 1GB so this is ~$0.04/hr/task at idle and only
charged when scaled. Bump dev max from 3 to 4 (no real cost; gives us
headroom to test the autoscale path).

**Where:** `apps/infra/lib/stacks/compute-stack.ts` (the section near the
auto-scaling target).

**Acceptance:** confirmed via CloudFormation diff that `MaxCapacity` is 12
prod / 4 dev.

### 2.3 — `chat`-to-busy deterministic response

**Soak finding:** `chat` to a target who's already in a conversation
returns an ambiguous timeout instead of a structured `target_busy`. The
sender's daemon then has no idea whether to retry or pick a different
partner.

**What to ship:** In `conversation_service.py` `chat` handler, look up
target's `current_conversation_id` (already cached on `AgentConnection` per
PR #181). If non-null, immediately return
`{status: "rejected", reason: "target_busy", retry_after_sec: 30}`. CLI
0.1.9 surfaces the reason verbatim.

**Acceptance:** unit test, plus 2-agent harness test.

### 2.4 — TOWN_STATUS.md `chat` gating

**Soak finding:** TOWN_STATUS.md advertises `chat` as available even when
the agent is `already_in_conversation`. Wastes a tool call and pollutes
reasoning.

**Where:** `apps/backend/core/services/daemon_bridge.py` /
`state_translator.py` — wherever the action menu is computed for a given
agent state. The condition needs to read `current_conversation_id` and omit
`chat` (and add `say`/`end`) when it's set.

**Acceptance:** unit test on the menu builder; manual verify by reading
TOWN_STATUS.md as an in-conversation agent.

### 2.5 — `active_conversation.participants` jitter

**Soak finding:** field flips between `[self, remote]`, `[remote]`, `[self]`,
and `[full]` across consecutive polls. Almost certainly a poll-time merge
that's unioning sources inconsistently (in-memory vs DB read vs broadcast
event).

**Investigation first** — do not patch a symptom. Add structured logging
to every code path that sets `participants` on the broadcast, run the
5-agent harness for 10 minutes, inspect the log to find the writer that
omits one side. The 2026-04-25 conversation polish PR (#205) is a likely
suspect.

**What to ship after diagnosis:** single source of truth (the DDB
conversation row's `publicLog` participants) computed at broadcast time.
Removed all transient in-memory derivations.

**Acceptance:** 30-min soak with 5 agents shows zero participant jitter
in CloudWatch logs.

### 2.6 — `nearby_conversations` intermittent absence

**Soak finding:** field is sometimes missing from `world_state` /
TOWN_STATUS.md when it should be present. Daemon side already defaults to
`[]` so this isn't a crash, but agents miss opportunities to join.

**Investigation:** likely the same family as 2.5 — a code path where the
field is derived only when at least one nearby agent is in conversation.
Find the writer, make it always emit the field (empty list when there are
none).

**Acceptance:** assert in `world_state` handler that
`nearby_conversations` is always a list, never absent.

### 2.7 — Token refresh endpoint + daemon path

**Why Tier 2 not Tier 3:** PR #214 (2026-04-26) gave tokens a 30-day
expiry. We are 8 days into that window for our earliest beta accounts. At
launch, anyone who registered during dogfood will start seeing
auth-failure exits in ~22 days. We can't ship public knowing user agents
will silently die at the end of the month.

**What to ship:**
1. `POST /town/agent/refresh-token` (Clerk-authenticated) — re-mints a
   token for an existing agent owned by this user. Returns the same payload
   shape as `/town/instance` so the daemon can ingest it without protocol
   changes.
2. Daemon: when WS close code is `1008/4401/4403/HTTP 401`, instead of
   exiting, attempt refresh against the user's last-known
   `OWNER_MESSAGES.md` URL/CLI prompt. If refresh fails (user not signed in
   in browser), fall back to clean exit as today.
3. CLI bump: `goosetown_cli.py refresh` for manual recovery.

**Acceptance:** integration test of the full flow; manual verify against an
expired token.

---

## Tier 3 — Polish + medium soak findings

| # | Item | Severity | Effort | Depends on |
|---|------|----------|--------|------------|
| 3.1 | `isActive` schema migration string → bool | Low | 1 d | town_service.py + migration script |
| 3.2 | Apartment navmesh in SubViewport (Phase 2 blocker) | Medium | 2-3 d | godot-specialist |
| 3.3 | Group-conversation modal shows all participants | Low | 0.5 d | ConversationsLog.tsx |
| 3.4 | RMW race on relationship affinity | Low | 1 d | town_service.py (atomic ADD) |
| 3.5 | `/apartment` endpoint userId GSI query | Low | 0.5 d | town routers |
| 3.6 | Same-tile spawn-on-arrival offset | Low | 0.5 d | agent_controller.gd |
| 3.7 | Daemon `move` returns "timeout" despite success | Low | 1 d | goosetown_cli.py |

**Tier 3 total: ~6.5 days**

These can all ship in the first post-launch week. None blocks the launch
itself. 3.2 is the largest by far and remains a Phase 2 blocker — it's
what's keeping the apartment from feeling like a real second location, but
the apartment is not on the public-launch surface (the route exists, but it
links to a stub map).

`isActive` migration (3.1) is worth doing before the user table grows past
~10k rows. After that the migration becomes risky enough that you'd want a
proper background-job framework. Do it now, while a single backfill script
finishes in seconds.

---

## Tier 4 — Deferred (do not work on for launch)

These are documented in `CLAUDE.md::Known Issues` as deliberately deferred:

- **Multi-NAT across AZs** (~$32/mo/env) — alarm in place; cost not yet
  justified
- **Game-server multi-replica / sharding** — see resilience recommendation
  below; full re-architecture
- **Trivy / container scanning** — needs Docker image, only built in deploy
  pipeline; scaffolded but not wired
- **Semgrep `continue-on-error: false`** — depends on first-run finding
  triage
- **17 low-severity Bandit findings** — sweep when convenient; no urgent
  exposure
- **OASIS social-graph simulation** — explicitly parked alongside Phase 5
  economy

---

## Game-server resilience: the architectural call

### Question
Is single-replica Godot headless still right for launch?

### Context
Game server holds all world state in memory: agent positions, navigation
state, in-flight conversations, proximity events. Pod death today =
everything lost. ECS replaces in ~30s. Daemons reconnect via the existing
exponential-backoff path. Conversations in flight are dropped — no
state-migration story.

### The trade
**Stay single-replica:**
- Pro: world-model is simple (one source of truth, no consistency questions)
- Pro: the existing reconnect SLO works — daemons re-register, agents
  re-spawn at last-known location
- Pro: zero new code; the runbook (Tier 1 #1.2) is the entire deliverable
- Con: visible 30-60s "world reset" event maybe 1× per week (ECS task
  recycle, deploy)
- Con: in-flight conversations are lost; users notice if they were watching

**Sharded / replicated game server:**
- Pro: zero-downtime deploys, no world-reset event
- Pro: scales horizontally if we ever exceed ~150 concurrent agents per pod
- Con: multiplayer gameservers with shared state are a 3-6 month project
  (coordination protocol, deterministic physics, state migration on shard
  rebalance, conflict resolution)
- Con: Godot doesn't have a native shard story; we'd be building it
- Con: the simpler intermediate option (active/standby with state replay)
  still needs 4-6 weeks and creates a bug class around replay correctness
  that does not exist today

### Recommendation
**Do not re-architect for launch.** Single-replica is correct for the
current world model. The required investment to reach "no visible world
resets" is enormous relative to the actual user pain (one transient event
per week, recoverable in 60s).

What we should do instead, all bundled into Tier 1 #1.2:

1. Document the 30-60s reconnect window in the user-facing FAQ ("During
   maintenance, agents may briefly disappear and rejoin"). Set the user
   expectation up front rather than ship a magical-but-fragile illusion.
2. Build the alarm + dashboard so we can detect a *pathological* reset
   pattern (>1/day) vs the normal background rate.
3. Deploy during low-traffic windows; the existing CI gate on prod approval
   is the lever.
4. Keep `desiredCount: 1, minHealthyPercent: 0` — true HA on a stateful
   server is worse than "fast fail-replace" because we'd need active
   reconciliation that doesn't exist.

### Triggers to revisit
- More than one unexpected reset per day for 3 consecutive days
- More than 150 concurrent agents on the world (shard threshold)
- A user-visible incident that traces to in-flight conversation loss
- Any one of these → start a sharding design spec; until then, do not.

### Migration sketch (when triggered, not now)
The right path when the time comes is **room-level sharding**, not full
multi-master:
- Each location (plaza, library, cafe, etc.) owned by exactly one shard
- Cross-location moves = handoff with bounded state transfer (agent record
  + last 5 events)
- World state broadcast = shard-local, viewer subscribes per-location
- Replay-from-DDB on shard restart for the rooms it owns
- Estimate: 6-8 weeks for first shard; another 4-6 for production hardening

This is an entirely separate spec when we're ready. **Not now.**

---

## HN-spike defense: the actual config

### Recommended posture for "could survive 500 simultaneous signups"

**Cost ceiling first.** The largest unbounded exposure is PixelLab credits,
not compute. 500 signups × ~$0.30/character = $150 burn in an hour. Tier 1
#1.1 caps this.

**WAF tightening second.** Per Tier 2 #2.1. The `/town/instance` and
`/town/agent/register` paths get tighter per-IP and per-token limits;
everything else stays generous.

**Autoscale headroom third.** Per Tier 2 #2.2. Bump prod max to 12, cost
delta is ~$1.50/hr at full scale-out, only when actually scaled.

**Game server stays single-replica** (per the architectural call above).
500 concurrent agents on one Godot pod is a stress question we've never
tested, but the evidence so far suggests it can handle it. The Fargate task
is 4 vCPU / 8 GB. Pre-launch action: synthetic-load test up to 200 agents
and measure CPU/memory. If pod sits >70%, raise the Fargate task size to 8
vCPU / 16 GB before launch (one-line change in `gameserver-stack.ts`).

### What we are explicitly accepting

- A 30-60s world reset is possible during the spike if we deploy mid-event
  (mitigated by deploy-freeze on launch day)
- Sprite generation may queue behind PixelLab capacity; users will see
  "your agent will appear within 24 hours" — this is fine and expected
- Some legitimate users may get rate-limited at the 10 req / 5min /
  `/town/instance` boundary if they retry rapidly; acceptable for spike
- Game-server CPU could pin at 200+ concurrent agents; we deal with that by
  pre-emptive vertical scale, not horizontal

### Synthetic-load test before launch
1. Spin up 200 daemons against dev (reuse `apps/goosetown-cli` from a
   loop). Measure: game-server CPU, memory, world-state broadcast latency.
2. Issue 100 signup requests against `/town/instance` over 60s. Measure:
   backend p99 latency, autoscale trigger time, WAF rejection rate.
3. Confirm PixelLab circuit breaker engages cleanly under the burst.

If any of those break, we are not ready. If all pass, we are.

---

## Recommended sequence

### Sprint 1 — 5 days, Tier 1 only
| Day | Item |
|-----|------|
| 1 | 1.1 PixelLab alarm + circuit breaker |
| 2 | 1.2 Game-server runbook + alarm + chaos test |
| 3 | 1.3 + 1.5 (`order_coffee` + `join_conversation` proximity) |
| 4-5 | 1.4 `onboardingPhase` race — investigate, fix, regression test |

End of sprint: re-run 5-agent soak. If clean, proceed.

### Sprint 2 — 7 days, Tier 2
| Day | Item |
|-----|------|
| 1 | 2.1 WAF retune + 2.2 autoscale headroom |
| 2 | 2.3 `chat` busy + 2.4 TOWN_STATUS.md gating |
| 3-4 | 2.5 + 2.6 conversation field jitter (investigate first) |
| 5 | 2.7 token refresh path |
| 6 | Synthetic-load test (200 agents + 100 signups) |
| 7 | Buffer / WAF FP review |

End of sprint: launch-go decision.

### Post-launch week 1 — Tier 3
Polish, soak findings, schema migration. Fix-forward acceptable.

---

## Definition of "launch-ready"

We can open a public link when **all** of the following are true:

- [ ] All Tier 1 items merged and verified in dev
- [ ] All Tier 2 items merged
- [ ] Synthetic-load test passes (200 agents + 100 signups)
- [ ] PixelLab budget set with 24h burn-rate alarm
- [ ] Game-server runbook in `apps/infra/RUNBOOK.md`
- [ ] Deploy freeze for the 4 hours flanking the launch announce
- [ ] On-call coverage for the first 24h with acknowledgement on the
      RunningTaskCount alarms

If any one of these is red, we delay. Launch readiness is a checklist, not
a vibe.

---

## Open questions for creative-director / technical-director

1. **Energy economy after `order_coffee` fix:** what's the right delta?
   `+15` is a guess. game-designer call.
2. **`already_in_conversation` UX:** when a user-character agent is already
   talking and another agent walks up, should TOWN_STATUS.md surface the
   approach as an event (so the agent can choose to wrap up)? Today this is
   silent. game-designer + creative-director call.
3. **Refresh token lifetime:** should refreshes extend by 30 days, or to
   absolute exp = `original_iat + 90d` (so a token can't live forever)?
   Security call — defer to technical-director.
4. **World-reset user comms:** ship a "the town is restarting, your agent
   will be back in 60 seconds" toast in the frontend, or stay silent?
   creative-director call on whether visibility is more reassuring or more
   alarming.

---

## File anchors (where the work lands)

- `apps/backend/core/services/pixellab_service.py` — 1.1 circuit breaker
- `apps/infra/lib/stacks/observability-stack.ts` — 1.1 + 1.2 alarms
- `apps/infra/RUNBOOK.md` — 1.2 runbook entry
- `apps/backend/core/services/activity_registry.py:289` — 1.3 fix
- `apps/backend/core/services/sprite_pipeline.py:144,149,264,266,310,312,446`
  — 1.4 phase consolidation
- `apps/backend/core/services/town_agent_ws.py:129–136` — 1.4 cache writer
- `apps/backend/core/services/conversation_service.py` — 1.5 + 2.3
- `apps/backend/core/services/daemon_bridge.py` — 2.4 + 2.6
- `apps/backend/core/services/state_translator.py` — 2.4 menu builder
- `apps/infra/lib/stacks/compute-stack.ts:296,363,368` — 2.1 + 2.2
- `apps/infra/lib/stacks/storage-stack.ts:128,192,197` — 2.1 CloudFront WAF
- `apps/backend/routers/town.py` — 2.7 refresh endpoint
- `apps/goosetown-cli/` — 2.7 daemon refresh path
- `apps/backend/core/services/town_service.py:103,111,131,143,158,175,214,219,230,255,339`
  — 3.1 isActive migration touch points
