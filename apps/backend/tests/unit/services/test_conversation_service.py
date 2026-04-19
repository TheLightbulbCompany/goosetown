"""Tests for the conversation methods on TownService and TownAgentWsManager."""

from core.services.town_agent_ws import TownAgentWsManager
from core.services.town_service import TownService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent(svc: TownService, user_id: str, agent_name: str, display_name: str) -> dict:
    """Register and return a test agent."""
    return svc.opt_in(user_id, agent_name, display_name, personality_summary="test agent")


# ===========================================================================
# TownService — create_active_conversation
# ===========================================================================


class TestCreateActiveConversation:
    def test_creates_conversation_record(self, dynamodb_tables):
        svc = TownService()
        a = _make_agent(svc, "u1", "agent-a", "Agent A")
        b = _make_agent(svc, "u2", "agent-b", "Agent B")

        conv = svc.create_active_conversation(
            initiator_id=a["id"],
            target_id=b["id"],
            location="plaza",
            opening_message="Hello there!",
            initiator_display_name="Agent A",
        )

        assert conv["status"] == "active"
        assert conv["location"] == "plaza"
        assert conv["turnCount"] == 1
        assert len(conv["publicLog"]) == 1
        assert conv["publicLog"][0]["speaker"] == "Agent A"
        assert conv["publicLog"][0]["text"] == "Hello there!"

    def test_participant_ids_are_canonically_sorted(self, dynamodb_tables):
        """participantAId < participantBId lexicographically."""
        svc = TownService()
        a = _make_agent(svc, "u1", "agent-a", "Agent A")
        b = _make_agent(svc, "u2", "agent-b", "Agent B")

        conv = svc.create_active_conversation(
            initiator_id=b["id"],
            target_id=a["id"],
            location="cafe",
            opening_message="Hey!",
            initiator_display_name="Agent B",
        )

        # Sorted order: participantAId <= participantBId
        assert conv["participantAId"] <= conv["participantBId"]

    def test_sets_current_conversation_id_on_both_agents(self, dynamodb_tables):
        svc = TownService()
        a = _make_agent(svc, "u1", "agent-a", "Agent A")
        b = _make_agent(svc, "u2", "agent-b", "Agent B")

        conv = svc.create_active_conversation(
            initiator_id=a["id"],
            target_id=b["id"],
            location="library",
            opening_message="Mind if I join?",
            initiator_display_name="Agent A",
        )
        conv_id = conv["id"]

        a_fresh = svc.get_town_agent_by_id(a["id"])
        b_fresh = svc.get_town_agent_by_id(b["id"])
        assert a_fresh["currentConversationId"] == conv_id
        assert b_fresh["currentConversationId"] == conv_id


# ===========================================================================
# TownService — get_conversation_by_id
# ===========================================================================


class TestGetConversationById:
    def test_returns_conversation(self, dynamodb_tables):
        svc = TownService()
        a = _make_agent(svc, "u1", "agent-a", "Agent A")
        b = _make_agent(svc, "u2", "agent-b", "Agent B")

        conv = svc.create_active_conversation(a["id"], b["id"], "plaza", "Hi", "Agent A")
        fetched = svc.get_conversation_by_id(conv["id"])

        assert fetched is not None
        assert fetched["id"] == conv["id"]
        assert fetched["status"] == "active"

    def test_returns_none_for_missing(self, dynamodb_tables):
        svc = TownService()
        result = svc.get_conversation_by_id("nonexistent-conv-id")
        assert result is None


# ===========================================================================
# TownService — append_message_to_conversation
# ===========================================================================


class TestAppendMessageToConversation:
    def test_appends_turn(self, dynamodb_tables):
        svc = TownService()
        a = _make_agent(svc, "u1", "agent-a", "Agent A")
        b = _make_agent(svc, "u2", "agent-b", "Agent B")

        conv = svc.create_active_conversation(a["id"], b["id"], "cafe", "Hello", "Agent A")
        updated = svc.append_message_to_conversation(conv["id"], b["id"], "Agent B", "Hey there!")

        assert updated is not None
        assert updated["turnCount"] == 2
        assert len(updated["publicLog"]) == 2
        assert updated["publicLog"][-1]["speaker"] == "Agent B"
        assert updated["publicLog"][-1]["text"] == "Hey there!"

    def test_returns_none_for_ended_conversation(self, dynamodb_tables):
        svc = TownService()
        a = _make_agent(svc, "u1", "agent-a", "Agent A")
        b = _make_agent(svc, "u2", "agent-b", "Agent B")

        conv = svc.create_active_conversation(a["id"], b["id"], "plaza", "Hi", "Agent A")
        svc.end_conversation(conv["id"])

        result = svc.append_message_to_conversation(conv["id"], b["id"], "Agent B", "Too late!")
        assert result is None

    def test_returns_none_for_missing_conversation(self, dynamodb_tables):
        svc = TownService()
        result = svc.append_message_to_conversation("no-such-id", "x", "Agent X", "Hello?")
        assert result is None

    def test_multiple_turns_accumulate(self, dynamodb_tables):
        svc = TownService()
        a = _make_agent(svc, "u1", "agent-a", "Agent A")
        b = _make_agent(svc, "u2", "agent-b", "Agent B")

        conv = svc.create_active_conversation(a["id"], b["id"], "library", "Start", "Agent A")
        svc.append_message_to_conversation(conv["id"], b["id"], "Agent B", "Reply 1")
        svc.append_message_to_conversation(conv["id"], a["id"], "Agent A", "Reply 2")
        updated = svc.append_message_to_conversation(conv["id"], b["id"], "Agent B", "Reply 3")

        assert updated["turnCount"] == 4
        assert len(updated["publicLog"]) == 4


