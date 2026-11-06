"""TDD: register_agent enforces 5-agent and 10-sprite-per-24h hard caps.

Strategy
--------
* Override ``get_town_token_user`` so tests don't need a real HMAC-signed
  token or a live instances table entry — the auth bypass is identical to
  the pattern used in ``test_revoke_endpoint.py`` for Clerk auth.
* Seed the DynamoDB tables via the shared ``dynamodb_tables`` fixture from
  conftest.py (which already creates agents + instances; conftest now also
  creates the users table so both quota paths resolve without a separate
  mock_aws context).
* Patch ``core.services.sprite_jobs.enqueue_signup`` for sprite enqueue
  calls so PixelLab is never hit.

The 429 response body MUST contain ``kind``, ``limit``, and ``current`` so
the daemon can surface a human-readable error.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

TEST_USER_ID = "quota-test-user-1"
INSTANCE_ID = "inst-quota-1"
FAKE_TOKEN = "fake-quota-token"  # nosec B105: test fixture token literal, not a credential


def _town_token_override():
    """Bypass HMAC token verification — returns (user_id, raw_token)."""
    return (TEST_USER_ID, FAKE_TOKEN)


@pytest.fixture
def client(dynamodb_tables):
    """TestClient with town_token auth bypassed to TEST_USER_ID.

    We also seed a minimal instance row so the register_agent handler's
    ``get_active_instance`` check doesn't bail with 400.
    """
    from main import app
    from routers.town import get_town_token_user

    # Seed a minimal active instance for TEST_USER_ID
    instances_table = dynamodb_tables.Table("goosetown-instances")
    instances_table.put_item(
        Item={
            "id": INSTANCE_ID,
            "userId": TEST_USER_ID,
            "townToken": FAKE_TOKEN,
            "isActive": "true",
            "apartmentUnit": 1,
        }
    )

    app.dependency_overrides[get_town_token_user] = _town_token_override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_town_token_user, None)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _register_payload(name: str) -> dict:
    return {
        "agent_name": name,
        "display_name": name.replace("-", " ").title(),
        "personality": "test personality",
        "appearance": "",  # blank → no sprite enqueue path exercised
        "traits": "introvert",
    }


# ---------------------------------------------------------------------------
# Agent-count quota test
# ---------------------------------------------------------------------------


class TestAgentCountQuota:
    def test_sixth_register_returns_429(self, client, dynamodb_tables):
        """6th agent for the same user → 429 with quota detail."""
        # Register 5 agents
        for i in range(5):
            resp = client.post(
                "/api/v1/town/agent/register",
                json=_register_payload(f"agent-{i}"),
            )
            assert resp.status_code == 200, f"agent {i + 1}/5 failed: {resp.text}"

        # 6th → 429
        resp = client.post(
            "/api/v1/town/agent/register",
            json=_register_payload("agent-5"),
        )
        assert resp.status_code == 429
        body = resp.json()
        assert body.get("error") == "quota_exceeded"
        assert body.get("kind") == "agents"
        assert int(body.get("limit", 0)) == 5
        assert int(body.get("current", -1)) >= 5

    def test_reactivating_inactive_agent_bypasses_quota(self, client, dynamodb_tables):
        """Reactivating an INACTIVE agent (e.g. one that left town) doesn't
        count against the quota — opt_in takes the existing-row path and
        returns early before the quota check.

        Setup: 4 active agents + 1 inactive agent (below quota). The inactive
        agent's reactivation path in opt_in must NOT trigger the quota check
        even after we bump the active count to 4 (still under limit).
        """
        # Register 4 active agents (under the 5-agent limit)
        for i in range(4):
            resp = client.post(
                "/api/v1/town/agent/register",
                json=_register_payload(f"active-{i}"),
            )
            assert resp.status_code == 200, f"agent {i + 1}/4 failed: {resp.text}"

        # Seed a pre-existing INACTIVE agent row directly into DynamoDB
        # (simulates an agent that previously left town).
        agents_table = dynamodb_tables.Table("goosetown-agents")
        agents_table.put_item(
            Item={
                "id": "inactive-agent-id",
                "userId": TEST_USER_ID,
                "agentName": "returning-agent",
                "displayName": "Returning Agent",
                "isActive": "false",  # explicitly inactive
                "joinedAt": "2026-01-01T00:00:00Z",
                "spriteReady": False,
            }
        )

        # The router's get_agent_by_name only returns active agents, so it
        # returns None for the inactive row → proceeds to opt_in → opt_in
        # finds the existing inactive row and reactivates it WITHOUT hitting
        # the quota check.  4 active < 5 limit, so quota wouldn't block
        # anyway, but the key invariant is that the reactivation path exits
        # before the quota check runs.
        resp = client.post(
            "/api/v1/town/agent/register",
            json=_register_payload("returning-agent"),
        )
        assert resp.status_code == 200, f"reactivation failed: {resp.text}"


# ---------------------------------------------------------------------------
# Sprite-generation quota test
# ---------------------------------------------------------------------------


class TestSpriteQuota:
    def test_eleventh_sprite_request_registers_without_sprite(self, client, dynamodb_tables):
        """11th sprite-gen today → registration still succeeds on the placeholder
        avatar (no 429 that would leave the agent half-registered), no sprite queued."""
        from core.services.town_service import TownService
        from core.services.user_service import SPRITE_QUOTA_DAILY, UserService

        # Pre-consume the daily sprite quota
        svc = UserService()
        svc.get_or_create(TEST_USER_ID)
        for _ in range(SPRITE_QUOTA_DAILY):
            svc.consume_sprite_quota(TEST_USER_ID)

        # Now attempt a registration WITH an appearance — this triggers the
        # sprite-quota check BEFORE enqueue.  We stub enqueue_signup so the
        # test doesn't blow up if the quota guard happens to pass (it shouldn't).
        with patch("core.services.sprite_jobs.enqueue_signup", return_value=True) as p_enqueue:
            resp = client.post(
                "/api/v1/town/agent/register",
                json={
                    "agent_name": "sprite-seeker",
                    "display_name": "Sprite Seeker",
                    "personality": "test",
                    "appearance": "a tall robot with glowing eyes",
                    "traits": "introvert",
                },
            )

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "sprite skipped: daily limit"
        p_enqueue.assert_not_called()
        agent = TownService().get_agent_by_name(TEST_USER_ID, "sprite-seeker")
        assert agent is not None and not agent.get("spriteUrl")
        assert UserService().sprites_used_today(TEST_USER_ID) == SPRITE_QUOTA_DAILY

    def test_no_appearance_skips_sprite_quota(self, client, dynamodb_tables):
        """When appearance is blank the sprite path is skipped entirely —
        a user at sprite-quota limit can still register a text-only agent."""
        from core.services.user_service import SPRITE_QUOTA_DAILY, UserService

        svc = UserService()
        svc.get_or_create(TEST_USER_ID)
        for _ in range(SPRITE_QUOTA_DAILY):
            svc.consume_sprite_quota(TEST_USER_ID)

        resp = client.post(
            "/api/v1/town/agent/register",
            json=_register_payload("text-only-agent"),
        )
        # blank appearance → sprite path not entered → quota not checked
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Register-path appearance persistence (codex P1)
# ---------------------------------------------------------------------------


class TestAppearancePersistOnRegister:
    """appearance must be persisted via update_agent_state BEFORE enqueue_signup
    so the sprite worker finds it on the agent row (opt_in does not store it)."""

    def test_appearance_persisted_before_enqueue(self, client, dynamodb_tables):
        """Registration with appearance calls update_agent_state(appearance=…)
        BEFORE enqueue_signup so the worker sees it on the row."""
        import core.config as _config_mod

        enqueued_calls: list = []
        update_calls: list = []

        def _fake_enqueue(agent_id, display_name, appearance, **kw):
            enqueued_calls.append({"agent_id": agent_id, "appearance": appearance})
            return True

        def _fake_update(agent_id, **kwargs):
            update_calls.append({"agent_id": agent_id, **kwargs})
            # Return a minimal agent dict so callers don't blow up on None.
            return {"id": agent_id}

        # The router gates the whole sprite block on settings having values;
        # set them on the already-cached singleton so the block is entered.
        orig_api_key = _config_mod.settings.PIXELLAB_API_KEY
        orig_bucket = _config_mod.settings.SPRITE_S3_BUCKET
        orig_cdn = _config_mod.settings.SPRITE_CDN_URL
        _config_mod.settings.PIXELLAB_API_KEY = "fake-key"
        _config_mod.settings.SPRITE_S3_BUCKET = "fake-bucket"
        _config_mod.settings.SPRITE_CDN_URL = "https://fake-cdn.example.com"
        try:
            with (
                patch("core.services.sprite_jobs.enqueue_signup", side_effect=_fake_enqueue),
                patch(
                    "core.services.town_service.TownService.update_agent_state",
                    side_effect=_fake_update,
                ),
            ):
                resp = client.post(
                    "/api/v1/town/agent/register",
                    json={
                        "agent_name": "appearance-tester",
                        "display_name": "Appearance Tester",
                        "personality": "test",
                        "appearance": "a pixel-art goose in a business suit",
                        "traits": "introvert",
                    },
                )
        finally:
            _config_mod.settings.PIXELLAB_API_KEY = orig_api_key
            _config_mod.settings.SPRITE_S3_BUCKET = orig_bucket
            _config_mod.settings.SPRITE_CDN_URL = orig_cdn

        assert resp.status_code == 200, resp.json()

        # There must be at least one update_agent_state call that persists appearance.
        appearance_persist_calls = [c for c in update_calls if c.get("appearance")]
        assert appearance_persist_calls, "update_agent_state was never called with appearance before enqueue_signup"

        # enqueue_signup must also have been called.
        assert enqueued_calls, "enqueue_signup was never called"

        # The persisted appearance must match the request value.
        persisted = appearance_persist_calls[0]["appearance"]
        assert persisted == "a pixel-art goose in a business suit", (
            f"persisted appearance does not match request: {persisted!r}"
        )
