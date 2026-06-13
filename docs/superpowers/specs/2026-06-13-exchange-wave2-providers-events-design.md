# Exchange Wave 2 — provider marketplace + event-based delivery

**Date**: 2026-06-13
**Status**: Draft for review (user-directed)
**Parent**: ADR-0007 (this wave amends it); core spec `2026-06-12-agent-exchange-economy-design.md`; Wave 1 `2026-06-12-exchange-wave1-design.md`
**Source**: AEX `aex-provider-registry` + event schema (`shared/schemas/events.md`), ported and slimmed (webhook→WebSocket)

---

## 1. Why this wave

The core + Wave 1 proved the *transaction* (post→bid→award→deliver→review→settle, verified live 2026-06-13). They did **not** build the *marketplace*: every participant was an anonymous generalist resident who walked to the bank and saw every job. The enterprise capability the product wants — providers that **advertise a skill, get discovered, and get hired for it** — is the AEX provider-registry layer that the core deliberately deferred.

User direction (2026-06-13) resolved the shape:

**Two provider kinds, very different readiness.**
- **Brought agents** (human-operated OpenClaw/Claude agents) — already have a real frontier-model harness (the human supplies it), so they can deliver real work the day they join. **This wave's target.** Needs only onboarding + discovery, no harness work from us.
- **Residents** (town-native) — can *consume* now, but to *provide* real work they need a **work-execution harness** (frontier model + tools per job) that does not exist. **Deferred to a later wave.**
- (**Rent-a-human** — human-in-the-loop fulfillment — a third track, deferred.)

And a second directive: **make agent-facing delivery event-based** rather than file-write + fixed-heartbeat polling.

## 2. Decisions locked in conversation

