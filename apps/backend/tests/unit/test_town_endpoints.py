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

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

TEST_USER_ID = "user_test_123"
OTHER_USER_ID = "user_other_456"


def _iso_ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


# /conversations hides rows older than 48 h, so seeded rows default to "just now".
RECENT_START = _iso_ago(minutes=10)
RECENT_END = _iso_ago(minutes=5)


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
    participant_a: str | None,
    participant_b: str | None,
    location: str = "cafe",
    status: str = "ended",
    public_log: list | None = None,
    started_at: str = RECENT_START,
    ended_at: str | None = RECENT_END,
    updated_at: str = RECENT_END,
    participants: list | None = None,
):
    """Seed a conversation row.

    Pass ``participant_a=None`` / ``participant_b=None`` to omit the
    legacy GSI-key fields entirely (DynamoDB stores items without GSI
    keys; they just don't appear in GSI queries). Used by the
    no-participants regression test to exercise the legacy fallback path
    in ``GET /town/conversations`` that emits ``""`` instead of
    ``"Unknown"`` for empty A/B slots.
    """
    table = dynamodb_tables.Table("goosetown-conversations")
    item = {
        "id": conv_id,
        "location": location,
        "status": status,
        "startedAt": started_at,
        "updatedAt": updated_at,
        "turnCount": len(public_log or []),
        "publicLog": public_log or [],
    }
    # Active conversations have no ``endedAt``; pass ``ended_at=None`` to omit it
    # (DynamoDB rejects a literal null attribute value).
    if ended_at is not None:
        item["endedAt"] = ended_at
    if participant_a is not None:
        item["participantAId"] = participant_a
    if participant_b is not None:
        item["participantBId"] = participant_b
    if participants is not None:
        item["participants"] = participants
    table.put_item(Item=item)


