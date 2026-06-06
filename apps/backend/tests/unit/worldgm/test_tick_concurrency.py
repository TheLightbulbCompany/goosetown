"""Tests for the worldgm tick's concurrent resident-brain dispatch.

Phase-2a observed on dev: with 20 residents the sequential ``await
step_resident_brain(...)`` loop serialized every move (each daemon's
``handle_tool_command`` blocks up to 120s on arrival), causing ticks to take
minutes and daemons to thrash on inactivity timeouts. The fix runs the brain
ticks under ``asyncio.gather`` — these tests pin that behavior so it can't
regress to serial.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from worldgm.__main__ import GmState, _tick
from worldgm.residents.behavior import ResidentRuntime
from worldgm.residents.personas import Persona


@dataclass
class _FakeHandle:
    persona: Persona
    daemon: Any  # the resident's TownDaemon stand-in


def _persona(name: str) -> Persona:
    return Persona(
        name=name,
        display_name=name.title(),
        personality="p",
        appearance="a",
        traits=("x",),
        hangout="plaza",
    )


def _state_with_residents(residents: dict[str, _FakeHandle]) -> GmState:
    """Build a minimal GmState — only the fields _tick reads."""
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


def _connected_daemon(location: str = "plaza"):
    daemon = AsyncMock()
    daemon.state = {"agent": {"location": location, "activity": "idle"}}
    return daemon


async def _fake_sync_locations(*_args, **_kwargs):
    return {}


@pytest.fixture(autouse=True)
def _stub_sync_locations(monkeypatch):
    """``_tick`` calls sync_locations at the top — stub it so the test
    isolates the resident-brain dispatch path."""
    import worldgm.__main__ as main_mod

    monkeypatch.setattr(main_mod, "sync_locations", _fake_sync_locations)


@pytest.mark.asyncio
async def test_brain_ticks_run_concurrently_not_sequentially(monkeypatch):
    """Two residents whose brain ticks each take ~0.3s real time should
    complete in ~0.3s wall-clock under concurrent dispatch — not ~0.6s as a
    sequential loop would. Generous bounds: the test would still fail
    catastrophically on a serial regression while staying robust on a
    contended CI runner."""
    import worldgm.__main__ as main_mod

    async def slow_step(daemon, runtime, status, **_):
        await asyncio.sleep(0.3)

    monkeypatch.setattr(main_mod, "step_resident_brain", slow_step)

    residents = {
        "alice": _FakeHandle(persona=_persona("alice"), daemon=_connected_daemon()),
        "bob": _FakeHandle(persona=_persona("bob"), daemon=_connected_daemon()),
    }
    state = _state_with_residents(residents)

    t0 = time.monotonic()
    await _tick(state)
    elapsed = time.monotonic() - t0

    # Concurrent: ~0.3s. Serial would be ~0.6s. Allow generous headroom on CI.
    assert elapsed < 0.55, f"_tick took {elapsed:.2f}s — likely serial regression"


@pytest.mark.asyncio
async def test_one_failing_brain_does_not_break_others(monkeypatch):
    """If one resident's brain tick raises, the other residents must still
    tick successfully (gather uses an inner try/except per-resident)."""
    import worldgm.__main__ as main_mod

    seen: list[str] = []

    async def step(daemon, runtime, status, **_):
        seen.append(runtime.name)
        if runtime.name == "broken":
            raise RuntimeError("boom")

    monkeypatch.setattr(main_mod, "step_resident_brain", step)

    residents = {
        "ok_one": _FakeHandle(persona=_persona("ok_one"), daemon=_connected_daemon()),
        "broken": _FakeHandle(persona=_persona("broken"), daemon=_connected_daemon()),
        "ok_two": _FakeHandle(persona=_persona("ok_two"), daemon=_connected_daemon()),
    }
    state = _state_with_residents(residents)

    # Must NOT raise even though one resident's brain blew up.
    await _tick(state)

    assert set(seen) == {"ok_one", "broken", "ok_two"}


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
    """Residents whose daemon is None (still being launched) or whose
    daemon.state is empty (haven't received initial state yet) must not have
    their brain ticked — that would crash on the None.state access."""
    import worldgm.__main__ as main_mod

    ticked: list[str] = []

    async def step(daemon, runtime, status, **_):
        ticked.append(runtime.name)

    monkeypatch.setattr(main_mod, "step_resident_brain", step)

    none_daemon_handle = _FakeHandle(persona=_persona("late"), daemon=None)
    empty_state_daemon = AsyncMock()
    empty_state_daemon.state = None
    empty_handle = _FakeHandle(persona=_persona("empty"), daemon=empty_state_daemon)
    ready_handle = _FakeHandle(persona=_persona("ready"), daemon=_connected_daemon())

    state = _state_with_residents({"late": none_daemon_handle, "empty": empty_handle, "ready": ready_handle})
    await _tick(state)

    assert ticked == ["ready"]


@pytest.mark.asyncio
async def test_runtime_created_lazily_with_persona_schedule(monkeypatch):
    """The runtime should be created on first tick, carrying the persona's
    sleep schedule + hangout."""
    import worldgm.__main__ as main_mod

    seen_runtimes: list[ResidentRuntime] = []

    async def step(daemon, runtime, status, **_):
        seen_runtimes.append(runtime)

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
    residents = {"ada": _FakeHandle(persona=persona, daemon=_connected_daemon())}
    state = _state_with_residents(residents)

    await _tick(state)

    assert len(seen_runtimes) == 1
    rt = seen_runtimes[0]
    assert rt.name == "ada"
    assert rt.hangout == "library"
    assert rt.sleep_hour == 23
    assert rt.wake_hour == 7
    # And subsequent ticks reuse the same runtime instance (no new dataclass).
    await _tick(state)
    assert state.resident_runtimes["ada"] is rt
