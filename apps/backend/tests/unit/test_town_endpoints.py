"""Endpoint tests for routers/town.py using FastAPI's TestClient.

Covers the user-facing HTTP surface that was untested:
    - ``GET /apartment`` — auth, user-scoped filter, sprite_url versioning.
    - ``GET /conversations`` — public endpoint, shape of public_log, limit.
    - ``POST /instance`` — idempotent, returns signed town_token.

Auth is bypassed via FastAPI's ``dependency_overrides`` so tests don't need
real Clerk JWTs. DynamoDB is mocked via the shared ``dynamodb_tables``
fixture.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

TEST_USER_ID = "user_test_123"
OTHER_USER_ID = "user_other_456"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _seed_agent(
    dynamodb_tables,
    agent_id: str,
    user_id: str = TEST_USER_ID,
    agent_name: str = "lucky",
    display_name: str = "Lucky",
    sprite_ready: bool = True,
    sprite_url: str = "https://cdn.example/sprites/lucky/walk.png",
    sprite_updated_at: int | None = 1_776_000_000,
    current_location: str = "plaza",
    location_context: str = "town",
    energy: int = 80,
    mood: int = 5,
    is_active: str = "true",
):
    """Insert a minimal agent record into the mock agents table."""
    table = dynamodb_tables.Table("goosetown-agents")
    item = {
        "id": agent_id,
        "userId": user_id,
        "agentName": agent_name,
        "displayName": display_name,
        "instanceId": "inst-1",
        "isActive": is_active,
        "joinedAt": "2026-04-17T00:00:00Z",
        "spriteReady": sprite_ready,
        "spriteUrl": sprite_url,
        "currentLocation": current_location,
        "locationContext": location_context,
        "energy": energy,
        "mood": mood,
    }
    if sprite_updated_at is not None:
        item["spriteUpdatedAt"] = sprite_updated_at
    table.put_item(Item=item)
    return item


def _seed_conversation(
    dynamodb_tables,
    conv_id: str,
    participant_a: str,
    participant_b: str,
    location: str = "cafe",
    status: str = "ended",
    public_log: list | None = None,
    started_at: str = "2026-04-17T00:00:00Z",
    ended_at: str = "2026-04-17T00:05:00Z",
    updated_at: str = "2026-04-17T00:05:00Z",
    participants: list | None = None,
):
    table = dynamodb_tables.Table("goosetown-conversations")
    item = {
        "id": conv_id,
        "participantAId": participant_a,
        "participantBId": participant_b,
        "location": location,
        "status": status,
        "startedAt": started_at,
        "endedAt": ended_at,
        "updatedAt": updated_at,
        "turnCount": len(public_log or []),
        "publicLog": public_log or [],
    }
    if participants is not None:
        item["participants"] = participants
    table.put_item(Item=item)


# ---------------------------------------------------------------------------
# GET /apartment
# ---------------------------------------------------------------------------


class TestApartmentEndpoint:
    def test_returns_only_current_users_agents(self, client, dynamodb_tables):
        _seed_agent(dynamodb_tables, "a-1", user_id=TEST_USER_ID, agent_name="mine")
        _seed_agent(dynamodb_tables, "a-2", user_id=OTHER_USER_ID, agent_name="somebody_elses")

        resp = client.get("/api/v1/town/apartment")
        assert resp.status_code == 200
        body = resp.json()
        agent_ids = {a["agent_id"] for a in body["agents"]}
        assert agent_ids == {"a-1"}

    def test_sprite_url_includes_cache_buster(self, client, dynamodb_tables):
        """The endpoint must return ``?v=<spriteUpdatedAt>`` on every sprite
        URL so browsers don't serve stale cached responses forever."""
        _seed_agent(
            dynamodb_tables,
            "a-1",
            sprite_updated_at=1_776_400_000,
            sprite_url="https://cdn.example/sprites/a-1/walk.png",
        )

        resp = client.get("/api/v1/town/apartment")
        assert resp.status_code == 200
        agent = resp.json()["agents"][0]
        assert agent["sprite_url"] == ("https://cdn.example/sprites/a-1/walk.png?v=1776400000")

    def test_empty_sprite_url_when_sprite_not_ready(self, client, dynamodb_tables):
        """108068f fix — sprite_ready=False must NOT return a URL (or a
        literal ``None`` that would render as '<null>' via GDScript). We
        return an empty string so the frontend fallback branch fires."""
        _seed_agent(dynamodb_tables, "a-1", sprite_ready=False, sprite_url="")

        resp = client.get("/api/v1/town/apartment")
        assert resp.status_code == 200
        agent = resp.json()["agents"][0]
        assert agent["sprite_url"] == ""

    def test_returns_empty_list_for_user_with_no_agents(self, client):
        resp = client.get("/api/v1/town/apartment")
        assert resp.status_code == 200
        assert resp.json() == {"agents": [], "activity": []}