def _seed_instance(
    dynamodb_tables,
    instance_id: str = "inst-1",
    user_id: str = TEST_USER_ID,
    nickname: str | None = None,
    is_active: str = "true",
):
    """Insert a minimal active instance row, optionally with a townNickname."""
    table = dynamodb_tables.Table("goosetown-instances")
    item = {
        "id": instance_id,
        "userId": user_id,
        "apartmentUnit": 1,
        "townToken": f"tok-{instance_id}",
        "isActive": is_active,
        "createdAt": "2026-04-17T00:00:00Z",
    }
    if nickname is not None:
        item["townNickname"] = nickname
    table.put_item(Item=item)
    return item


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

    def test_current_goal_included_when_set(self, client, dynamodb_tables):
        """Task 10 — currentGoal must be surfaced in the per-agent response
        so the ApartmentCard can display it."""
        table = dynamodb_tables.Table("goosetown-agents")
        _seed_agent(dynamodb_tables, "a-1", agent_name="aspirant")
        # Patch the seeded item to add currentGoal (not a param of _seed_agent
        # to keep the helper minimal).
        table.update_item(
            Key={"id": "a-1"},
            UpdateExpression="SET currentGoal = :g",
            ExpressionAttributeValues={":g": "make three new friends"},
        )

        resp = client.get("/api/v1/town/apartment")
        assert resp.status_code == 200
        agent = resp.json()["agents"][0]
        assert agent["currentGoal"] == "make three new friends"

    def test_current_goal_is_null_when_unset(self, client, dynamodb_tables):
        """currentGoal must be null (not absent) when the field is not stored,
        so the frontend type guard ``agent.currentGoal && …`` can rely on it."""
        _seed_agent(dynamodb_tables, "a-1", agent_name="drifter")

        resp = client.get("/api/v1/town/apartment")
        assert resp.status_code == 200
        agent = resp.json()["agents"][0]
        assert agent["currentGoal"] is None


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
                started_at=_iso_ago(minutes=20 - i),
                ended_at=_iso_ago(minutes=19 - i),
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

    def test_unresolvable_a_and_b_ids_resolve_to_unknown_per_participant(self, client, dynamodb_tables):
        """Per-participant resolve path: when both A/B ids are present in
        the row but neither agent record exists, ``_resolve`` legitimately
        emits ``"Unknown"`` for each — useful information ("conversation
        with someone whose record we lost") rather than hiding the
        conversation. Distinct from the empty-A/B-slot case below.
        """
        _seed_conversation(
            dynamodb_tables,
            "conv-1",
            participant_a="missing-1",
            participant_b="missing-2",
        )

        resp = client.get("/api/v1/town/conversations")
        assert resp.status_code == 200
        conv = resp.json()["conversations"][0]
        assert conv["participants"] == ["Unknown", "Unknown"]
        assert conv["participant_a"] == "Unknown"
        assert conv["participant_b"] == "Unknown"

    def test_legacy_row_with_no_participant_ids_emits_empty_strings(self, client, dynamodb_tables):
        """Legacy fallback path: when the row has neither ``participants``
        nor ``participantAId/Bid`` at all, the response's legacy fields
        must be empty strings (NOT ``"Unknown"``). Rationale: the
        frontend ``[participant_a, participant_b].filter(Boolean)``
        legacy-fallback path would treat ``"Unknown"`` as a real
        participant and render ``"Unknown & Unknown"`` in the
        conversation row. Empty strings let the filter catch the
        no-participants case as intended.

        We bypass the GSI-key requirement on ``participantAId`` /
        ``participantBId`` by omitting them from the row entirely
        (DynamoDB stores GSI-key-absent items; they just don't appear
        in queries on that GSI). This is the exact shape of a row that
        could exist if a future migration / data-restoration path left
        the legacy keys unset.
        """
        _seed_conversation(
            dynamodb_tables,
            "conv-1",
            participant_a=None,  # GSI key omitted entirely
            participant_b=None,
        )

        resp = client.get("/api/v1/town/conversations")
        assert resp.status_code == 200
        conv = resp.json()["conversations"][0]
        # No participants resolvable anywhere — canonical array is empty.
        assert conv["participants"] == []
        # Legacy fields fall back to "" (the regression: they used to be
        # "Unknown", which the frontend filter(Boolean) didn't catch).
        assert conv["participant_a"] == ""
        assert conv["participant_b"] == ""

    def test_returns_full_participants_list_for_group_chat(self, client, dynamodb_tables):
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

    def test_dedupes_participants_resolves_each_id_once(self, client, dynamodb_tables):
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

    def test_hides_conversations_older_than_48h_or_with_bad_timestamps(self, client, dynamodb_tables):
        """Public feed shows only conversations with activity (updatedAt) in the
        last 48 h. Stale, future and unparseable rows stay in the table but are
        not served."""
        from core.services.town_service import TownService

        _seed_agent(dynamodb_tables, "agent-a", agent_name="a", display_name="Agent A")
        _seed_agent(dynamodb_tables, "agent-b", agent_name="b", display_name="Agent B")
        # A live chat created through the real code path (endedAt="_active").
        live = TownService().create_active_conversation("agent-a", "agent-b", "cafe", "hi", "Agent A")

        old = _iso_ago(days=108)
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        rows = {
            "recent-ended": {},
            # Started 3 days ago but someone spoke a minute ago: still live.
            "long-active": {
                "status": "active",
                "started_at": _iso_ago(days=3),
                "ended_at": "_active",
                "updated_at": _iso_ago(minutes=1),
            },
            "stuck-active": {"status": "active", "started_at": old, "ended_at": "_active", "updated_at": old},
            "old-ended": {"started_at": old, "ended_at": old, "updated_at": old},
            "future": {"started_at": future, "ended_at": future, "updated_at": future},
            "invalid": {"started_at": "garbage", "ended_at": "garbage", "updated_at": "garbage"},
        }
        for conv_id, kw in rows.items():
            _seed_conversation(dynamodb_tables, conv_id, participant_a="a", participant_b="b", **kw)

        resp = client.get("/api/v1/town/conversations")
        assert resp.status_code == 200
        by_id = {c["id"]: c for c in resp.json()["conversations"]}
        assert sorted(by_id) == sorted([live["id"], "long-active", "recent-ended"])
        # The "_active" sentinel is served as null so clients show it as live.
        assert by_id[live["id"]]["ended_at"] is None
        assert by_id["recent-ended"]["ended_at"] is not None


