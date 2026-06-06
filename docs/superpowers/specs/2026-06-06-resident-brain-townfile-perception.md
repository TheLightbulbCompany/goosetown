# Resident Brain — Perceive Like an External Agent

**Status:** Approved (user-directed 2026-06-06). Fix to subsystem #1.

**Goal:** Make residents reason over the **same world picture external agents get from the town files** — mood, energy, nearby, activities, recent events, and all move options (including "go to town") — instead of the impoverished bespoke perception the agentic brain currently builds. Residents are then just "external agents with a Nova Lite brain."

## The bug

The agentic brain (`agentic_brain.py`) reconstructs a thin subset of what the daemon already renders into `TOWN_STATUS.md`. It **drops mood, energy, recent events, and goal entirely**, and its prompt never mentions them. So the resident LLM is blind to its own mood/energy and the town's event stream. Result: awake residents pick cozy apartment activities forever (333 activities / 0 moves observed at wake-up), never going to town to socialize — because nothing in their perception drives them to. The existing mood/energy + trait system is meant to drive this, but the decision-maker can't see it.

By contrast, an **external agent** reads `TOWN_STATUS.md` (mood `happy (10)`, energy, **Nearby**, activities, action menu incl. `goosetown act move exit` → return to town) + `TOWN_EVENTS.md`, and reasons holistically: "I'm an extrovert, low energy, alone — I'll head to the plaza." That's the loop residents should run.

## Design

Residents keep the same daemon, town files, and act commands as external agents. The only change is the brain's **perception**: it now mirrors `TOWN_STATUS.md` / `TOWN_EVENTS.md` content.

### `DecisionContext` gains
- `mood: str` — rendered label+value (e.g. `"happy (10)"`) from `daemon.state["agent"]["mood"]` via the shared `render_mood`.
- `energy: int` — from `daemon.state["agent"]["energy"]`.
- `recent_events: list[str]` — last ~6 lines of `TOWN_EVENTS.md` (read from `daemon._workspace_path`), the same event stream external agents see. Falls back to `[]` if the file is absent.

### `read_status` gains
`mood`, `energy` (from `state["agent"]`), and `recent_events` (tail of `TOWN_EVENTS.md`). These join the existing `nearby` / `active_conversation` / `pending_messages` so the brain reads the full daemon-sourced world view, exactly like the file an external agent reads.

### `build_prompt` changes (the heart of the fix)
Render the full picture an agent would reason over:
```
You are {display_name}, a resident of GooseTown.
Personality: {personality}
Your traits: {traits}

How you feel: {mood}, energy {energy}/100.
Where you are: {location}.
Nearby: {names or "no one — you're on your own"}.
Recently:
  {recent_events bulleted, or "nothing of note"}
{conversation transcript if any}
{activities here, if any}

You live by your own personality, mood, and energy — like a real person with
a day to fill. If you're social and no one's around, go where people gather
(the plaza, café, library). If your energy is low, rest or head home. Don't
just sit in one spot.

Choose ONE action. Reply with a single JSON object:
{legal action schemas, including move to town locations}
```
The persona + mood/energy framing makes the LLM weigh socializing vs solitude the way the trait system intends — no bespoke "social drive" flag.

### Move options
The legal-action move list stays the town set (`plaza/library/cafe/activity_center/bank`) plus `residence` (home). The backend already transparently transitions an apartment-bound resident who picks `move plaza` (cross-context). So a homebound resident **can** choose to go socialize; the prompt now gives it a reason to.

### Unchanged
`parse_action`, `validate_action`, `to_command`, dispatch, the pre-gates (sleep/turn-taking), the FSM fallback. This is a perception+prompt enrichment, not a rewrite.

## Secondary: mood/energy actually update

Observed: resident `mood=None`, `energy=100` frozen despite 333 activities. Verify the activity-completion → mood/energy writeback (`activity_service.apply_activity_effect` → persisted to the agent record → broadcast to the daemon so `state["agent"]["mood"]` updates). If broken, fix so `TOWN_STATUS.md` shows live mood and the brain has a real signal. (If non-trivial, ship the perception fix first and file the writeback separately — the brain reading mood is the structural fix either way.)

## Testing

- `build_decision_context` populates mood/energy/recent_events from a fake daemon (state + a temp `TOWN_EVENTS.md`).
- `build_prompt` renders the mood/energy line, the recent-events block, and the "go where people gather" framing; omits blocks when empty.
- `read_status` surfaces mood/energy/recent_events; tail-reads `TOWN_EVENTS.md`; tolerates a missing file.
- Existing brain tests still pass (FSM fallback + pre-gates unchanged).
- Mock model: a resident alone at home with low energy → the prompt contains its mood/energy + the go-to-town option (we assert the prompt, not the LLM's choice).

## Files
- Modify: `worldgm/residents/agentic_brain.py` (DecisionContext + build_decision_context + build_prompt), `worldgm/residents/brain.py` (read_status: mood/energy/recent_events).
- Tests: extend `test_agentic_brain.py`, `test_brain_agentic.py`.
- Investigate: `core/services/activity_service.py` mood writeback (secondary).

## Rollout
Single PR on top of main. worldgm-only; redeploys on merge (expect a brief reconnection blip). The brain still falls back to the FSM if `model=None`, so no hard dependency.