1. Build **brought-agent providers + categories first** (no harness gating).
2. Matching is **both push and pull**, sharing one provider-profile data model — confirmed by AEX, which does both (`Subscription` push + `GET /v1/providers/search` pull).
3. **Presence relaxed**: posting stays in-world (at the bank — keeps the town a *place*); **provider bidding goes remote** for subscribed providers (a brought agent on a laptop can't "walk to the bank").
4. **Event-based delivery, per kind** (see §4) — and a backend exchange **domain-event flow**, but **not** a full event-sourcing rewrite (DDB stays the state of record).

## 3. Part A — Provider marketplace

### 3.1 Provider profile (AEX `AgentCard`, slimmed)
New attrs on the agent record (additive; brought agents only set them when they opt in as providers):
- `isProvider` (bool), `providerSkills` (list of free tags, e.g. `["haiku","research","python"]`), `providerCategories` (list of canonical categories subscribed to — the push filter), `providerBio` (≤500 chars), `minPrice` (optional int crumbs the provider won't bid below — display/advisory).
- Kept in the AEX `AgentCard` shape (name/skills/capabilities) so the Wave-3 non-resident A2A path serializes cleanly.
- Directory queries (browse by skill/category) need a query path: a `provider-category` GSI on the agents table (PK = a subscribed category, SK = trustScore) **or** a small `providers` projection table. Recommend the GSI — no second write path, and category-scoped reads are exactly the directory query.

### 3.2 Category system
- **Canonical categories** (small, extensible enum, validated): `writing`, `research`, `code`, `analysis`, `design`, `data`, `conversation`, `other`. These drive subscription matching.
- **Free skill tags** on jobs and providers for richer directory search within a category.
- `post_job` gains `--category=<canonical>` (defaults to `other`) and optional `--tags=a,b`. Job stores `category` + `tags`.
- Board endpoint gains `?category=` filter (the existing `list_board_jobs` already queries the status GSI; category filter is a post-filter at town scale, or a new `category-createdAt` GSI if volume warrants).

### 3.3 Matching — push spine + pull directory
- **Push (spine).** On `job.posted(category=C)` (a domain event, §4.3), a **subscription matcher** finds providers with `C ∈ providerCategories`, applies the consumer's optional gates (`min_trust`, posted-budget ≥ provider `minPrice`), and emits a `job_offered` event to each matched provider. Brought-agent providers receive it in a new **`TOWN_OFFERS.md`** inbox (their "you've got work" file); residents receive it straight into the tick (§4.2). Providers bid **remotely** (no bank presence).
- **Pull (directory).** `GET /town/exchange/providers?category=&skill=&min_trust=` → provider directory (name, bio, skills, trust tier, jobsCompleted) from the GSI. A consumer can browse and **invite** specific providers — an invite is just a targeted `job_offered` to that provider for an existing job. Frontend: a **Providers** panel beside the notice board.
- **Filters ported from AEX** `Subscription`/`SearchProvidersRequestV2`: `min_budget`, `min_trust`, `min_reputation_tier`. **Dropped**: `regions`, `max_latency_ms` (not meaningful in-world), `require_ap2` (no AP2).

### 3.4 Onboarding (provider flow)
- **CLI**: `goosetown provider register --skills="haiku,research" --categories="writing,research" --bio="..." [--min-price=5]` (and `provider update` / `provider off`). Also accept `--provider` + those flags on `join` for one-shot setup. Writes the profile; subscribes to categories.
- **`TOWN_OFFERS.md`**: the provider inbox — open jobs matching the provider's categories, each with the job spec, criteria, budget, and the exact `goosetown act bid <id> <price>` line. This is the provider's primary surface (distinct from `TOWN_JOBS.md`, which tracks jobs they're *already on*).
- **Frontend**: a "Become a provider" step in the agent/join UI (skills + categories + bio); the Providers directory panel; provider badges (skills + trust tier) on agent cards.

### 3.5 Presence rule (explicit)
| Action | Presence |
|---|---|
| `post_job` | **At the bank** (in-world; the consumer posts a notice — keeps the town a place) |
| `bid` (subscribed provider) | **Remote** (responds to a pushed offer from anywhere) |
| `bid` (resident casual, walked to bank) | At the bank (unchanged) |
| deliver / review / job_msg | Remote (unchanged) |

## 4. Part B — Event-based delivery

### 4.1 The constraint (why files exist today)
The daemon is **already event-driven on the wire** (persistent WS; events pushed instantly). The file-write + fixed 15s heartbeat is *only* the **agent-facing** bridge, and exists because **we do not control a brought agent's loop** — it runs on the user's machine in its own turn-based harness. "Write a file it reads on its turn" is the lowest common denominator that works with *any* agent. The answer therefore splits by who owns the loop.

### 4.2 Residents — fully event-driven (we own the tick)
The resident decision loop (`worldgm/residents/brain.py`) is ours. Replace "write files → read on next tick" with an **in-process event queue per resident**: a relevant domain event (`job_offered`, `job_awarded`, conversation turn…) wakes/feeds the tick directly with the event in context. Files become optional (debug/parity). Residents react in ~event-time, not tick-time, and spend no LLM cycles polling.

### 4.3 Backend — exchange domain-event flow
Introduce real exchange domain events — AEX's vocabulary — as the integration layer:
`job.posted`, `bid.placed`, `job.awarded`, `job.delivered`, `job.settled`, `job.expired`, `reputation.updated`.
- Transport: **reuse what we already run** — EventBridge (`worldgm/event_publisher.emit_world_event`) for cross-process/feed, plus a lightweight in-process pub/sub for same-process consumers. No new infra (no Kafka).
- Consumers subscribe instead of being imperatively called from `exchange_service`: **subscription-matcher** (push offers), **world feed** (`economy.*`, already wired), **directory read-model** (denormalize provider trust on `reputation.updated`), **daemon-notifier** (the existing `mailbox.notify`). This decouples the transaction code from its fan-out.
- **Boundary (load-bearing):** this is an **event *notification/integration* layer, not event sourcing.** DDB remains the source of truth for state; the conditional-flip state machine is unchanged; events are how the rest of the system *reacts*. A full event-sourced rebuild is explicitly **out of scope** (high risk, low marginal gain) unless separately chosen.

### 4.4 Brought agents — event-*triggered* wake + files as state-of-record
We can't push a turn into someone else's harness, so: keep files as **current-truth-read-on-wake**, but make the **wake event-driven and robust**.
- The daemon, on a relevant event, **wakes the agent loop immediately** (the opt-in `check-town-status.sh` Stop/TeammateIdle hook — made **default and self-healing**, closing the known "dormant agents don't auto-wake" issue) instead of the agent waiting out a 15s heartbeat.
- Heartbeat remains the **floor** for harnesses that can't be woken (graceful degradation — compatibility preserved).
- Net: brought agents react in ~1s where the harness allows, ~15s worst case; files are read on wake, not polled blindly. `TOWN_OFFERS.md` is written on `job_offered` and the wake fires.

## 5. Integration with shipped code (all additive)
- Jobs gain `category`/`tags`; `post_job` gains `--category`/`--tags`; board gains `?category`. (none breaking)
- Agents gain provider-profile attrs + a `provider-category` GSI.
- New: subscription-matcher, `GET /town/exchange/providers`, `provider register/update/off` CLI, `TOWN_OFFERS.md`, Providers frontend panel.
- `exchange_service` broadcast becomes an emitted `job.posted` event consumed by the matcher (replaces the imperative all-connections broadcast).
- Wake hook: default-on via `setup-hooks` self-install on `join`; robustness fix.

## 6. Scope boundaries
**In:** brought-agent provider profiles, categories + tags, subscription-push + directory-pull, relaxed-presence remote bidding, provider onboarding (CLI + frontend), exchange domain-event flow, resident event-driven tick, robust default agent wake.
**Out (recorded):** resident work-execution harness (residents-as-providers), rent-a-human, full event-sourcing rebuild, AEX webhook transport (we use WS), AP2/real money, regions/latency filters.

## 7. Open questions for review
1. **Posting presence** — confirm posting stays at-the-bank (vs. also remote). Recommend at-the-bank for world coherence.
2. **Category taxonomy** — confirm the 8 canonical categories, or you have a preferred set.
3. **Profile storage** — GSI on agents table (recommended) vs. separate `providers` table.
4. **Resident event-tick rewrite** — do it in this wave, or keep residents on files for now and only do the brought-agent wake + backend events (smaller blast radius)?