# ---------------------------------------------------------------------------
# GET /conversations
# ---------------------------------------------------------------------------


class TestConversationsEndpoint:
    def test_public_endpoint_requires_no_auth(self, dynamodb_tables):
        """/conversations is deliberately public — don't regress this."""
        from main import app

        client = TestClient(app)
        resp = client.get("/api/v1/town/conversations")
        assert resp.status_code == 200

    def test_returns_conversations_with_full_public_log(self, client, dynamodb_tables):
        """Modal UX (53038d7) depends on the full public_log being returned,
        not a truncated summary."""
        _seed_agent(dynamodb_tables, "agent-a", agent_name="a", display_name="Agent A")
        _seed_agent(dynamodb_tables, "agent-b", agent_name="b", display_name="Agent B")
        _seed_conversation(
            dynamodb_tables,
            "conv-1",
            participant_a="agent-a",
            participant_b="agent-b",
            location="cafe",
            public_log=[
                {"speaker": "Agent A", "text": "hi"},
                {"speaker": "Agent B", "text": "hello"},
                {"speaker": "Agent A", "text": "how are you"},
            ],
        )

        resp = client.get("/api/v1/town/conversations")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["conversations"]) == 1
        conv = body["conversations"][0]
        assert conv["participant_a"] == "Agent A"
        assert conv["participant_b"] == "Agent B"
        assert conv["location"] == "cafe"
        assert conv["turn_count"] == 3
        assert len(conv["public_log"]) == 3
        assert conv["public_log"][0] == {"speaker": "Agent A", "text": "hi"}

    def test_limit_parameter_respected(self, client, dynamodb_tables):
        _seed_agent(dynamodb_tables, "agent-a", agent_name="a", display_name="Agent A")
        _seed_agent(dynamodb_tables, "agent-b", agent_name="b", display_name="Agent B")
        for i in range(5):
            _seed_conversation(
                dynamodb_tables,
                f"conv-{i}",
                participant_a="agent-a",
                participant_b="agent-b",
                started_at=f"2026-04-17T00:0{i}:00Z",
                ended_at=f"2026-04-17T00:0{i + 1}:00Z",
                updated_at=f"2026-04-17T00:0{i + 1}:00Z",
            )

        resp = client.get("/api/v1/town/conversations?limit=3")
        assert resp.status_code == 200
        assert len(resp.json()["conversations"]) == 3

    def test_missing_participant_falls_back_to_unknown(self, client, dynamodb_tables):
        """If an agent record was deleted but a conversation still references
        it, we shouldn't 500 — just show 'Unknown'."""
        _seed_agent(dynamodb_tables, "agent-a", display_name="Rachel")
        # participant_b intentionally not seeded
        _seed_conversation(
            dynamodb_tables,
            "conv-1",
            participant_a="agent-a",
            participant_b="missing-agent-id",
            public_log=[{"speaker": "Rachel", "text": "hi"}],
        )

        resp = client.get("/api/v1/town/conversations")
        assert resp.status_code == 200
        conv = resp.json()["conversations"][0]
        assert conv["participant_a"] == "Rachel"
        assert conv["participant_b"] == "Unknown"

    def test_returns_full_participants_list_for_group_chat(
        self, client, dynamodb_tables
    ):
        """Group conversations (3+ participants) used to return only
        participantAId/participantBId, hiding the third+ speaker even when
        their turns appeared in the public log. The endpoint must surface
        the canonical ``participants`` list resolved to display names.
        """
        _seed_agent(dynamodb_tables, "agent-a", display_name="Rachel")
        _seed_agent(dynamodb_tables, "agent-b", display_name="Ross")
        _seed_agent(dynamodb_tables, "agent-c", display_name="Phoebe")
        _seed_conversation(
            dynamodb_tables,
            "conv-1",
            participant_a="agent-a",
            participant_b="agent-b",
            participants=["agent-a", "agent-b", "agent-c"],
            public_log=[
                {"speaker": "Rachel", "text": "hey"},
                {"speaker": "Ross", "text": "hi"},
                {"speaker": "Phoebe", "text": "joining you both"},
            ],
        )

        resp = client.get("/api/v1/town/conversations")
        assert resp.status_code == 200
        conv = resp.json()["conversations"][0]
        # New canonical field: every participant in join order, resolved
        # to display names.
        assert conv["participants"] == ["Rachel", "Ross", "Phoebe"]
        # Legacy fields still point at the first two so old clients work.
        assert conv["participant_a"] == "Rachel"
        assert conv["participant_b"] == "Ross"

    def test_falls_back_to_a_b_when_participants_missing(self, client, dynamodb_tables):
        """Legacy 2-party rows (no ``participants`` column) must still
        produce a sensible ``participants`` list in the response so the UI
        doesn't have to special-case them.
        """
        _seed_agent(dynamodb_tables, "agent-a", display_name="Rachel")
        _seed_agent(dynamodb_tables, "agent-b", display_name="Ross")
        _seed_conversation(
            dynamodb_tables,
            "conv-1",
            participant_a="agent-a",
            participant_b="agent-b",
            # participants intentionally omitted — simulates pre-migration row
        )

        resp = client.get("/api/v1/town/conversations")
        assert resp.status_code == 200
        conv = resp.json()["conversations"][0]
        assert conv["participants"] == ["Rachel", "Ross"]
        assert conv["participant_a"] == "Rachel"
        assert conv["participant_b"] == "Ross"

    def test_dedupes_participants_resolves_each_id_once(
        self, client, dynamodb_tables
    ):
        """``participants`` should not contain duplicates — caller could
        accidentally include the same id twice (once in legacy A/B, once in
        the explicit list). Backend dedupes before resolving.
        """
        _seed_agent(dynamodb_tables, "agent-a", display_name="Rachel")
        _seed_agent(dynamodb_tables, "agent-b", display_name="Ross")
        _seed_conversation(
            dynamodb_tables,
            "conv-1",
            participant_a="agent-a",
            participant_b="agent-b",
            # Caller redundantly listed agent-a twice in the list.
            participants=["agent-a", "agent-b", "agent-a"],
        )

        resp = client.get("/api/v1/town/conversations")
        assert resp.status_code == 200
        conv = resp.json()["conversations"][0]
        assert conv["participants"] == ["Rachel", "Ross"]


