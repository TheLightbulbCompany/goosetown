# GM Storyline Injection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the narrative-arc GM to life — wire kickoff/beats into the tick, deliver storylines to residents in-process so the agentic brain reacts, and publish each arc to EventBridge + `world-events`.

**Architecture:** The GM arc step runs once per tick (slow Bedrock/EventBridge calls offloaded via `to_thread`), caches active arcs on `GmState.active_arcs`, and each resident at an arc's location gets its narrative threaded into the agentic brain's decision context. `arc_orchestrator` gains best-effort `emit_world_event` calls on kickoff/beat/resolve.

**Tech Stack:** Python 3.12, asyncio, Amazon Bedrock (Nova Lite), Amazon EventBridge + DynamoDB (boto3), pytest. Run from `apps/backend/` with `uv run pytest`.

**Spec:** `docs/superpowers/specs/2026-06-05-gm-storyline-injection-design.md`
**Branch:** `feat/gm-storyline-injection` (stacked on `feat/agentic-resident-brain` / PR #341 — depends on the `storyline` seam #1 adds).

---

## Context the engineer needs

- **Run tests:** `cd apps/backend && uv run pytest tests/unit/worldgm/ -v`
- **Lint:** `cd apps/backend && uv run ruff check . && uv run ruff format --check .`
- `arc_orchestrator.py` functions are `async def` but their bodies are entirely synchronous boto3 + `narrative_resolver.resolve` (Bedrock). The resident daemons' WebSocket loops share this event loop, so the **slow** calls (`resolve` ~1-2 s; `emit_world_event` = EventBridge put + DDB put) must be offloaded with `await asyncio.to_thread(...)`. Small DDB reads (`table.query`) stay inline.
- `emit_world_event(*, tag, detail, witnesses, wake, location, summary) -> str` lives in `worldgm/event_publisher.py`. `arc.*` tags default to `wake=False` via `_WAKE_DEFAULTS` — don't override.
- `GmState` is a mutable `@dataclass` (`worldgm/__main__.py:128`). Its last field is `resident_runtimes: dict = field(default_factory=dict)`.
- The agentic brain seam (subsystem #1, this branch's parent): `build_decision_context(persona, parsed_status)` in `worldgm/residents/agentic_brain.py`; `DecisionContext` dataclass; `build_prompt(ctx)`; `step_resident_brain(daemon, runtime, parsed_status, *, model=None, persona=None, rng=None, now_utc_hour=None)` in `worldgm/residents/brain.py`.
- Existing arc tests (`tests/unit/worldgm/test_arc_orchestrator.py`) mock with `MagicMock` state + `@patch("worldgm.arc_orchestrator.dynamo.worldgm_arcs_table")` and call via `asyncio.run(...)`. Keep that style.
- **TDD:** failing test first; mock Bedrock/DDB/EventBridge; no real AWS.

## File Structure

| File | Change |
|---|---|
| `worldgm/residents/agentic_brain.py` | `build_decision_context(..., *, storyline=None)` → `DecisionContext.active_storyline`; `build_prompt` renders the storyline line. |
| `worldgm/residents/brain.py` | `step_resident_brain(..., storyline=None)` forwards to `build_decision_context`. |
| `worldgm/arc_orchestrator.py` | best-effort `emit_world_event` on kickoff/beat/resolve; offload `resolve` + `emit` via `to_thread`. |
| `worldgm/__main__.py` | `GmState.active_arcs`; `_arc_text_for_location` helper; GM arc step + cache in `_tick`; `_tick_one_resident` passes `storyline`. |
| tests | extend `test_agentic_brain.py`, `test_brain_agentic.py`, `test_arc_orchestrator.py`; new `test_tick_arcs.py`. |

---

### Task 1: Storyline in the decision context + prompt

**Files:**
- Modify: `apps/backend/worldgm/residents/agentic_brain.py`
- Test: `apps/backend/tests/unit/worldgm/residents/test_agentic_brain.py`

- [ ] **Step 1: Write the failing test**

```python
# append to test_agentic_brain.py
def test_context_carries_storyline():
    ctx = build_decision_context(_PERSONA, _status(), storyline="A lost dog wandered into the cafe.")
    assert ctx.active_storyline == "A lost dog wandered into the cafe."


def test_context_storyline_defaults_none():
    ctx = build_decision_context(_PERSONA, _status())
    assert ctx.active_storyline is None


def test_build_prompt_renders_storyline_when_present():
    ctx = build_decision_context(_PERSONA, _status(), storyline="A lost dog wandered into the cafe.")
    prompt = build_prompt(ctx)
    assert "A lost dog wandered into the cafe." in prompt
    assert "happening" in prompt.lower()


def test_build_prompt_omits_storyline_when_absent():
    ctx = build_decision_context(_PERSONA, _status())
    prompt = build_prompt(ctx)
    assert "happening here" not in prompt.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/residents/test_agentic_brain.py -k storyline -v`
Expected: FAIL — `build_decision_context() got an unexpected keyword argument 'storyline'`

- [ ] **Step 3: Write minimal implementation**

In `agentic_brain.py`, add the field to `DecisionContext` (after `legal_actions`):

```python
    legal_actions: frozenset[str]
    active_storyline: str | None = None
```

Change `build_decision_context` signature + the return to thread `storyline` through:

```python
def build_decision_context(persona: Persona, parsed_status: dict, *, storyline: str | None = None) -> DecisionContext:
```

and in the returned `DecisionContext(...)`, add as the final argument:

```python
        legal_actions=legal_actions,
        active_storyline=storyline,
    )
```

In `build_prompt`, render the storyline inside the "Current moment" block — insert right after the `- You are at:` / nearby lines and before the conversation block:

```python
    if ctx.active_storyline:
        lines.append(f"- Something is happening here: {ctx.active_storyline}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/residents/test_agentic_brain.py -v`
Expected: PASS (all, including the 4 new).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/worldgm/residents/agentic_brain.py apps/backend/tests/unit/worldgm/residents/test_agentic_brain.py
git commit -m "feat(worldgm): thread active_storyline into decision context + prompt"
```

---

### Task 2: step_resident_brain forwards storyline

**Files:**
- Modify: `apps/backend/worldgm/residents/brain.py`
- Test: `apps/backend/tests/unit/worldgm/residents/test_brain_agentic.py`

- [ ] **Step 1: Write the failing test**

```python
# append to test_brain_agentic.py
async def test_storyline_reaches_the_model_prompt():
    """A storyline passed to step_resident_brain must appear in the model's prompt."""
    d = _FakeDaemon(_state())

    class _CapturingModel:
        def __init__(self):
            self.prompt = None

        def sample_text(self, prompt, **kw):
            self.prompt = prompt
            return '{"action": "idle"}'

    model = _CapturingModel()
    await step_resident_brain(
        d, _runtime(), read_status(d), model=model, persona=_PERSONA,
        now_utc_hour=12, storyline="A traveling merchant set up a stall.",
    )
    assert "A traveling merchant set up a stall." in model.prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/residents/test_brain_agentic.py -k storyline -v`
Expected: FAIL — `step_resident_brain() got an unexpected keyword argument 'storyline'`

- [ ] **Step 3: Write minimal implementation**

In `brain.py`, add `storyline` to the signature (keyword-only, with the others):

```python
async def step_resident_brain(
    daemon,
    runtime: ResidentRuntime,
    parsed_status: dict,
    *,
    model=None,
    persona=None,
    storyline: str | None = None,
    rng=None,
    now_utc_hour: int | None = None,
) -> None:
```

In the agentic path, pass it to `build_decision_context`:

```python
    ctx = build_decision_context(persona, parsed_status, storyline=storyline)
```

(The `model is None` fallback path ignores `storyline` — the FSM has no prompt.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/residents/test_brain_agentic.py -v`
Expected: PASS (all, including the new one; the existing #1 tests still pass).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/worldgm/residents/brain.py apps/backend/tests/unit/worldgm/residents/test_brain_agentic.py
git commit -m "feat(worldgm): step_resident_brain forwards storyline to the brain"
```

---

### Task 3: Arc events to EventBridge (best-effort, offloaded)

**Files:**
- Modify: `apps/backend/worldgm/arc_orchestrator.py`
- Test: `apps/backend/tests/unit/worldgm/test_arc_orchestrator.py`

- [ ] **Step 1: Write the failing test**

```python
# append to test_arc_orchestrator.py
@patch("worldgm.arc_orchestrator.emit_world_event")
@patch("worldgm.arc_orchestrator.random.random", return_value=0.0)
@patch("worldgm.arc_orchestrator.dynamo.worldgm_arcs_table")
def test_kickoff_emits_world_event(mock_table, mock_rand, mock_emit):
    from worldgm.arc_orchestrator import maybe_kickoff_arc

    mock_table.return_value.query.return_value = {"Items": []}
    mock_table.return_value.scan.return_value = {"Items": []}
    state = _state_with({"alice": "library", "bob": "library"})
    arc = asyncio.run(maybe_kickoff_arc(state))
    assert arc is not None
    mock_emit.assert_called_once()
    kwargs = mock_emit.call_args.kwargs
    assert kwargs["tag"] == "arc.kickoff"
    assert kwargs["summary"] == "kickoff text"


@patch("worldgm.arc_orchestrator.emit_world_event", side_effect=RuntimeError("bus down"))
@patch("worldgm.arc_orchestrator.random.random", return_value=0.0)
@patch("worldgm.arc_orchestrator.dynamo.worldgm_arcs_table")
def test_kickoff_survives_emit_failure(mock_table, mock_rand, mock_emit):
    """A publish failure must NOT prevent the arc from being persisted."""
    from worldgm.arc_orchestrator import maybe_kickoff_arc

    mock_table.return_value.query.return_value = {"Items": []}
    mock_table.return_value.scan.return_value = {"Items": []}
    state = _state_with({"alice": "library", "bob": "library"})
    arc = asyncio.run(maybe_kickoff_arc(state))
    assert arc is not None
    mock_table.return_value.put_item.assert_called_once()  # arc still persisted


@patch("worldgm.arc_orchestrator.emit_world_event")
@patch("worldgm.arc_orchestrator.dynamo.worldgm_arcs_table")
def test_resolve_emits_world_event(mock_table, mock_emit):
    import time

    from worldgm.arc_orchestrator import process_arc_beats

    now = int(time.time() * 1000)
    expired = {
        "arcId": "x", "templateId": "traveling_merchant", "status": "active",
        "expiresAt": now - 1000, "nextBeatAt": str(now - 100), "narrativeKickoff": "k",
    }
    mock_table.return_value.query.return_value = {"Items": [expired]}
    state = _state_with({"alice": "cafe", "bob": "library"})
    asyncio.run(process_arc_beats(state))
    tags = [c.kwargs["tag"] for c in mock_emit.call_args_list]
    assert "arc.resolved" in tags
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/test_arc_orchestrator.py -k "emits or survives" -v`
Expected: FAIL — `AttributeError: <module 'worldgm.arc_orchestrator'> does not have the attribute 'emit_world_event'`

- [ ] **Step 3: Write minimal implementation**

In `arc_orchestrator.py`, add the imports (top, with the others):

```python
import asyncio

from .event_publisher import _emit_metric, emit_world_event
```

Add a best-effort offloaded helper near the top:

```python
async def _emit_arc_event(*, tag: str, arc: dict, summary: str, location: str | None, witnesses: list[str]) -> None:
    """Publish an arc event to EventBridge/world-events. Best-effort: a failure
    is logged and swallowed so the arc (already persisted) is unaffected. Runs
    in a thread because emit_world_event does sync EventBridge + DDB puts."""
    try:
        await asyncio.to_thread(
            emit_world_event,
            tag=tag,
            detail={"arcId": arc["arcId"], "templateId": arc.get("templateId", "")},
            witnesses=witnesses,
            location=location,
            summary=summary,
        )
    except Exception:
        logger.exception("[arc:%s] emit_world_event failed for %s", arc["arcId"][:8], tag)
```

In `_kickoff_arc`, offload the Bedrock resolve and emit after persisting. Change:

```python
    narrative_text = state.narrative_resolver.resolve(template["kickoff_prompt"])
```
to:
```python
    narrative_text = await asyncio.to_thread(state.narrative_resolver.resolve, template["kickoff_prompt"])
```

and after `_emit_metric("ArcsKickoff")` (before the `return arc`), add:

```python
    await _emit_arc_event(
        tag="arc.kickoff", arc=arc, summary=narrative_text, location=preferred, witnesses=witnesses
    )
```

In `_fire_beat`, change:
```python
    beat_text = state.narrative_resolver.resolve(beat_prompt)
```
to:
```python
    beat_text = await asyncio.to_thread(state.narrative_resolver.resolve, beat_prompt)
```
and after the `table.update_item(...)` that writes the beat, add:
```python
    await _emit_arc_event(
        tag="arc.beat", arc=arc, summary=beat_text,
        location=template.get("preferred_location"), witnesses=arc.get("witnessIds", []),
    )
```

In `_resolve_arc`, after `_emit_metric("ArcsResolved")`, add:
```python
    await _emit_arc_event(
        tag="arc.resolved", arc=arc, summary=arc.get("lastBeatText") or arc.get("narrativeKickoff", ""),
        location=template.get("preferred_location"), witnesses=arc.get("witnessIds", []),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/test_arc_orchestrator.py -v`
Expected: PASS (existing + 3 new; existing tests unaffected since they don't assert on emit).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/worldgm/arc_orchestrator.py apps/backend/tests/unit/worldgm/test_arc_orchestrator.py
git commit -m "feat(worldgm): publish arc kickoff/beat/resolve to EventBridge (best-effort, offloaded)"
```

---

### Task 4: `_arc_text_for_location` helper

**Files:**
- Modify: `apps/backend/worldgm/__main__.py`
- Test: `apps/backend/tests/unit/worldgm/test_tick_arcs.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/worldgm/test_tick_arcs.py
"""Tests for the GM arc step + storyline delivery in the worldgm tick."""
from __future__ import annotations

from worldgm.__main__ import _arc_text_for_location


def _arc(arc_id, location, kickoff="k", beat=None):
    a = {"arcId": arc_id, "preferred_location": location, "narrativeKickoff": kickoff}
    if beat is not None:
        a["lastBeatText"] = beat
    return a


def test_arc_text_matches_resident_location():
    arcs = [_arc("a1", "cafe", kickoff="A lost dog appears.")]
    assert _arc_text_for_location(arcs, "cafe") == "A lost dog appears."


def test_arc_text_none_when_no_arc_here():
    arcs = [_arc("a1", "cafe")]
    assert _arc_text_for_location(arcs, "library") is None


def test_arc_text_prefers_latest_beat_over_kickoff():
    arcs = [_arc("a1", "plaza", kickoff="k", beat="The rumor deepens.")]
    assert _arc_text_for_location(arcs, "plaza") == "The rumor deepens."


def test_arc_without_location_is_town_wide():
    arcs = [{"arcId": "storm", "narrativeKickoff": "A storm rolls in."}]
    assert _arc_text_for_location(arcs, "anywhere") == "A storm rolls in."


def test_arc_text_none_for_empty_arcs():
    assert _arc_text_for_location([], "cafe") is None


def test_arc_text_skips_entry_without_narrative():
    arcs = [{"arcId": "blank", "preferred_location": "cafe"}]
    assert _arc_text_for_location(arcs, "cafe") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/test_tick_arcs.py -v`
Expected: FAIL — `ImportError: cannot import name '_arc_text_for_location'`

- [ ] **Step 3: Write minimal implementation**

In `__main__.py`, add the pure helper (near `_tick_one_resident`):

```python
def _arc_text_for_location(active_arcs: list[dict], location: str) -> str | None:
    """Return the narrative of an active arc the resident at ``location`` can
    perceive, or None. An arc with no ``preferred_location`` is town-wide and
    matches everywhere. The most recently updated matching arc wins (last in
    the list, since _read_active_arcs is insertion-ordered by the table)."""
    match: str | None = None
    for arc in active_arcs:
        arc_loc = arc.get("preferred_location")
        if arc_loc is not None and arc_loc != location:
            continue
        narrative = arc.get("lastBeatText") or arc.get("narrativeKickoff")
        if narrative:
            match = narrative  # later matches override earlier ones
    return match
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/test_tick_arcs.py -v`
Expected: PASS (6).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/worldgm/__main__.py apps/backend/tests/unit/worldgm/test_tick_arcs.py
git commit -m "feat(worldgm): _arc_text_for_location — location-based storyline lookup"
```

---

### Task 5: Wire the GM arc step + cache + storyline delivery into the tick

**Files:**
- Modify: `apps/backend/worldgm/__main__.py`
- Test: `apps/backend/tests/unit/worldgm/test_tick_arcs.py`

- [ ] **Step 1: Write the failing test**

```python
# append to test_tick_arcs.py
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def _stub_sync_locations(monkeypatch):
    import worldgm.__main__ as wm

    async def _fake_sync(*_a, **_k):
        return {}

    monkeypatch.setattr(wm, "sync_locations", _fake_sync)


def _gm_state(residents, active_arcs):
    import worldgm.__main__ as wm

    return wm.GmState(
        model_lite=object(),
        model_micro=None,
        location_filter=SimpleNamespace(entities_at=lambda *_: set(), location_of=lambda *_: "unknown"),
        narrative_resolver=None,
        gm=None,
        town_service=None,
        resident_manager=SimpleNamespace(residents=residents),
        active_arcs=active_arcs,
    )


@pytest.mark.asyncio
async def test_tick_runs_arc_step_and_caches_active_arcs(monkeypatch):
    import worldgm.__main__ as wm

    monkeypatch.setattr(wm, "maybe_kickoff_arc", AsyncMock(return_value=None))
    monkeypatch.setattr(wm, "process_arc_beats", AsyncMock(return_value=0))
    monkeypatch.setattr(wm, "_read_active_arcs", AsyncMock(return_value=[{"arcId": "a1", "preferred_location": "cafe", "narrativeKickoff": "k"}]))

    state = _gm_state(residents={}, active_arcs=[])
    await wm._tick(state)

    wm.maybe_kickoff_arc.assert_awaited_once()
    wm.process_arc_beats.assert_awaited_once()
    assert state.active_arcs == [{"arcId": "a1", "preferred_location": "cafe", "narrativeKickoff": "k"}]


@pytest.mark.asyncio
async def test_tick_one_resident_passes_storyline_for_arc_at_location(monkeypatch):
    import worldgm.__main__ as wm

    captured = {}

    async def _capture(daemon, runtime, status, *, model=None, persona=None, storyline=None, **_):
        captured["storyline"] = storyline

    monkeypatch.setattr(wm, "step_resident_brain", _capture)

    class _D:
        state = {"agent": {"location": "cafe", "location_context": "town"}}
        async def handle_tool_command(self, cmd):  # pragma: no cover
            return {}

    handle = SimpleNamespace(daemon=_D(), persona=object())
    state = _gm_state(
        residents={"ada": handle},
        active_arcs=[{"arcId": "a1", "preferred_location": "cafe", "narrativeKickoff": "A lost dog appears."}],
    )
    await wm._tick_one_resident(state, "ada", handle)
    assert captured["storyline"] == "A lost dog appears."
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/test_tick_arcs.py -k "tick" -v`
Expected: FAIL — `TypeError: GmState.__init__() got an unexpected keyword argument 'active_arcs'` (and the storyline assertion fails: `_tick_one_resident` doesn't pass it yet).

- [ ] **Step 3: Write minimal implementation**

(a) Add the field to `GmState` (after `resident_runtimes`):

```python
    resident_runtimes: dict = field(default_factory=dict)
    active_arcs: list = field(default_factory=list)
```

(b) Import the arc functions at the top of `__main__.py` (with the other worldgm imports):

```python
from worldgm.arc_orchestrator import _read_active_arcs, maybe_kickoff_arc, process_arc_beats
```

(c) Replace the `# TODO(Phase C3): maybe_kickoff_arc, process_arc_beats` line in `_tick` with:

```python
    # --- GM arc step: kick off / advance storylines, then cache active arcs
    # so residents at an arc's location perceive it this tick. Arc helpers
    # offload their own slow Bedrock/EventBridge calls; the small reads here
    # are cheap. Best-effort: a failure must not break the resident gather.
    try:
        await maybe_kickoff_arc(state)
        await process_arc_beats(state)
        state.active_arcs = await _read_active_arcs()
    except Exception:
        logger.exception("GM arc step failed; continuing with stale arcs")
```

(d) In `_tick_one_resident`, compute the storyline and pass it. Replace the `step_resident_brain(...)` call with:

```python
    storyline = _arc_text_for_location(state.active_arcs, status.get("location", "unknown"))
    try:
        await step_resident_brain(
            daemon,
            runtime,
            status,
            model=getattr(state, "model_lite", None),
            persona=handle.persona,
            storyline=storyline,
        )
    except Exception:
        logger.exception("Resident brain tick failed for %s", name)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/test_tick_arcs.py tests/unit/worldgm/test_tick_concurrency.py -v`
Expected: PASS (new arc-tick tests + the existing concurrency tests — the latter build `GmState` via `_state_with_residents`, which omits `active_arcs`; the new field defaults so they still pass).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/worldgm/__main__.py apps/backend/tests/unit/worldgm/test_tick_arcs.py
git commit -m "feat(worldgm): wire GM arc step into the tick + deliver storylines to residents"
```

---

### Task 6: Add the WORLDGM_ARCS_ENABLED kill switch

**Files:**
- Modify: `apps/backend/worldgm/__main__.py`
- Test: `apps/backend/tests/unit/worldgm/test_tick_arcs.py`

- [ ] **Step 1: Write the failing test**

```python
# append to test_tick_arcs.py
@pytest.mark.asyncio
async def test_arc_step_skipped_when_disabled(monkeypatch):
    import worldgm.__main__ as wm

    monkeypatch.setattr(wm, "ARCS_ENABLED", False)
    monkeypatch.setattr(wm, "maybe_kickoff_arc", AsyncMock(return_value=None))
    monkeypatch.setattr(wm, "process_arc_beats", AsyncMock(return_value=0))

    state = _gm_state(residents={}, active_arcs=[])
    await wm._tick(state)

    wm.maybe_kickoff_arc.assert_not_awaited()
    wm.process_arc_beats.assert_not_awaited()
    assert state.active_arcs == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/test_tick_arcs.py -k disabled -v`
Expected: FAIL — `AttributeError: module 'worldgm.__main__' has no attribute 'ARCS_ENABLED'`

- [ ] **Step 3: Write minimal implementation**

In `__main__.py`, near the other module constants (e.g. by `TICK_INTERVAL_SECONDS`):

```python
ARCS_ENABLED = os.environ.get("WORLDGM_ARCS_ENABLED", "true").lower() == "true"
```

Guard the arc step in `_tick`:

```python
    if ARCS_ENABLED:
        try:
            await maybe_kickoff_arc(state)
            await process_arc_beats(state)
            state.active_arcs = await _read_active_arcs()
        except Exception:
            logger.exception("GM arc step failed; continuing with stale arcs")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/test_tick_arcs.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/worldgm/__main__.py apps/backend/tests/unit/worldgm/test_tick_arcs.py
git commit -m "feat(worldgm): WORLDGM_ARCS_ENABLED kill switch for the GM arc step"
```

---

### Task 7: Full suite + lint gate

**Files:** none (verification only)

- [ ] **Step 1: worldgm suite**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/ -v`
Expected: PASS (all new + existing).

- [ ] **Step 2: Full backend regression**

Run: `cd apps/backend && uv run pytest tests/ -q`
Expected: PASS (no regressions).

- [ ] **Step 3: Lint + format**

Run: `cd apps/backend && uv run ruff check . && uv run ruff format --check .`
Expected: `All checks passed!` + already-formatted. Fix any unused imports flagged in `arc_orchestrator.py` / `__main__.py`.

- [ ] **Step 4: Commit any lint fixes**

```bash
git add -A && git commit -m "chore(worldgm): lint/format pass for GM storyline injection" || echo "nothing to fix"
```

---

## Self-Review (completed by plan author)

**1. Spec coverage:** Wire kickoff/beats → Task 5. In-process location-based delivery → Tasks 4 (helper) + 5 (wiring). `active_storyline` context/prompt seam → Task 1. `step_resident_brain` forwarding → Task 2. EventBridge emit on kickoff/beat/resolve (best-effort, offloaded) → Task 3. Offload slow Bedrock/emit → Task 3 (resolve + emit) + the helpers already offload. `GmState.active_arcs` cache (read-after) → Task 5. Kill switch → Task 6. Error handling (arc step best-effort; emit swallowed) → Tasks 3 + 5 tests. Testing strategy → every task TDD + Task 7 gate. No gaps.

**2. Placeholder scan:** No TBD/vague steps — every code step shows complete code and exact insertion points.

**3. Type/name consistency:** `build_decision_context(persona, parsed_status, *, storyline=None)` consistent across Tasks 1/2. `DecisionContext.active_storyline` set in Task 1, read by `build_prompt` (Task 1). `step_resident_brain(..., storyline=None)` defined Task 2, called with `storyline=` in Task 5. `_arc_text_for_location(active_arcs, location)` defined Task 4, called in Task 5. `GmState.active_arcs` added Task 5, defaulted so existing `test_tick_concurrency.py` / `test_main_bootstrap.py` constructions still pass. `_emit_arc_event` / `emit_world_event` tag strings (`arc.kickoff`/`arc.beat`/`arc.resolved`) consistent between impl and tests. `ARCS_ENABLED` defined Task 6, guards the block added Task 5.
