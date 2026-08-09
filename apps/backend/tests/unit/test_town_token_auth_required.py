"""Regression guard for #426 — town_token routes must answer 401, not 422.

Every route behind ``get_town_token_user`` used to declare the header as
``Header(...)`` (required). FastAPI resolves a missing required header as a
*validation* error and returns **422 before the dependency body runs**, so an
unauthenticated probe of ``/exchange/upload-url`` came back 422 — the same
status a well-authenticated request with a bad body gets. That is what made
#426 read as "no auth gate" from the outside.

The gate was in fact always there. These tests pin the *status code* so the
wire contract can't drift back to an ambiguous 422: no credentials → 401.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

# Routes that authenticate with an agent town_token (not a Clerk JWT).
# (method, path, minimal body that would otherwise pass schema validation)
TOWN_TOKEN_ROUTES = [
    ("POST", "/api/v1/town/agent/register", {}),
    ("GET", "/api/v1/town/agent/status", None),
    (
        "POST",
        "/api/v1/town/exchange/upload-url",
        {"job_id": "j1", "filename": "out.txt", "content_length": 10},
    ),
]


@pytest.fixture
def client(dynamodb_tables):
    """TestClient with NO auth override — exercises the real dependency."""
    from main import app

    return TestClient(app)


@pytest.mark.parametrize(("method", "path", "body"), TOWN_TOKEN_ROUTES)
def test_missing_authorization_header_returns_401(client, method, path, body):
    """No Authorization header at all → 401 (was: 422)."""
    resp = client.request(method, path, json=body)
    assert resp.status_code == 401, f"{method} {path} returned {resp.status_code}: {resp.text[:200]}"


@pytest.mark.parametrize(("method", "path", "body"), TOWN_TOKEN_ROUTES)
def test_malformed_authorization_header_returns_401(client, method, path, body):
    """Present-but-not-Bearer header → 401, not 422 and not a 500."""
    resp = client.request(method, path, json=body, headers={"Authorization": "Basic abc"})
    assert resp.status_code == 401


@pytest.mark.parametrize(("method", "path", "body"), TOWN_TOKEN_ROUTES)
def test_bad_signature_bearer_token_returns_401(client, method, path, body):
    """Bearer token that fails HMAC verification → 401."""
    resp = client.request(method, path, json=body, headers={"Authorization": "Bearer not.a.real.token"})
    assert resp.status_code == 401


def test_upload_url_rejects_auth_before_parsing_body(client):
    """Auth must be decided without trusting the body.

    An unauthenticated caller sending a *garbage* body still gets 401 — the
    401 does not depend on the body being well-formed, so an attacker can't
    use body-shape to distinguish "gated" from "ungated".
    """
    resp = client.post("/api/v1/town/exchange/upload-url", json={"nonsense": True})
    assert resp.status_code == 401
