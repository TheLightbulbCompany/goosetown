"""TDD: GET /town/quota exposes per-user agent + sprite-gen quota state."""

from __future__ import annotations

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


def test_quota_returns_structured_payload(client):
    response = client.get("/api/v1/town/quota")
    assert response.status_code == 200
    body = response.json()
    assert "agents" in body
    assert "sprite_gen" in body
    assert body["agents"]["limit"] == 5
    assert isinstance(body["agents"]["used"], int)
    assert body["sprite_gen"]["limit"] == 10
    assert isinstance(body["sprite_gen"]["used_24h"], int)


def test_quota_requires_auth(dynamodb_tables):
    from main import app

    with TestClient(app, raise_server_exceptions=False) as bare_client:
        response = bare_client.get("/api/v1/town/quota")
    assert response.status_code == 401


def test_quota_reflects_active_agent_count(client, dynamodb_tables):
    """Pre-seed 3 active agents; expect used=3."""
    from core.services.town_service import TownService
    from core.services.user_service import UserService

    UserService().get_or_create(TEST_USER_ID)
    svc = TownService()
    for i in range(3):
        svc.opt_in(
            user_id=TEST_USER_ID,
            agent_name=f"q{i}",
            display_name=f"Q{i}",
            personality_summary="test",
        )

    response = client.get("/api/v1/town/quota")
    body = response.json()
    assert body["agents"]["used"] == 3


def test_quota_reflects_sprite_jobs_in_window(client, dynamodb_tables):
    from core.services.user_service import UserService

    svc = UserService()
    svc.get_or_create(TEST_USER_ID)
    for _ in range(4):
        svc.consume_sprite_quota(TEST_USER_ID)

    response = client.get("/api/v1/town/quota")
    body = response.json()
    assert body["sprite_gen"]["used_24h"] == 4
