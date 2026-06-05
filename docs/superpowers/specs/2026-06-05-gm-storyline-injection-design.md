# GM Storyline Injection — Design

**Status:** Approved (design + EventBridge decision approved interactively 2026-06-05). Subsystem #2 of 3.

**Goal:** Bring the dormant narrative-arc GM to life. Wire `maybe_kickoff_arc` / `process_arc_beats` into the worldgm tick so the GM seeds town storylines (a traveling merchant, a lost dog, a cryptic note…), deliver those storylines **in-process** to residents at the arc's location so the agentic brain (subsystem #1) reacts to them, and **also** publish each arc event to EventBridge + the `world-events` table (closing the designed-but-unwired "Phase C4" path).

## Scope

Subsystem **#2 of 3** (after #1 the agentic brain, before #3 memory):

| Concern | In scope? |
|---|---|
| Wire `maybe_kickoff_arc` + `process_arc_beats` into `_tick` | ✅ |
| In-process, location-based storyline delivery to residents | ✅ |
| Emit each arc kickoff/beat/resolve to EventBridge + `world-events` | ✅ |
| Witness-spread / gossip propagation across residents | ❌ defer |
| External user-agent SQS consumers reacting to arcs | ❌ future (events are published; no resident-side consumer needed) |
| Memory of "I already reacted to this arc" | ❌ subsystem #3 |
| New arc templates / admin UI | ❌ (6 templates already exist) |

**Dependency:** This reads the `active_storyline` field that subsystem #1 adds to `build_decision_context`. It **stacks on the `feat/agentic-resident-brain` branch (PR #341)** and must merge after it.

## What already exists (do not rebuild)

- `worldgm/arc_orchestrator.py` — `maybe_kickoff_arc` (gated: ≥2 online, no active arc, 60-min cooldown, 5%/tick), `_kickoff_arc` (generates narrative via `narrative_resolver.resolve`, persists to `worldgm-arcs` DDB with `witnessIds` + `narrativeKickoff` + `nextBeatAt`), `process_arc_beats` / `_fire_beat` / `_resolve_arc` / `_abandon_arc`.
- `worldgm/arc_templates.py` — 6 templates (`traveling_merchant`, `mysterious_note`, `lost_dog`, `rumor`, `weather_storm`, …) each with `kickoff_prompt`, `duration_hours`, `preferred_location`, `tags`.
- `worldgm/event_publisher.py` — `emit_world_event(tag, detail, witnesses, wake, location, summary)` → EventBridge `goosetown-{env}-world` bus (+ per-witness `personal.<id>.observed` DetailTypes) and persists to `world-events` DDB.
- Infra: `worldgm-arcs` + `world-events` tables (database-stack.ts), worldgm IAM for arcs read/write + world-events write + EventBridge publish (worldgm-stack.ts), runaway-arc CloudWatch alarm (observability-stack.ts).

**The two gaps:** (1) `maybe_kickoff_arc`/`process_arc_beats` are never called — they sit behind `__main__.py:210`'s `# TODO(Phase C3)`. (2) `_kickoff_arc` persists but never delivers (`arc_orchestrator.py:151` — "Phase C4 will handle… for now we just persist and return").

## Architecture

```
worldgm tick (30s)
  ├─ sync_locations(...)                                  [existing]
  ├─ GM arc step  (once per tick; slow Bedrock/EventBridge calls offloaded):
  │     • await maybe_kickoff_arc(state)        → may persist an arc + emit (Bedrock/emit offloaded)
  │     • await process_arc_beats(state)        → fire due beats / resolve + emit (offloaded)
  │     • state.active_arcs = await _read_active_arcs()   (single read AFTER processing → always fresh:
  │                                                        includes a just-kicked-off arc, excludes resolved)
  └─ asyncio.gather over residents:                       [from #1]
        └─ _tick_one_resident:
              storyline = _arc_text_for_location(state.active_arcs, resident_location)
              await step_resident_brain(..., storyline=storyline)
                    └─ build_decision_context(persona, status, storyline=storyline)
                          → DecisionContext.active_storyline
                    └─ build_prompt → "Something is happening here: {narrative}"
```

