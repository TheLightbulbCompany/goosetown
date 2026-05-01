"""TDD: POST /town/instance/revoke calls UserService.revoke_tokens."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

TEST_USER_ID = "user_test_123"


def _auth_override():
    from core.auth import AuthContext

    return AuthContext(user_id=TEST_USER_ID)


@pytest.fixture
def client(dynamodb_tables):
    """TestClient with Clerk auth short-circuited to TEST_USER_ID."""
    from core.auth import get_current_user
    from main import app

    app.dependency_overrides[get_current_user] = _auth_override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_revoke_endpoint_returns_204_and_calls_user_service(client):
    """Authenticated user hits the endpoint, UserService.revoke_tokens fires."""
    with patch("core.services.user_service.UserService") as mock_us:
        mock_instance = mock_us.return_value
        response = client.post("/api/v1/town/instance/revoke")
        assert response.status_code == 204
        mock_instance.revoke_tokens.assert_called_once_with(TEST_USER_ID)


def test_revoke_endpoint_requires_auth(dynamodb_tables):
    """No JWT → 401 (the app maps missing credentials to 401)."""
    from main import app

    # Use a bare TestClient — no dependency override and no Authorization header.
    with TestClient(app, raise_server_exceptions=False) as bare_client:
        response = bare_client.post("/api/v1/town/instance/revoke")
    assert response.status_code == 401