# ===========================================================================
# TownService — end_conversation
# ===========================================================================


class TestEndConversation:
    def test_marks_conversation_ended(self, dynamodb_tables):
        svc = TownService()
        a = _make_agent(svc, "u1", "agent-a", "Agent A")
        b = _make_agent(svc, "u2", "agent-b", "Agent B")

        conv = svc.create_active_conversation(a["id"], b["id"], "plaza", "Hi", "Agent A")
        ended = svc.end_conversation(conv["id"])

        assert ended is not None
        assert ended["status"] == "ended"
        assert ended["endedAt"] is not None

    def test_clears_current_conversation_id_on_both_agents(self, dynamodb_tables):
        svc = TownService()
        a = _make_agent(svc, "u1", "agent-a", "Agent A")
        b = _make_agent(svc, "u2", "agent-b", "Agent B")

        conv = svc.create_active_conversation(a["id"], b["id"], "cafe", "Hey", "Agent A")
        svc.end_conversation(conv["id"])

        a_fresh = svc.get_town_agent_by_id(a["id"])
        b_fresh = svc.get_town_agent_by_id(b["id"])
        assert a_fresh.get("currentConversationId") is None
        assert b_fresh.get("currentConversationId") is None

    def test_sets_last_conversation_at(self, dynamodb_tables):
        svc = TownService()
        a = _make_agent(svc, "u1", "agent-a", "Agent A")
        b = _make_agent(svc, "u2", "agent-b", "Agent B")

        conv = svc.create_active_conversation(a["id"], b["id"], "plaza", "Hi", "Agent A")
        svc.end_conversation(conv["id"])

        a_fresh = svc.get_town_agent_by_id(a["id"])
        assert a_fresh.get("lastConversationAt") is not None

    def test_returns_none_for_missing_conversation(self, dynamodb_tables):
        svc = TownService()
        result = svc.end_conversation("nonexistent-conv-id")
        assert result is None

    def test_conversation_no_longer_appendable_after_end(self, dynamodb_tables):
        svc = TownService()
        a = _make_agent(svc, "u1", "agent-a", "Agent A")
        b = _make_agent(svc, "u2", "agent-b", "Agent B")

        conv = svc.create_active_conversation(a["id"], b["id"], "plaza", "Hi", "Agent A")
        svc.end_conversation(conv["id"])
        result = svc.append_message_to_conversation(conv["id"], b["id"], "Agent B", "No reply")
        assert result is None


# ===========================================================================
# TownService — get_agent_by_agent_name_active
# ===========================================================================


class TestGetAgentByAgentNameActive:
    def test_returns_active_agent(self, dynamodb_tables):
        svc = TownService()
        _make_agent(svc, "u1", "lucky", "Lucky")

        result = svc.get_agent_by_agent_name_active("lucky")
        assert result is not None
        assert result["agentName"] == "lucky"
        assert result["isActive"] == "true"

    def test_returns_none_for_inactive_agent(self, dynamodb_tables):
        svc = TownService()
        _make_agent(svc, "u1", "lucky", "Lucky")
        svc.opt_out("u1", "lucky")

        result = svc.get_agent_by_agent_name_active("lucky")
        assert result is None

    def test_returns_none_for_missing_agent(self, dynamodb_tables):
        svc = TownService()
        result = svc.get_agent_by_agent_name_active("no-such-agent")
        assert result is None


# ===========================================================================
# TownAgentWsManager — get_by_agent_id
# ===========================================================================


class TestTownAgentWsManagerGetByAgentId:
    def test_returns_connection_for_registered_agent(self):
        mgr = TownAgentWsManager()
        mgr.register(
            connection_id="conn-1",
            user_id="user-1",
            agent_name="lucky",
            agent_id="agent-uuid-1",
            instance_id="instance-1",
        )

        conn = mgr.get_by_agent_id("agent-uuid-1")
        assert conn is not None
        assert conn.connection_id == "conn-1"
        assert conn.agent_id == "agent-uuid-1"

    def test_returns_none_for_unknown_agent_id(self):
        mgr = TownAgentWsManager()
        result = mgr.get_by_agent_id("unknown-uuid")
        assert result is None

    def test_returns_none_after_unregister(self):
        mgr = TownAgentWsManager()
        mgr.register(
            connection_id="conn-1",
            user_id="user-1",
            agent_name="lucky",
            agent_id="agent-uuid-1",
            instance_id="instance-1",
        )
        mgr.unregister("conn-1")

        result = mgr.get_by_agent_id("agent-uuid-1")
        assert result is None

    def test_re_registration_updates_index(self):
        """Re-connecting agent gets a new connection ID — old one should be evicted."""
        mgr = TownAgentWsManager()
        mgr.register("conn-old", "u1", "lucky", "agent-uuid-1", "inst-1")
        mgr.unregister("conn-old")
        mgr.register("conn-new", "u1", "lucky", "agent-uuid-1", "inst-1")

        conn = mgr.get_by_agent_id("agent-uuid-1")
        assert conn.connection_id == "conn-new"
