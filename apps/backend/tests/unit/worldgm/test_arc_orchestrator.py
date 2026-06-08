import asyncio
from unittest.mock import MagicMock, patch

import pytest

from worldgm.narrative_director import Injection


@pytest.fixture(autouse=True)
def _stub_metric(monkeypatch):
    monkeypatch.setattr("worldgm.arc_orchestrator._emit_metric", lambda *a, **k: None)
    # Reset the process-local rate-cap window so tests don't pollute each other.
    monkeypatch.setattr("worldgm.arc_orchestrator._recent_kickoffs", [])


def _state(residents_online=2):
    state = MagicMock()
    state.last_known_locations = {f"a{i}": "cafe" for i in range(residents_online)}
    return state


@patch("worldgm.arc_orchestrator.build_snapshot")
@patch("worldgm.arc_orchestrator.compute_signals", return_value=[])
@patch("worldgm.arc_orchestrator.author_kickoff")
@patch("worldgm.arc_orchestrator.emit_world_event")
@patch("worldgm.arc_orchestrator.dynamo.worldgm_arcs_table")
def test_kickoff_persists_cast_based_arc(mock_table, mock_emit, mock_author, mock_sigs, mock_snap):
    from worldgm.arc_orchestrator import maybe_kickoff_arc

    mock_table.return_value.query.return_value = {"Items": []}  # no active arcs
    # Real-attribute stubs: MagicMock(name="A") does NOT set .name to "A" (name is
    # reserved by MagicMock), which would break the name→id mapping.
    snap = WorldSnapshotStub(residents=[ResidentStub("a0", "A"), ResidentStub("a1", "B")])
    mock_snap.return_value = snap
    mock_author.return_value = Injection(narrative="A and B spar.", cast=["A", "B"], location="cafe")

    state = _state()
    with patch("worldgm.arc_orchestrator.random.random", return_value=0.0):
        arc = asyncio.run(maybe_kickoff_arc(state))
    assert arc is not None
    item = mock_table.return_value.put_item.call_args.kwargs["Item"]
    assert item["cast"] == ["a0", "a1"]  # names mapped back to ids
    assert item["narrativeKickoff"] == "A and B spar."
    # delivered per-witness to the cast ids
    assert mock_emit.call_args.kwargs["witnesses"] == ["a0", "a1"]


@patch("worldgm.arc_orchestrator.build_snapshot")
@patch("worldgm.arc_orchestrator.compute_signals", return_value=[])
@patch("worldgm.arc_orchestrator.author_kickoff", return_value=None)
@patch("worldgm.arc_orchestrator.emit_world_event")
@patch("worldgm.arc_orchestrator.dynamo.worldgm_arcs_table")
def test_kickoff_noop_when_director_declines(mock_table, mock_emit, mock_author, mock_sigs, mock_snap):
    from worldgm.arc_orchestrator import maybe_kickoff_arc

    mock_table.return_value.query.return_value = {"Items": []}
    mock_snap.return_value = MagicMock(residents=[MagicMock(agent_id="a0", name="A")])
    with patch("worldgm.arc_orchestrator.random.random", return_value=0.0):
        arc = asyncio.run(maybe_kickoff_arc(_state()))
    assert arc is None
    mock_table.return_value.put_item.assert_not_called()


@patch("worldgm.arc_orchestrator.build_snapshot")
@patch("worldgm.arc_orchestrator.compute_signals", return_value=[])
@patch("worldgm.arc_orchestrator.author_kickoff")
@patch("worldgm.arc_orchestrator.emit_world_event")
@patch("worldgm.arc_orchestrator.dynamo.worldgm_arcs_table")
def test_kickoff_skips_agents_already_in_a_storyline(mock_table, mock_emit, mock_author, mock_sigs, mock_snap):
    from worldgm.arc_orchestrator import maybe_kickoff_arc

    # a0 already cast in an active arc → director must receive a snapshot WITHOUT a0,
    # but with >=2 free residents remaining so a kickoff is still attempted.
    mock_table.return_value.query.return_value = {"Items": [{"arcId": "x", "status": "active", "cast": ["a0"]}]}
    snap = WorldSnapshotStub(
        residents=[ResidentStub("a0", "A"), ResidentStub("a1", "B"), ResidentStub("a2", "C")],
    )
    mock_snap.return_value = snap
    mock_author.return_value = None
    with patch("worldgm.arc_orchestrator.random.random", return_value=0.0):
        asyncio.run(maybe_kickoff_arc(_state()))
    # The snapshot passed to author_kickoff has a0 filtered out.
    passed_snap = mock_author.call_args.args[1]
    assert all(r.agent_id != "a0" for r in passed_snap.residents)


