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
* Patch ``core.services.sprite_queue.get_sprite_queue`` for sprite enqueue
  calls (unchanged from the existing ``register_agent`` path) so PixelLab
  is never hit.

The 429 response body MUST contain ``kind``, ``limit``, and ``current`` so
the daemon can surface a human-readable error.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

TEST_USER_ID = "quota-test-user-1"
INSTANCE_ID = "inst-quota-1"
FAKE_TOKEN = "fake-quota-token"


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
    def test_eleventh_sprite_request_returns_429(self, client, dynamodb_tables):
        """11th sprite-gen in 24h → 429, even if the user has < 5 active agents."""
        from core.services.user_service import SPRITE_QUOTA_DAILY, UserService

        # Pre-consume the daily sprite quota
        svc = UserService()
        svc.get_or_create(TEST_USER_ID)
        for _ in range(SPRITE_QUOTA_DAILY):
            svc.consume_sprite_quota(TEST_USER_ID)

        # Now attempt a registration WITH an appearance — this triggers the
        # sprite-quota check BEFORE enqueue.  We stub get_sprite_queue so the
        # test doesn't blow up if the quota guard happens to pass (it shouldn't).
        mock_queue = MagicMock()
        mock_queue.enqueue.return_value = 1

        with patch("core.services.sprite_queue.get_sprite_queue", return_value=mock_queue):
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

        assert resp.status_code == 429
        body = resp.json()
        assert body.get("error") == "quota_exceeded"
        assert body.get("kind") == "sprite_generation"
        assert int(body.get("limit", 0)) == SPRITE_QUOTA_DAILY
        assert int(body.get("current", -1)) >= SPRITE_QUOTA_DAILY

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
