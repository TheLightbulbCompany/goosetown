"""Tests for TownService (DynamoDB backend)."""

from types import SimpleNamespace

from core import dynamo
from core.services.town_service import RESIDENT_USER_ID, TownService


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

    def test_opt_in_creates_name_claim_row(self, dynamodb_tables):
        """The fresh-agent path writes BOTH the agent row and a
        name-claim row atomically (TransactWriteItems). The claim row
        has a synthetic PK ``name-claim#<user_id>#<agent_name>`` so
        the next ``opt_in`` for the same pair can condition on it."""
        from core import dynamo
        from core.services.town_service import _name_claim_id

        svc = TownService()
        agent = svc.opt_in("user-1", "agent-a", "Agent A")

        claim_id = _name_claim_id("user-1", "agent-a")
        table = dynamo.agents_table()
        claim_resp = table.get_item(Key={"id": claim_id})
        claim = claim_resp.get("Item")

        assert claim is not None, "name-claim row must be written alongside the agent"
        assert claim["agentId"] == agent["id"]
        assert claim["kind"] == "name_claim"

    def test_claim_row_invisible_to_existing_reads(self, dynamodb_tables):
        """The claim row omits ``userId`` / ``agentName`` / ``isActive``
        / ``joinedAt`` so it stays out of every GSI. Verified via the
        three read paths that former TOCTOU-era tests would've tripped
        on if claim rows leaked into results."""
        svc = TownService()
        svc.opt_in("user-1", "agent-a", "Agent A")
        svc.opt_in("user-1", "agent-b", "Agent B")

        # userId-agentName-index — per-user query
        per_user = svc.get_active_agents_for_user("user-1")
        names = sorted(a["agentName"] for a in per_user)
        assert names == ["agent-a", "agent-b"]

        # isActive-joinedAt-index — global active agents
        active = svc.get_active_agents()
        assert sorted(a["agentName"] for a in active) == ["agent-a", "agent-b"]

        # _get_town_agent — the internal single-agent lookup
        lookup = svc._get_town_agent("user-1", "agent-a")
        assert lookup is not None
        assert lookup["agentName"] == "agent-a"
        # NOT the claim row (which has no agentName, no displayName, etc)
        assert "displayName" in lookup

    def test_concurrent_registration_race_returns_winner(self, dynamodb_tables, monkeypatch):
        """Race scenario: two concurrent opt_in calls for the same
        ``(user_id, agent_name)`` both pass the initial
        ``_get_town_agent`` check. The first hits ``TransactWriteItems``
        and wins (writes claim + agent); the second's claim put fails
        with ``ConditionalCheckFailed``. The loser re-reads
        ``_get_town_agent``, finds the winner's agent, and returns it —
        the race is invisible to the caller.

        Simulated with a stub that makes the first ``_get_town_agent``
        return None (initial check) and subsequent calls return the real
        row — the effect of a winner committing between our check and
        our transact call.
        """
        svc = TownService()
        winner = svc.opt_in("user-1", "agent-a", "Winner")

        # Simulate: a SECOND opt_in that happens to race. The initial
        # check returned None (empty at the time) so it reaches the
        # transact; by then the winner is visible.
        call_count = {"n": 0}
        real_get = svc._get_town_agent

        def racing_get(user_id, agent_name):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return None  # initial check — winner hadn't committed yet
            return real_get(user_id, agent_name)  # post-failure re-read

        monkeypatch.setattr(svc, "_get_town_agent", racing_get)

        # Must not raise, must return the WINNER's row (same id — no
        # duplicate agent was written).
        result = svc.opt_in("user-1", "agent-a", "Loser")
        assert result["id"] == winner["id"]
        assert result["displayName"] == "Winner"  # winner's values preserved

    def test_orphan_claim_without_agent_raises_specific_error(self, dynamodb_tables):
        """If a claim row exists but no matching agent does, opt_in
        detects the orphan and raises a clear error rather than silently
        succeeding or hanging."""
        import pytest

        from core import dynamo
        from core.services.town_service import _name_claim_id

        # Manually write a claim row with no agent.
        table = dynamo.agents_table()
        claim_id = _name_claim_id("user-1", "ghost")
        table.put_item(
            Item={
                "id": claim_id,
                "kind": "name_claim",
                "agentId": "no-such-agent",
                "claimedAt": "2026-04-24T00:00:00Z",
            }
        )

        svc = TownService()
        with pytest.raises(RuntimeError, match="orphan claim"):
            svc.opt_in("user-1", "ghost", "Ghost")

    def test_two_users_can_share_agent_name(self, dynamodb_tables):
        """Name claim is keyed by ``(user_id, agent_name)``, not by
        ``agent_name`` alone — two different users CAN both have an
        agent called "jim". (This is existing behaviour, but the claim-
        lock must not break it.)"""
        svc = TownService()
        jim_a = svc.opt_in("user-a", "jim", "Jim from A")
        jim_b = svc.opt_in("user-b", "jim", "Jim from B")

        assert jim_a["id"] != jim_b["id"]
        assert jim_a["userId"] == "user-a"
        assert jim_b["userId"] == "user-b"


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
    def test_update_agent_state_maps_onboarding_phase_to_camel_case(self, dynamodb_tables):
        svc = TownService()
        agent = svc.opt_in("user-1", "test-onboarding", "Test Onboarding")
        svc.update_agent_state(agent["id"], onboarding_phase="static")
        fetched = svc.get_town_agent_by_id(agent["id"])
        assert fetched.get("onboardingPhase") == "static"

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