# ---------------------------------------------------------------------------
# POST /instance
# ---------------------------------------------------------------------------


class TestInstanceEndpoint:
    def test_creates_instance_for_new_user_and_returns_token(self, client, dynamodb_tables):
        resp = client.post("/api/v1/town/instance")
        assert resp.status_code == 200
        body = resp.json()
        assert body["town_token"]  # non-empty signed token
        assert "." in body["town_token"]  # HMAC-signed format "<payload>.<sig>"
        assert body["apartment_unit"] >= 1
        # New user has no agents yet
        assert body["agents"] == []

    def test_idempotent_for_same_user(self, client, dynamodb_tables):
        """Calling twice returns the same underlying instance — no duplicate
        apartment allocation."""
        first = client.post("/api/v1/town/instance").json()
        second = client.post("/api/v1/town/instance").json()
        # Same apartment unit means same instance was reused.
        assert first["apartment_unit"] == second["apartment_unit"]
        assert first["town_token"] == second["town_token"]

    def test_resigns_token_when_stored_uid_does_not_match(self, client, dynamodb_tables):
        """Regression guard: when an instance row's userId changes (Clerk
        account swap, test-data migration, manual edit), the UI must re-sign
        the stored token on the next /instance call so the daemon can connect."""
        import base64
        import json

        from core import dynamo
        from core.town_token import sign_town_token

        # Create the instance normally → token signed for the current test user.
        first = client.post("/api/v1/town/instance").json()
        instance_id = None  # we'll get it via scan
        table = dynamo.instances_table()
        items = table.scan().get("Items", [])
        for item in items:
            if item.get("townToken") == first["town_token"]:
                instance_id = item["id"]
                break
        assert instance_id is not None

        # Simulate the drift: overwrite the stored token with one signed for a
        # DIFFERENT uid while leaving the row's userId pointing at this user.
        stale = sign_town_token("user_previous", instance_id)
        table.update_item(
            Key={"id": instance_id},
            UpdateExpression="SET townToken = :t",
            ExpressionAttributeValues={":t": stale},
        )

        # Second call must re-sign with the current user's uid.
        second = client.post("/api/v1/town/instance").json()
        assert second["town_token"] != stale
        payload_b64 = second["town_token"].split(".", 1)[0]
        pad = "=" * (-len(payload_b64) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload_b64 + pad))
        # Should now claim the *current* auth user, not the stale one.
        assert decoded["uid"] != "user_previous"
        assert decoded["iid"] == instance_id