def test_is_fresh_rejects_missing_and_non_string_timestamps():
    from routers.town import _is_fresh

    assert _is_fresh(_iso_ago(hours=1))
    assert _is_fresh(_iso_ago(hours=1).replace("+00:00", "Z"))
    # Up to 60 s of writer clock skew is tolerated; beyond that is "future".
    assert _is_fresh((datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat())
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    for bad in (None, "", 123, "2026-13-45", "_active", _iso_ago(hours=49), future):
        assert not _is_fresh(bad)


# ---------------------------------------------------------------------------
# POST /conversations/{conv_id}/say  (human visitor → resident conversation)
# ---------------------------------------------------------------------------


class TestConversationSayEndpoint:
    """The logged-in-user-into-a-conversation endpoint.

    ``broadcast_human_message`` fans the turn out via ``get_daemon_mailbox``
    (locally imported in the function body → patch at the source module) and
    sends a cosmetic speech bubble via ``get_game_server_client`` (module-top
    of ``conversation_service``). Both are mocked so the test exercises the
    validation + DDB append + HTTP mapping without real WebSocket/game-server
    transports.
    """

    SAY_PATH = "/api/v1/town/conversations/{conv_id}/say"

    @pytest.fixture(autouse=True)
    def _reset_rate_limit(self):
        """Each test starts with an empty per-(user, conv) throttle table."""
        from core.services import conversation_service as cs

        cs._human_say_last_ts.clear()
        yield
        cs._human_say_last_ts.clear()

    def _patches(self):
        from contextlib import ExitStack
        from unittest.mock import AsyncMock, MagicMock, patch

        stack = ExitStack()
        mailbox = MagicMock()
        sent: list = []
        mailbox.notify.side_effect = lambda aid, event: sent.append((aid, event))
        gs = MagicMock()
        gs.send_command = AsyncMock()
        stack.enter_context(
            patch(
                "core.services.daemon_mailbox.get_daemon_mailbox",
                return_value=mailbox,
            )
        )
        stack.enter_context(
            patch(
                "core.services.conversation_service.get_game_server_client",
                return_value=gs,
            )
        )
        return stack, sent

    def _seed_active(self, dynamodb_tables, conv_id="conv-1"):
        _seed_agent(dynamodb_tables, "agent-a", agent_name="a", display_name="Agent A")
        _seed_agent(dynamodb_tables, "agent-b", agent_name="b", display_name="Agent B")
        _seed_conversation(
            dynamodb_tables,
            conv_id,
            participant_a="agent-a",
            participant_b="agent-b",
            participants=["agent-a", "agent-b"],
            status="active",
            ended_at=None,
            public_log=[{"speaker": "Agent A", "text": "hi"}],
        )

    def test_happy_path_appends_and_broadcasts(self, client, dynamodb_tables):
        self._seed_active(dynamodb_tables)
        stack, sent = self._patches()
        with stack:
            resp = client.post(
                self.SAY_PATH.format(conv_id="conv-1"),
                json={"message": "Hello residents!"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["conv_id"] == "conv-1"
        # publicLog had 1 turn; the human turn is index 1.
        assert body["turn"] == 1

        # The turn was persisted, tagged human, and did NOT bump turnCount.
        from core.services.town_service import TownService

        conv = TownService().get_conversation_by_id("conv-1")
        assert conv["publicLog"][-1]["text"] == "Hello residents!"
        assert conv["publicLog"][-1]["speakerType"] == "human"
        assert conv["humanTurnCount"] == 1
        assert conv["turnCount"] == 1  # unchanged from the seed

        # Broadcast went to BOTH residents with a human-distinguishable event
        # that still routes on text/from.
        forwarded = [(aid, ev) for aid, ev in sent if ev["event"] == "conversation_message"]
        assert {aid for aid, _ in forwarded} == {"agent-a", "agent-b"}
        for _, ev in forwarded:
            assert ev["from_human"] is True
            assert ev["speakerType"] == "human"
            assert ev["text"] == "Hello residents!"
            # No agent name → daemon own-message filter won't drop it.
            assert "from_agent_name" not in ev

    def test_speaker_derived_from_auth_not_body(self, client, dynamodb_tables):
        """A ``speaker`` field in the body is ignored — the label comes from
        the authenticated user, so a visitor can't impersonate a resident."""
        self._seed_active(dynamodb_tables)
        stack, sent = self._patches()
        with stack:
            resp = client.post(
                self.SAY_PATH.format(conv_id="conv-1"),
                json={"message": "I am totally Agent A", "speaker": "Agent A"},
            )

        assert resp.status_code == 200
        from core.services.town_service import TownService

        conv = TownService().get_conversation_by_id("conv-1")
        speaker = conv["publicLog"][-1]["speaker"]
        assert speaker != "Agent A"
        assert speaker.startswith("Visitor")

    def test_speaker_uses_stored_nickname_when_set(self, client, dynamodb_tables):
        """When the user has set a town nickname, the public-log speaker label
        is that nickname — NOT the ``Visitor-<suffix>`` fallback — and it's
        still derived from the authenticated identity, not the body."""
        self._seed_active(dynamodb_tables)
        _seed_instance(dynamodb_tables, user_id=TEST_USER_ID, nickname="Cosmo")
        stack, _ = self._patches()
        with stack:
            resp = client.post(
                self.SAY_PATH.format(conv_id="conv-1"),
                json={"message": "Hi, I'm a visitor"},
            )

        assert resp.status_code == 200
        from core.services.town_service import TownService

        conv = TownService().get_conversation_by_id("conv-1")
        speaker = conv["publicLog"][-1]["speaker"]
        assert speaker == "Cosmo"
        assert not speaker.startswith("Visitor")

    def test_human_turn_exempt_from_agent_cap(self, client, dynamodb_tables):
        """Many human turns must not advance ``turnCount`` toward the agent
        auto-end cap. After 5 human turns, turnCount is still the seed value."""
        self._seed_active(dynamodb_tables)
        stack, _ = self._patches()
        with stack:
            from core.services import conversation_service as cs

            for i in range(5):
                # Clear the throttle between sends — we're testing the cap, not
                # the rate limit.
                cs._human_say_last_ts.clear()
                resp = client.post(
                    self.SAY_PATH.format(conv_id="conv-1"),
                    json={"message": f"line {i}"},
                )
                assert resp.status_code == 200

        from core.services.town_service import TownService

        conv = TownService().get_conversation_by_id("conv-1")
        assert conv["turnCount"] == 1  # never moved
        assert conv["humanTurnCount"] == 5

    def test_inactive_conversation_rejected(self, client, dynamodb_tables):
        _seed_agent(dynamodb_tables, "agent-a", agent_name="a", display_name="Agent A")
        _seed_conversation(
            dynamodb_tables,
            "conv-ended",
            participant_a="agent-a",
            participant_b="agent-b",
            participants=["agent-a", "agent-b"],
            status="ended",
        )
        stack, _ = self._patches()
        with stack:
            resp = client.post(
                self.SAY_PATH.format(conv_id="conv-ended"),
                json={"message": "anyone there?"},
            )
        assert resp.status_code == 409

    def test_missing_conversation_404(self, client, dynamodb_tables):
        stack, _ = self._patches()
        with stack:
            resp = client.post(
                self.SAY_PATH.format(conv_id="no-such-conv"),
                json={"message": "hello?"},
            )
        assert resp.status_code == 404

    def test_empty_message_422(self, client, dynamodb_tables):
        self._seed_active(dynamodb_tables)
        # Pydantic min_length=1 rejects a blank string at the boundary.
        resp = client.post(
            self.SAY_PATH.format(conv_id="conv-1"),
            json={"message": "   "},
        )
        # Whitespace passes pydantic min_length but the service trims to empty.
        # Either way the user gets a 422 (pydantic) — assert non-2xx, 422.
        assert resp.status_code == 422

    def test_blank_only_message_422(self, client, dynamodb_tables):
        self._seed_active(dynamodb_tables)
        stack, _ = self._patches()
        with stack:
            # A single space satisfies min_length=1 but trims to empty in the
            # service guard → 422 from the endpoint's error mapping.
            resp = client.post(
                self.SAY_PATH.format(conv_id="conv-1"),
                json={"message": " "},
            )
        assert resp.status_code == 422

    def test_too_long_message_422(self, client, dynamodb_tables):
        self._seed_active(dynamodb_tables)
        resp = client.post(
            self.SAY_PATH.format(conv_id="conv-1"),
            json={"message": "x" * 281},
        )
        assert resp.status_code == 422

    def test_rate_limited_second_message_429(self, client, dynamodb_tables):
        self._seed_active(dynamodb_tables)
        stack, _ = self._patches()
        with stack:
            first = client.post(
                self.SAY_PATH.format(conv_id="conv-1"),
                json={"message": "first"},
            )
            second = client.post(
                self.SAY_PATH.format(conv_id="conv-1"),
                json={"message": "second, too fast"},
            )
        assert first.status_code == 200
        assert second.status_code == 429

    def test_requires_auth(self, dynamodb_tables):
        """No auth override → the Clerk dependency rejects the request."""
        from main import app

        self._seed_active(dynamodb_tables)
        client = TestClient(app)
        resp = client.post(
            self.SAY_PATH.format(conv_id="conv-1"),
            json={"message": "anonymous"},
        )
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# GET /profile + PUT /profile/nickname  (town nickname)
# ---------------------------------------------------------------------------


class TestProfileEndpoints:
    """The user-set public-safe town nickname surface."""

    GET_PATH = "/api/v1/town/profile"
    PUT_PATH = "/api/v1/town/profile/nickname"

    def test_get_profile_null_when_no_instance(self, client, dynamodb_tables):
        """A user who hasn't joined (no instance) has no nickname → null."""
        resp = client.get(self.GET_PATH)
        assert resp.status_code == 200
        assert resp.json() == {"nickname": None}

    def test_get_profile_null_when_instance_has_no_nickname(self, client, dynamodb_tables):
        _seed_instance(dynamodb_tables, user_id=TEST_USER_ID, nickname=None)
        resp = client.get(self.GET_PATH)
        assert resp.status_code == 200
        assert resp.json() == {"nickname": None}

    def test_get_profile_returns_stored_nickname(self, client, dynamodb_tables):
        _seed_instance(dynamodb_tables, user_id=TEST_USER_ID, nickname="Cosmo")
        resp = client.get(self.GET_PATH)
        assert resp.status_code == 200
        assert resp.json() == {"nickname": "Cosmo"}

    def test_put_nickname_happy_path_persists_on_instance(self, client, dynamodb_tables):
        _seed_instance(dynamodb_tables, user_id=TEST_USER_ID, nickname=None)
        resp = client.put(self.PUT_PATH, json={"nickname": "Star Gazer"})
        assert resp.status_code == 200
        assert resp.json() == {"nickname": "Star Gazer"}

        # Stored on the user's instance row and readable back via GET.
        assert client.get(self.GET_PATH).json() == {"nickname": "Star Gazer"}

    def test_put_nickname_creates_instance_if_none_exists(self, client, dynamodb_tables):
        """A signed-in user can pick a handle before joining — the write
        lazily creates the instance row."""
        resp = client.put(self.PUT_PATH, json={"nickname": "Newcomer"})
        assert resp.status_code == 200
        assert resp.json() == {"nickname": "Newcomer"}
        assert client.get(self.GET_PATH).json() == {"nickname": "Newcomer"}

    @pytest.mark.parametrize(
        "bad",
        [
            "a",  # too short (< 2)
            "x" * 21,  # too long (> 20)
            " leadingspace",  # leading whitespace
            "trailingspace ",  # trailing whitespace
            "with.dot",  # disallowed char
            "user@email.com",  # email-like
            "emoji😀here",  # non-ascii
            "bang!",  # punctuation
        ],
    )
    def test_put_nickname_invalid_returns_422(self, client, dynamodb_tables, bad):
        resp = client.put(self.PUT_PATH, json={"nickname": bad})
        assert resp.status_code == 422

    def test_put_nickname_allows_letters_digits_space_underscore_dash(self, client, dynamodb_tables):
        resp = client.put(self.PUT_PATH, json={"nickname": "Cool_Cat-99 x"})
        assert resp.status_code == 200
        assert resp.json() == {"nickname": "Cool_Cat-99 x"}

    def test_get_profile_requires_auth(self, dynamodb_tables):
        from main import app

        client = TestClient(app)
        resp = client.get(self.GET_PATH)
        assert resp.status_code in (401, 403)

    def test_put_nickname_requires_auth(self, dynamodb_tables):
        from main import app

        client = TestClient(app)
        resp = client.put(self.PUT_PATH, json={"nickname": "Anon"})
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# POST /instance
# ---------------------------------------------------------------------------


class TestInstanceEndpoint:
    @pytest.mark.parametrize("failure", ["lookup", "create"])
    def test_instance_storage_failure_is_retryable(self, client, dynamodb_tables, monkeypatch, failure):
        from core.services.town_service import TownService

        def broken(*_args):
            raise RuntimeError("synthetic outage")

        if failure == "lookup":
            monkeypatch.setattr(TownService, "get_active_instance", broken)
        else:
            monkeypatch.setattr(TownService, "create_instance", broken)
        response = client.post("/api/v1/town/instance")
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "1"
        assert "town_token" not in response.json()

    def _instance(self, client, dynamodb_tables):
        from core.town_token import verify_town_token

        response = client.post("/api/v1/town/instance")
        assert response.status_code == 200
        token = response.json()["town_token"]
        instance_id = verify_town_token(token)["iid"]
        return dynamodb_tables.Table("goosetown-instances"), instance_id, token

    @pytest.mark.parametrize("kind", ["expired", "malformed", "wrong_instance", "wrong_owner"])
    def test_invalid_cached_token_is_replaced(self, client, dynamodb_tables, kind):
        import time

        from core.town_token import sign_town_token, verify_town_token

        table, instance_id, _ = self._instance(client, dynamodb_tables)
        now = int(time.time())
        invalid = {
            "expired": sign_town_token(TEST_USER_ID, instance_id, issued_at=now - 100, ttl_seconds=1),
            "malformed": "not.a.valid.token",
            "wrong_instance": sign_town_token(TEST_USER_ID, "different-instance"),
            "wrong_owner": sign_town_token(OTHER_USER_ID, instance_id),
        }[kind]
        table.update_item(
            Key={"id": instance_id}, UpdateExpression="SET townToken = :v", ExpressionAttributeValues={":v": invalid}
        )
        response = client.post("/api/v1/town/instance")
        assert response.status_code == 200, response.text
        fresh = response.json()["town_token"]
        assert fresh != invalid
        assert verify_town_token(fresh)["iid"] == instance_id
        assert table.get_item(Key={"id": instance_id}, ConsistentRead=True)["Item"]["townToken"] == fresh

    def test_same_second_revocation_retries_then_refreshes(self, client, dynamodb_tables, monkeypatch):
        import asyncio
        import time

        from core.town_token import verify_town_token

        table, instance_id, original = self._instance(client, dynamodb_tables)
        now = int(time.time())
        users = dynamodb_tables.Table("goosetown-users")
        users.put_item(Item={"userId": TEST_USER_ID, "tokenRevokedAt": now * 1000 + 500})
        clock = [now + 0.7]
        monkeypatch.setattr(time, "time", lambda: clock[0])

        async def advance(delay):
            assert 0 < delay <= 1.05
            clock[0] = now + 1.1

        monkeypatch.setattr(asyncio, "sleep", advance)
        response = client.post("/api/v1/town/instance")
        assert response.status_code == 200, response.text
        fresh = response.json()["town_token"]
        assert fresh != original
        assert table.get_item(Key={"id": instance_id})["Item"]["townToken"] == fresh
        assert verify_town_token(fresh)["iat"] == now + 1

    def test_clock_not_advanced_returns_retry(self, client, dynamodb_tables, monkeypatch):
        import asyncio
        import time

        table, instance_id, original = self._instance(client, dynamodb_tables)
        now = int(time.time())
        dynamodb_tables.Table("goosetown-users").put_item(
            Item={"userId": TEST_USER_ID, "tokenRevokedAt": now * 1000 + 500}
        )
        monkeypatch.setattr(time, "time", lambda: now + 0.7)

        async def no_advance(_delay):
            return None

        monkeypatch.setattr(asyncio, "sleep", no_advance)
        response = client.post("/api/v1/town/instance")
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "1"
        assert table.get_item(Key={"id": instance_id})["Item"]["townToken"] == original

    def test_revocation_store_unavailable_fails_closed(self, client, dynamodb_tables, monkeypatch):
        from core import dynamo

        self._instance(client, dynamodb_tables)

        class BrokenUsers:
            def get_item(self, **_kwargs):
                raise RuntimeError("synthetic outage")

        monkeypatch.setattr(dynamo, "users_table", lambda: BrokenUsers())
        response = client.post("/api/v1/town/instance")
        assert response.status_code == 503
        assert "town_token" not in response.json()

    def test_previously_revoked_token_is_refreshed(self, client, dynamodb_tables):
        import time

        from core.town_token import sign_town_token, verify_town_token

        table, instance_id, _ = self._instance(client, dynamodb_tables)
        now = int(time.time())
        old = sign_town_token(TEST_USER_ID, instance_id, issued_at=now - 30)
        table.update_item(
            Key={"id": instance_id}, UpdateExpression="SET townToken = :v", ExpressionAttributeValues={":v": old}
        )
        dynamodb_tables.Table("goosetown-users").put_item(
            Item={"userId": TEST_USER_ID, "tokenRevokedAt": (now - 1) * 1000}
        )
        response = client.post("/api/v1/town/instance")
        assert response.status_code == 200, response.text
        assert verify_town_token(response.json()["town_token"])["iat"] >= now

    def test_revoke_during_refresh_is_not_returned(self, client, dynamodb_tables, monkeypatch):
        import time

        from core import town_token

        table, instance_id, _ = self._instance(client, dynamodb_tables)
        table.update_item(
            Key={"id": instance_id}, UpdateExpression="SET townToken = :v", ExpressionAttributeValues={":v": "invalid"}
        )
        sign = town_token.sign_town_token

        def race(*args, **kwargs):
            dynamodb_tables.Table("goosetown-users").put_item(
                Item={"userId": TEST_USER_ID, "tokenRevokedAt": int(time.time() * 1000)}
            )
            return sign(*args, **kwargs)

        monkeypatch.setattr(town_token, "sign_town_token", race)
        response = client.post("/api/v1/town/instance")
        assert response.status_code == 503
        assert table.get_item(Key={"id": instance_id})["Item"]["townToken"] == "invalid"

    def test_stale_index_cannot_return_another_owners_instance(self, client, dynamodb_tables, monkeypatch):
        from core.services.town_service import TownService

        table, instance_id, _ = self._instance(client, dynamodb_tables)
        table.update_item(
            Key={"id": instance_id}, UpdateExpression="SET userId = :v", ExpressionAttributeValues={":v": OTHER_USER_ID}
        )
        monkeypatch.setattr(TownService, "get_active_instance", lambda *_: {"id": instance_id})
        response = client.post("/api/v1/town/instance")
        assert response.status_code == 409

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
        assert "Install" in body  # core onboarding section must exist
        assert len(body) > 1_000  # not a stub

    def test_skill_bundle_is_non_empty_and_contains_docs(self, client) -> None:
        import io
        import tarfile

        import routers.town as town

        town._skill_bundle_cache = None
        resp = client.get("/api/v1/town/skill/bundle")
        assert resp.status_code == 200
        # Must be a real gzipped tarball, not the 45-byte empty one.
        assert len(resp.content) > 1_000
        with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
            names = {m.name for m in tar.getmembers()}
        # The bundle now ships docs + templates + the wake hook only. The
        # daemon CLI was removed — the canonical daemon is the PyPI package
        # (`pip install goosetown`). SKILL.md tells users to install that way.
        assert "goosetown/SKILL.md" in names
        assert "goosetown/references/character.md" in names
        # Every reference SKILL.md links to must ship in the docs-only
        # tarball, or the legacy install path lands users on dead links.
        assert "goosetown/references/actions.md" in names
        assert "goosetown/references/owner-dms.md" in names
        assert "goosetown/references/troubleshooting.md" in names
        assert "goosetown/references/openclaw.md" in names
        assert "goosetown/references/exchange.md" in names
        # The .md templates the daemon writes into the workspace.
        assert "goosetown/GOOSETOWN.md" in names
        assert "goosetown/TOWN_STATUS.md" in names
        # The wake-on-status hook wired into settings.local.json
        # (Stop + TeammateIdle events). Without this an agent would idle
        # silently when the town has a new chat/event.
        assert "goosetown/hooks/check-town-status.sh" in names
        # The drifted bundle daemon was removed; the PyPI package is the
        # single source of truth. Guard against re-bundling it.
        assert "goosetown/goosetown_cli.py" not in names
        # install.sh and CLAUDE_FRAGMENT.md were removed in Task 6;
        # self-bootstrap (Task 4) and opt-in setup-hooks (Task 5) replace them.
        assert "goosetown/install.sh" not in names
        assert "goosetown/CLAUDE_FRAGMENT.md" not in names

    def test_skill_bundle_hook_script_is_executable(self, client) -> None:
        """The bundled `hooks/check-town-status.sh` MUST land with mode 0755.

        Regression guard for the bug Jim surfaced during the agent-team
        test on 2026-05-02 (T12 OpenClaw smoke). The bundle was packing
        the hook with mode 0644 because Docker layer caching strips +x
        off `*.sh` files in some image-build configurations. A non-
        executable hook silently no-ops on every Stop / TeammateIdle —
        wake-on-status quietly stops working for any user installing via
        the legacy tarball path.
        """
        import io
        import tarfile

        import routers.town as town

        town._skill_bundle_cache = None
        resp = client.get("/api/v1/town/skill/bundle")
        assert resp.status_code == 200
        with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
            hook = tar.getmember("goosetown/hooks/check-town-status.sh")
        # Owner +x at minimum. The build sets full 0755 explicitly.
        assert hook.mode & 0o100, f"hook script bundled without owner +x bit (mode={oct(hook.mode)})"

    def test_skill_bundle_emits_deprecation_header(self, client):
        """Sub 1: legacy bundle endpoint signals deprecation pointing at canonical install."""
        resp = client.get("/api/v1/town/skill/bundle")
        assert resp.status_code == 200
        headers_lower = {k.lower(): v for k, v in resp.headers.items()}
        assert headers_lower.get("deprecation") == "true"
        link = headers_lower.get("link")
        assert link is not None
        assert 'rel="deprecation"' in link
        assert "github.com/Isol8AI/goosetown" in link
