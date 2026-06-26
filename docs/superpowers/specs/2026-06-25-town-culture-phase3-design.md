# Town-Level Culture — Phase 3 Design

**Date:** 2026-06-25
**Status:** Approved (autonomous mandate) → implementation plan
**Builds on:** Phase 1 (conversation craft) + Phase 2 (memory/reflection). Branch base `feat/resident-cast-bibles` (post-#409).

## 1. Goal

Phase 1 made conversations good; Phase 2 made them *accumulate*. Phase 3 makes the accumulation **differentiate residents into a society**: each resident develops a distinct role and pursues short-term social goals (grounded in their Phase-2 memory + relationships), so they seek allies / avoid rivals (factions emerge), and the town builds a small body of **shared lore** everyone can reference. This is the Project-Sid differentiation mechanism (role/social-goal generation) + a shared-culture layer, Nova-right-sized.

**Non-goals (explicitly deferred):** per-location "cultural island" tracking + a meme conversion-graph (heavy, speculative — revisit after a soak); a new lore DynamoDB table (reuse the existing `world-events` table); any change to the human-agent path.

## 2. Key decisions (locked)

- **Two flag-gated sub-features**, both default OFF in prod, ON in dev:
  - `RESIDENT_SOCIAL_GOALS_ENABLED` — resident roles + memory-grounded social goals (→ factions).
  - `TOWN_LORE_ENABLED` — GM-synthesized town-lore digest injected into resident prompts.
- **Worldgm-side only.** Extends the existing resident brain + GM tick. No `conversation_service`/CLI-daemon changes. No new DynamoDB table (lore reuses `world-events`; social goals live on the resident runtime + are grounded in the Phase-2 memories table).
- **Reuse, don't reinvent.** Social-goal generation mirrors Phase-2 `reflection.synthesize_reflection` (Nova-Lite, periodic, best-effort). Town-lore synthesis mirrors the GM arc step (Nova-Lite, in `_tick`, gated like `WORLDGM_ARCS_ENABLED`).
- **Best-effort + cost-bounded.** Social goals regenerate at most every `_SOCIAL_GOAL_EVERY_N` idle ticks per resident (default 10). Lore synthesizes at most every `WORLDGM_LORE_CYCLE_MIN` minutes (default 30) town-wide. All Nova Lite. Any failure → no-op, log, continue.
- **Factions are emergent, not authored.** No faction data structure — factions arise from residents acting on affinity + social goals (the LLM following relationship cues already in `build_prompt`, now sharpened + given a goal to pursue).

## 3. Sub-feature A: Roles + social goals (differentiation → factions)

### 3.1 Roles
Add a stable `role: str = ""` to `Persona` (a one-line social archetype: e.g. Dwight → "self-appointed enforcer of order"; Obama → "elder statesman who mentors"; Dracula → "nocturnal provocateur"). Author for the 19 prod cast. The role is the *fixed* differentiator; the social goal is the *evolving* one.

### 3.2 Social goals
`worldgm/residents/social_goal.py`:
- `async generate_social_goal(model, store, my_id, persona, relationship_summary, now_iso) -> str` — one Nova-Lite call grounded in the resident's `role`, their top self-reflections (`reflection.recall_self`), and a short relationship summary (who they're close to / clash with). Returns a single concrete short-term aim ("patch things up with Dwight", "hold court at the café", "find someone worth debating"). "" on failure.
- The goal is stored on `ResidentRuntime.social_goal: str` and regenerated periodically (every `_SOCIAL_GOAL_EVERY_N` idle ticks, tracked on the runtime), in `_tick_one_resident` — the same shape as Phase-2 periodic reflection.

### 3.3 Injection + faction gravity
- `DecisionContext.social_goal: str = ""` and `role: str = ""`, copied from `parsed_status`.
- `build_prompt` (the decision prompt) renders the role + goal and adds a faction-gravity nudge that uses the relationship colour already shown per nearby agent: *"You're {role}. Right now you want to: {social_goal}. Spend time with people you click with, steer clear of those you clash with, and act on what you're after."* This biases move/chat without any hard weighting — factions emerge from the LLM following affinity + goal.

## 4. Sub-feature B: Town lore (shared culture)

`worldgm/town_lore.py` (GM-side):
- `async synthesize_town_lore(state, now_ms) -> list[str]` — at most every `WORLDGM_LORE_CYCLE_MIN` minutes: read recent notable `world-events` (arc beats/resolutions + any high-signal events) via the existing `world_events_table`, one Nova-Lite call → 1–3 short durable "town lore" lines ("the week Dwight and LeBron launched beet-wine"). Append to a rolling `GmState.town_lore: list[str]` (cap ~12, FIFO) and persist each as a `world-events` item with `tag="town.lore"` (so a GM restart can re-warm the digest by reading recent `tag="town.lore"` rows). Best-effort.
- **Boot warm-up:** on GM start, read recent `tag="town.lore"` rows into `GmState.town_lore`.
- **Injection:** `_tick_one_resident` puts a few salient lore lines into `parsed_status["town_lore"]`; `build_decision_context` → `DecisionContext.town_lore: str`; `build_prompt`/`build_dialogue_prompt` render *"Town lore everyone knows: …"* so residents share cultural references.
- **Hook:** `synthesize_town_lore` is called in `_tick` after the arc step, gated by `TOWN_LORE_ENABLED` (mirrors `ARCS_ENABLED`).

## 5. Config / infra

- `core/config.py`: `RESIDENT_SOCIAL_GOALS_ENABLED` (default false), `TOWN_LORE_ENABLED` (default false).
- `worldgm/__main__.py`: knobs `WORLDGM_SOCIAL_GOAL_EVERY_N` (default 10), `WORLDGM_LORE_CYCLE_MIN` (default 30), mirroring the existing `WORLDGM_*` pattern.
- `worldgm-stack.ts`: set `RESIDENT_SOCIAL_GOALS_ENABLED` + `TOWN_LORE_ENABLED` per-env (dev `"true"`, prod `"false"`). No new table, no new grant (the `world-events` table is already granted to the worldgm task; the `memories` table grant from Phase 2 covers social-goal reads).
- `GmState`: add `town_lore: list[str]` (default empty).

## 6. Cost

Social goals: 1 Nova-Lite call per resident per ~10 idle ticks (~5 min) → modest. Lore: 1 Nova-Lite call per ~30 min town-wide → negligible. Both Nova Lite, flag-gated off in prod. Order: low single dollars/month when enabled.

## 7. Validation gate

Dev soak with both flags on: do residents develop visibly distinct roles/goals; do they cluster (factions) by affinity; do conversations start citing shared lore + earlier events; does the town read as a society rather than 19 parallel monologues? Inspect `goosetown-dev-memories`, the `tag="town.lore"` events, and dev conversations. Prod stays off until Phases 1–3 have all soaked.

## 8. Risks

- **Speculative emergence on Nova.** Most validated on GPT-4o/Claude-tier. Flag-gated; dev-soak-gated; degrades to Phase-2 behavior when off.
- **Goal/lore prompt bloat.** Adding role + goal + lore lines to the prompt grows it; keep each to 1–2 lines, cap lore injected to ~3 lines.
- **Cost creep** if intervals too tight — the `_EVERY_N` / `_CYCLE_MIN` knobs bound it; defaults conservative.

## 9. Out of scope (→ later)
Per-location cultural-island tracking; meme conversion-graph / spread analytics; faction data structures; a dedicated lore table; frontend surfacing of roles/lore.
