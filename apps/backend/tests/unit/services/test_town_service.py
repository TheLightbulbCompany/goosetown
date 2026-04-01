"""Tests for TownService (DynamoDB backend)."""

from types import SimpleNamespace

from core.services.town_service import TownService


class TestOptIn:
    def test_opt_in_creates_agent(self, dynamodb_tables):
        svc = TownService()
        agent = svc.opt_in("user-1", "agent-a", "Agent A", personality_summary="friendly")

        assert agent["userId"] == "user-1"
        assert agent["agentName"] == "agent-a"
        assert agent["displayName"] == "Agent A"
        assert agent["isActive"] == "true"
        assert agent["currentLocation"] == "exit"
        assert agent["locationContext"] == "apartment"

    def test_opt_in_idempotent(self, dynamodb_tables):
        svc = TownService()
        a1 = svc.opt_in("user-1", "agent-a", "Agent A")
        a2 = svc.opt_in("user-1", "agent-a", "Agent A")
        assert a1["id"] == a2["id"]

    def test_opt_in_reactivates(self, dynamodb_tables):
        svc = TownService()
        svc.opt_in("user-1", "agent-a", "Agent A")
        svc.opt_out("user-1", "agent-a")

        agent = svc.opt_in("user-1", "agent-a", "Agent A v2")
        assert agent["isActive"] == "true"
        assert agent["displayName"] == "Agent A v2"


class TestOptOut:
    def test_opt_out_deactivates(self, dynamodb_tables):
        svc = TownService()
        svc.opt_in("user-1", "agent-a", "Agent A")
        result = svc.opt_out("user-1", "agent-a")
        assert result is True

        agent = svc._get_town_agent("user-1", "agent-a")
        assert agent["isActive"] == "false"

    def test_opt_out_nonexistent(self, dynamodb_tables):
        svc = TownService()
        assert svc.opt_out("user-1", "nope") is False


class TestGetActiveAgents:
    def test_returns_active_only(self, dynamodb_tables):
        svc = TownService()
        svc.opt_in("user-1", "a1", "A1")
        svc.opt_in("user-1", "a2", "A2")
        svc.opt_out("user-1", "a2")

        active = svc.get_active_agents()
        names = [a["agentName"] for a in active]
        assert "a1" in names
        assert "a2" not in names


class TestGetTownState:
    def test_returns_sprite_ready_only(self, dynamodb_tables):
        svc = TownService()
        agent = svc.opt_in("user-1", "a1", "A1")
        # Not sprite ready yet
        assert svc.get_town_state() == []

        # Mark sprite ready
        svc.update_agent_state(agent["id"], sprite_ready=True)
        state = svc.get_town_state()
        assert len(state) == 1
        assert state[0]["agent_name"] == "a1"
        assert state[0]["current_location"] == "exit"


class TestUpdateAgentState:
    def test_updates_fields(self, dynamodb_tables):
        svc = TownService()
        agent = svc.opt_in("user-1", "a1", "A1")
        updated = svc.update_agent_state(
            agent["id"],
            mood="happy",
            current_location="cafe",
        )
        assert updated["mood"] == "happy"
        assert updated["currentLocation"] == "cafe"

    def test_returns_none_for_missing_agent(self, dynamodb_tables):
        svc = TownService()
        result = svc.update_agent_state("nonexistent-id", mood="sad")
        assert result is None


class TestUpdateAgentContext:
    def test_town_to_apartment(self, dynamodb_tables):
        svc = TownService()
        agent = svc.opt_in("user-1", "a1", "A1")

        updated = svc.update_agent_context(agent["id"], "apartment", "exit")

        assert updated is not None
        assert updated["locationContext"] == "apartment"
        assert updated["currentLocation"] == "exit"

    def test_apartment_to_town(self, dynamodb_tables):
        svc = TownService()
        agent = svc.opt_in("user-1", "a1", "A1")
        svc.update_agent_context(agent["id"], "apartment", "exit")

        updated = svc.update_agent_context(agent["id"], "town", "residence")

        assert updated is not None
        assert updated["locationContext"] == "town"
        assert updated["currentLocation"] == "residence"

    def test_returns_none_for_missing_agent(self, dynamodb_tables):
        svc = TownService()
        result = svc.update_agent_context("nonexistent-id", "apartment", "exit")
        assert result is None

    def test_persists_to_db(self, dynamodb_tables):
        svc = TownService()
        agent = svc.opt_in("user-1", "a1", "A1")
        svc.update_agent_context(agent["id"], "apartment", "exit")

        fetched = svc.get_town_agent_by_id(agent["id"])
        assert fetched["locationContext"] == "apartment"
        assert fetched["currentLocation"] == "exit"


