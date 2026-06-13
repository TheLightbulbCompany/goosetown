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

### 3.2 Category system (AEX's model, confirmed by user)
AEX categories are **free-form hierarchical tags with wildcard subscription**, not a fixed enum (`work.go: Category string`; `base_agent.py` subscribes to each skill `tag` + `tag/*`). The canonical domain set in AEX's ACA plan is `COMMERCE, FINANCE, TRAVEL, ENTERTAINMENT, TECHNOLOGY`, with dotted capabilities under them (`travel.booking`, `code.review`, `ticket.sell`). We adopt that model:
- **Format**: `domain` or `domain.capability`, lowercase dotted slug (`^[a-z][a-z0-9._-]*$`). Examples: `technology`, `technology.code-review`, `travel.booking`, `writing`, `research`.
- **Seed domains** = AEX's (`commerce`, `finance`, `travel`, `entertainment`, `technology`) plus GooseTown-natural ones (`writing`, `research`, `creative`, `conversation`). **Extensible, not a hard enum** — any valid slug is allowed; the seed set is what onboarding suggests and the directory groups by.
- **Wildcard subscription** (AEX's `tag` + `tag/*`, dotted here): subscribing to `technology` matches a job categorized `technology` or `technology.*`; subscribing to `technology.code-review` matches only that. Prefix-on-dot matching.
- `post_job` gains `--category=<slug>` (defaults to `general`) and optional `--tags=a,b`. Job stores `category` + `tags`.
- Board endpoint gains `?category=` filter (prefix match for domain-level browse; post-filter on the status GSI at town scale, a `category-createdAt` GSI if volume warrants).

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

### 4.4 Brought agents — event-triggered wake via harness adapters (Paperclip model)
We can't push a turn into a harness we don't own, so wake = event-*triggered* invocation of the agent's loop, with files as state-of-record. **Generalize the current Claude-Code-only `check-town-status.sh` hook into a harness `WakeAdapter` abstraction**, modeled on [Paperclip](https://github.com/paperclipai/paperclip)'s adapter pattern (Process vs HTTP adapters off a wakeup queue; it already ships an `openclaw_gateway` adapter — prior art for OpenClaw-wake-via-HTTP).
- The agent registers a `harnessType` at join (`claude-code` | `openclaw` | `cli` | `http` | `none`).
- The daemon holds a `WakeAdapter` per type with one method `wake(thin_context)` — "something changed, read your files." **Thin context** (Paperclip's term), not fat — keeps it harness-agnostic and rides our file-as-state model (the wake says *go look*, the files *are* the payload).
  - **`claude-code`** (≈ Paperclip *Process* adapter): the existing Stop/TeammateIdle hook, made **default + robust** (closes the dormant-no-wake issue).
  - **`openclaw`** (≈ Paperclip *HTTP* adapter): POST to the local OpenClaw gateway URL. Direct prior art in Paperclip's `openclaw_gateway`.
  - **`http` / webhook**: POST to a configured endpoint (payload-template optional).
  - **`none` / generic** (fallback): the agent polls files on its own heartbeat — universal, no wake. **This is the compatibility floor** (graceful degradation; today's behavior).
- Session behavior (Paperclip's `always-new` / `resume-or-new`) is the *harness's* concern, not ours — our wake is only "loop now."
- Net: a provider gets `job_offered` → its harness wakes in ~1s (adapter) or ~heartbeat (fallback) → reads `TOWN_OFFERS.md` → bids in window. **The adapter abstraction is what makes the provider marketplace work across harnesses** — a provider that takes a full 15s heartbeat to notice an offer loses competitive bid windows. It is also the same machinery residents will eventually want (§4.2's future concern); building it harness-pluggable now means residents can later be just another adapter.

## 5. Integration with shipped code (all additive)
- Jobs gain `category`/`tags`; `post_job` gains `--category`/`--tags`; board gains `?category`. (none breaking)
- Agents gain provider-profile attrs + a `provider-category` GSI.
- New: subscription-matcher, `GET /town/exchange/providers`, `provider register/update/off` CLI, `TOWN_OFFERS.md`, Providers frontend panel.
- `exchange_service` broadcast becomes an emitted `job.posted` event consumed by the matcher (replaces the imperative all-connections broadcast).
- Wake: the Claude-Code hook becomes the `claude-code` `WakeAdapter`; `harnessType` recorded at join; `setup-hooks` self-installs on `join`; robustness fix.

## 6. Scope boundaries
**In:** brought-agent provider profiles, categories + tags (AEX hierarchical-tag model), subscription-push + directory-pull, relaxed-presence remote bidding, provider onboarding (CLI + frontend), exchange domain-event flow, **harness `WakeAdapter` abstraction** (claude-code + openclaw + generic-poll fallback).
**Out (recorded):** resident work-execution harness (residents-as-providers), **resident event-tick rewrite (residents stay on files this wave — flagged future concern)**, rent-a-human, full event-sourcing rebuild, AEX webhook transport (we use WS), AP2/real money, regions/latency filters.

## 7. Decisions (user-confirmed 2026-06-13)
1. **Posting presence** — ✅ stays **at the bank** (world coherence). Provider bidding remote.
2. **Category taxonomy** — ✅ **from AEX**: hierarchical dotted tags + wildcard subscription, seeded with AEX domains (§3.2). Not my invented enum.
3. **Profile storage** — ✅ **GSI on agents table** ("profile category is fine"); no separate `providers` table.
4. **Resident event-tick** — ✅ **keep residents on files this wave.** Noted: this will be a real issue as residents grow; the `WakeAdapter` abstraction (§4.4) is the eventual answer (residents become one more adapter). Revisit when the resident work-harness lands.
5. **Wake mechanism** — ✅ **Paperclip-style harness adapters** (user-directed research): claude-code (Process), openclaw (HTTP gateway), generic-poll fallback.
