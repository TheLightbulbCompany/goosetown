"""Regression guard for #430 — /town/admin/* needs a credential, not just an env.

What the "403 gate" actually was
--------------------------------
``if settings.ENVIRONMENT != "dev": raise HTTPException(403, "dev-only endpoint")``

That is a **deploy-environment kill switch, not an authentication check**. It is
not Clerk-admin, not signed, and not a static header — it does not look at the
caller at all. Consequences:

* In prod it refused *everyone*, admins included — which is why external probing
  saw a 403 and reasonably guessed there was an auth gate behind it.
* In **dev** it required nothing whatsoever, and ``api-dev.goosetown.isol8.co``
  is a public ALB. ``regenerate-sprite`` spends PixelLab generation credits and
  ``recomposite-sprites`` fans out across every agent in the table — against a
  PixelLab account shared with prod.

These tests pin: anonymous callers are refused **in dev too**, the allowlist is
honoured, and the OpenAPI spec now advertises the requirement.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core.auth import AuthContext

ADMIN_ROUTES = [
    "/api/v1/town/admin/recomposite-sprites",
    "/api/v1/town/admin/regenerate-sprite/PROBE",
]

ADMIN_UID = "user_admin_1"
OTHER_UID = "user_rando_2"


@pytest.fixture
def client(dynamodb_tables):
    from main import app

    return TestClient(app)


@pytest.fixture
def dev_env(monkeypatch):
    """Dev — the environment where these endpoints are *meant* to work."""
    from core.config import settings

    monkeypatch.setattr(settings, "ENVIRONMENT", "dev")
    monkeypatch.setattr(settings, "ADMIN_USER_IDS", "")


def _override_user(app, user_id: str):
    from core.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id=user_id)


@pytest.mark.parametrize("path", ADMIN_ROUTES)
def test_anonymous_refused_in_dev(client, dev_env, path):
    """THE regression: dev is a public ALB, so anonymous must be refused there.

    Previously this returned 200 in dev and queued PixelLab work.
    """
    resp = client.post(path)
    assert resp.status_code in (401, 403), f"{path} returned {resp.status_code}: {resp.text[:200]}"


@pytest.mark.parametrize("path", ADMIN_ROUTES)
def test_garbage_bearer_token_refused(client, dev_env, path):
    resp = client.post(path, headers={"Authorization": "Bearer not-a-real-jwt"})
    assert resp.status_code in (401, 403)


@pytest.mark.parametrize("path", ADMIN_ROUTES)
def test_non_allowlisted_user_refused(client, monkeypatch, path):
    """With ADMIN_USER_IDS set, an ordinary signed-in user is not an admin."""
    from core.config import settings
    from main import app

    monkeypatch.setattr(settings, "ENVIRONMENT", "dev")
    monkeypatch.setattr(settings, "ADMIN_USER_IDS", ADMIN_UID)
    _override_user(app, OTHER_UID)
    try:
        resp = client.post(path)
        assert resp.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_allowlisted_admin_is_accepted(client, monkeypatch):
    """Over-correction guard: a real admin must still get through in dev."""
    from core.config import settings
    from main import app

    monkeypatch.setattr(settings, "ENVIRONMENT", "dev")
    monkeypatch.setattr(settings, "ADMIN_USER_IDS", f"{ADMIN_UID}, someone_else")
    _override_user(app, ADMIN_UID)
    try:
        resp = client.post("/api/v1/town/admin/regenerate-sprite/PROBE")
        # 404 = passed auth and reached the handler (no such agent). Not 401/403.
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_authenticated_user_denied_when_no_allowlist_configured(client, dev_env):
    """An empty allowlist does not grant admin access."""
    from main import app

    _override_user(app, OTHER_UID)
    try:
        resp = client.post("/api/v1/town/admin/regenerate-sprite/PROBE")
        assert resp.status_code == 403
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("path", ADMIN_ROUTES)
def test_env_gate_still_applies_in_prod(client, monkeypatch, path):
    """The dev-only kill switch is retained as a second layer, not replaced."""
    from core.config import settings
    from main import app

    monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
    monkeypatch.setattr(settings, "ADMIN_USER_IDS", ADMIN_UID)
    _override_user(app, ADMIN_UID)
    try:
        resp = client.post(path)
        assert resp.status_code == 403
        assert "dev-only" in resp.text
    finally:
        app.dependency_overrides.clear()


def test_openapi_spec_marks_admin_routes_as_secured(client, dev_env):
    """Spec hygiene (#430): the schema must stop advertising these as public."""
    spec = client.get("/openapi.json").json()

    for path in ("/api/v1/town/admin/recomposite-sprites", "/api/v1/town/admin/regenerate-sprite/{agent_id}"):
        operation = spec["paths"][path]["post"]
        assert operation.get("security"), f"{path} still declared public in the OpenAPI spec"