@patch("worldgm.arc_orchestrator.build_snapshot")
@patch("worldgm.arc_orchestrator.author_beat", return_value="They reconcile.")
@patch("worldgm.arc_orchestrator.emit_world_event")
@patch("worldgm.arc_orchestrator.dynamo.worldgm_arcs_table")
def test_beat_advances_active_arc(mock_table, mock_emit, mock_beat, mock_snap):
    import time

    from worldgm.arc_orchestrator import process_arc_beats

    now = int(time.time() * 1000)
    arc = {
        "arcId": "x",
        "status": "active",
        "cast": ["a0", "a1"],
        "expiresAt": now + 3_600_000,
        "nextBeatAt": str(now - 100),
        "narrativeKickoff": "they fought",
    }
    mock_table.return_value.query.return_value = {"Items": [arc]}
    mock_snap.return_value = MagicMock()
    still = asyncio.run(process_arc_beats(_state()))
    assert len(still) == 1
    assert still[0]["lastBeatText"] == "They reconcile."


@patch("worldgm.arc_orchestrator.build_snapshot")
@patch("worldgm.arc_orchestrator.author_beat", return_value=None)  # DONE
@patch("worldgm.arc_orchestrator.emit_world_event")
@patch("worldgm.arc_orchestrator.dynamo.worldgm_arcs_table")
def test_beat_done_resolves_arc(mock_table, mock_emit, mock_beat, mock_snap):
    import time

    from worldgm.arc_orchestrator import process_arc_beats

    now = int(time.time() * 1000)
    arc = {
        "arcId": "x",
        "status": "active",
        "cast": ["a0"],
        "expiresAt": now + 3_600_000,
        "nextBeatAt": str(now - 100),
        "narrativeKickoff": "k",
    }
    mock_table.return_value.query.return_value = {"Items": [arc]}
    mock_snap.return_value = MagicMock()
    still = asyncio.run(process_arc_beats(_state()))
    assert still == []  # resolved → dropped
    resolved = [
        c
        for c in mock_table.return_value.update_item.call_args_list
        if "resolved" in str(c.kwargs.get("ExpressionAttributeValues", {}))
    ]
    assert resolved


@patch("worldgm.arc_orchestrator.build_snapshot")
@patch("worldgm.arc_orchestrator.author_beat")
@patch("worldgm.arc_orchestrator.emit_world_event")
@patch("worldgm.arc_orchestrator.dynamo.worldgm_arcs_table")
def test_beat_resolves_pre_migration_arc_without_cast(mock_table, mock_emit, mock_beat, mock_snap):
    """A leftover arc from the old template GM has no `cast` — it can't be
    delivered to anyone, so it's resolved on its next beat (self-heal) rather
    than replaying the old invented fiction."""
    import time

    from worldgm.arc_orchestrator import process_arc_beats

    now = int(time.time() * 1000)
    old_arc = {
        "arcId": "legacy",
        "status": "active",
        "expiresAt": now + 86_400_000,  # old multi-day duration
        "nextBeatAt": str(now - 100),
        "narrativeKickoff": "Ana the librarian interrupts the group.",
        # no "cast" key (old format)
    }
    mock_table.return_value.query.return_value = {"Items": [old_arc]}
    still = asyncio.run(process_arc_beats(_state()))
    assert still == []  # resolved → dropped
    mock_beat.assert_not_called()  # never authored a continuation beat
    resolved = [
        c
        for c in mock_table.return_value.update_item.call_args_list
        if "resolved" in str(c.kwargs.get("ExpressionAttributeValues", {}))
    ]
    assert resolved


# Lightweight stand-ins so _filter_snapshot (which rebuilds a real WorldSnapshot)
# operates on objects with the attributes it reads.
class ResidentStub:
    def __init__(self, agent_id, name):
        self.agent_id = agent_id
        self.name = name


class WorldSnapshotStub:
    def __init__(self, residents):
        self.residents = residents
        self.relationships = []
        self.recent_conversations = []
        self.clusters = {}
        self.hour_utc = 0
