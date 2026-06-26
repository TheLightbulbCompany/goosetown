"""Tests for the conversation methods on TownService and TownAgentWsManager."""

from core.services.town_agent_ws import TownAgentWsManager
from core.services.town_service import TownService

# ---------------------------------------------------------------------------
# Task 4: env-tunable turn cap
# ---------------------------------------------------------------------------


def test_max_conversation_turns_reads_env(monkeypatch):
    monkeypatch.setenv("CONVERSATION_MAX_TURNS", "6")
    import importlib

    from core.services import conversation_service

    importlib.reload(conversation_service)
    assert conversation_service._MAX_CONVERSATION_TURNS == 6
    monkeypatch.delenv("CONVERSATION_MAX_TURNS")
    importlib.reload(conversation_service)
    assert conversation_service._MAX_CONVERSATION_TURNS == 12


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
# TownService — append_human_message_to_conversation
# ===========================================================================


class TestAppendHumanMessageToConversation:
    def test_appends_human_tagged_turn(self, dynamodb_tables):
        svc = TownService()
        a = _make_agent(svc, "u1", "agent-a", "Agent A")
        b = _make_agent(svc, "u2", "agent-b", "Agent B")

        conv = svc.create_active_conversation(a["id"], b["id"], "cafe", "Hello", "Agent A")
        updated = svc.append_human_message_to_conversation(conv["id"], "Visitor-abcd", "Hi residents!")

        assert updated is not None
        assert updated["publicLog"][-1]["speaker"] == "Visitor-abcd"
        assert updated["publicLog"][-1]["text"] == "Hi residents!"
        assert updated["publicLog"][-1]["speakerType"] == "human"
        assert updated["humanTurnCount"] == 1

    def test_does_not_advance_agent_turn_count(self, dynamodb_tables):
        """A human turn must NOT bump ``turnCount`` — that drives the agent
        turn-cap, and a visitor must not be able to push the conversation
        toward a premature auto-end."""
        svc = TownService()
        a = _make_agent(svc, "u1", "agent-a", "Agent A")
        b = _make_agent(svc, "u2", "agent-b", "Agent B")

        conv = svc.create_active_conversation(a["id"], b["id"], "plaza", "Hello", "Agent A")
        turn_count_before = conv["turnCount"]
        last_speaker_before = conv.get("lastSpeakerId")

        updated = svc.append_human_message_to_conversation(conv["id"], "Visitor-abcd", "A line")

        assert updated["turnCount"] == turn_count_before
        # ``lastSpeakerId`` (agents-only "don't speak twice" key) is untouched.
        assert updated.get("lastSpeakerId") == last_speaker_before

    def test_human_turn_count_accumulates(self, dynamodb_tables):
        svc = TownService()
        a = _make_agent(svc, "u1", "agent-a", "Agent A")
        b = _make_agent(svc, "u2", "agent-b", "Agent B")

        conv = svc.create_active_conversation(a["id"], b["id"], "library", "Start", "Agent A")
        svc.append_human_message_to_conversation(conv["id"], "Visitor-abcd", "One")
        updated = svc.append_human_message_to_conversation(conv["id"], "Visitor-abcd", "Two")

        assert updated["humanTurnCount"] == 2

    def test_returns_none_for_ended_conversation(self, dynamodb_tables):
        svc = TownService()
        a = _make_agent(svc, "u1", "agent-a", "Agent A")
        b = _make_agent(svc, "u2", "agent-b", "Agent B")

        conv = svc.create_active_conversation(a["id"], b["id"], "plaza", "Hi", "Agent A")
        svc.end_conversation(conv["id"])

        result = svc.append_human_message_to_conversation(conv["id"], "Visitor-abcd", "Too late")
        assert result is None

    def test_returns_none_for_missing_conversation(self, dynamodb_tables):
        svc = TownService()
        result = svc.append_human_message_to_conversation("no-such-id", "Visitor-abcd", "Hello?")
        assert result is None


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


# ===========================================================================
# restore_inactivity_timers — startup timer recovery
# ===========================================================================


