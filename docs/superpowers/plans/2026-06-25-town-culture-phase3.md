# Town-Level Culture — Phase 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Differentiate residents into a society — stable roles + memory-grounded evolving social goals (→ emergent factions) + a shared town-lore digest — all flag-gated off in prod.

**Architecture:** Worldgm-side only, building on Phase 2 memory. Two flags: `RESIDENT_SOCIAL_GOALS_ENABLED` (roles + social goals) and `TOWN_LORE_ENABLED` (GM-synthesized lore digest). No new DynamoDB table (lore reuses the existing `world-events` table; social goals live on the resident runtime, grounded in the Phase-2 `memories` table). Social-goal generation mirrors Phase-2 `reflection.synthesize_reflection`; lore synthesis mirrors the GM arc step.

**Tech Stack:** Python 3.12, pytest, boto3, AWS CDK (TS). Backend: `cd apps/backend && uv run pytest`. Infra: `cd apps/infra && npx jest` (NOT pnpm).

## Global Constraints
- Branch base `feat/resident-cast-bibles` (has Phases 1+2). Do NOT rebase onto main.
- Both flags default **false**; flag-off behavior is exactly Phase-2 behavior.
- Worldgm-side only; no `conversation_service`/CLI-daemon changes; no new DynamoDB table.
- All new LLM calls are Nova Lite (`model_lite`), best-effort (any model/DDB failure logs + no-ops, never aborts a tick or conversation; `asyncio.CancelledError` must propagate — use `except Exception`).
- Follow the Phase-2 patterns: `worldgm/residents/reflection.py` (LLM-gen + tolerant JSON + `asyncio.to_thread`), the `step_resident_brain` memory block (gating + best-effort), `worldgm/residents/memory_store.py` (injectable store). Read them before writing analogous code.
- Commit only each task's files; never stage the unrelated working-tree files (session-logs, video-marketing, skills-lock). `uv run ruff check <dirs>` before each backend commit.

---

### Task 1: Config flags
**Files:** Modify `apps/backend/core/config.py`; Test `apps/backend/tests/unit/test_config.py`.
**Produces:** `settings.RESIDENT_SOCIAL_GOALS_ENABLED: bool`, `settings.TOWN_LORE_ENABLED: bool`.
- [ ] **Step 1: failing tests** — mirror the existing `test_resident_memory_flags_*` tests (reload `core.config`, assert both default `False`, and `"true"` → `True`).
- [ ] **Step 2: run → fail.**
- [ ] **Step 3: implement** — next to the Phase-2 flags in `class Settings`:
```python
    # Phase 3: town-level culture. Both default false; prod stays off until a dev soak.
    RESIDENT_SOCIAL_GOALS_ENABLED: bool = os.environ.get("RESIDENT_SOCIAL_GOALS_ENABLED", "false").lower() == "true"
    TOWN_LORE_ENABLED: bool = os.environ.get("TOWN_LORE_ENABLED", "false").lower() == "true"
```
- [ ] **Step 4: run → pass.**
- [ ] **Step 5: commit** — `git add apps/backend/core/config.py apps/backend/tests/unit/test_config.py` → `feat(worldgm): Phase-3 config flags (social goals, town lore)` + trailer.

---

