"""Tests for TownSimulation push/event logic.

Tests cover:
1. Arrived event generation
2. Nearby event generation
3. World update push (periodic)
4. Context summary building
5. Inactive detection (with connection guard)
6. Viewer broadcast
7. Conversation probability
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from core.services.town_simulation import TownSimulation, PROXIMITY_THRESHOLD


# ─── Helpers ──────────────────────────────────────────────────────────


def _make_agent_state(
    agent_id="agent-1",
    agent_name="goose-1",
    display_name="Goose One",
    position_x=10.0,
    position_y=10.0,
    target_x=None,
    target_y=None,
    location_state="active",
    location_context="town",
    current_location="plaza",
    current_activity="idle",
    current_conversation_id=None,
    last_heartbeat_at=None,
    mood="0",
    energy=80,
    wake_at=None,
    wake_timezone=None,
    personality_summary="friendly goose",
    traits="",
    target_location=None,
    sprite_ready=True,
    speed=0.0,
    facing_x=0.0,
    facing_y=1.0,
):
    """Create a mock agent state dict matching TownService.get_town_state() format."""
    if last_heartbeat_at is None:
        last_heartbeat_at = datetime.now(timezone.utc)
    return {
        "agent_id": agent_id,
        "user_id": "user-1",
        "agent_name": agent_name,
        "display_name": display_name,
        "position_x": position_x,
        "position_y": position_y,
        "target_x": target_x,
        "target_y": target_y,
        "target_location": target_location,
        "location_state": location_state,
        "location_context": location_context,
        "current_location": current_location,
        "current_activity": current_activity,
        "current_conversation_id": current_conversation_id,
        "last_heartbeat_at": last_heartbeat_at,
        "mood": mood,
        "energy": energy,
        "wake_at": wake_at,
        "wake_timezone": wake_timezone,
        "personality_summary": personality_summary,
        "traits": traits,
        "sprite_ready": sprite_ready,
        "sprite_url": None,
        "speed": speed,
        "facing_x": facing_x,
        "facing_y": facing_y,
        "status_message": None,
        "last_decision_at": None,
        "last_conversation_at": None,
        "home_location": "apartment",
        "character": None,
    }


# ─── Inactive Detection Tests ────────────────────────────────────────


class TestInactiveDetection:
    @pytest.mark.asyncio
    async def test_disconnected_inactive_agent_sent_home(self, dynamodb_tables):
        """An active agent with no heartbeat for >5min AND not connected should be sent home."""
        sim = TownSimulation()
        old_heartbeat = datetime.now(timezone.utc) - timedelta(minutes=10)

        agent = _make_agent_state(
            location_state="active",
            location_context="town",
            last_heartbeat_at=old_heartbeat,
        )

        mock_service = MagicMock()
        mock_service.get_town_state.return_value = [agent]

        mock_conn_svc = MagicMock()
        mock_conn_svc.is_agent_connected.return_value = False

        with patch("core.services.town_service.TownService", return_value=mock_service), \
             patch.object(sim, "_get_connection_service", return_value=mock_conn_svc), \
             patch.object(sim, "_get_mgmt_client", return_value=None):
            await sim._tick()

        # Should send the agent home
        mock_service.update_agent_state.assert_called()
        call_kwargs = mock_service.update_agent_state.call_args[1]
        assert call_kwargs["location_state"] == "going_home"

    @pytest.mark.asyncio
    async def test_connected_inactive_agent_not_sent_home(self, dynamodb_tables):
        """An active agent with stale heartbeat but still connected should NOT be sent home."""
        sim = TownSimulation()
        old_heartbeat = datetime.now(timezone.utc) - timedelta(minutes=10)

        agent = _make_agent_state(
            location_state="active",
            location_context="town",
            last_heartbeat_at=old_heartbeat,
        )

        mock_service = MagicMock()
        mock_service.get_town_state.return_value = [agent]

        mock_conn_svc = MagicMock()
        mock_conn_svc.is_agent_connected.return_value = True

        with patch("core.services.town_service.TownService", return_value=mock_service), \
             patch.object(sim, "_get_connection_service", return_value=mock_conn_svc), \
             patch.object(sim, "_get_mgmt_client", return_value=None):
            await sim._tick()

        # Should NOT have sent the agent home
        mock_service.update_agent_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_active_agent_with_fresh_heartbeat_not_sent_home(self, dynamodb_tables):
        """Active agent with recent heartbeat should never be sent home."""
        sim = TownSimulation()
        fresh_heartbeat = datetime.now(timezone.utc) - timedelta(seconds=30)

        agent = _make_agent_state(
            location_state="active",
            location_context="town",
            last_heartbeat_at=fresh_heartbeat,
        )

        mock_service = MagicMock()
        mock_service.get_town_state.return_value = [agent]

        mock_conn_svc = MagicMock()
        mock_conn_svc.is_agent_connected.return_value = False

        with patch("core.services.town_service.TownService", return_value=mock_service), \
             patch.object(sim, "_get_connection_service", return_value=mock_conn_svc), \
             patch.object(sim, "_get_mgmt_client", return_value=None):
            await sim._tick()

        mock_service.update_agent_state.assert_not_called()


# ─── Proximity / Nearby Event Tests ─────────────────────────────────


class TestNearbyEvents:
    @pytest.mark.asyncio
    async def test_new_proximity_pushes_nearby_event(self, dynamodb_tables):
        """When two agents become newly proximate, both should receive nearby events."""
        sim = TownSimulation()

        agent_a = _make_agent_state(
            agent_id="a1", agent_name="goose-1", display_name="Goose One",
            position_x=10.0, position_y=10.0,
        )
        agent_b = _make_agent_state(
            agent_id="a2", agent_name="goose-2", display_name="Goose Two",
            position_x=11.0, position_y=10.0,  # within PROXIMITY_THRESHOLD
        )

        mock_service = MagicMock()
        mock_service.get_town_state.return_value = [agent_a, agent_b]

        mock_conn_svc = MagicMock()
        mock_conn_svc.is_agent_connected.return_value = True
        mock_conn_svc.get_agent_connection_id.side_effect = lambda name: {
            "goose-1": "conn-1",
            "goose-2": "conn-2",
        }.get(name)

        mock_mgmt = MagicMock()
        mock_mgmt.send_message.return_value = True

        with patch("core.services.town_service.TownService", return_value=mock_service), \
             patch.object(sim, "_get_connection_service", return_value=mock_conn_svc), \
             patch.object(sim, "_get_mgmt_client", return_value=mock_mgmt):
            await sim._tick()

        # Should have pushed nearby events (check that both agents got notified)
        nearby_calls = [
            call for call in mock_mgmt.send_message.call_args_list
            if call[0][1].get("event") == "nearby"
        ]
        assert len(nearby_calls) == 2

        # Verify agent names in events
        event_targets = {call[0][0] for call in nearby_calls}
        assert event_targets == {"conn-1", "conn-2"}

    @pytest.mark.asyncio
    async def test_already_nearby_does_not_re_push(self, dynamodb_tables):
        """If agents were already nearby last tick, don't push again."""
        sim = TownSimulation()
        # Pre-populate nearby pairs from "last tick"
        sim._nearby_pairs = {frozenset(("goose-1", "goose-2"))}

        agent_a = _make_agent_state(
            agent_id="a1", agent_name="goose-1",
            position_x=10.0, position_y=10.0,
        )
        agent_b = _make_agent_state(
            agent_id="a2", agent_name="goose-2",
            position_x=11.0, position_y=10.0,
        )

        mock_service = MagicMock()
        mock_service.get_town_state.return_value = [agent_a, agent_b]

        mock_conn_svc = MagicMock()
        mock_conn_svc.is_agent_connected.return_value = True

        mock_mgmt = MagicMock()

        with patch("core.services.town_service.TownService", return_value=mock_service), \
             patch.object(sim, "_get_connection_service", return_value=mock_conn_svc), \
             patch.object(sim, "_get_mgmt_client", return_value=mock_mgmt):
            await sim._tick()

        # No nearby events should have been pushed
        nearby_calls = [
            call for call in mock_mgmt.send_message.call_args_list
            if isinstance(call[0][1], dict) and call[0][1].get("event") == "nearby"
        ]
        assert len(nearby_calls) == 0

    @pytest.mark.asyncio
    async def test_different_contexts_not_nearby(self, dynamodb_tables):
        """Agents in different location contexts should never be considered nearby."""
        sim = TownSimulation()

        agent_a = _make_agent_state(
            agent_id="a1", agent_name="goose-1",
            position_x=10.0, position_y=10.0, location_context="town",
        )
        agent_b = _make_agent_state(
            agent_id="a2", agent_name="goose-2",
            position_x=10.0, position_y=10.0, location_context="apartment",
        )

        mock_service = MagicMock()
        mock_service.get_town_state.return_value = [agent_a, agent_b]

        mock_conn_svc = MagicMock()
        mock_conn_svc.is_agent_connected.return_value = False

        with patch("core.services.town_service.TownService", return_value=mock_service), \
             patch.object(sim, "_get_connection_service", return_value=mock_conn_svc), \
             patch.object(sim, "_get_mgmt_client", return_value=None):
            await sim._tick()

        assert len(sim._nearby_pairs) == 0


