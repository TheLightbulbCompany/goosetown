"""Tests for arc_orchestrator."""

import asyncio
from unittest.mock import MagicMock, patch


def _state_with(online=None, narrative_text="kickoff text"):
    state = MagicMock()
    state.last_known_locations = online or {}
    state.narrative_resolver.resolve.return_value = narrative_text
    return state


@patch("worldgm.arc_orchestrator.dynamo.worldgm_arcs_table")
def test_maybe_kickoff_arc_no_op_when_too_few_agents(mock_table):
    from worldgm.arc_orchestrator import maybe_kickoff_arc

    state = _state_with({"alice": "cafe"})  # only 1 agent
    result = asyncio.run(maybe_kickoff_arc(state))
    assert result is None
    mock_table.return_value.put_item.assert_not_called()


@patch("worldgm.arc_orchestrator.dynamo.worldgm_arcs_table")
def test_maybe_kickoff_arc_no_op_when_active_arc_exists(mock_table):
    from worldgm.arc_orchestrator import maybe_kickoff_arc

    mock_table.return_value.query.return_value = {"Items": [{"arcId": "x", "status": "active"}]}
    state = _state_with({"alice": "cafe", "bob": "library"})
    result = asyncio.run(maybe_kickoff_arc(state))
    assert result is None


@patch("worldgm.arc_orchestrator.random.random", return_value=0.0)  # always kickoff
@patch("worldgm.arc_orchestrator.dynamo.worldgm_arcs_table")
def test_maybe_kickoff_arc_picks_template_and_persists(mock_table, mock_rand):
    from worldgm.arc_orchestrator import maybe_kickoff_arc

    mock_table.return_value.query.return_value = {"Items": []}
    mock_table.return_value.scan.return_value = {"Items": []}  # no prior arcs
    state = _state_with({"alice": "cafe", "bob": "library"})
    result = asyncio.run(maybe_kickoff_arc(state))
    assert result is not None
    assert result["status"] == "active"
    assert "arcId" in result
    assert result["narrativeKickoff"] == "kickoff text"
    mock_table.return_value.put_item.assert_called_once()


@patch("worldgm.arc_orchestrator.dynamo.worldgm_arcs_table")
def test_process_arc_beats_resolves_expired_arc(mock_table):
    import time

    from worldgm.arc_orchestrator import process_arc_beats

    now = int(time.time() * 1000)
    expired_arc = {
        "arcId": "x",
        "templateId": "traveling_merchant",
        "status": "active",
        "expiresAt": now - 1000,  # expired
        "nextBeatAt": str(now - 100),  # ready to fire
    }
    mock_table.return_value.query.return_value = {"Items": [expired_arc]}
    state = _state_with({"alice": "cafe", "bob": "library"})
    fired = asyncio.run(process_arc_beats(state))
    assert fired == 1
    # The arc should have been resolved
    update_calls = mock_table.return_value.update_item.call_args_list
    resolved_call = next(c for c in update_calls if "resolved" in str(c.kwargs.get("ExpressionAttributeValues", {})))
    assert resolved_call is not None


@patch("worldgm.arc_orchestrator.dynamo.worldgm_arcs_table")
def test_process_arc_beats_fires_continuation_beat(mock_table):
    import time

    from worldgm.arc_orchestrator import process_arc_beats

    now = int(time.time() * 1000)
    active_arc = {
        "arcId": "x",
        "templateId": "traveling_merchant",
        "status": "active",
        "expiresAt": now + 3_600_000,  # not yet expired
        "nextBeatAt": str(now - 100),  # ready to fire next beat
        "narrativeKickoff": "the merchant arrived",
    }
    mock_table.return_value.query.return_value = {"Items": [active_arc]}
    state = _state_with({"alice": "cafe", "bob": "library"}, narrative_text="next beat text")
    fired = asyncio.run(process_arc_beats(state))
    assert fired == 1
    state.narrative_resolver.resolve.assert_called()
    # arc was updated with new beat text
    update_calls = mock_table.return_value.update_item.call_args_list
    beat_call = next(c for c in update_calls if "lastBeatText" in str(c.kwargs.get("UpdateExpression", "")))
    assert beat_call is not None