### Task 2: Roles on Persona + decision-prompt rendering (role + social goal + faction gravity)
**Files:** Modify `apps/backend/worldgm/residents/personas.py` (Persona field + author 19 roles), `apps/backend/worldgm/residents/personas_prod.py` (set `role=`), `apps/backend/worldgm/residents/agentic_brain.py` (DecisionContext fields, build_decision_context copy, build_prompt render). Test `apps/backend/tests/unit/worldgm/residents/test_agentic_brain.py`.
**Produces:** `Persona.role: str = ""`; `DecisionContext.role: str = ""`, `DecisionContext.social_goal: str = ""`; rendered in `build_prompt`.
- [ ] **Step 1: failing tests** — (a) `DecisionContext` has `role` and `social_goal` defaulting `""`; (b) `build_decision_context` copies `role=persona.role` and `social_goal=parsed_status.get("social_goal","")`; (c) `build_prompt` renders the role + goal + a faction-gravity nudge when set (assert the goal text + a phrase like "spend time with" appears), and renders nothing extra when both empty.
- [ ] **Step 2: run → fail.**
- [ ] **Step 3: implement** —
  - `personas.py`: add `role: str = ""` to `Persona` (after `disagreeableness`), with a comment: "stable social archetype that differentiates behavior (Phase 3)".
  - `agentic_brain.py`: add `role: str = ""` and `social_goal: str = ""` to `DecisionContext`; in `build_decision_context` add `role=persona.role` and `social_goal=parsed_status.get("social_goal", "")`.
  - `build_prompt` (the decision prompt): after the nearby/relationship block and before the action menu, add (only when set):
```python
    if ctx.role or ctx.social_goal:
        bits = []
        if ctx.role:
            bits.append(f"You are {ctx.display_name}, the town's {ctx.role}.")
        if ctx.social_goal:
            bits.append(f"Right now you want to: {ctx.social_goal}.")
        bits.append("Gravitate toward people you click with, steer clear of those you clash with, and act on what you're after — don't drift aimlessly.")
        lines.append(" ".join(bits))
```
  - `personas_prod.py`: add a one-line `role=` to each of the 19 personas (e.g. Dwight → "self-appointed enforcer of order", Obama → "elder statesman who mentors", Dracula → "nocturnal provocateur", LeBron → "ambitious legacy-builder", Biden → "folksy dealmaker", etc. — one short archetype each, divergent across the cast).
- [ ] **Step 4: run → pass** (`uv run pytest tests/unit/worldgm/residents/ -q`).
- [ ] **Step 5: commit** — scoped `git add` → `feat(worldgm): resident roles + social-goal/faction-gravity in the decision prompt` + trailer.

---

### Task 3: `social_goal.py` — memory-grounded goal generation
**Files:** Create `apps/backend/worldgm/residents/social_goal.py`; Test `apps/backend/tests/unit/worldgm/residents/test_social_goal.py`.
**Produces:** `async generate_social_goal(model, store, my_id, persona, relationship_summary, now_iso) -> str` (one Nova-Lite call, grounded in `persona.role` + `reflection.recall_self(store, my_id, now_iso)` + `relationship_summary`; returns a single short aim, `""` on failure).
**Consumes:** Phase-2 `reflection.recall_self`, `memory_store.MemoryStore`.
- [ ] **Step 1: failing tests** (fake model + fake store, mirror `test_reflection.py`):
  - returns the model's goal text (strip/clean to one short line) on a normal reply;
  - returns `""` on a model exception (wrap a raising fake model);
  - the prompt handed to the model includes the role and any self-reflections (assert via a capturing fake model that the role string appears in the prompt).
- [ ] **Step 2: run → fail.**
- [ ] **Step 3: implement** — mirror `reflection.generate_dialogue`/`reflect_on_conversation`: build a prompt from `persona.role`, `reflection.recall_self(store, my_id, now_iso)`, and `relationship_summary`, ask for ONE short concrete social aim (plain text, ≤140 chars), call via `asyncio.to_thread(model.sample_text, prompt, max_tokens=60, temperature=0.8)`, strip to one line, cap length; `try/except Exception` → `""`.
- [ ] **Step 4: run → pass.**
- [ ] **Step 5: commit** — `git add apps/backend/worldgm/residents/social_goal.py apps/backend/tests/unit/worldgm/residents/test_social_goal.py` → `feat(worldgm): social_goal — memory-grounded resident goal generation (Nova-Lite, best-effort)` + trailer.

---

