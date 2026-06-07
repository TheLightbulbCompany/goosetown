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
