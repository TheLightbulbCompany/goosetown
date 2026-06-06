"""Tests for the GM arc step + storyline delivery in the worldgm tick."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from worldgm.__main__ import _arc_text_for_location
from worldgm.residents.personas import Persona

_PERSONA = Persona(
    name="ada",
    display_name="Ada",
    personality="p",
    appearance="a",
    traits=("x",),
    hangout="plaza",
)


def _arc(arc_id, location, kickoff="k", beat=None):
    a = {"arcId": arc_id, "preferred_location": location, "narrativeKickoff": kickoff}
    if beat is not None:
        a["lastBeatText"] = beat
    return a


# ---------------------------------------------------------------------------
# _arc_text_for_location
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# _tick GM arc step + storyline delivery
# ---------------------------------------------------------------------------


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
    arc = {"arcId": "a1", "preferred_location": "cafe", "narrativeKickoff": "k"}
    monkeypatch.setattr(wm, "_read_active_arcs", AsyncMock(return_value=[arc]))

    state = _gm_state(residents={}, active_arcs=[])
    await wm._tick(state)

    wm.maybe_kickoff_arc.assert_awaited_once()
    wm.process_arc_beats.assert_awaited_once()
    assert state.active_arcs == [arc]


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


@pytest.mark.asyncio
async def test_tick_one_resident_injects_storyline_into_snapshot(monkeypatch):
    import worldgm.__main__ as wm

    captured = {}

    async def _capture(daemon, runtime, status, *, model=None, persona=None, **_):
        captured["status"] = status

    monkeypatch.setattr(wm, "step_resident_brain", _capture)

    class _D:
        state = {"agent": {"location": "cafe", "location_context": "town"}}

        async def handle_tool_command(self, cmd):  # pragma: no cover
            return {}

    handle = SimpleNamespace(daemon=_D(), persona=_PERSONA)
    state = _gm_state(
        residents={"ada": handle},
        active_arcs=[{"arcId": "a1", "preferred_location": "cafe", "narrativeKickoff": "A lost dog appears."}],
    )
    await wm._tick_one_resident(state, "ada", handle)
    assert captured["status"]["active_storyline"] == "A lost dog appears."


@pytest.mark.asyncio
async def test_tick_one_resident_no_storyline_when_no_arc_here(monkeypatch):
    import worldgm.__main__ as wm

    captured = {}

    async def _capture(daemon, runtime, status, *, model=None, persona=None, **_):
        captured["status"] = status

    monkeypatch.setattr(wm, "step_resident_brain", _capture)

    class _D:
        state = {"agent": {"location": "library", "location_context": "town"}}

        async def handle_tool_command(self, cmd):  # pragma: no cover
            return {}

    handle = SimpleNamespace(daemon=_D(), persona=_PERSONA)
    state = _gm_state(
        residents={"ada": handle},
        active_arcs=[{"arcId": "a1", "preferred_location": "cafe", "narrativeKickoff": "A lost dog appears."}],
    )
    await wm._tick_one_resident(state, "ada", handle)
    assert captured["status"]["active_storyline"] is None
