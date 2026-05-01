"""Tests for the public ``GET /town/anonymous-viewer-token`` endpoint.

No auth required — the endpoint mints a short-lived HMAC-signed viewer token
with sentinel uid/iid values so unauthenticated browsers can subscribe to
world_state broadcasts without a Clerk session.
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from core.config import settings
from core.town_token import verify_town_token


def test_anonymous_viewer_token_endpoint_returns_token():
    """Anonymous viewer endpoint mints a token without requiring auth."""
    from main import app

    client = TestClient(app)
    response = client.get("/api/v1/town/anonymous-viewer-token")
    assert response.status_code == 200
    body = response.json()
    assert "token" in body
    assert isinstance(body["token"], str)
    assert len(body["token"]) > 20


def test_anonymous_viewer_token_verifies_with_sentinel_payload(monkeypatch):
    """The minted token verifies and carries the anonymous sentinel uid/iid."""
    monkeypatch.setattr(settings, "TOWN_TOKEN_SECRET", "test-secret-for-unit-tests")
    from main import app

    client = TestClient(app)
    response = client.get("/api/v1/town/anonymous-viewer-token")
    token = response.json()["token"]
    payload = verify_town_token(token)
    assert payload is not None
    assert payload["uid"] == "anonymous"
    assert payload["iid"] == "anonymous-viewer"


def test_anonymous_viewer_token_has_short_ttl(monkeypatch):
    """Anonymous tokens expire ~30 minutes after issue (not the 30-day default)."""
    monkeypatch.setattr(settings, "TOWN_TOKEN_SECRET", "test-secret-for-unit-tests")
    from main import app

    client = TestClient(app)
    before = int(time.time())
    response = client.get("/api/v1/town/anonymous-viewer-token")
    after = int(time.time())
    token = response.json()["token"]
    payload = verify_town_token(token)
    assert payload is not None
    exp = int(payload["exp"])
    iat = int(payload["iat"])
    assert 1795 <= (exp - iat) <= 1805
    assert before <= iat <= after