class TestInstances:
    def test_create_and_get_instance(self, dynamodb_tables):
        svc = TownService()
        instance = svc.create_instance("user-1")
        assert instance["userId"] == "user-1"
        assert instance["isActive"] == "true"
        assert instance["apartmentUnit"] == 1

        found = svc.get_active_instance("user-1")
        assert found["id"] == instance["id"]

    def test_instance_token_lookup(self, dynamodb_tables):
        svc = TownService()
        instance = svc.create_instance("user-1")
        found = svc.get_instance_by_token(instance["townToken"])
        assert found is not None
        assert found["id"] == instance["id"]

    def test_deactivate_instance(self, dynamodb_tables):
        svc = TownService()
        instance = svc.create_instance("user-1")
        svc.deactivate_instance(instance["id"])
        assert svc.get_active_instance("user-1") is None

    def test_opt_in_instance(self, dynamodb_tables):
        svc = TownService()
        agents_data = [
            SimpleNamespace(agent_name="a1", display_name="A1", personality_summary="nice"),
            SimpleNamespace(agent_name="a2", display_name="A2", personality_summary="cool"),
        ]
        instance, agents = svc.opt_in_instance("user-1", agents_data)
        assert len(agents) == 2
        assert instance["isActive"] == "true"

        # Check agents are linked to instance
        inst_agents = svc.get_instance_agents(instance["id"])
        assert len(inst_agents) == 2

    def test_opt_in_instance_duplicate_raises(self, dynamodb_tables):
        svc = TownService()
        agents_data = [SimpleNamespace(agent_name="a1", display_name="A1", personality_summary="nice")]
        svc.opt_in_instance("user-1", agents_data)
        try:
            svc.opt_in_instance("user-1", agents_data)
            assert False, "Should have raised ValueError"
        except ValueError:
            pass

    def test_opt_out_instance(self, dynamodb_tables):
        svc = TownService()
        agents_data = [
            SimpleNamespace(agent_name="a1", display_name="A1", personality_summary="nice"),
        ]
        svc.opt_in_instance("user-1", agents_data)
        instance, count = svc.opt_out_instance("user-1")
        assert instance is not None
        assert count == 1
        assert svc.get_active_instance("user-1") is None


class TestRelationships:
    def test_create_relationship(self, dynamodb_tables):
        svc = TownService()
        rel, created = svc.get_or_create_relationship("aaa", "bbb")
        assert created is True
        assert rel["agentAId"] == "aaa"
        assert rel["agentBId"] == "bbb"

    def test_get_existing_relationship(self, dynamodb_tables):
        svc = TownService()
        svc.get_or_create_relationship("aaa", "bbb")
        rel, created = svc.get_or_create_relationship("bbb", "aaa")  # reversed
        assert created is False
        assert rel["agentAId"] == "aaa"

    def test_get_all_relationships(self, dynamodb_tables):
        svc = TownService()
        svc.get_or_create_relationship("aaa", "bbb")
        svc.get_or_create_relationship("ccc", "ddd")
        rels = svc.get_all_relationships()
        assert len(rels) == 2


class TestConversations:
    def test_store_conversation(self, dynamodb_tables):
        svc = TownService()
        conv = svc.store_conversation(
            "agent-a",
            "agent-b",
            "park",
            [{"speaker": "A", "text": "hello"}, {"speaker": "B", "text": "hi"}],
            topic_summary="greeting",
        )
        assert conv["location"] == "park"
        assert conv["turnCount"] == 2
        assert conv["status"] == "ended"

    def test_get_recent_conversations(self, dynamodb_tables):
        svc = TownService()
        svc.store_conversation("a", "b", "park", [{"speaker": "A", "text": "hi"}])
        svc.store_conversation("c", "d", "cafe", [{"speaker": "C", "text": "yo"}])

        recent = svc.get_recent_conversations(limit=10)
        assert len(recent) == 2

    def test_get_recent_speech(self, dynamodb_tables):
        svc = TownService()
        svc.store_conversation("a", "b", "park", [{"speaker": "Agent A", "text": "hello world"}])

        speeches = svc.get_recent_speech(since_seconds=10.0)
        assert len(speeches) >= 1
        assert speeches[0]["speaker"] == "Agent A"