class TestGetTownAgentByIdConsistency:
    """The Exchange location gate needs a strongly-consistent read (finding 2)."""

    def _spy_table(self, monkeypatch):
        """Wrap agents_table().get_item to record the ConsistentRead kwarg."""
        real_table = dynamo.agents_table()
        calls: list[dict] = []
        real_get_item = real_table.get_item

        def recording_get_item(*args, **kwargs):
            calls.append(dict(kwargs))
            return real_get_item(*args, **kwargs)

        wrapper = SimpleNamespace(get_item=recording_get_item)
        monkeypatch.setattr(dynamo, "agents_table", lambda: wrapper)
        return calls

    def test_default_read_is_eventually_consistent(self, dynamodb_tables, monkeypatch):
        svc = TownService()
        agent = svc.opt_in("user-1", "a1", "A1")
        calls = self._spy_table(monkeypatch)

        svc.get_town_agent_by_id(agent["id"])

        assert calls and calls[-1].get("ConsistentRead") is False

    def test_consistent_true_passes_consistent_read(self, dynamodb_tables, monkeypatch):
        svc = TownService()
        agent = svc.opt_in("user-1", "a1", "A1")
        calls = self._spy_table(monkeypatch)

        fetched = svc.get_town_agent_by_id(agent["id"], consistent=True)

        assert calls and calls[-1].get("ConsistentRead") is True
        # moto honours the kwarg and still returns the row.
        assert fetched is not None and fetched["id"] == agent["id"]


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


class TestCreateResident:
    def test_create_resident_is_system_owned_and_quota_invisible(self, dynamodb_tables):
        svc = TownService()
        agent = svc.create_resident(
            name="tobias",
            display_name="Tobias",
            personality="Retired lighthouse keeper, narrates his life in third person.",
            appearance="Barrel-chested old man, salt-and-pepper beard, mustard raincoat.",
            traits=["gruff", "observant", "nostalgic"],
        )

        assert agent["isResident"] is True
        assert agent["userId"] == "system:resident"
        assert agent["isActive"] == "true"

        # invisible to a real user's quota:
        assert svc.get_active_agents_for_user("user_real") == []

        # idempotent on name:
        again = svc.create_resident(
            name="tobias",
            display_name="Tobias",
            personality="x",
            appearance="y",
            traits=["z"],
        )
        assert again["id"] == agent["id"]
        # ...and no duplicate row was written under the happy path:
        rows = [i for i in dynamo.agents_table().scan()["Items"] if i.get("agentName") == "tobias"]
        assert len(rows) == 1

    def test_create_resident_has_expected_fields(self, dynamodb_tables):
        svc = TownService()
        agent = svc.create_resident(
            name="clara",
            display_name="Clara",
            personality="Curious botanist, always talking about plants.",
            appearance="Petite woman with green hair and a flower crown.",
            traits=["curious", "warm"],
        )

        assert agent["agentName"] == "clara"
        assert agent["displayName"] == "Clara"
        assert agent["personalitySummary"] == "Curious botanist, always talking about plants."
        assert agent["isResident"] is True
        assert agent["isActive"] == "true"
        assert agent["currentLocation"] == "exit"
        assert agent["locationContext"] == "apartment"
        assert "id" in agent
        assert "joinedAt" in agent

    def test_create_resident_idempotent_reactivates_inactive(self, dynamodb_tables):
        """If a resident was deactivated (e.g. by opt_out), create_resident reactivates it."""
        svc = TownService()

        agent = svc.create_resident(
            name="remy",
            display_name="Remy",
            personality="Chef.",
            appearance="Chef's hat, apron.",
            traits=["focused"],
        )
        original_id = agent["id"]

        # Deactivate the resident
        svc.opt_out(RESIDENT_USER_ID, "remy")
        deactivated = svc.get_town_agent_by_id(original_id)
        assert deactivated["isActive"] == "false"

        # create_resident should reactivate
        reactivated = svc.create_resident(
            name="remy",
            display_name="Remy",
            personality="Chef.",
            appearance="Chef's hat, apron.",
            traits=["focused"],
        )
        assert reactivated["id"] == original_id
        assert reactivated["isActive"] == "true"

    def test_residents_invisible_to_global_active_agents_for_real_user(self, dynamodb_tables):
        """Residents must not appear in any real user's quota count."""
        svc = TownService()
        svc.create_resident(
            name="nova",
            display_name="Nova",
            personality="Astronomer.",
            appearance="Telescope and starmap.",
            traits=["dreamy"],
        )
        svc.opt_in("user-real", "my-agent", "My Agent")

        user_agents = svc.get_active_agents_for_user("user-real")
        assert len(user_agents) == 1
        assert user_agents[0]["agentName"] == "my-agent"


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