# ─── Context Summary Tests ───────────────────────────────────────────


class TestBuildContextSummary:
    def test_town_location_summary(self):
        agent = _make_agent_state(
            location_context="town",
            current_location="plaza",
            current_activity="chatting",
            mood="15",
            energy=75,
        )
        summary = TownSimulation._build_context_summary(agent, [])
        assert "GooseTown Status" in summary
        assert "plaza" in summary.lower() or "Plaza" in summary
        assert "chatting" in summary
        assert "75%" in summary
        assert "no one" in summary.lower()

    def test_apartment_location_summary(self):
        agent = _make_agent_state(
            location_context="apartment",
            current_location="bedroom",
            current_activity="resting",
            mood="0",
            energy=100,
        )
        summary = TownSimulation._build_context_summary(agent, [])
        assert "apartment" in summary.lower()

    def test_nearby_agents_in_summary(self):
        agent = _make_agent_state()
        nearby = [
            {
                "name": "Duck Norris",
                "agent_name": "duck-norris",
                "distance": 2.1,
                "activity": "reading",
                "personality": "wise and calm",
            }
        ]
        summary = TownSimulation._build_context_summary(agent, nearby)
        assert "Duck Norris" in summary
        assert "2.1" in summary
        assert "reading" in summary
        assert "wise and calm" in summary

    def test_nearby_agents_with_relationship(self):
        agent = _make_agent_state()
        nearby = [
            {
                "name": "Quack",
                "agent_name": "quack",
                "distance": 1.5,
                "activity": "idle",
                "relationship_type": "friend",
                "interaction_count": 5,
                "last_topic": "weather",
            }
        ]
        summary = TownSimulation._build_context_summary(agent, nearby)
        assert "friend" in summary
        assert "5" in summary
        assert "weather" in summary

    def test_pending_messages_in_summary(self):
        agent = _make_agent_state()
        summary = TownSimulation._build_context_summary(
            agent, [], pending_messages=["msg1", "msg2"]
        )
        assert "2" in summary
        assert "town_check" in summary


