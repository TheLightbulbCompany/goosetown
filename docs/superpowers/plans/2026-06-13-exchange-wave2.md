# Exchange Wave 2 Implementation Plan — provider marketplace + event-based delivery

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Execute task-by-task. Authority: spec `docs/superpowers/specs/2026-06-13-exchange-wave2-providers-events-design.md` (behavior) + ADR-0008 (storage architecture). The shipped exchange code is the pattern template (conditional flips, `_notify_outcome`, outcome futures, TOWN_JOBS rendering, moto tests, `_pop_flag` CLI flags).

**Goal:** Brought-agent provider marketplace (profiles, AEX categories, subscription-push + directory-pull, relaxed presence) + harness wake adapters + a backend notification-event flow — on the Wave-1 codebase, additive.

**Architecture (ADR-0008):** DynamoDB stays system-of-record (transactional core untouched). Discovery is a read projection (`provider-subscriptions` table) **behind a `ProviderDirectory` seam** so an OpenSearch swap is later/non-breaking. Offer creation is **synchronous in `post_job`** (not event-driven). Events (EventBridge, already running) feed only cosmetic fan-out.

**Gates (every task):** backend `uv run pytest tests/unit -q` + ruff; CLI `uv run pytest tests/ -q` + ruff; infra `npx jest` + synth (drop/restore backend `.venv`); frontend `pnpm typecheck && lint && test`. TDD: failing test first. Commits end with the Claude trailer. Branch off the latest `main`.

---

