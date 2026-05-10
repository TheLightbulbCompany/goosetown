"""Tests for goal_relevance."""

from unittest.mock import MagicMock


def test_goal_match_returns_empty_when_no_witnesses():
    from worldgm.goal_relevance import goal_match

    model = MagicMock()
    assert goal_match(model, "an event", []) == {}
    model.sample_text.assert_not_called()


def test_goal_match_parses_json_array_response():
    from worldgm.goal_relevance import goal_match

    model = MagicMock()
    model.sample_text.return_value = '[{"agent_id": "alice", "reason": "Bob is the subject of her goal"}]'
    result = goal_match(model, "Bob arrived", [("alice", "find out why Bob is distant")])
    assert result == {"alice": "Bob is the subject of her goal"}


def test_goal_match_handles_code_fence():
    from worldgm.goal_relevance import goal_match

    model = MagicMock()
    model.sample_text.return_value = '```json\n[{"agent_id": "alice", "reason": "x"}]\n```'
    result = goal_match(model, "evt", [("alice", "g")])
    assert result == {"alice": "x"}


def test_goal_match_returns_empty_on_invalid_json():
    from worldgm.goal_relevance import goal_match

    model = MagicMock()
    model.sample_text.return_value = "not json at all"
    assert goal_match(model, "evt", [("alice", "g")]) == {}


def test_goal_match_returns_empty_on_llm_exception():
    from worldgm.goal_relevance import goal_match

    model = MagicMock()
    model.sample_text.side_effect = RuntimeError("boom")
    assert goal_match(model, "evt", [("alice", "g")]) == {}


def test_goal_match_empty_array_is_valid():
    from worldgm.goal_relevance import goal_match

    model = MagicMock()
    model.sample_text.return_value = "[]"
    assert goal_match(model, "evt", [("alice", "g")]) == {}
