"""Endpoint tests for POST /api/v1/town/internal/sprite-event.

Covers:
  1. type=progress → _send_to_daemon called with sprite_progress + status.
  2. type=static_ready → apply_sprite_update awaited with ("a-1", "static_ready").
  3. type=animations_updated → apply_sprite_update awaited with kind="animations_updated".
  4. Auth: SPRITE_EVENT_SECRET set (any env) — missing/wrong header → 403,
     correct header → 200; prod + NO secret → 403 (fail closed); dev + no
     secret → allowed.
  5. Invalid type value → 422 (Pydantic Literal enforcement).

Auth semantics (codex P2 fix): the endpoint has a DEDICATED shared secret
(SPRITE_EVENT_SECRET, header x-sprite-event-secret) wired through CDK into
both the backend and the sprite worker — unlike the vacuous WS_LAMBDA_SECRET
gate on /ws-message. Missing secret outside dev fails CLOSED.
The non-dev auth tests patch settings inline to simulate the env.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

_URL = "/api/v1/town/internal/sprite-event"

# In dev + empty SPRITE_EVENT_SECRET the gate is skipped entirely; no auth
# header is needed.
_DEV_HEADERS: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def client(dynamodb_tables):
    """Bare TestClient — no Clerk auth override needed (endpoint is internal)."""
    from main import app

    yield TestClient(app)


# ---------------------------------------------------------------------------
# Case 1: type=progress
# ---------------------------------------------------------------------------


class TestProgressEvent:
    def test_progress_calls_send_to_daemon(self, client):
        """`type=progress` must call _send_to_daemon with sprite_progress event."""
        captured: list[dict] = []

        def fake_send(agent_id, payload):
            captured.append({"agent_id": agent_id, "payload": payload})
            return True

        with patch("routers.town._send_to_daemon", side_effect=fake_send):
            resp = client.post(
                _URL,
                json={"agent_id": "agent-abc", "type": "progress", "status": "walk 2/4"},
                headers=_DEV_HEADERS,
            )

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True}
        assert len(captured) == 1
        assert captured[0]["agent_id"] == "agent-abc"
        assert captured[0]["payload"]["event"] == "sprite_progress"
        assert captured[0]["payload"]["status"] == "walk 2/4"

    def test_progress_status_defaults_to_empty_string(self, client):
        """Missing status field defaults to empty string in the daemon payload."""
        captured: list[dict] = []

        def fake_send(agent_id, payload):
            captured.append(payload)
            return True

        with patch("routers.town._send_to_daemon", side_effect=fake_send):
            resp = client.post(
                _URL,
                json={"agent_id": "agent-xyz", "type": "progress"},
                headers=_DEV_HEADERS,
            )

        assert resp.status_code == 200, resp.text
        assert captured[0]["status"] == ""


# ---------------------------------------------------------------------------
# Case 2 & 3: type=static_ready / animations_updated → apply_sprite_update
# ---------------------------------------------------------------------------


class TestSpriteReadyEvents:
    @pytest.mark.parametrize("kind", ["static_ready", "animations_updated"])
    def test_non_progress_calls_apply_sprite_update(self, client, kind):
        """`type={kind}` must call apply_sprite_update with the agent_id and kind.

        apply_sprite_update is imported lazily inside the endpoint body, so we
        patch at the canonical source module rather than ``routers.town``.
        """
        mock_apply = AsyncMock()

        with patch("core.services.sprite_update_apply.apply_sprite_update", mock_apply):
            resp = client.post(
                _URL,
                json={"agent_id": "agent-1", "type": kind},
                headers=_DEV_HEADERS,
            )

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True}
        mock_apply.assert_awaited_once_with("agent-1", kind)

    def test_static_ready_does_not_call_send_to_daemon(self, client):
        """apply_sprite_update path must NOT directly call _send_to_daemon from
        the endpoint layer (it may call it internally, but that's tested
        separately in test_sprite_update_apply.py)."""
        mock_apply = AsyncMock()
        send_calls: list = []

        def fake_send(aid, payload):
            send_calls.append(payload)
            return True

        with (
            patch("core.services.sprite_update_apply.apply_sprite_update", mock_apply),
            patch("routers.town._send_to_daemon", side_effect=fake_send),
        ):
            resp = client.post(
                _URL,
                json={"agent_id": "agent-1", "type": "static_ready"},
                headers=_DEV_HEADERS,
            )

        assert resp.status_code == 200
        # The endpoint layer itself must not call _send_to_daemon for non-progress types
        assert send_calls == [], f"Unexpected _send_to_daemon calls: {send_calls}"


# ---------------------------------------------------------------------------
# Case 4: Auth gate (dedicated SPRITE_EVENT_SECRET, header x-sprite-event-secret)
# ---------------------------------------------------------------------------


class TestAuthGate:
    def test_missing_header_returns_403_when_secret_set(self, client):
        """With SPRITE_EVENT_SECRET set (any env, even dev), missing header → 403."""
        from core.config import settings

        original_secret = settings.SPRITE_EVENT_SECRET
        settings.SPRITE_EVENT_SECRET = "super-secret-123"  # nosec B105: dummy shared-secret fixture, not a real credential
        try:
            resp = client.post(
                _URL,
                json={"agent_id": "a1", "type": "progress"},
                # No x-sprite-event-secret header
            )
        finally:
            settings.SPRITE_EVENT_SECRET = original_secret

        assert resp.status_code == 403

    def test_wrong_header_returns_403_when_secret_set(self, client):
        """With SPRITE_EVENT_SECRET set, wrong header value → 403 (prod env)."""
        from core.config import settings

        original_env = settings.ENVIRONMENT
        original_secret = settings.SPRITE_EVENT_SECRET
        settings.ENVIRONMENT = "prod"
        settings.SPRITE_EVENT_SECRET = "super-secret-123"  # nosec B105: dummy shared-secret fixture, not a real credential
        try:
            resp = client.post(
                _URL,
                json={"agent_id": "a1", "type": "progress"},
                headers={"x-sprite-event-secret": "wrong-value"},
            )
        finally:
            settings.ENVIRONMENT = original_env
            settings.SPRITE_EVENT_SECRET = original_secret

        assert resp.status_code == 403

    def test_correct_header_returns_200_in_prod(self, client):
        """In prod env with correct x-sprite-event-secret header → 200."""
        from core.config import settings

        original_env = settings.ENVIRONMENT
        original_secret = settings.SPRITE_EVENT_SECRET
        settings.ENVIRONMENT = "prod"
        settings.SPRITE_EVENT_SECRET = "super-secret-123"  # nosec B105: dummy shared-secret fixture, not a real credential
        try:
            with patch("routers.town._send_to_daemon", return_value=True):
                resp = client.post(
                    _URL,
                    json={"agent_id": "a1", "type": "progress", "status": "test"},
                    headers={"x-sprite-event-secret": "super-secret-123"},
                )
        finally:
            settings.ENVIRONMENT = original_env
            settings.SPRITE_EVENT_SECRET = original_secret

        assert resp.status_code == 200

    def test_prod_with_no_secret_fails_closed(self, client):
        """Prod env + NO secret configured → 403. An unset secret in prod is a
        misconfiguration, not an excuse to accept unauthenticated calls."""
        from core.config import settings

        original_env = settings.ENVIRONMENT
        original_secret = settings.SPRITE_EVENT_SECRET
        settings.ENVIRONMENT = "prod"
        settings.SPRITE_EVENT_SECRET = ""  # nosec B105: dummy shared-secret fixture, not a real credential
        try:
            resp = client.post(
                _URL,
                json={"agent_id": "a1", "type": "progress"},
            )
        finally:
            settings.ENVIRONMENT = original_env
            settings.SPRITE_EVENT_SECRET = original_secret

        assert resp.status_code == 403

    def test_dev_mode_no_secret_skips_gate(self, client):
        """Dev env + empty SPRITE_EVENT_SECRET → gate skipped entirely, no header needed."""
        from core.config import settings

        # This is the normal conftest state (ENVIRONMENT=dev, SPRITE_EVENT_SECRET="")
        assert settings.ENVIRONMENT == "dev"
        assert settings.SPRITE_EVENT_SECRET == ""  # nosec B105: dummy shared-secret fixture, not a real credential

        with patch("routers.town._send_to_daemon", return_value=True):
            resp = client.post(
                _URL,
                json={"agent_id": "a1", "type": "progress"},
                # No header at all — should be allowed
            )

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Case 5: Invalid type value → 422
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_invalid_type_returns_422(self, client):
        """A type value not in the Literal set → 422 from Pydantic."""
        resp = client.post(
            _URL,
            json={"agent_id": "agent-1", "type": "explode_the_sprites"},
            headers=_DEV_HEADERS,
        )
        assert resp.status_code == 422

    def test_missing_agent_id_returns_422(self, client):
        """Missing required agent_id → 422."""
        resp = client.post(
            _URL,
            json={"type": "progress"},
            headers=_DEV_HEADERS,
        )
        assert resp.status_code == 422

    def test_missing_type_returns_422(self, client):
        """Missing required type → 422."""
        resp = client.post(
            _URL,
            json={"agent_id": "agent-1"},
            headers=_DEV_HEADERS,
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Case 5: auth resolves BEFORE body validation (#428)
# ---------------------------------------------------------------------------


class TestAuthPrecedesBodyValidation:
    """The gate must not be reachable only via a well-formed body.

    #428 was filed because an empty-body probe returned 422, read as "no auth
    gate". The gate was in fact real and fail-closed — but it lived *inside*
    the handler, so Pydantic rejected the body first and the auth code never
    ran. Moving it to a dependency makes the refusal honest.

    The 422 cases in TestValidation above remain correct: those are *authorized*
    callers (dev + no secret) sending bad bodies.
    """

    @pytest.fixture
    def prod_with_secret(self, monkeypatch):
        from core.config import settings

        monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
        monkeypatch.setattr(settings, "SPRITE_EVENT_SECRET", "sprite-secret-abc")  # nosec B105: test fixture

    @pytest.mark.parametrize(
        "body",
        [
            {},  # the exact probe from the issue
            {"garbage": True},
            {"agent_id": "agent-1"},  # missing `type`
            {"agent_id": "agent-1", "type": "explode_the_sprites"},  # bad enum
        ],
        ids=["empty", "garbage", "missing-type", "bad-enum"],
    )
    def test_unauthenticated_returns_403_regardless_of_body(self, client, prod_with_secret, body):
        """No secret header → 403 for every body shape, well-formed or not."""
        resp = client.post(_URL, json=body)
        assert resp.status_code == 403, f"body={body} returned {resp.status_code}"

    def test_empty_header_returns_403(self, client, prod_with_secret):
        """An empty header must not satisfy the gate."""
        resp = client.post(_URL, json={}, headers={"x-sprite-event-secret": ""})
        assert resp.status_code == 403

    def test_unconfigured_secret_outside_dev_fails_closed(self, client, monkeypatch):
        """Missing secret in prod refuses rather than opening the seam."""
        from core.config import settings

        monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
        monkeypatch.setattr(settings, "SPRITE_EVENT_SECRET", "")
        resp = client.post(_URL, json={})
        assert resp.status_code == 403

    def test_authorized_caller_still_reaches_body_validation(self, client, prod_with_secret):
        """With the right secret, a bad body still gets 422 — gate isn't swallowing traffic."""
        resp = client.post(
            _URL,
            json={"agent_id": "agent-1"},
            headers={"x-sprite-event-secret": "sprite-secret-abc"},
        )
        assert resp.status_code == 422