### Task 1: Infra — `provider-subscriptions` table + `offeredTo` plumbing
**Files:** `apps/infra/lib/stacks/database-stack.ts`, `apps/infra/test/database-stack.test.ts`; `apps/backend/core/dynamo.py`, `apps/backend/tests/conftest.py`.
- Add table `goosetown-${env}-provider-subscriptions`: PK `category` (S), SK `providerSort` (S, = `f"{trustScore:08.4f}#{providerId}"` so a category query returns subscribers trust-descending via `ScanIndexForward=False`). PITR prod-only, PAY_PER_REQUEST, removalPolicy var. Add to `DatabaseTables` + `this.tables`.
- `dynamo.py`: `provider_subscriptions_table()`. `conftest.py`: create it in `_create_tables` (PK category S, SK providerSort S).
- (No new table for `offeredTo` — it's a `SS` set attr on the existing jobs item.)
- Tests: infra asserts the table + key schema; `test_dynamo.py` asserts the accessor.
- Commit `feat(infra): provider-subscriptions table for Wave 2 discovery`.

### Task 2: `exchange_categories.py` — slug validation + wildcard expansion (pure, TDD)
**Files:** create `apps/backend/core/services/exchange_categories.py`; test `tests/unit/test_exchange_categories.py`.
- `SEED_DOMAINS = ("commerce","finance","travel","entertainment","technology","writing","research","creative","conversation")` (AEX domains + GooseTown).
- `normalize_category(raw) -> str | None`: lowercase, validate `^[a-z][a-z0-9._-]*$` (≤64); None if invalid.
- `wildcard_parents(cat) -> list[str]`: for `technology.code-review` → `["technology.code-review","technology"]` (split on `.`, accumulate prefixes). Used to write subscription rows AND to expand a job's category for matching.
- `match_categories(job_category) -> list[str]`: the keys to look up subscribers under = `wildcard_parents(job_category)`.
- Tests: validation table (valid slugs, rejects spaces/caps/leading-dot/empty); parent expansion (single, dotted, deep); idempotence.
- Commit `feat(exchange): category slug + wildcard helpers (AEX model)`.

### Task 3: `ProviderDirectory` seam + provider profile writes (TDD, moto)
**Files:** create `apps/backend/core/services/provider_directory.py`; extend `exchange_service.py` (profile attrs via `town_service.update_agent_state` field_map); test `tests/unit/test_provider_directory.py`.
- `update_agent_state` field_map gains: `is_provider→isProvider`, `provider_skills→providerSkills`, `provider_bio→providerBio`, `min_price→minPrice`, `provider_categories→providerCategories` (the canonical list lives on the agent for display/audit; the *queryable* index is the projection).
- `ProviderDirectory` class (the ADR-0008 seam — one place a future OpenSearch swap happens):
  - `register(agent_id, skills, categories, bio, min_price)`: validate categories (`exchange_categories`); write agent profile attrs; **rebuild** the provider's subscription rows — delete the agent's existing rows (query by a `providerId-index` GSI on the subs table, or store the agent's category set and diff), then for each category write rows for `category` + each wildcard parent (PK=parent-or-self, SK=`trustSort#agentId`). Comment: "membership index; ADR-0008 ProviderDirectory seam."
  - `unregister(agent_id)`: clear `isProvider`, delete subscription rows.
  - `subscribers_for(category) -> list[dict]`: `wildcard_parents`? NO — matching expands the *job* category; here query the subs table PK=category (the job's category and its parents are queried by the matcher). Return distinct provider profiles (dedup agent_id), trust-desc.
  - `search(category=None, skill=None, min_trust=0.0, limit=25) -> list[dict]`: category → subs-table query (trust-desc); optional in-memory `skill`/`min_trust` filter (town scale; the ADR's >2-filter trigger is where this moves to OpenSearch). No category → small scan of `isProvider` agents (documented town-scale fallback).
- NOTE on subs-table cleanup: add a `providerId-index` GSI (PK providerId) to the subs table in Task 1 so `register`/`unregister` can find-and-delete an agent's rows. (Amend Task 1 if not already.)
- Tests: register writes rows under each category + parent; trust-desc ordering; subscribers_for returns matches incl. via wildcard (provider on `technology` matches job `technology.code-review`); re-register replaces (no stale rows); unregister clears; search skill/min_trust filters.
- Commit `feat(exchange): ProviderDirectory seam + subscription projection (ADR-0008)`.

### Task 4: Categories on jobs + synchronous matcher writing `offeredTo` (TDD)
**Files:** `exchange_service.py` (`post_job`); `core/ws_models.py` (post_job `category`/`tags`); `routers/town.py` dispatch; test `tests/unit/test_exchange_matcher.py`.
- ws_models post_job gains `category: str | None` (validated slug), `tags: list[str] | None` (≤8, each ≤40).
- `post_job(..., category="general", tags=None)`: store `category` (normalized; default `general`), `tags`. **After** the job row is written (escrow already done), run the matcher **synchronously in-request**:
  - `matched = ProviderDirectory().subscribers_for_job(job_category)` (excludes the consumer; applies consumer gates `min_trust`/budget≥minPrice if the consumer set them — add optional `--min-bidder-trust` later, default none).
  - Write `offeredTo = {provider ids}` onto the job (same item; `ADD offeredTo :ids` or SET).
  - Notify each matched provider: daemon event `job_offered` (job_id, title, category, max_price, criteria, the `act bid` line) → CLI writes `TOWN_OFFERS.md`. Best-effort; `offeredTo` is the durable record.
  - Keep the existing all-connections broadcast? **Replace** it: the broadcast becomes "notify offeredTo" (targeted). A job with no subscribers in its category still appears on the public board (pull path) — so discovery isn't lost, it's just not pushed.
- Tests: post with category → offeredTo set to matching subscribers; non-subscribers not notified/offered; consumer excluded from own offer; category default `general`; subscriber via wildcard parent is offered; sealed (offer event carries no other bids).
- Commit `feat(exchange): category-matched synchronous offers (offeredTo) on post_job`.

### Task 5: Remote-bid authz (presence-OR-offer) + invite (TDD)
**Files:** `exchange_service.py` (`place_bid`, new `invite_provider`); `routers/town.py`; test extend `test_exchange_matcher.py` / `test_exchange_lifecycle.py`.
- `place_bid`: the existing at-bank presence check becomes **presence OR offer**: allow if (bidder at bank) OR (bidder ∈ job `offeredTo`). Reject `not_invited` otherwise (message: "You weren't offered this job and aren't at the Exchange — move to the bank or wait to be offered."). Keep `own_household`, `own_job`, `not_open`, rate-limit ahead of it.
- `invite_provider(consumer_conn, job_id, provider_id)`: consumer-only, job OPEN; append provider to `offeredTo` (durable); notify `job_offered` (with an `invited: true` flag). Surfaces the directory→invite path. New WS verb `invite` (job_id, provider_id) + dispatch.
- Resync (`resync_agent_jobs`): a provider with `agent ∈ offeredTo` on an OPEN job gets a resync `job_offered` (reconstructs lost offers — ADR/codex P1).
- Tests: remote bid by offered provider OK; remote bid by non-offered non-present provider → `not_invited`; at-bank bid by non-offered provider OK; invite appends offeredTo + notifies; off-category invited provider can then bid remotely; resync re-offers OPEN offeredTo jobs.
- Commit `feat(exchange): presence-or-offer remote-bid authz + provider invite`.

### Task 6: Directory endpoint + board category filter (TDD)
**Files:** `routers/town.py` (`GET /town/exchange/providers`, board `?category`); `exchange_service.list_board_jobs`; test `tests/unit/test_exchange_endpoints.py`.
- `GET /town/exchange/providers?category=&skill=&min_trust=` (public): `ProviderDirectory().search(...)` → list of `{agent_id, name, bio, skills, trust_tier, trust_score, jobs_completed}`. No wallet/PII.
- `list_board_jobs` gains optional `category` filter (prefix match via `wildcard_parents` on each job's category; post-filter at town scale per ADR-0008).
- `GET /town/exchange/jobs?category=` passes through.
- Tests: directory returns providers in category trust-desc; skill/min_trust filter; empty; board category filter (incl. wildcard).
- Commit `feat(exchange): provider directory endpoint + board category filter`.

### Task 7: Backend notification-event flow (lightweight, TDD)
**Files:** extend `exchange_service.py` `_emit_economy_event` → emit structured `job.*` domain events; ensure the feed/denorm consume them; test `test_exchange_economy_events.py`.
- Confirm/extend: `job.posted`, `job.awarded`, `job.settled`, `reputation.updated` emitted **after** state writes (already partly done in Wave-1's `economy.*`). These feed the world feed + a `reputation.updated`→directory trust denorm (update the provider's subscription-row SK on trust change so directory ranking stays fresh — small consumer).
- **Do NOT** route offer creation through events (synchronous per Task 4 — assert this in a test: a job posted with the event emitter patched-to-raise still creates `offeredTo` + notifies).
- Tests: events emitted at each point; raising emitter doesn't break post/settle/offer; reputation.updated re-ranks the provider's subscription rows.
- Commit `feat(exchange): structured job.* domain events for feed + directory denorm`.

### Task 8: CLI — provider onboarding, TOWN_OFFERS.md, category flags, harnessType (TDD)
**Files:** `apps/goosetown-cli/src/goosetown/core.py` (`provider` subcommand, `post_job --category/--tags`, `join --provider/--skills/--categories/--bio/--harness`), `daemon.py` (`job_offered` handler + `_write_offers`), `cli.py` (subparsers), `pyproject.toml` → 0.5.0; tests `tests/test_exchange_provider_cli.py`, `tests/test_exchange_daemon.py`.
- `goosetown provider register --skills=a,b --categories=writing,research --bio="..." [--min-price=N]` / `provider update` / `provider off` → REST or WS to the backend register/unregister. `provider status` reads cached profile.
- `join` accepts `--provider --skills= --categories= --bio= --harness=claude-code|openclaw|http|none` (records `harnessType`).
- `post_job` gains `--category=` / `--tags=` (via `_pop_flag`, position-independent like the 0.4.1 fix).
- daemon `_on_job_offered`: append a 💼 TOWN_EVENTS line + cache + `_write_offers()` → `TOWN_OFFERS.md`: header (your skills/categories), then each offered open job (title, category, budget, spec, criteria, deadline, `goosetown act bid <id> <price>`). Distinct from TOWN_JOBS.md (jobs you're on).
- Tests: provider flag parsing; post_job category/tags payloads (trailing + leading); `_on_job_offered` writes TOWN_OFFERS.md with the bid line, no event-line duplication on resync; harnessType recorded.
- Commit `feat(cli): provider onboarding, TOWN_OFFERS.md, category flags, harnessType (0.5.0)`.

### Task 9: CLI — harness WakeAdapter abstraction + coalescing (TDD)
**Files:** create `apps/goosetown-cli/src/goosetown/wake.py`; wire into `daemon.py`; tests `tests/test_wake_adapters.py`.
- `WakeAdapter` base: `async def wake(self, agent_id, thin_context: dict) -> None`. Subclasses: `ClaudeCodeAdapter` (ensure/refresh the Stop/TeammateIdle hook — the existing `check-town-status.sh`, made robust + self-installed), `OpenClawAdapter` (HTTP POST to a configured gateway URL), `HttpAdapter` (POST to a configured endpoint), `NoneAdapter` (no-op; relies on heartbeat). Factory `get_wake_adapter(harness_type, config)`.
- **Coalescing (codex P2):** a `WakeCoordinator` per daemon holds one in-flight wake per agent + a debounce window (e.g. 500ms) + a "re-wake pending" flag; a burst collapses to one wake; a wake requested while in-flight sets the pending flag, fired once on completion. The daemon calls `coordinator.request_wake()` on relevant events (job_offered, job_awarded, …) instead of assuming the heartbeat.
- Tests: factory selects adapter by type; burst of N requests → 1 wake (coalesced); request-during-inflight → exactly one follow-up; None adapter no-ops; OpenClaw/Http adapters POST with the right payload (stub the HTTP layer); claude-code adapter ensures the hook file.
- Commit `feat(cli): harness WakeAdapter abstraction with per-agent coalescing (Paperclip model)`.

### Task 10: Frontend — Providers directory + provider badges (design-system)
**Files:** `apps/frontend/src/hooks/useProviders.ts`, `apps/frontend/src/components/ProvidersPanel.tsx`, extend `ApartmentCard.tsx` (provider badge) + agent type; tests.
- Design contract: same pixel-bank aesthetic; providers as a directory list (name, trust stamp chip, skill tags as small chips, bio); category filter as a row of selectable category chips. Reuse existing palette (no new tokens). Provider badge on agent cards = skill chips + "provider" marker.
- `useProviders(category?)` polls `GET /town/exchange/providers`. `ProvidersPanel` mounts beside the ExchangeBoard.
- Tests: renders providers, category filter, empty state; typecheck/lint/test green.
- Commit `feat(frontend): providers directory panel + provider badges`.

### Task 11: Docs + ADR amendment
**Files:** `references/exchange.md` (provider flow, categories, TOWN_OFFERS.md, invite, remote bidding), new `references/providers.md` (how to become a provider), SKILL.md link (≤100 lines), `actions.md` (provider/invite verbs), ADR-0007 (link Wave 2 + ADR-0008), CLAUDE.md (Exchange row, new tables/services/CLI 0.5.0).
- Verify every documented fact against code (the live-test lesson). `test_skill_md` / `test_plugin_manifests` green.
- Commit `docs(exchange): Wave 2 provider/category/wake docs + ADR links`.

### Task 12: Verification + PR
- All gates all packages + `cdk synth`. Final integration review subagent over the diff: crumb conservation unchanged, the synchronous-matcher/offeredTo authz holds (no learn-id-bypass), subscription projection has no stale rows after re-register, wake coalescing is single-flight, directory leaks no PII, category wildcard matching correct. Open PR linking the spec + ADR-0008. CLI 0.5.0 publish only after merge.
