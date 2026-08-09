"""Regression guard for #427 — the API Gateway WS bridge is a closed seam.

``/town/ws-message`` and ``/town/ws-disconnect`` are only ever meant to be
called by the API Gateway $default / $disconnect Lambda integrations. Before
this fix:

* ``ws-disconnect`` had **no gate at all** — a bare ``{"connectionId": ...}``
  POST returned 200 and evicted that agent from the game server.
* ``ws-message``'s gate was **vacuous**: ``WS_LAMBDA_SECRET`` was never wired
  through CDK, so the check compared ``"" != ""`` and admitted everyone.

The tests below pin both the rejection and the *fail-closed* direction, since
the original bug was an auth check that silently passed when unconfigured.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

SECRET = "test-ws-lambda-secret"  # nosec B105: test fixture, not a credential

WS_ROUTES = [
    ("/api/v1/town/ws-message", {"connectionId": "c1", "principalId": "p1", "body": {}}),
    ("/api/v1/town/ws-disconnect", {"connectionId": "c1"}),
]


@pytest.fixture
def client(dynamodb_tables):
    from main import app

    return TestClient(app)


@pytest.fixture
def prod_with_secret(monkeypatch):
    """Prod-like: secret configured, so the gate must enforce it."""
    from core.config import settings

    monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
    monkeypatch.setattr(settings, "WS_LAMBDA_SECRET", SECRET)


@pytest.fixture
def prod_without_secret(monkeypatch):
    """Prod-like misconfiguration: secret missing. Must FAIL CLOSED."""
    from core.config import settings

    monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
    monkeypatch.setattr(settings, "WS_LAMBDA_SECRET", "")


@pytest.mark.parametrize(("path", "body"), WS_ROUTES)
def test_no_secret_header_returns_403(client, prod_with_secret, path, body):
    """The headline regression: unauthenticated POST must be refused."""
    resp = client.post(path, json=body)
    assert resp.status_code == 403, f"{path} returned {resp.status_code}: {resp.text[:200]}"


@pytest.mark.parametrize(("path", "body"), WS_ROUTES)
def test_wrong_secret_returns_403(client, prod_with_secret, path, body):
    resp = client.post(path, json=body, headers={"x-ws-lambda-secret": "wrong"})
    assert resp.status_code == 403


@pytest.mark.parametrize(("path", "body"), WS_ROUTES)
def test_empty_secret_header_returns_403(client, prod_with_secret, path, body):
    """An empty header must not satisfy the check.

    This is the exact shape of the original bug: ``provided != settings.X``
    with both sides empty evaluated to False and admitted the caller.
    """
    resp = client.post(path, json=body, headers={"x-ws-lambda-secret": ""})
    assert resp.status_code == 403


@pytest.mark.parametrize(("path", "body"), WS_ROUTES)
def test_unconfigured_secret_fails_closed_outside_dev(client, prod_without_secret, path, body):
    """Misconfiguration must refuse, not silently disable auth."""
    resp = client.post(path, json=body)
    assert resp.status_code == 403


@pytest.mark.parametrize(("path", "body"), WS_ROUTES)
def test_auth_runs_before_body_validation(client, prod_with_secret, path, body):
    """403, not 422, for an unauthenticated caller with a garbage body.

    Auth is a dependency so it resolves before Pydantic parses the body. This
    is what makes the seam legible from outside — the old inline check let a
    422 mask the fact that a gate existed at all (and, for ws-disconnect,
    that it did not).
    """
    resp = client.post(path, json={"garbage": True})
    assert resp.status_code == 403


@pytest.mark.parametrize(("path", "body"), WS_ROUTES)
def test_correct_secret_is_accepted(client, prod_with_secret, path, body):
    """The legitimate API Gateway caller still gets through.

    Guards against over-correcting into a gate that rejects everything: the
    routes must not answer 403 when the right header is present.
    """
    resp = client.post(path, json=body, headers={"x-ws-lambda-secret": SECRET})
    assert resp.status_code != 403


def test_dev_without_secret_stays_open(client, monkeypatch):
    """Local dev with no Secrets Manager keeps working."""
    from core.config import settings

    monkeypatch.setattr(settings, "ENVIRONMENT", "dev")
    monkeypatch.setattr(settings, "WS_LAMBDA_SECRET", "")
    resp = client.post("/api/v1/town/ws-disconnect", json={"connectionId": "c1"})
    assert resp.status_code == 200


def test_ws_lambda_secret_required_in_prod_config():
    """config.validate() must fail fast if the secret is missing outside dev."""
    from core.config import Settings

    s = Settings()
    s.ENVIRONMENT = "prod"
    s.TOWN_TOKEN_SECRET = "x"  # nosec B105: placeholder so validate() reaches the WS_LAMBDA_SECRET check
    s.WS_MANAGEMENT_API_URL = "x"
    s.SPRITE_S3_BUCKET = "x"
    s.SPRITE_CDN_URL = "x"
    s.CLERK_ISSUER = "x"
    s.WS_LAMBDA_SECRET = ""  # nosec B105: deliberately empty — this is the condition under test

    with pytest.raises(RuntimeError, match="WS_LAMBDA_SECRET"):
        s.validate()
