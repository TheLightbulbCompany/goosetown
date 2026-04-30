import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from main import app


def _auth_headers() -> dict:
    """Headers with a fake bearer token. The Clerk dependency is patched
    in each test to return a controlled AuthContext."""
    return {"Authorization": "Bearer fake-token"}


def _make_auth_override(user_id: str):
    """Return a sync callable for FastAPI dependency_overrides."""
    from core.auth import AuthContext

    def _fake():
        return AuthContext(user_id=user_id)

    return _fake


def test_post_owner_message_succeeds_for_owner():
    """Endpoint returns 200 with the persisted message id."""
    from core.auth import get_current_user

    persisted = {
        "id": "msg-1",
        "agentId": "agent-1",
        "ownerId": "user-1",
        "direction": "owner_to_agent",
        "text": "hello",
        "createdAt": int(time.time() * 1000),
    }
    app.dependency_overrides[get_current_user] = _make_auth_override("user-1")
    try:
        with (
            patch(
                "routers.town.OwnerChatService.persist_message",
                return_value=persisted,
            ) as p_persist,
            patch(
                "routers.town._send_to_daemon",
                return_value=True,
            ) as p_send,
        ):
            client = TestClient(app)
            r = client.post(
                "/api/v1/town/agent/agent-1/owner-message",
                json={"text": "hello"},
                headers=_auth_headers(),
            )
            assert r.status_code == 200
            body = r.json()
            assert body["id"] == "msg-1"
            assert body["createdAt"] == persisted["createdAt"]
            # Service called with the auth user_id, not from the body.
            p_persist.assert_called_once()
            kwargs = p_persist.call_args.kwargs
            assert kwargs["agent_id"] == "agent-1"
            assert kwargs["owner_id"] == "user-1"
            assert kwargs["direction"] == "owner_to_agent"
            assert kwargs["text"] == "hello"
            # Daemon was pushed.
            p_send.assert_called_once()
            send_args = p_send.call_args
            assert send_args.args[0] == "agent-1"
            assert send_args.args[1]["type"] == "owner_message"
            assert send_args.args[1]["text"] == "hello"
            assert send_args.args[1]["message_id"] == "msg-1"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_post_owner_message_returns_403_for_non_owner():
    """Endpoint returns 403 when the service raises OwnershipError."""
    from core.auth import get_current_user
    from core.services.owner_chat_service import OwnershipError

    app.dependency_overrides[get_current_user] = _make_auth_override("user-INTRUDER")
    try:
        with patch(
            "routers.town.OwnerChatService.persist_message",
            side_effect=OwnershipError("not owner"),
        ):
            client = TestClient(app)
            r = client.post(
                "/api/v1/town/agent/agent-1/owner-message",
                json={"text": "hi"},
                headers=_auth_headers(),
            )
            assert r.status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_post_owner_message_returns_429_when_rate_limited():
    """Endpoint returns 429 when the service raises RateLimitError."""
    from core.auth import get_current_user
    from core.services.owner_chat_service import RateLimitError

    app.dependency_overrides[get_current_user] = _make_auth_override("user-1")
    try:
        with patch(
            "routers.town.OwnerChatService.persist_message",
            side_effect=RateLimitError("too fast"),
        ):
            client = TestClient(app)
            r = client.post(
                "/api/v1/town/agent/agent-1/owner-message",
                json={"text": "spam"},
                headers=_auth_headers(),
            )
            assert r.status_code == 429
            assert r.headers["retry-after"] == "2"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_post_owner_message_rejects_empty_text():
    """Endpoint returns 400 on empty / whitespace text without invoking the service."""
    from core.auth import get_current_user

    app.dependency_overrides[get_current_user] = _make_auth_override("user-1")
    try:
        with patch("routers.town.OwnerChatService.persist_message") as p_persist:
            client = TestClient(app)
            r = client.post(
                "/api/v1/town/agent/agent-1/owner-message",
                json={"text": "   "},
                headers=_auth_headers(),
            )
            assert r.status_code == 400
            p_persist.assert_not_called()
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_get_owner_chat_returns_history_for_owner():
    """Endpoint returns the message list from the service."""
    from core.auth import get_current_user

    history = [
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
    app.dependency_overrides[get_current_user] = _make_auth_override("user-1")
    try:
        with patch(
            "routers.town.OwnerChatService.fetch_history",
            return_value=history,
        ) as p_fetch:
            client = TestClient(app)
            r = client.get(
                "/api/v1/town/agent/agent-1/owner-chat",
                headers=_auth_headers(),
            )
            assert r.status_code == 200
            body = r.json()
            assert body["messages"] == history
            kwargs = p_fetch.call_args.kwargs
            assert kwargs["agent_id"] == "agent-1"
            assert kwargs["owner_id"] == "user-1"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_get_owner_chat_returns_403_for_non_owner():
    """Endpoint returns 403 when caller is not the owner."""
    from core.auth import get_current_user
    from core.services.owner_chat_service import OwnershipError

    app.dependency_overrides[get_current_user] = _make_auth_override("user-INTRUDER")
    try:
        with patch(
            "routers.town.OwnerChatService.fetch_history",
            side_effect=OwnershipError("not owner"),
        ):
            client = TestClient(app)
            r = client.get(
                "/api/v1/town/agent/agent-1/owner-chat",
                headers=_auth_headers(),
            )
            assert r.status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_get_owner_chat_caps_limit_at_200():
    """If client requests limit > 200, the service is called with 200."""
    from core.auth import get_current_user

    app.dependency_overrides[get_current_user] = _make_auth_override("user-1")
    try:
        with patch(
            "routers.town.OwnerChatService.fetch_history",
            return_value=[],
        ) as p_fetch:
            client = TestClient(app)
            r = client.get(
                "/api/v1/town/agent/agent-1/owner-chat?limit=999",
                headers=_auth_headers(),
            )
            assert r.status_code == 200
            kwargs = p_fetch.call_args.kwargs
            assert kwargs["limit"] == 200
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_get_owner_chat_passes_before_param():
    """``before`` query param is forwarded as before_ms."""
    from core.auth import get_current_user

    app.dependency_overrides[get_current_user] = _make_auth_override("user-1")
    try:
        with patch(
            "routers.town.OwnerChatService.fetch_history",
            return_value=[],
        ) as p_fetch:
            client = TestClient(app)
            r = client.get(
                "/api/v1/town/agent/agent-1/owner-chat?limit=10&before=12345",
                headers=_auth_headers(),
            )
            assert r.status_code == 200
            kwargs = p_fetch.call_args.kwargs
            assert kwargs["before_ms"] == 12345
            assert kwargs["limit"] == 10
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_handle_reply_owner_persists_and_pushes_to_owner_viewers():
    """_handle_reply_owner_action persists agent's reply with direction
    'agent_to_owner' and pushes an owner_reply event to all of the owner's
    viewer connections via _push_to_owner_viewers."""
    from routers.town import _handle_reply_owner_action

    persisted = {
        "id": "msg-2",
        "agentId": "agent-1",
        "ownerId": "user-1",
        "direction": "agent_to_owner",
        "text": "OK heading there",
        "createdAt": 12345,
    }
    with (
        patch(
            "routers.town.OwnerChatService.persist_message",
            return_value=persisted,
        ) as p_persist,
        patch(
            "routers.town._push_to_owner_viewers",
        ) as p_push,
    ):
        _handle_reply_owner_action(
            agent_id="agent-1",
            owner_id="user-1",
            text="OK heading there",
            ref_message_id=None,
        )
        # Persisted with direction='agent_to_owner'.
        p_persist.assert_called_once()
        kwargs = p_persist.call_args.kwargs
        assert kwargs["agent_id"] == "agent-1"
        assert kwargs["owner_id"] == "user-1"
        assert kwargs["direction"] == "agent_to_owner"
        assert kwargs["text"] == "OK heading there"
        # Pushed to owner.
        p_push.assert_called_once()
        push_kwargs = p_push.call_args.kwargs
        assert push_kwargs["user_id"] == "user-1"
        payload = push_kwargs["payload"]
        assert payload["event"] == "owner_reply"
        assert payload["agent_id"] == "agent-1"
        assert payload["text"] == "OK heading there"
        assert payload["message_id"] == "msg-2"
        assert payload["timestamp"] == 12345


def test_handle_reply_owner_drops_empty_text():
    """Empty / whitespace text is silently dropped — no persist, no push."""
    from routers.town import _handle_reply_owner_action

    with (
        patch("routers.town.OwnerChatService.persist_message") as p_persist,
        patch("routers.town._push_to_owner_viewers") as p_push,
    ):
        _handle_reply_owner_action(
            agent_id="agent-1",
            owner_id="user-1",
            text="   ",
            ref_message_id=None,
        )
        p_persist.assert_not_called()
        p_push.assert_not_called()


def test_reply_owner_passes_ws_validator():
    """The full ws-message body for reply_owner validates without 422.

    Regression for the validator-vs-dispatcher mismatch caught in final review:
    the AgentAction Literal must include 'reply_owner' or every reply gets
    422'd before reaching _handle_reply_owner_action.
    """
    from pydantic import TypeAdapter

    from core.ws_models import WsMessage

    adapter = TypeAdapter(WsMessage)
    body = {
        "type": "town_agent_act",
        "action": "reply_owner",
        "text": "OK heading there",
    }
    # Should not raise.
    parsed = adapter.validate_python(body)
    assert parsed is not None


def test_reply_owner_with_ref_message_id_passes_ws_validator():
    """reply_owner with optional ref_message_id also validates."""
    from pydantic import TypeAdapter

    from core.ws_models import WsMessage

    adapter = TypeAdapter(WsMessage)
    body = {
        "type": "town_agent_act",
        "action": "reply_owner",
        "text": "Sure, heading to the library now.",
        "ref_message_id": "msg-abc-123",
    }
    parsed = adapter.validate_python(body)
    assert parsed is not None


def test_reply_owner_without_text_fails_ws_validator():
    """reply_owner missing text is correctly rejected by the validator."""
    from pydantic import TypeAdapter, ValidationError

    from core.ws_models import WsMessage

    adapter = TypeAdapter(WsMessage)
    body = {
        "type": "town_agent_act",
        "action": "reply_owner",
    }
    with pytest.raises(ValidationError):
        adapter.validate_python(body)