### Task 4: Social-goal stepper integration
**Files:** Modify `apps/backend/worldgm/residents/behavior.py` (ResidentRuntime fields), `apps/backend/worldgm/__main__.py` (`_tick_one_resident`: periodic gen + inject). Test `apps/backend/tests/unit/worldgm/residents/test_brain_memory.py` or a new `test_social_goal_integration.py`.
**Produces:** periodic social-goal regen + `parsed_status["social_goal"]` injection, gated by `RESIDENT_SOCIAL_GOALS_ENABLED`.
- [ ] **Step 1: failing tests** — drive `_tick_one_resident` (reuse the existing tick test harness in `test_tick_concurrency.py`): (a) flag off → no goal gen, `parsed_status` gets no social_goal; (b) flag on + `ticks_since_social_goal >= _SOCIAL_GOAL_EVERY_N` + store present → `generate_social_goal` called, `runtime.social_goal` set, counter reset; (c) the runtime's `social_goal` is injected into `status["social_goal"]` each tick before the brain step. Inject fakes via monkeypatch.
- [ ] **Step 2: run → fail.**
- [ ] **Step 3: implement** —
  - `behavior.py` `ResidentRuntime`: add `social_goal: str = ""`, `ticks_since_social_goal: int = 0`.
  - `__main__.py` `_tick_one_resident`: (1) before the brain step, set `status["social_goal"] = runtime.social_goal` (and `status` already carries role via persona). Increment `runtime.ticks_since_social_goal`. (2) After the brain step (when `settings.RESIDENT_SOCIAL_GOALS_ENABLED and agent_id` and not in a conversation and `ticks_since_social_goal >= _SOCIAL_GOAL_EVERY_N`): build `store = memory_store_from_env()`; if not None, build a short `relationship_summary` (best-effort — e.g. reuse the resident's recent memory subjects, or pass ""), `runtime.social_goal = await generate_social_goal(model_lite, store, agent_id, persona, relationship_summary, _now_iso())`, reset counter to 0 ONLY after the call (mirror the Phase-2 store-None guard fix — only reset when store is not None). Best-effort try/except. Add `_SOCIAL_GOAL_EVERY_N = int(os.environ.get("WORLDGM_SOCIAL_GOAL_EVERY_N", "10"))`.
- [ ] **Step 4: run → pass** (`uv run pytest tests/unit/worldgm/ -q`).
- [ ] **Step 5: commit** — scoped → `feat(worldgm): periodic social-goal regen + injection in the resident tick (flag-gated)` + trailer.

---

### Task 5: `town_lore.py` — GM lore synthesis + injection
**Files:** Create `apps/backend/worldgm/town_lore.py`; Test `apps/backend/tests/unit/worldgm/test_town_lore.py`; Modify `apps/backend/worldgm/__main__.py` (GmState field, `_tick` hook, boot warm-up, inject into `_tick_one_resident`), `apps/backend/worldgm/residents/agentic_brain.py` (`DecisionContext.town_lore` + render in `build_prompt`/`build_dialogue_prompt`). Test the prompt render in `test_agentic_brain.py`.
**Produces:** `async synthesize_town_lore(state, now_ms) -> list[str]`; `async warm_boot_lore(state) -> None`; `GmState.town_lore: list[str]`; `DecisionContext.town_lore: str` rendered in prompts.
**Consumes:** existing `world_events_table` (read recent events; write `tag="town.lore"` items via the existing event publisher), `state.model_lite`.
- [ ] **Step 1: failing tests** — for `town_lore.py` (inject a fake events-table + fake model + fake state): (a) `synthesize_town_lore` reads recent events, one model call → appends 1–3 lore lines to `state.town_lore`, caps the list at ~12 (FIFO), and writes `tag="town.lore"` event items; (b) returns `[]`/no-op when the model fails; (c) `warm_boot_lore` populates `state.town_lore` from recent `tag="town.lore"` rows. For `agentic_brain.py`: `DecisionContext.town_lore` renders in `build_dialogue_prompt` when set, nothing when empty.
- [ ] **Step 2: run → fail.**
- [ ] **Step 3: implement** —
  - `town_lore.py`: mirror `arc_orchestrator`/`reflection` patterns. `synthesize_town_lore(state, now_ms)`: read recent notable `world-events` (use the existing `world_events_table()` accessor + a recency query like the arc code; if that's awkward, accept the events list injected for testability — design the function to take the table/accessor so tests pass a fake), one `asyncio.to_thread(state.model_lite.sample_text, ...)` → 1–3 short lore lines, append to `state.town_lore` (cap 12 FIFO), persist each via the existing event publisher with `tag="town.lore"`. `warm_boot_lore(state)`: read recent `tag="town.lore"` rows → `state.town_lore`. All best-effort.
  - `__main__.py`: add `town_lore: list = field(default_factory=list)` to `GmState`; call `await warm_boot_lore(state)` in `_bootstrap_gm`; in `_tick`, after the arc step, `if TOWN_LORE_ENABLED: await synthesize_town_lore(state, now_ms)` (gate const `TOWN_LORE_ENABLED = settings.TOWN_LORE_ENABLED`, plus a `WORLDGM_LORE_CYCLE_MIN` interval guard tracked on `GmState` so it only fires every N minutes); in `_tick_one_resident`, set `status["town_lore"] = " · ".join(state.town_lore[-3:])` (the 3 most recent lore lines) before the brain step.
  - `agentic_brain.py`: add `town_lore: str = ""` to `DecisionContext`; copy `town_lore=parsed_status.get("town_lore","")` in `build_decision_context`; render in `build_dialogue_prompt` (and/or `build_prompt`) as `if ctx.town_lore: lines.append(f"Town lore everyone knows: {ctx.town_lore}")`.
- [ ] **Step 4: run → pass** (`uv run pytest tests/unit/worldgm/ -q`).
- [ ] **Step 5: commit** — scoped → `feat(worldgm): town-lore synthesis + injection (GM-side, reuses world-events, flag-gated)` + trailer.

---

### Task 6: worldgm-stack env flags
**Files:** Modify `apps/infra/lib/stacks/worldgm-stack.ts`. Test: extend an existing `apps/infra/test/worldgm-stack.test.ts` if present (else verify via `npx tsc --noEmit`).
**Produces:** worldgm container env `RESIDENT_SOCIAL_GOALS_ENABLED` + `TOWN_LORE_ENABLED` (dev `"true"`, prod `"false"`).
- [ ] **Step 1: implement** — in the worldgm container `environment:` block (next to the Phase-2 `RESIDENT_MEMORY_ENABLED`), add `RESIDENT_SOCIAL_GOALS_ENABLED: env === "prod" ? "false" : "true"` and `TOWN_LORE_ENABLED: env === "prod" ? "false" : "true"`. No new table/grant.
- [ ] **Step 2: verify** — `cd apps/infra && npx tsc --noEmit` (exit 0). If `worldgm-stack.test.ts` exists, add an assertion that both env vars are set with the prod/dev gating and run `npx jest worldgm-stack`.
- [ ] **Step 3: commit** — `git add apps/infra/lib/stacks/worldgm-stack.ts` (+ test if added) → `feat(infra): worldgm Phase-3 env flags (social goals, town lore — dev on, prod off)` + trailer.

---

## Validation (after all tasks)
1. `cd apps/backend && uv run pytest tests/unit/worldgm/ tests/unit/test_config.py -q` green; `uv run ruff check worldgm/ core/config.py` clean.
2. `cd apps/infra && npx tsc --noEmit` exit 0; jest green.
3. Dev soak with both flags on: residents show distinct roles/goals, cluster by affinity (factions), cite shared lore + earlier events. Prod stays off until Phases 1–3 all soak.

## Out of scope (tracked)
Per-location cultural-island tracking; meme conversion-graph; faction data structures; dedicated lore table; frontend surfacing.