class TestRestoreInactivityTimers:
    """Tests for restore_inactivity_timers() in conversation_service."""

    def test_empty_list_returns_zero_no_scheduling(self, monkeypatch):
        """Empty active conversations → returns 0, no timer scheduled."""
        from unittest.mock import MagicMock, patch

        from core.services import conversation_service

        monkeypatch.setattr(
            "core.services.conversation_service._schedule_inactivity_timeout",
            MagicMock(),
        )

        with patch("core.services.conversation_service.TownService") as MockSvc:
            MockSvc.return_value.get_active_conversations.return_value = []
            import asyncio

            result = asyncio.run(conversation_service.restore_inactivity_timers())

        assert result == 0
        conversation_service._schedule_inactivity_timeout.assert_not_called()

    def test_stale_conv_scheduled_with_delay_zero(self, monkeypatch):
        """A conv whose updatedAt is well past the timeout gets delay≈0."""
        from datetime import datetime, timedelta, timezone
        from unittest.mock import MagicMock, patch

        from core.services import conversation_service

        schedule_mock = MagicMock()
        monkeypatch.setattr(
            "core.services.conversation_service._schedule_inactivity_timeout",
            schedule_mock,
        )

        # updatedAt far in the past (10 minutes ago — well past the 180s timeout)
        stale_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        stale_conv = {"id": "conv-stale", "status": "active", "updatedAt": stale_ts}

        with patch("core.services.conversation_service.TownService") as MockSvc:
            MockSvc.return_value.get_active_conversations.return_value = [stale_conv]
            import asyncio

            result = asyncio.run(conversation_service.restore_inactivity_timers())

        assert result == 1
        args = schedule_mock.call_args
        conv_id_arg = args[0][0]
        delay_kwarg = args[1].get("delay") if args[1] else args[0][1]
        assert conv_id_arg == "conv-stale"
        assert delay_kwarg == 0.0

    def test_fresh_conv_scheduled_with_remaining_delay(self, monkeypatch):
        """A conv updated just now gets delay ≈ _INACTIVITY_TIMEOUT_SEC."""
        from datetime import datetime, timedelta, timezone
        from unittest.mock import MagicMock, patch

        from core.services import conversation_service

        schedule_mock = MagicMock()
        monkeypatch.setattr(
            "core.services.conversation_service._schedule_inactivity_timeout",
            schedule_mock,
        )

        # updatedAt is 5 seconds ago — still has ~175s remaining (at 180s timeout)
        fresh_ts = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        fresh_conv = {"id": "conv-fresh", "status": "active", "updatedAt": fresh_ts}

        with patch("core.services.conversation_service.TownService") as MockSvc:
            MockSvc.return_value.get_active_conversations.return_value = [fresh_conv]
            import asyncio

            result = asyncio.run(conversation_service.restore_inactivity_timers())

        assert result == 1
        args = schedule_mock.call_args
        delay_kwarg = args[1].get("delay") if args[1] else args[0][1]
        # Should be close to (timeout - 5), with some slack for test execution time
        assert delay_kwarg > 0.0
        assert delay_kwarg <= conversation_service._INACTIVITY_TIMEOUT_SEC

    def test_both_stale_and_fresh_both_scheduled(self, monkeypatch):
        """Both a stale and a fresh conv are scheduled; stale gets delay 0."""
        from datetime import datetime, timedelta, timezone
        from unittest.mock import MagicMock, patch

        from core.services import conversation_service

        schedule_mock = MagicMock()
        monkeypatch.setattr(
            "core.services.conversation_service._schedule_inactivity_timeout",
            schedule_mock,
        )

        stale_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        fresh_ts = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        convs = [
            {"id": "conv-stale", "status": "active", "updatedAt": stale_ts},
            {"id": "conv-fresh", "status": "active", "updatedAt": fresh_ts},
        ]

        with patch("core.services.conversation_service.TownService") as MockSvc:
            MockSvc.return_value.get_active_conversations.return_value = convs
            import asyncio

            result = asyncio.run(conversation_service.restore_inactivity_timers())

        assert result == 2
        assert schedule_mock.call_count == 2

        calls_by_id = {}
        for c in schedule_mock.call_args_list:
            cid = c[0][0]
            delay = c[1].get("delay") if c[1] else c[0][1]
            calls_by_id[cid] = delay

        assert calls_by_id["conv-stale"] == 0.0
        assert calls_by_id["conv-fresh"] > 0.0

    def test_bad_updated_at_skips_conv_no_exception(self, monkeypatch):
        """A conv with unparseable updatedAt is skipped; no exception escapes."""
        from datetime import datetime, timedelta, timezone
        from unittest.mock import MagicMock, patch

        from core.services import conversation_service

        schedule_mock = MagicMock()
        monkeypatch.setattr(
            "core.services.conversation_service._schedule_inactivity_timeout",
            schedule_mock,
        )

        fresh_ts = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        convs = [
            {"id": "conv-bad", "status": "active", "updatedAt": "not-a-timestamp"},
            {"id": "conv-good", "status": "active", "updatedAt": fresh_ts},
        ]

        with patch("core.services.conversation_service.TownService") as MockSvc:
            MockSvc.return_value.get_active_conversations.return_value = convs
            import asyncio

            # Must not raise
            result = asyncio.run(conversation_service.restore_inactivity_timers())

        # Only the good one was scheduled
        assert result == 1
        assert schedule_mock.call_count == 1
        assert schedule_mock.call_args[0][0] == "conv-good"

    def test_uses_started_at_when_updated_at_missing(self, monkeypatch):
        """Falls back to startedAt when updatedAt is absent."""
        from datetime import datetime, timedelta, timezone
        from unittest.mock import MagicMock, patch

        from core.services import conversation_service

        schedule_mock = MagicMock()
        monkeypatch.setattr(
            "core.services.conversation_service._schedule_inactivity_timeout",
            schedule_mock,
        )

        stale_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        conv = {"id": "conv-no-updated", "status": "active", "startedAt": stale_ts}

        with patch("core.services.conversation_service.TownService") as MockSvc:
            MockSvc.return_value.get_active_conversations.return_value = [conv]
            import asyncio

            result = asyncio.run(conversation_service.restore_inactivity_timers())

        assert result == 1
        delay = schedule_mock.call_args[1].get("delay") if schedule_mock.call_args[1] else schedule_mock.call_args[0][1]
        assert delay == 0.0


