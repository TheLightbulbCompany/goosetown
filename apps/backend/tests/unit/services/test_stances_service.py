"""Unit tests for stances_service."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from core.services import stances_service


@patch("core.services.stances_service.TownService")
def test_describe_stance_writes_stance_AtoB_when_caller_is_A(mock_town):
    """When the caller's id sorts lex-smaller than target, write stanceAtoB."""
    mock_town.return_value.upsert_relationship_stance.return_value = None
    stances_service.describe_stance(
        agent_id="alice",
        target_agent_id="bob",
        text="kind colleague",
    )
    args = mock_town.return_value.upsert_relationship_stance.call_args.kwargs
    assert args["agent_a_id"] == "alice"
    assert args["agent_b_id"] == "bob"
    assert args["stance_a_to_b"] == "kind colleague"
    assert "stance_b_to_a" not in args or args.get("stance_b_to_a") is None


@patch("core.services.stances_service.TownService")
def test_describe_stance_writes_stance_BtoA_when_caller_is_B(mock_town):
    """When the caller's id sorts lex-larger, the order swaps internally."""
    mock_town.return_value.upsert_relationship_stance.return_value = None
    stances_service.describe_stance(
        agent_id="bob",
        target_agent_id="alice",
        text="annoying but means well",
    )
    args = mock_town.return_value.upsert_relationship_stance.call_args.kwargs
    # canonical order: alice (a-id) < bob (b-id)
    assert args["agent_a_id"] == "alice"
    assert args["agent_b_id"] == "bob"
    assert args["stance_b_to_a"] == "annoying but means well"


@patch("core.services.stances_service.TownService")
def test_describe_stance_rejects_self_target(mock_town):
    with pytest.raises(ValueError, match="self"):
        stances_service.describe_stance(
            agent_id="alice",
            target_agent_id="alice",
            text="x",
        )
    mock_town.return_value.upsert_relationship_stance.assert_not_called()


@patch("core.services.stances_service.TownService")
def test_describe_stance_rejects_empty_text(mock_town):
    with pytest.raises(ValueError, match="empty"):
        stances_service.describe_stance(
            agent_id="alice",
            target_agent_id="bob",
            text="   ",
        )


@patch("core.services.stances_service.TownService")
def test_describe_stance_rejects_text_over_500_chars(mock_town):
    with pytest.raises(ValueError, match="500"):
        stances_service.describe_stance(
            agent_id="alice",
            target_agent_id="bob",
            text="x" * 501,
        )


@patch("core.services.stances_service.TownService")
def test_get_pair_stances_returns_both_directions_and_updated_at(mock_town):
    mock_town.return_value.get_relationship.return_value = {
        "agentAId": "alice",
        "agentBId": "bob",
        "stanceAtoB": "trustworthy",
        "stanceBtoA": "warm but distracted",
        "stanceUpdatedAt": 12345,
    }
    result = stances_service.get_pair_stances("alice", "bob")
    assert result["stance_a_to_b"] == "trustworthy"
    assert result["stance_b_to_a"] == "warm but distracted"
    assert result["updated_at"] == 12345


@patch("core.services.stances_service.TownService")
def test_get_pair_stances_returns_None_when_no_relationship(mock_town):
    mock_town.return_value.get_relationship.return_value = None
    result = stances_service.get_pair_stances("alice", "bob")
    assert result is None


@patch("core.services.stances_service.TownService")
def test_get_stance_for_caller_returns_caller_perspective(mock_town):
    """get_stance(viewer, subject) returns what `viewer` thinks of `subject`."""
    mock_town.return_value.get_relationship.return_value = {
        "agentAId": "alice",
        "agentBId": "bob",
        "stanceAtoB": "kind colleague",  # what alice thinks of bob
        "stanceBtoA": "good company",  # what bob thinks of alice
    }
    # alice (viewer) toward bob (subject): use stanceAtoB
    assert stances_service.get_stance("alice", "bob") == "kind colleague"
    # bob (viewer) toward alice (subject): use stanceBtoA
    assert stances_service.get_stance("bob", "alice") == "good company"
