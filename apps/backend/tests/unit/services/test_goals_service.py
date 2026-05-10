"""Unit tests for goals_service."""

from unittest.mock import patch
import time
import pytest
from core.services import goals_service


def _now_ms() -> int:
    return int(time.time() * 1000)


@patch("core.services.goals_service.TownService")
def test_set_goal_writes_currentGoal_and_goalSetAt(mock_town):
    mock_town.return_value.update_agent_state.return_value = None
    goals_service.set_goal(agent_id="alice-123", text="find out why Bob has been distant")
    args = mock_town.return_value.update_agent_state.call_args.kwargs
    assert args["current_goal"] == "find out why Bob has been distant"
    assert "goal_set_at" in args
    assert args["goal_set_at"] >= _now_ms() - 5000


@patch("core.services.goals_service.dynamo.completed_goals_table")
@patch("core.services.goals_service.TownService")
def test_complete_goal_archives_then_clears_currentGoal(mock_town, mock_table):
    mock_town.return_value.get_town_agent_by_id.return_value = {
        "id": "alice-123",
        "currentGoal": "find out why Bob has been distant",
        "goalSetAt": _now_ms() - 60_000,
    }
    mock_table.return_value.put_item.return_value = None
    mock_town.return_value.update_agent_state.return_value = None

    goals_service.complete_goal(agent_id="alice-123", reflection="we talked, all good")

    # archive write — camelCase keys
    put_args = mock_table.return_value.put_item.call_args.kwargs["Item"]
    assert put_args["agentId"] == "alice-123"
    assert put_args["goalText"] == "find out why Bob has been distant"
    assert put_args["reflection"] == "we talked, all good"
    assert "completedAt" in put_args
    assert "goalId" in put_args

    # clear current
    update_args = mock_town.return_value.update_agent_state.call_args.kwargs
    assert update_args["current_goal"] is None
    assert update_args["goal_set_at"] is None


@patch("core.services.goals_service.TownService")
def test_complete_goal_noop_when_no_current_goal(mock_town):
    mock_town.return_value.get_town_agent_by_id.return_value = {
        "id": "alice-123",
        "currentGoal": None,
    }
    result = goals_service.complete_goal(agent_id="alice-123", reflection="...")
    assert result["status"] == "no_active_goal"
    mock_town.return_value.update_agent_state.assert_not_called()


@patch("core.services.goals_service.TownService")
def test_set_goal_rejects_empty_text(mock_town):
    with pytest.raises(ValueError, match="empty"):
        goals_service.set_goal(agent_id="alice-123", text="   ")
    mock_town.return_value.update_agent_state.assert_not_called()


@patch("core.services.goals_service.TownService")
def test_set_goal_rejects_text_over_500_chars(mock_town):
    with pytest.raises(ValueError, match="500"):
        goals_service.set_goal(agent_id="alice-123", text="x" * 501)


@patch("core.services.goals_service.TownService")
def test_get_goal_returns_currentGoal(mock_town):
    mock_town.return_value.get_town_agent_by_id.return_value = {
        "id": "alice-123",
        "currentGoal": "make a friend",
    }
    assert goals_service.get_goal("alice-123") == "make a friend"


@patch("core.services.goals_service.TownService")
def test_get_goal_returns_None_when_unset(mock_town):
    mock_town.return_value.get_town_agent_by_id.return_value = {
        "id": "alice-123",
    }
    assert goals_service.get_goal("alice-123") is None


@patch("core.services.goals_service.TownService")
def test_get_goal_returns_None_when_agent_missing(mock_town):
    mock_town.return_value.get_town_agent_by_id.return_value = None
    assert goals_service.get_goal("alice-123") is None