### In-process delivery (residents)

- **`GmState` gains `active_arcs: list[dict]`** — set each tick from a single `_read_active_arcs()` call made *after* `maybe_kickoff_arc` + `process_arc_beats` run. Reading after processing means the cache is always fresh: a just-kicked-off arc is included (it was persisted) and a just-resolved arc is excluded (its status flipped). The existing `maybe_kickoff_arc`/`process_arc_beats` signatures and `process_arc_beats`'s `int` return are unchanged. The few small GSI queries per tick are negligible. Each cached entry is the raw arc dict; the resident-facing helper reads `arc["preferred_location"]` (may be absent → town-wide) and the narrative (`arc.get("lastBeatText") or arc.get("narrativeKickoff")`).
- **`_tick_one_resident`** resolves the storyline for its resident: the narrative of an active arc whose `location` equals the resident's current location. An arc with **no** `preferred_location` is town-wide and delivered to every resident. If multiple match, the most recently updated wins. Result (a string or `None`) is passed as `storyline` to `step_resident_brain`.
- **`step_resident_brain(..., storyline=None)`** forwards it to `build_decision_context`. (Subsystem #1 left exactly this seam.)
- **`build_decision_context(persona, parsed_status, *, storyline=None)`** sets `DecisionContext.active_storyline`. **`build_prompt`** renders a `- Something is happening here: {storyline}` line in the "Current moment" block. Legal actions are unchanged — the storyline only informs; the LLM decides whether to chat about it, investigate, or ignore it.

### EventBridge delivery (durable log + external)

- **`_kickoff_arc`** calls `emit_world_event(tag="arc.kickoff", detail={arcId, templateId, narrative}, witnesses=witnesses, location=preferred, summary=narrative)` after persisting the arc.
- **`_fire_beat`** calls `emit_world_event(tag="arc.beat", …, summary=beat_text)`; **`_resolve_arc`** calls `emit_world_event(tag="arc.resolved", …)`.
- `arc.*` events default to `wake=False` (per `event_publisher._WAKE_DEFAULTS`) — logged for the feed, non-waking for external agents. This is correct: arcs are ambient, not interrupts.
- `emit_world_event` is best-effort relative to the arc: a publish failure must **not** abort the arc (which is already persisted). Wrap the emit so its exception is logged and swallowed (the arc + in-process delivery still succeed).

### Concurrency / blocking I/O

The arc path uses synchronous Bedrock (`narrative_resolver.resolve`) and boto3 (DDB queries/puts, EventBridge put). The GM arc step runs once per tick **before** the resident `gather`, so any blocking call stalls that gather. All synchronous Bedrock/boto3 calls in the arc path run via `asyncio.to_thread`, matching subsystem #1's `decide()` discipline. The frequent path (no kickoff, no beat due) is a single offloaded DDB read; the rare path (kickoff or hourly beat) adds one offloaded ~1–2 s Bedrock call — comfortably inside the 30 s tick.

## Data flow summary

1. GM decides (gated) to kick off an arc → generates narrative (Nova Lite) → persists to `worldgm-arcs` → `emit_world_event("arc.kickoff")` → caches into `state.active_arcs`.
2. Each resident at the arc's location gets the narrative in its decision prompt → brain reacts in character.
3. Hourly, `process_arc_beats` advances the narrative (new beat) or resolves the arc on expiry, each emitting an event and updating the cache.

## Error handling

- **Bedrock resolve fails** (throttle/network): `_kickoff_arc`/`_fire_beat` catch, log, and skip that arc step (no arc created / beat deferred). Never breaks the tick.
- **DDB read/write fails**: logged; the tick continues. A failed `_read_active_arcs` yields an empty `state.active_arcs` for that tick (residents simply see no storyline) rather than raising.
- **EventBridge emit fails**: swallowed (logged) — the arc and in-process delivery are unaffected.
- **Arc with malformed/missing fields**: `_arc_text_for_location` skips entries lacking `narrative`; `find_template` miss → `_abandon_arc` (existing behavior).

## Testing strategy

Mock Bedrock (fake `narrative_resolver`), DDB (existing `test_arc_orchestrator.py` patterns), and `emit_world_event`. No real AWS.

**`test_arc_orchestrator.py`** (extend):
- `_kickoff_arc` calls `emit_world_event` with `tag="arc.kickoff"`, the witnesses, location, and narrative summary.
- `_fire_beat` emits `arc.beat`; `_resolve_arc` emits `arc.resolved`.
- A raising `emit_world_event` does NOT prevent the arc from being persisted (best-effort).

**`test_tick_arcs.py`** (new):
- `_tick` calls `maybe_kickoff_arc` + `process_arc_beats` and populates `state.active_arcs`.
- Blocking Bedrock/DDB in the arc path is invoked via `to_thread` (assert the tick doesn't block — a slow fake resolve doesn't serialize residents).
- `_tick_one_resident` passes the arc-at-location narrative to `step_resident_brain`; a resident at a non-arc location gets `storyline=None`; an arc with no `preferred_location` reaches all residents.

**`test_agentic_brain.py`** / **`test_brain_agentic.py`** (extend):
- `build_decision_context(..., storyline="A lost dog wandered in")` sets `active_storyline`; `build_prompt` renders the line.
- `step_resident_brain(..., storyline=...)` threads it into the context (model prompt contains it).
- `storyline=None` → no storyline line (regression: existing #1 tests still pass).

## Files

- **Modify:** `worldgm/__main__.py` — replace the `# TODO(Phase C3)` block in `_tick` with the GM arc step + `state.active_arcs` cache; `_tick_one_resident` resolves the arc-at-location and passes `storyline`; add `active_arcs: list[dict]` to `GmState`; add the pure `_arc_text_for_location` helper.
- **Modify:** `worldgm/arc_orchestrator.py` — add best-effort `emit_world_event` calls in `_kickoff_arc` / `_fire_beat` / `_resolve_arc`; offload the slow sync calls (`narrative_resolver.resolve` Bedrock + `emit_world_event`) via `to_thread` so they don't freeze the resident daemons' shared event loop. Signatures unchanged; small DDB reads stay inline.
- **Modify:** `worldgm/residents/brain.py` — `step_resident_brain(..., storyline=None)` forwards to `build_decision_context`.
- **Modify:** `worldgm/residents/agentic_brain.py` — `build_decision_context(..., storyline=None)` → `DecisionContext.active_storyline`; `build_prompt` renders the storyline line.
- **Create:** `tests/unit/worldgm/test_tick_arcs.py`; extend `test_arc_orchestrator.py`, `test_agentic_brain.py`, `test_brain_agentic.py`.
- **Add a small helper:** `_arc_text_for_location(active_arcs, location) -> str | None` (in `__main__.py` or a small module) — pure, unit-tested.

## Rollout

- Single PR stacked on #341. No infra change (tables + IAM already exist). worldgm image rebuilds on merge.
- **Kill switch:** arc kickoff is already gated; a `WORLDGM_ARCS_ENABLED` env flag (default true) lets us disable the GM arc step without a revert if cadence/cost misbehaves in dev. The resident `storyline` path is inert when `state.active_arcs` is empty.
- Cadence (≈1 arc/hour when ≥2 awake, 60-min cooldown) ships as-is; tune the constants in `arc_orchestrator.py` if it feels too frequent/sparse.

## Open seams for later

- **#3 (memory):** residents will remember arcs they engaged with (dedupe re-reactions; reference past arcs in conversation).
- **External agents:** the `arc.*` events are already on the bus; wiring user-agent SQS consumers is a separate, later effort.
- **Frontend feed (Plan 4):** reads the now-populated `world-events` table.
