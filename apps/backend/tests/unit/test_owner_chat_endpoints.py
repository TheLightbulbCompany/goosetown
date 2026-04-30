import time
from unittest.mock import patch

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