# ===========================================================================
# _schedule_inactivity_timeout — optional delay parameter
# ===========================================================================


class TestScheduleInactivityTimeoutDelay:
    """_schedule_inactivity_timeout passes an explicit delay through to the task."""

    def test_custom_delay_is_used_not_default(self, monkeypatch):
        """When delay=5.0 is passed, the created task sleeps 5.0 not _INACTIVITY_TIMEOUT_SEC."""
        import asyncio
        from unittest.mock import AsyncMock, patch

        from core.services import conversation_service

        sleep_calls = []

        async def capturing_sleep(seconds):
            sleep_calls.append(seconds)
            # Don't actually sleep in the test

        monkeypatch.setattr(asyncio, "sleep", capturing_sleep)

        # Patch finalize so the timer doesn't do real DDB work when it fires
        mock_finalize = AsyncMock()
        monkeypatch.setattr(
            "core.services.conversation_service._finalize_conversation",
            mock_finalize,
        )
        # Patch TownService get_conversation_by_id to return non-active so finalize bails early
        with patch("core.services.conversation_service.TownService") as MockSvc:
            MockSvc.return_value.get_conversation_by_id.return_value = {"status": "ended"}

            async def run():
                conversation_service._schedule_inactivity_timeout("test-conv-delay", delay=5.0)
                # Run the event loop briefly to let the task execute
                task = conversation_service._inactivity_tasks.get("test-conv-delay")
                if task:
                    await asyncio.wait_for(task, timeout=2.0)

            asyncio.run(run())

        assert sleep_calls == [5.0], f"Expected [5.0] but got {sleep_calls}"

    def test_no_delay_uses_default_timeout(self, monkeypatch):
        """When no delay is passed, the task sleeps _INACTIVITY_TIMEOUT_SEC."""
        import asyncio
        from unittest.mock import patch

        from core.services import conversation_service

        sleep_calls = []

        async def capturing_sleep(seconds):
            sleep_calls.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", capturing_sleep)

        with patch("core.services.conversation_service.TownService") as MockSvc:
            MockSvc.return_value.get_conversation_by_id.return_value = {"status": "ended"}

            async def run():
                conversation_service._schedule_inactivity_timeout("test-conv-default")
                task = conversation_service._inactivity_tasks.get("test-conv-default")
                if task:
                    await asyncio.wait_for(task, timeout=2.0)

            asyncio.run(run())

        assert sleep_calls == [conversation_service._INACTIVITY_TIMEOUT_SEC]


# ===========================================================================
# get_active_conversations — TownService DynamoDB query
# ===========================================================================


class TestGetActiveConversations:
    """get_active_conversations queries the status-updatedAt-index GSI."""

    def test_queries_correct_gsi_with_status_active(self, monkeypatch):
        """Asserts that the method queries status-updatedAt-index with status=active."""
        from unittest.mock import MagicMock, patch

        from core.services.town_service import TownService

        mock_table = MagicMock()
        mock_table.query.return_value = {"Items": []}

        with patch("core.services.town_service.dynamo") as mock_dynamo:
            mock_dynamo.conversations_table.return_value = mock_table
            svc = TownService()
            result = svc.get_active_conversations()

        assert result == []
        mock_table.query.assert_called_once()
        call_kwargs = mock_table.query.call_args[1]
        assert call_kwargs.get("IndexName") == "status-updatedAt-index"

    def test_returns_decimal_native_items(self, monkeypatch):
        """Items are converted from Decimal to native Python types."""
        from decimal import Decimal
        from unittest.mock import MagicMock, patch

        from core.services.town_service import TownService

        mock_table = MagicMock()
        mock_table.query.return_value = {"Items": [{"id": "conv-1", "status": "active", "turnCount": Decimal("3")}]}

        with patch("core.services.town_service.dynamo") as mock_dynamo:
            mock_dynamo.conversations_table.return_value = mock_table
            svc = TownService()
            result = svc.get_active_conversations()

        assert len(result) == 1
        assert isinstance(result[0]["turnCount"], int)