# ─── Conversation Probability Tests ──────────────────────────────────


class TestConversationProbability:
    def test_stranger_probability(self):
        prob = TownSimulation._conversation_probability(0)
        assert prob == pytest.approx(0.15, abs=0.01)

    def test_friend_probability(self):
        prob = TownSimulation._conversation_probability(50)
        assert prob == pytest.approx(0.425, abs=0.01)

    def test_best_friend_probability(self):
        prob = TownSimulation._conversation_probability(100)
        assert prob == pytest.approx(0.70, abs=0.01)

    def test_capped_at_70_percent(self):
        prob = TownSimulation._conversation_probability(200)  # beyond max
        assert prob <= 0.70


# ─── World Update Push Tests ─────────────────────────────────────────


class TestWorldUpdatePush:
    @pytest.mark.asyncio
    async def test_world_update_pushed_on_interval(self, dynamodb_tables):
        """After WORLD_UPDATE_INTERVAL ticks, world_update should be pushed."""
        from core.services.town_simulation import WORLD_UPDATE_INTERVAL

        sim = TownSimulation()
        agent = _make_agent_state()

        mock_service = MagicMock()
        mock_service.get_town_state.return_value = [agent]
        mock_service.get_all_relationships.return_value = []

        mock_conn_svc = MagicMock()
        mock_conn_svc.is_agent_connected.return_value = True
        mock_conn_svc.get_agent_connection_id.return_value = "conn-1"

        mock_mgmt = MagicMock()
        mock_mgmt.send_message.return_value = True

        with patch("core.services.town_service.TownService", return_value=mock_service), \
             patch.object(sim, "_get_connection_service", return_value=mock_conn_svc), \
             patch.object(sim, "_get_mgmt_client", return_value=mock_mgmt):

            # Run enough ticks to trigger world update
            for _ in range(WORLD_UPDATE_INTERVAL):
                await sim._tick()

        # Should have a world_update push
        world_update_calls = [
            call for call in mock_mgmt.send_message.call_args_list
            if isinstance(call[0][1], dict) and call[0][1].get("type") == "world_update"
        ]
        assert len(world_update_calls) >= 1

        # Verify world_update payload structure
        payload = world_update_calls[0][0][1]
        assert "you" in payload
        assert "nearby_agents" in payload
        assert "locations" in payload
        assert "context_summary" in payload

    @pytest.mark.asyncio
    async def test_world_update_includes_think_flag_for_idle_agent(self, dynamodb_tables):
        """Idle agents should get think=True in world_update."""
        from core.services.town_simulation import WORLD_UPDATE_INTERVAL

        sim = TownSimulation()
        agent = _make_agent_state(current_activity="idle")

        mock_service = MagicMock()
        mock_service.get_town_state.return_value = [agent]
        mock_service.get_all_relationships.return_value = []

        mock_conn_svc = MagicMock()
        mock_conn_svc.is_agent_connected.return_value = True
        mock_conn_svc.get_agent_connection_id.return_value = "conn-1"

        mock_mgmt = MagicMock()
        mock_mgmt.send_message.return_value = True

        with patch("core.services.town_service.TownService", return_value=mock_service), \
             patch.object(sim, "_get_connection_service", return_value=mock_conn_svc), \
             patch.object(sim, "_get_mgmt_client", return_value=mock_mgmt):

            for _ in range(WORLD_UPDATE_INTERVAL):
                await sim._tick()

        world_update_calls = [
            call for call in mock_mgmt.send_message.call_args_list
            if isinstance(call[0][1], dict) and call[0][1].get("type") == "world_update"
        ]
        assert len(world_update_calls) >= 1
        assert world_update_calls[0][0][1].get("think") is True

    @pytest.mark.asyncio
    async def test_sleeping_agents_dont_get_world_update(self, dynamodb_tables):
        """Sleeping agents should not receive world_update pushes."""
        from core.services.town_simulation import WORLD_UPDATE_INTERVAL

        sim = TownSimulation()
        agent = _make_agent_state(location_state="sleeping")

        mock_service = MagicMock()
        mock_service.get_town_state.return_value = [agent]
        mock_service.get_all_relationships.return_value = []

        mock_conn_svc = MagicMock()
        mock_conn_svc.is_agent_connected.return_value = True
        mock_conn_svc.get_agent_connection_id.return_value = "conn-1"

        mock_mgmt = MagicMock()

        with patch("core.services.town_service.TownService", return_value=mock_service), \
             patch.object(sim, "_get_connection_service", return_value=mock_conn_svc), \
             patch.object(sim, "_get_mgmt_client", return_value=mock_mgmt):

            for _ in range(WORLD_UPDATE_INTERVAL):
                await sim._tick()

        world_update_calls = [
            call for call in mock_mgmt.send_message.call_args_list
            if isinstance(call[0][1], dict) and call[0][1].get("type") == "world_update"
        ]
        assert len(world_update_calls) == 0


# ─── Viewer Broadcast Tests ──────────────────────────────────────────


class TestViewerBroadcast:
    @pytest.mark.asyncio
    async def test_notify_fn_called_on_tick(self, dynamodb_tables):
        """The viewer broadcast callback should be called after each tick."""
        sim = TownSimulation()
        notify_mock = MagicMock()
        sim.set_notify_fn(notify_mock)

        mock_service = MagicMock()
        mock_service.get_town_state.return_value = []

        mock_conn_svc = MagicMock()

        with patch("core.services.town_service.TownService", return_value=mock_service), \
             patch.object(sim, "_get_connection_service", return_value=mock_conn_svc), \
             patch.object(sim, "_get_mgmt_client", return_value=None):
            await sim._tick()

        notify_mock.assert_called_once()