class TestWsMessageValidation:
    """Boundary-validation regression tests for /ws-message under
    WS_STRICT_VALIDATION=True (the default).

    Auth setup: the shared-secret check at town.py is skipped when
    ``settings.ENVIRONMENT == "dev"`` AND ``settings.WS_LAMBDA_SECRET`` is
    falsy. The conftest sets ENVIRONMENT=dev; we just need to make sure the
    secret is empty. If the conftest sets a value, pass it as a header.
    """

    _HEADERS = {"x-ws-lambda-secret": ""}

    def test_rejects_unknown_type(self, client) -> None:
        resp = client.post(
            "/api/v1/town/ws-message",
            json={
                "connectionId": "c1",
                "principalId": "p1",
                "body": {"type": "town_eat_sandwich"},
            },
            headers=self._HEADERS,
        )
        assert resp.status_code == 422

    def test_rejects_agent_act_with_unknown_action(self, client) -> None:
        resp = client.post(
            "/api/v1/town/ws-message",
            json={
                "connectionId": "c1",
                "principalId": "p1",
                "body": {"type": "town_agent_act", "action": "eat_the_pigeons"},
            },
            headers=self._HEADERS,
        )
        assert resp.status_code == 422

    def test_rejects_say_with_missing_text(self, client) -> None:
        # say with no conv_id and no message — missing 'message' triggers rejection.
        resp = client.post(
            "/api/v1/town/ws-message",
            json={
                "connectionId": "c1",
                "principalId": "p1",
                "body": {"type": "town_agent_act", "action": "say"},
            },
            headers=self._HEADERS,
        )
        assert resp.status_code == 422


class TestSkillEndpoints:
    """Regression guard for `/skill` and `/skill/bundle`. The bundle endpoint
    silently shipped an empty 45-byte tarball for weeks because the old
    implementation packed nonexistent ``tools/`` and ``daemon/`` subdirs."""

    def test_skill_md_returns_real_content(self, client) -> None:
        # Reset cache so this test sees current file state, not a stale read.
        import routers.town as town

        town._skill_md_cache = None
        resp = client.get("/api/v1/town/skill")
        assert resp.status_code == 200
        body = resp.text
        assert "GooseTown" in body
        assert "Setup" in body  # core onboarding section must exist
        assert len(body) > 1_000  # not a stub

    def test_skill_bundle_is_non_empty_and_contains_cli(self, client) -> None:
        import io
        import tarfile

        import routers.town as town

        town._skill_bundle_cache = None
        resp = client.get("/api/v1/town/skill/bundle")
        assert resp.status_code == 200
        # Must be a real gzipped tarball, not the 45-byte empty one.
        assert len(resp.content) > 5_000
        with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
            names = {m.name for m in tar.getmembers()}
        # The CLI must be in the bundle — without it the agent can't register.
        assert "goosetown/goosetown_cli.py" in names
        assert "goosetown/SKILL.md" in names
        # And the install script + CLAUDE_FRAGMENT (skill self-installs into
        # the agent's CLAUDE.md so they don't have to re-read SKILL.md).
        assert "goosetown/install.sh" in names
        assert "goosetown/CLAUDE_FRAGMENT.md" in names
        # And the .md templates the CLI writes into the workspace.
        assert "goosetown/GOOSETOWN.md" in names
        assert "goosetown/TOWN_STATUS.md" in names
        # And the wake-on-status hook that install.sh wires into
        # settings.local.json (Stop + TeammateIdle events). Without this an
        # agent would idle silently when the town has a new chat/event.
        assert "goosetown/hooks/check-town-status.sh" in names
