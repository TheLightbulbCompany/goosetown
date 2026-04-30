from unittest.mock import MagicMock

import pytest

from core.services.owner_chat_service import (
    OwnerChatService,
    OwnershipError,
    RateLimitError,
)


@pytest.fixture
def mock_dynamo(monkeypatch):
    """Mock owner_chats_table — every test uses this."""
    table = MagicMock()
    table.put_item = MagicMock(return_value={})
    table.query = MagicMock(return_value={"Items": []})

    monkeypatch.setattr(
        "core.services.owner_chat_service.dynamo.owner_chats_table",
        lambda: table,
    )
    return table


def test_persist_message_writes_owner_to_agent_record(mock_dynamo, monkeypatch):
    """persist_message writes a row with direction='owner_to_agent'."""
    monkeypatch.setattr(
        "core.services.owner_chat_service._get_agent",
        lambda aid: {"id": "agent-1", "userId": "user-1"},
    )
    svc = OwnerChatService()
    msg = svc.persist_message(
        agent_id="agent-1",
        owner_id="user-1",
        direction="owner_to_agent",
        text="hello",
    )
    assert msg["id"]
    assert msg["agentId"] == "agent-1"
    assert msg["ownerId"] == "user-1"
    assert msg["direction"] == "owner_to_agent"
    assert msg["text"] == "hello"
    assert msg["createdAt"] > 0
    mock_dynamo.put_item.assert_called_once()


def test_persist_message_rejects_when_owner_mismatch(mock_dynamo, monkeypatch):
    """OwnershipError when caller's owner_id != agent.userId."""
    monkeypatch.setattr(
        "core.services.owner_chat_service._get_agent",
        lambda aid: {"id": "agent-1", "userId": "user-OTHER"},
    )
    svc = OwnerChatService()
    with pytest.raises(OwnershipError):
        svc.persist_message(
            agent_id="agent-1",
            owner_id="user-1",
            direction="owner_to_agent",
            text="hi",
        )


def test_rate_limit_blocks_second_message_within_2s(mock_dynamo, monkeypatch):
    """Rate limit allows 1 msg / 2s / (owner, agent) pair."""
    monkeypatch.setattr(
        "core.services.owner_chat_service._get_agent",
        lambda aid: {"id": "agent-1", "userId": "user-1"},
    )
    # Simulate the rate-limit query finding a recent message on the
    # second call (the persist_message itself uses the same table mock,
    # so we have to set the side-effect cleverly):
    call_count = {"n": 0}

    def fake_query(*args, **kwargs):
        call_count["n"] += 1
        # First persist_message call: rate-limit query is the first invocation,
        # returns empty (no recent messages). Then put_item runs.
        # Second persist_message call: rate-limit query returns a recent
        # message → RateLimitError.
        if call_count["n"] == 1:
            return {"Items": []}
        return {"Items": [{"id": "prev", "createdAt": 999_999_999_999}]}

    mock_dynamo.query.side_effect = fake_query

    svc = OwnerChatService()
    svc.persist_message(
        agent_id="agent-1",
        owner_id="user-1",
        direction="owner_to_agent",
        text="hi 1",
    )
    with pytest.raises(RateLimitError):
        svc.persist_message(
            agent_id="agent-1",
            owner_id="user-1",
            direction="owner_to_agent",
            text="hi 2",
        )


def test_rate_limit_does_not_apply_to_agent_replies(mock_dynamo, monkeypatch):
    """Agent replies (direction='agent_to_owner') are not rate-limited."""
    monkeypatch.setattr(
        "core.services.owner_chat_service._get_agent",
        lambda aid: {"id": "agent-1", "userId": "user-1"},
    )
    svc = OwnerChatService()
    # Both succeed even though no time passes between them.
    svc.persist_message(
        agent_id="agent-1",
        owner_id="user-1",
        direction="agent_to_owner",
        text="reply 1",
    )
    svc.persist_message(
        agent_id="agent-1",
        owner_id="user-1",
        direction="agent_to_owner",
        text="reply 2",
    )
    # The rate-limit query should NEVER have been called for agent replies.
    # (put_item is called twice; query is called zero times.)
    assert mock_dynamo.query.call_count == 0


def test_fetch_history_returns_messages_ordered_oldest_first(mock_dynamo, monkeypatch):
    """fetch_history queries GSI agentId-createdAt-index and returns oldest first."""
    monkeypatch.setattr(
        "core.services.owner_chat_service._get_agent",
        lambda aid: {"id": "agent-1", "userId": "user-1"},
    )
    mock_dynamo.query.return_value = {
        "Items": [
            {
                "id": "m1",
                "agentId": "agent-1",
                "ownerId": "user-1",
                "direction": "owner_to_agent",
                "text": "hi",
                "createdAt": 1000,
            },
            {
                "id": "m2",
                "agentId": "agent-1",
                "ownerId": "user-1",
                "direction": "agent_to_owner",
                "text": "yo",
                "createdAt": 2000,
            },
        ]
    }
    svc = OwnerChatService()
    history = svc.fetch_history(agent_id="agent-1", owner_id="user-1", limit=50)
    assert len(history) == 2
    assert history[0]["id"] == "m1"
    assert history[1]["id"] == "m2"
    mock_dynamo.query.assert_called_once()
    kwargs = mock_dynamo.query.call_args.kwargs
    assert kwargs["IndexName"] == "agentId-createdAt-index"


def test_fetch_history_rejects_non_owner(mock_dynamo, monkeypatch):
    """OwnershipError when caller's owner_id != agent.userId."""
    monkeypatch.setattr(
        "core.services.owner_chat_service._get_agent",
        lambda aid: {"id": "agent-1", "userId": "user-OTHER"},
    )
    svc = OwnerChatService()
    with pytest.raises(OwnershipError):
        svc.fetch_history(agent_id="agent-1", owner_id="user-1", limit=50)
