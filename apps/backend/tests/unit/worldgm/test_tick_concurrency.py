"""Tests for the worldgm resident-brain dispatch.

Residents used to be stepped by a central ``_tick`` gather. They're now driven
by one ``_resident_stepper`` task each: step on a fresh conversation message
(``handle.wake``, set by the daemon) OR every ``TICK_INTERVAL_SECONDS``. That
fixed conversation replies crawling at the 30s tick rate. These tests pin both
the per-resident step behaviors (``_tick_one_resident``) and the stepper's
event-driven + periodic wake logic.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from worldgm.__main__ import GmState, _resident_stepper
from worldgm.residents.behavior import ResidentRuntime
from worldgm.residents.personas import Persona


@dataclass
class _FakeHandle:
    persona: Persona
    daemon: Any  # the resident's TownDaemon stand-in
    wake: asyncio.Event = field(default_factory=asyncio.Event)


def _persona(name: str) -> Persona:
    return Persona(
        name=name,
        display_name=name.title(),
        personality="p",
        appearance="a",
        traits=("x",),
        hangout="plaza",
    )


def _connected_daemon(location: str = "plaza"):
    daemon = AsyncMock()
    daemon.state = {"agent": {"location": location, "activity": "idle"}}
    return daemon


def _state_with_residents(residents: dict[str, _FakeHandle]) -> GmState:
    """Build a minimal GmState — only the fields the dispatch path reads."""
    return GmState(
        model_lite=None,
        model_micro=None,
        location_filter=SimpleNamespace(
            entities_at=lambda *_: set(),
            location_of=lambda *_: "unknown",
        ),
        narrative_resolver=None,
        gm=None,
        town_service=None,
        resident_manager=SimpleNamespace(residents=residents),
    )


# ---------------------------------------------------------------------------
# _tick_one_resident — per-resident step behaviors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_one_resident_threads_model_and_persona(monkeypatch):
    """_tick_one_resident passes state.model_lite + handle.persona into the brain."""
    import worldgm.__main__ as main_mod

    captured: dict = {}

    async def capture_step(daemon, runtime, status, *, model=None, persona=None, **_):
        captured["model"] = model
        captured["persona"] = persona

    monkeypatch.setattr(main_mod, "step_resident_brain", capture_step)

    persona = _persona("ada")
    handle = _FakeHandle(persona=persona, daemon=_connected_daemon())
    state = _state_with_residents({"ada": handle})
    sentinel_model = object()
    state.model_lite = sentinel_model

    await main_mod._tick_one_resident(state, "ada", handle)

    assert captured["model"] is sentinel_model
    assert captured["persona"] is persona


@pytest.mark.asyncio
async def test_unconnected_residents_are_skipped(monkeypatch):
    """Residents whose daemon is None (still launching) or whose daemon.state is
    empty (no initial state yet) must not be ticked — that would crash on the
    None.state access."""
    import worldgm.__main__ as main_mod

    ticked: list[str] = []

    async def step(daemon, runtime, status, **_):
        ticked.append(runtime.name)

    monkeypatch.setattr(main_mod, "step_resident_brain", step)

    none_daemon = _FakeHandle(persona=_persona("late"), daemon=None)
    empty_daemon = AsyncMock()
    empty_daemon.state = None
    empty_handle = _FakeHandle(persona=_persona("empty"), daemon=empty_daemon)
    ready = _FakeHandle(persona=_persona("ready"), daemon=_connected_daemon())
    state = _state_with_residents({"late": none_daemon, "empty": empty_handle, "ready": ready})

    for name, handle in state.resident_manager.residents.items():
        await main_mod._tick_one_resident(state, name, handle)

    assert ticked == ["ready"]


@pytest.mark.asyncio
async def test_one_failing_brain_does_not_propagate(monkeypatch):
    """A resident whose brain raises must not propagate out of
    _tick_one_resident — its stepper keeps running and other residents'
    steppers are untouched."""
    import worldgm.__main__ as main_mod

    async def step(daemon, runtime, status, **_):
        raise RuntimeError("boom")

    monkeypatch.setattr(main_mod, "step_resident_brain", step)

    handle = _FakeHandle(persona=_persona("broken"), daemon=_connected_daemon())
    state = _state_with_residents({"broken": handle})

    # Must NOT raise even though the brain blew up.
    await main_mod._tick_one_resident(state, "broken", handle)


@pytest.mark.asyncio
async def test_runtime_created_lazily_with_persona_schedule(monkeypatch):
    """The runtime is created on first tick, carrying the persona's sleep
    schedule + hangout, and reused on subsequent ticks."""
    import worldgm.__main__ as main_mod

    seen: list[ResidentRuntime] = []

    async def step(daemon, runtime, status, **_):
        seen.append(runtime)

    monkeypatch.setattr(main_mod, "step_resident_brain", step)

    persona = Persona(
        name="ada",
        display_name="Ada",
        personality="p",
        appearance="a",
        traits=("x",),
        hangout="library",
        sleep_hour=23,
        wake_hour=7,
    )
    handle = _FakeHandle(persona=persona, daemon=_connected_daemon())
    state = _state_with_residents({"ada": handle})

    await main_mod._tick_one_resident(state, "ada", handle)
    assert len(seen) == 1
    rt = seen[0]
    assert rt.name == "ada"
    assert rt.hangout == "library"
    assert rt.sleep_hour == 23
    assert rt.wake_hour == 7

    await main_mod._tick_one_resident(state, "ada", handle)
    assert state.resident_runtimes["ada"] is rt


# ---------------------------------------------------------------------------
# _resident_stepper — event-driven + periodic wake
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stepper_steps_immediately_on_wake(monkeypatch):
    """Setting handle.wake makes the stepper run a step right away — it must NOT
    wait out the full tick interval. This is the whole point: conversation
    replies fire on the message, not on the tick."""
    import worldgm.__main__ as main_mod

    # Long tick so a wake-driven step is unambiguously faster than the timeout.
    monkeypatch.setattr(main_mod, "TICK_INTERVAL_SECONDS", 100.0)
    # Pacing is tested separately; neutralise it here so this test isolates the
    # "wake fires a step before the tick timeout" property.
    monkeypatch.setattr(main_mod, "CONVO_TURN_PACING_SECONDS", 0.0)
    steps: list[float] = []

    async def fake_step(state, name, handle):
        steps.append(asyncio.get_event_loop().time())

    monkeypatch.setattr(main_mod, "_tick_one_resident", fake_step)

    handle = _FakeHandle(persona=_persona("ada"), daemon=_connected_daemon())
    state = _state_with_residents({"ada": handle})
    task = asyncio.create_task(_resident_stepper(state, "ada", handle))
    try:
        await asyncio.sleep(0.02)  # let the stepper reach wait_for
        handle.wake.set()
        await asyncio.sleep(0.05)  # let it wake + step
        assert len(steps) >= 1, "stepper did not step on wake"
        # The event is cleared before stepping so a later message re-wakes it.
        assert not handle.wake.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stepper_paces_conversation_reply(monkeypatch):
    """A wake-driven turn waits CONVO_TURN_PACING_SECONDS before stepping, so the
    exchange unfolds at a watchable cadence instead of stampeding at raw LLM
    latency. The pacing must still be far shorter than the tick timeout — the
    reply is paced, not shackled to the 30s tick."""
    import worldgm.__main__ as main_mod

    monkeypatch.setattr(main_mod, "TICK_INTERVAL_SECONDS", 100.0)
    monkeypatch.setattr(main_mod, "CONVO_TURN_PACING_SECONDS", 0.1)
    steps: list[float] = []

    async def fake_step(state, name, handle):
        steps.append(asyncio.get_event_loop().time())

    monkeypatch.setattr(main_mod, "_tick_one_resident", fake_step)

    handle = _FakeHandle(persona=_persona("ada"), daemon=_connected_daemon())
    state = _state_with_residents({"ada": handle})
    task = asyncio.create_task(_resident_stepper(state, "ada", handle))
    try:
        await asyncio.sleep(0.02)  # let the stepper reach wait_for
        handle.wake.set()
        # Before the pacing delay elapses, no step yet.
        await asyncio.sleep(0.05)
        assert len(steps) == 0, "stepper stepped before the pacing delay elapsed"
        # After the pacing delay, exactly one step has fired.
        await asyncio.sleep(0.12)
        assert len(steps) >= 1, "stepper never stepped after pacing delay"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stepper_steps_on_timeout_when_no_wake(monkeypatch):
    """With no wake, the stepper still steps once per tick interval (idle tick)."""
    import worldgm.__main__ as main_mod

    monkeypatch.setattr(main_mod, "TICK_INTERVAL_SECONDS", 0.02)
    steps: list[int] = []

    async def fake_step(state, name, handle):
        steps.append(1)

    monkeypatch.setattr(main_mod, "_tick_one_resident", fake_step)

    handle = _FakeHandle(persona=_persona("ada"), daemon=_connected_daemon())
    state = _state_with_residents({"ada": handle})
    task = asyncio.create_task(_resident_stepper(state, "ada", handle))
    try:
        await asyncio.sleep(0.1)  # several timeout intervals
        assert len(steps) >= 2, "stepper did not run periodic idle ticks"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stepper_swallows_step_exception_and_keeps_running(monkeypatch):
    """A raising step must not kill the stepper loop — it logs and continues."""
    import worldgm.__main__ as main_mod

    monkeypatch.setattr(main_mod, "TICK_INTERVAL_SECONDS", 0.02)
    calls: list[int] = []

    async def boom_then_ok(state, name, handle):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")

    monkeypatch.setattr(main_mod, "_tick_one_resident", boom_then_ok)

    handle = _FakeHandle(persona=_persona("ada"), daemon=_connected_daemon())
    state = _state_with_residents({"ada": handle})
    task = asyncio.create_task(_resident_stepper(state, "ada", handle))
    try:
        await asyncio.sleep(0.1)
        assert len(calls) >= 2, "stepper stopped after a step raised"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stepper_exits_on_cancel(monkeypatch):
    """Cancelling the stepper task must propagate CancelledError (clean shutdown)."""
    import worldgm.__main__ as main_mod

    monkeypatch.setattr(main_mod, "TICK_INTERVAL_SECONDS", 100.0)
    monkeypatch.setattr(main_mod, "_tick_one_resident", AsyncMock())

    handle = _FakeHandle(persona=_persona("ada"), daemon=_connected_daemon())
    state = _state_with_residents({"ada": handle})
    task = asyncio.create_task(_resident_stepper(state, "ada", handle))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# Reflection counter guard — store=None must NOT reset the counter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reflection_counter_not_reset_when_store_is_none(monkeypatch):
    """When memory_store_from_env() returns None (RESIDENT_MEMORIES_TABLE not set),
    runtime.memories_since_reflection must NOT be zeroed — synthesis never ran."""
    import worldgm.__main__ as main_mod

    monkeypatch.setattr(main_mod, "step_resident_brain", AsyncMock())

    # Patch settings so the reflection block is entered.
    # RESIDENT_SOCIAL_GOALS_ENABLED=False keeps the Phase-3 block from
    # triggering (which would also need its own memory_store fake).
    fake_settings = SimpleNamespace(RESIDENT_MEMORY_ENABLED=True, RESIDENT_SOCIAL_GOALS_ENABLED=False)
    monkeypatch.setattr("worldgm.__main__.__builtins__", {}, raising=False)

    # Patch at the import site used by _tick_one_resident (deferred import).
    import types

    fake_memory_mod = types.ModuleType("worldgm.residents.memory_store")
    fake_memory_mod.memory_store_from_env = lambda: None  # store is None
    monkeypatch.setitem(
        __import__("sys").modules, "worldgm.residents.memory_store", fake_memory_mod
    )

    reflection_called = []

    fake_reflection_mod = types.ModuleType("worldgm.residents.reflection")

    async def fake_synthesize(*args, **kwargs):
        reflection_called.append(True)
        return 0

    fake_reflection_mod.synthesize_reflection = fake_synthesize
    monkeypatch.setitem(
        __import__("sys").modules, "worldgm.residents.reflection", fake_reflection_mod
    )

    import core.config  # noqa: F401 — ensure importable; patch settings attr directly

    monkeypatch.setattr("core.config.settings", fake_settings, raising=False)

    # Re-patch via the module reference _tick_one_resident actually uses.
    import importlib
    import sys

    core_config_mod = sys.modules.get("core.config")
    if core_config_mod is not None:
        monkeypatch.setattr(core_config_mod, "settings", fake_settings, raising=False)

    handle = _FakeHandle(persona=_persona("ada"), daemon=_connected_daemon())
    handle.config = SimpleNamespace(agent_id="agent-uuid-1")
    state = _state_with_residents({"ada": handle})

    # Seed the runtime with a count above the threshold.
    from worldgm.residents.behavior import ResidentRuntime

    rt = ResidentRuntime(
        name="ada",
        location="plaza",
        state="idle",
        idle_ticks=0,
        hangout="plaza",
    )
    rt.memories_since_reflection = 6  # >= _REFLECT_EVERY_N (default 6)
    state.resident_runtimes["ada"] = rt

    await main_mod._tick_one_resident(state, "ada", handle)

    # synthesis was NOT called (store was None)
    assert reflection_called == [], "synthesize_reflection should not have been called"
    # counter must NOT have been zeroed
    assert rt.memories_since_reflection == 6, (
        "memories_since_reflection was reset to 0 even though store was None — bug!"
    )


@pytest.mark.asyncio
async def test_reflection_counter_reset_when_store_is_present(monkeypatch):
    """When memory_store_from_env() returns a real store, runtime.memories_since_reflection
    IS reset to 0 after synthesize_reflection completes."""
    import worldgm.__main__ as main_mod

    monkeypatch.setattr(main_mod, "step_resident_brain", AsyncMock())

    # RESIDENT_SOCIAL_GOALS_ENABLED=False isolates this test to the Phase-2 path.
    fake_settings = SimpleNamespace(RESIDENT_MEMORY_ENABLED=True, RESIDENT_SOCIAL_GOALS_ENABLED=False)

    import types, sys

    fake_store = object()  # truthy non-None store
    fake_memory_mod = types.ModuleType("worldgm.residents.memory_store")
    fake_memory_mod.memory_store_from_env = lambda: fake_store
    monkeypatch.setitem(sys.modules, "worldgm.residents.memory_store", fake_memory_mod)

    reflection_called = []

    fake_reflection_mod = types.ModuleType("worldgm.residents.reflection")

    async def fake_synthesize(*args, **kwargs):
        reflection_called.append(True)
        return 2

    fake_reflection_mod.synthesize_reflection = fake_synthesize
    monkeypatch.setitem(sys.modules, "worldgm.residents.reflection", fake_reflection_mod)

    core_config_mod = sys.modules.get("core.config")
    if core_config_mod is not None:
        monkeypatch.setattr(core_config_mod, "settings", fake_settings, raising=False)

    handle = _FakeHandle(persona=_persona("ada"), daemon=_connected_daemon())
    handle.config = SimpleNamespace(agent_id="agent-uuid-2")
    state = _state_with_residents({"ada": handle})

    from worldgm.residents.behavior import ResidentRuntime

    rt = ResidentRuntime(
        name="ada",
        location="plaza",
        state="idle",
        idle_ticks=0,
        hangout="plaza",
    )
    rt.memories_since_reflection = 6
    state.resident_runtimes["ada"] = rt

    await main_mod._tick_one_resident(state, "ada", handle)

    assert reflection_called == [True], "synthesize_reflection should have been called once"
    assert rt.memories_since_reflection == 0, (
        "memories_since_reflection should be 0 after successful synthesis"
    )


# ---------------------------------------------------------------------------
# Social-goal integration — Phase 3
# ---------------------------------------------------------------------------


def _fake_settings(*, social_goals: bool = False, memory: bool = False):
    return SimpleNamespace(
        RESIDENT_SOCIAL_GOALS_ENABLED=social_goals,
        RESIDENT_MEMORY_ENABLED=memory,
    )


def _patch_settings(monkeypatch, fake_settings):
    import sys

    core_config_mod = sys.modules.get("core.config")
    if core_config_mod is not None:
        monkeypatch.setattr(core_config_mod, "settings", fake_settings, raising=False)


def _install_social_goal_fakes(monkeypatch, store_value, goal_return: str = "find a debate partner"):
    import types, sys

    fake_memory_mod = types.ModuleType("worldgm.residents.memory_store")
    fake_memory_mod.memory_store_from_env = lambda: store_value
    monkeypatch.setitem(sys.modules, "worldgm.residents.memory_store", fake_memory_mod)

    goal_calls = []

    fake_sg_mod = types.ModuleType("worldgm.residents.social_goal")

    async def fake_generate(*args, **kwargs):
        goal_calls.append(args)
        return goal_return

    fake_sg_mod.generate_social_goal = fake_generate
    monkeypatch.setitem(sys.modules, "worldgm.residents.social_goal", fake_sg_mod)

    return goal_calls


@pytest.mark.asyncio
async def test_social_goal_flag_off_no_generation(monkeypatch):
    """When RESIDENT_SOCIAL_GOALS_ENABLED=False, generate_social_goal must never be called
    and the status dict gets social_goal="" (the runtime default)."""
    import worldgm.__main__ as main_mod

    monkeypatch.setattr(main_mod, "step_resident_brain", AsyncMock())
    _patch_settings(monkeypatch, _fake_settings(social_goals=False))
    goal_calls = _install_social_goal_fakes(monkeypatch, store_value=object())

    handle = _FakeHandle(persona=_persona("ada"), daemon=_connected_daemon())
    handle.config = SimpleNamespace(agent_id="agent-uuid-sg-off")
    state = _state_with_residents({"ada": handle})

    from worldgm.residents.behavior import ResidentRuntime

    rt = ResidentRuntime(name="ada", location="plaza", state="idle", idle_ticks=0, hangout="plaza")
    rt.ticks_since_social_goal = 20  # well above threshold
    state.resident_runtimes["ada"] = rt

    captured_statuses: list[dict] = []

    async def capture_step(daemon, runtime, status, **_):
        captured_statuses.append(dict(status))

    monkeypatch.setattr(main_mod, "step_resident_brain", capture_step)

    await main_mod._tick_one_resident(state, "ada", handle)

    assert goal_calls == [], "generate_social_goal must not be called when flag is off"
    # social_goal key is injected with the runtime default ("") before the brain step
    assert captured_statuses[0].get("social_goal") == ""


@pytest.mark.asyncio
async def test_social_goal_generated_and_counter_reset_when_store_present(monkeypatch):
    """Flag on + counter >= threshold + store present → generate_social_goal called,
    runtime.social_goal set, counter reset to 0."""
    import worldgm.__main__ as main_mod

    monkeypatch.setattr(main_mod, "step_resident_brain", AsyncMock())
    _patch_settings(monkeypatch, _fake_settings(social_goals=True))
    fake_store = object()
    goal_calls = _install_social_goal_fakes(monkeypatch, store_value=fake_store, goal_return="hold court at the café")

    handle = _FakeHandle(persona=_persona("ada"), daemon=_connected_daemon())
    handle.config = SimpleNamespace(agent_id="agent-uuid-sg-gen")
    state = _state_with_residents({"ada": handle})

    from worldgm.residents.behavior import ResidentRuntime

    rt = ResidentRuntime(name="ada", location="plaza", state="idle", idle_ticks=0, hangout="plaza")
    rt.ticks_since_social_goal = 10  # == _SOCIAL_GOAL_EVERY_N default
    state.resident_runtimes["ada"] = rt

    await main_mod._tick_one_resident(state, "ada", handle)

    assert len(goal_calls) == 1, "generate_social_goal should have been called exactly once"
    assert rt.social_goal == "hold court at the café"
    assert rt.ticks_since_social_goal == 0, "counter must be reset after successful generation"


@pytest.mark.asyncio
async def test_social_goal_counter_not_reset_when_store_is_none(monkeypatch):
    """When memory_store_from_env() returns None, the counter must NOT be reset
    (mirroring the Phase-2 store-None guard)."""
    import worldgm.__main__ as main_mod

    monkeypatch.setattr(main_mod, "step_resident_brain", AsyncMock())
    _patch_settings(monkeypatch, _fake_settings(social_goals=True))
    goal_calls = _install_social_goal_fakes(monkeypatch, store_value=None)

    handle = _FakeHandle(persona=_persona("ada"), daemon=_connected_daemon())
    handle.config = SimpleNamespace(agent_id="agent-uuid-sg-none")
    state = _state_with_residents({"ada": handle})

    from worldgm.residents.behavior import ResidentRuntime

    rt = ResidentRuntime(name="ada", location="plaza", state="idle", idle_ticks=0, hangout="plaza")
    rt.ticks_since_social_goal = 10
    state.resident_runtimes["ada"] = rt

    await main_mod._tick_one_resident(state, "ada", handle)

    assert goal_calls == [], "generate_social_goal must not be called when store is None"
    assert rt.ticks_since_social_goal == 11, (
        "counter must NOT be reset when store is None (was 10 before tick, incremented to 11)"
    )


@pytest.mark.asyncio
async def test_social_goal_blank_return_preserves_prior_goal(monkeypatch):
    """When generate_social_goal returns "" (transient Nova hiccup), the prior
    runtime.social_goal must NOT be wiped, but ticks_since_social_goal IS reset
    to 0 (cost bound preserved — no retry-storm on persistent blanks)."""
    import worldgm.__main__ as main_mod

    monkeypatch.setattr(main_mod, "step_resident_brain", AsyncMock())
    _patch_settings(monkeypatch, _fake_settings(social_goals=True))
    fake_store = object()
    goal_calls = _install_social_goal_fakes(monkeypatch, store_value=fake_store, goal_return="")

    handle = _FakeHandle(persona=_persona("ada"), daemon=_connected_daemon())
    handle.config = SimpleNamespace(agent_id="agent-uuid-sg-blank")
    state = _state_with_residents({"ada": handle})

    from worldgm.residents.behavior import ResidentRuntime

    rt = ResidentRuntime(name="ada", location="plaza", state="idle", idle_ticks=0, hangout="plaza")
    rt.social_goal = "old goal"
    rt.ticks_since_social_goal = 10  # == _SOCIAL_GOAL_EVERY_N default
    state.resident_runtimes["ada"] = rt

    await main_mod._tick_one_resident(state, "ada", handle)

    assert len(goal_calls) == 1, "generate_social_goal should have been called"
    assert rt.social_goal == "old goal", (
        "prior social_goal must be preserved when generate_social_goal returns ''"
    )
    assert rt.ticks_since_social_goal == 0, (
        "ticks_since_social_goal must be reset to 0 even on a blank/failed gen (cost bound)"
    )


@pytest.mark.asyncio
async def test_social_goal_injected_into_status_before_brain_step(monkeypatch):
    """runtime.social_goal is always injected into status['social_goal'] BEFORE
    the brain step, regardless of flag state."""
    import worldgm.__main__ as main_mod

    _patch_settings(monkeypatch, _fake_settings(social_goals=False))
    _install_social_goal_fakes(monkeypatch, store_value=None)

    captured_statuses: list[dict] = []

    async def capture_step(daemon, runtime, status, **_):
        captured_statuses.append(dict(status))

    monkeypatch.setattr(main_mod, "step_resident_brain", capture_step)

    handle = _FakeHandle(persona=_persona("ada"), daemon=_connected_daemon())
    handle.config = SimpleNamespace(agent_id="agent-uuid-sg-inject")
    state = _state_with_residents({"ada": handle})

    from worldgm.residents.behavior import ResidentRuntime

    rt = ResidentRuntime(name="ada", location="plaza", state="idle", idle_ticks=0, hangout="plaza")
    rt.social_goal = "find someone worth debating"
    state.resident_runtimes["ada"] = rt

    await main_mod._tick_one_resident(state, "ada", handle)

    assert len(captured_statuses) == 1
    assert captured_statuses[0]["social_goal"] == "find someone worth debating", (
        "social_goal must be injected into status before the brain step"
    )


# ---------------------------------------------------------------------------
# Town-lore injection + tick gating — Phase 3 Task 5
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_town_lore_injected_into_status_before_brain_step(monkeypatch):
    """_tick_one_resident injects the 3 most-recent town_lore lines (joined with
    ' · ') into status['town_lore'] BEFORE the brain step."""
    import worldgm.__main__ as main_mod

    _patch_settings(monkeypatch, _fake_settings(social_goals=False))
    _install_social_goal_fakes(monkeypatch, store_value=None)

    captured_statuses: list[dict] = []

    async def capture_step(daemon, runtime, status, **_):
        captured_statuses.append(dict(status))

    monkeypatch.setattr(main_mod, "step_resident_brain", capture_step)

    handle = _FakeHandle(persona=_persona("ada"), daemon=_connected_daemon())
    handle.config = SimpleNamespace(agent_id="agent-uuid-lore")
    state = _state_with_residents({"ada": handle})
    # 4 lore lines — only the 3 most recent should be injected.
    state.town_lore = ["lore-1", "lore-2", "lore-3", "lore-4"]

    await main_mod._tick_one_resident(state, "ada", handle)

    assert len(captured_statuses) == 1
    assert captured_statuses[0]["town_lore"] == "lore-2 · lore-3 · lore-4"


@pytest.mark.asyncio
async def test_town_lore_injected_empty_when_no_lore(monkeypatch):
    """With no town lore, status['town_lore'] is an empty string (not missing)."""
    import worldgm.__main__ as main_mod

    _patch_settings(monkeypatch, _fake_settings(social_goals=False))
    _install_social_goal_fakes(monkeypatch, store_value=None)

    captured_statuses: list[dict] = []

    async def capture_step(daemon, runtime, status, **_):
        captured_statuses.append(dict(status))

    monkeypatch.setattr(main_mod, "step_resident_brain", capture_step)

    handle = _FakeHandle(persona=_persona("ada"), daemon=_connected_daemon())
    handle.config = SimpleNamespace(agent_id="agent-uuid-lore-empty")
    state = _state_with_residents({"ada": handle})

    await main_mod._tick_one_resident(state, "ada", handle)

    assert captured_statuses[0]["town_lore"] == ""


def _stub_tick_deps(monkeypatch):
    """Neutralise sync_locations + the arc step so _tick can be exercised in
    isolation for the lore gating tests."""
    import worldgm.__main__ as main_mod

    async def _noop_sync(*a, **k):
        return {}

    async def _noop_kickoff(*a, **k):
        return None

    async def _noop_beats(*a, **k):
        return []

    monkeypatch.setattr(main_mod, "sync_locations", _noop_sync)
    monkeypatch.setattr(main_mod, "maybe_kickoff_arc", _noop_kickoff)
    monkeypatch.setattr(main_mod, "process_arc_beats", _noop_beats)


@pytest.mark.asyncio
async def test_tick_skips_lore_when_flag_off(monkeypatch):
    """TOWN_LORE_ENABLED=False → synthesize_town_lore is never called in _tick."""
    import worldgm.__main__ as main_mod

    _stub_tick_deps(monkeypatch)
    monkeypatch.setattr(main_mod, "TOWN_LORE_ENABLED", False)
    calls: list[int] = []

    async def fake_synth(state, now_ms, **_):
        calls.append(now_ms)
        return []

    monkeypatch.setattr(main_mod, "synthesize_town_lore", fake_synth)

    state = _state_with_residents({})
    await main_mod._tick(state)

    assert calls == [], "synthesize_town_lore must not run when the flag is off"


@pytest.mark.asyncio
async def test_tick_runs_lore_when_flag_on_and_interval_elapsed(monkeypatch):
    """Flag on + last_lore_at_ms far in the past → synthesize runs once and the
    interval guard timestamp is advanced."""
    import worldgm.__main__ as main_mod

    _stub_tick_deps(monkeypatch)
    monkeypatch.setattr(main_mod, "TOWN_LORE_ENABLED", True)
    monkeypatch.setattr(main_mod, "_LORE_CYCLE_MS", 1000)
    calls: list[int] = []

    async def fake_synth(state, now_ms, **_):
        calls.append(now_ms)
        return ["new lore"]

    monkeypatch.setattr(main_mod, "synthesize_town_lore", fake_synth)

    state = _state_with_residents({})
    state.last_lore_at_ms = 0  # epoch — well past the cycle

    await main_mod._tick(state)

    assert len(calls) == 1, "synthesize_town_lore should run when the interval has elapsed"
    assert state.last_lore_at_ms > 0, "the interval guard timestamp must be advanced"


@pytest.mark.asyncio
async def test_tick_skips_lore_when_interval_not_elapsed(monkeypatch):
    """Flag on but last_lore_at_ms is recent → the interval guard skips synthesis."""
    import time as _time

    import worldgm.__main__ as main_mod

    _stub_tick_deps(monkeypatch)
    monkeypatch.setattr(main_mod, "TOWN_LORE_ENABLED", True)
    monkeypatch.setattr(main_mod, "_LORE_CYCLE_MS", 30 * 60_000)  # 30 min
    calls: list[int] = []

    async def fake_synth(state, now_ms, **_):
        calls.append(now_ms)
        return []

    monkeypatch.setattr(main_mod, "synthesize_town_lore", fake_synth)

    state = _state_with_residents({})
    # Synthesized 'just now' → well inside the 30-min window.
    state.last_lore_at_ms = int(_time.time() * 1000)

    await main_mod._tick(state)

    assert calls == [], "synthesize_town_lore must be skipped inside the interval window"
