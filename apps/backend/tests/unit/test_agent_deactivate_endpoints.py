"""Tests for agent deactivate / reactivate endpoints.

Surfaced from the 2026-05-02 fresh-real-user dry runs (finding E): the
5-agent quota was reachable from the UI but had no remove control. These
endpoints are the soft-delete primitive a future "Remove" button calls.

Soft-delete only — sets ``isActive="false"`` so the agent record (with
TOWN_LIFE.md, sprite, conversation history, relationships) is preserved.
Hard delete is intentionally out of scope.
"""

from unittest.mock import patch

from fastapi.testclient import TestClient

from core.quota import QuotaExceeded
from main import app


def _auth_headers() -> dict:
    return {"Authorization": "Bearer fake-token"}


def _make_auth_override(user_id: str):
    from core.auth import AuthContext

    def _fake():
        return AuthContext(user_id=user_id)

    return _fake


def _agent_record(*, agent_id: str, user_id: str, is_active: str = "true") -> dict:
    return {
        "id": agent_id,
        "userId": user_id,
        "agentName": "test-agent",
        "displayName": "Test Agent",
        "isActive": is_active,
        "currentLocation": "plaza",
    }


# ---------------------------------------------------------------------------
# Deactivate
# ---------------------------------------------------------------------------


def test_deactivate_succeeds_for_owner():
    from core.auth import get_current_user

    app.dependency_overrides[get_current_user] = _make_auth_override("user-1")
    try:
        with (
            patch(
                "routers.town.TownService.get_town_agent_by_id",
                return_value=_agent_record(agent_id="agent-1", user_id="user-1"),
            ),
            patch("routers.town.TownService.set_agent_active") as p_set,
            patch("routers.town.get_town_agent_ws_manager"),
            patch("routers.town.get_game_server_client"),
        ):
            client = TestClient(app)
            r = client.post(
                "/api/v1/town/agent/agent-1/deactivate",
                headers=_auth_headers(),
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["isActive"] is False
            assert body["id"] == "agent-1"
            # Goes through set_agent_active so the quota slot is released.
            p_set.assert_called_once_with("agent-1", False)
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_deactivate_returns_403_for_non_owner():
    from core.auth import get_current_user

    app.dependency_overrides[get_current_user] = _make_auth_override("user-attacker")
    try:
        with patch(
            "routers.town.TownService.get_town_agent_by_id",
            return_value=_agent_record(agent_id="agent-1", user_id="user-victim"),
        ):
            client = TestClient(app)
            r = client.post(
                "/api/v1/town/agent/agent-1/deactivate",
                headers=_auth_headers(),
            )
            assert r.status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_deactivate_returns_404_when_agent_missing():
    from core.auth import get_current_user

    app.dependency_overrides[get_current_user] = _make_auth_override("user-1")
    try:
        with patch(
            "routers.town.TownService.get_town_agent_by_id",
            return_value=None,
        ):
            client = TestClient(app)
            r = client.post(
                "/api/v1/town/agent/missing/deactivate",
                headers=_auth_headers(),
            )
            assert r.status_code == 404
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_deactivate_is_idempotent_when_already_inactive():
    from core.auth import get_current_user

    app.dependency_overrides[get_current_user] = _make_auth_override("user-1")
    try:
        with (
            patch(
                "routers.town.TownService.get_town_agent_by_id",
                return_value=_agent_record(agent_id="agent-1", user_id="user-1", is_active="false"),
            ),
            patch("routers.town.TownService.set_agent_active") as p_set,
            patch("routers.town.get_town_agent_ws_manager"),
            patch("routers.town.get_game_server_client"),
        ):
            client = TestClient(app)
            r = client.post(
                "/api/v1/town/agent/agent-1/deactivate",
                headers=_auth_headers(),
            )
            assert r.status_code == 200
            body = r.json()
            assert body["isActive"] is False
            # No update needed if already inactive.
            p_set.assert_not_called()
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# Reactivate
# ---------------------------------------------------------------------------


def test_reactivate_succeeds_for_owner_under_quota():
    from core.auth import get_current_user

    app.dependency_overrides[get_current_user] = _make_auth_override("user-1")
    try:
        with (
            patch(
                "routers.town.TownService.get_town_agent_by_id",
                return_value=_agent_record(agent_id="agent-1", user_id="user-1", is_active="false"),
            ),
            patch("routers.town.TownService.set_agent_active") as p_set,
        ):
            client = TestClient(app)
            r = client.post(
                "/api/v1/town/agent/agent-1/reactivate",
                headers=_auth_headers(),
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["isActive"] is True
            p_set.assert_called_once_with("agent-1", True)
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_reactivate_returns_429_when_quota_full():
    from core.auth import get_current_user

    app.dependency_overrides[get_current_user] = _make_auth_override("user-1")
    try:
        with (
            patch(
                "routers.town.TownService.get_town_agent_by_id",
                return_value=_agent_record(agent_id="agent-1", user_id="user-1", is_active="false"),
            ),
            patch(
                "routers.town.TownService.set_agent_active",
                side_effect=QuotaExceeded(kind="agents", limit=5, current=5),
            ),
        ):
            client = TestClient(app)
            r = client.post(
                "/api/v1/town/agent/agent-1/reactivate",
                headers=_auth_headers(),
            )
            assert r.status_code == 429, r.text
            assert r.json()["detail"]["current"] == 5
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_reactivate_returns_403_for_non_owner():
    from core.auth import get_current_user

    app.dependency_overrides[get_current_user] = _make_auth_override("user-attacker")
    try:
        with patch(
            "routers.town.TownService.get_town_agent_by_id",
            return_value=_agent_record(agent_id="agent-1", user_id="user-victim", is_active="false"),
        ):
            client = TestClient(app)
            r = client.post(
                "/api/v1/town/agent/agent-1/reactivate",
                headers=_auth_headers(),
            )
            assert r.status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)
