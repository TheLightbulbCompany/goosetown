"""Regression tests for U / X / N′ from the 2026-05-04 5-agent soak.

U — ``pending_messages`` echoes own ``say`` messages back to the
   speaker, leaving 5/5 soak agents seeing their own turns
   re-appear on every poll for 10+ minutes.
X — Heartbeat-driven move loop calls ``act move`` mid-conversation
   and yanks the agent away. margot↔ranger-kai got cut at turn 4.
N′ — ``_handle_world_update`` overwrites the daemon's local
   location with the raw backend value on every ~2s tick, undoing
   the ``_on_arrived`` normalization shipped in PR #286.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from goosetown.daemon import TownDaemon


@pytest.fixture
def daemon(tmp_path: Path) -> TownDaemon:
    state_dir = tmp_path / "state"
    workspace = tmp_path / "workspace"
    state_dir.mkdir()
    workspace.mkdir()
    return TownDaemon(
        ws_url="wss://example.invalid",
        token="t",
        agent_name="testagent",
        state_dir=state_dir,
        workspace_path=workspace,
    )


# ---------------------------------------------------------------------------
# U — pending_messages excludes own messages
# ---------------------------------------------------------------------------


class TestPendingMessagesExcludesOwn:
    def test_own_message_does_not_enter_pending(self, daemon: TownDaemon):
        """Backend echoes the speaker's own ``say`` as a
        ``conversation_message`` event with ``from_agent_name`` matching
        ``self._agent_name``. Daemon must not treat that as something
        the agent needs to respond to."""
        daemon.state["active_conversation"] = {
            "conv_id": "c1",
            "participants": ["other"],
            "turns": [],
        }
        daemon._on_conversation_message(
            "conversation_message",
            {
                "conv_id": "c1",
                "from": "Test Agent",
                "from_agent_name": "testagent",  # matches daemon's own name
                "text": "hello",
                "turn": 1,
            },
        )
        assert daemon.state.get("pending_messages", []) == []

    def test_other_agent_message_enters_pending(self, daemon: TownDaemon):
        """Symmetric sanity: messages from OTHERS still flow to pending."""
        daemon.state["active_conversation"] = {
            "conv_id": "c1",
            "participants": ["other"],
            "turns": [],
        }
        daemon._on_conversation_message(
            "conversation_message",
            {
                "conv_id": "c1",
                "from": "Other Agent",
                "from_agent_name": "otheragent",  # different
                "text": "hi back",
                "turn": 2,
            },
        )
        assert len(daemon.state.get("pending_messages", [])) == 1

    def test_own_message_still_appended_to_active_conversation_turns(
        self, daemon: TownDaemon
    ):
        """The speaker MUST still see their own turn in
        active_conversation.turns — that's what F8 from the 2026-04-21
        dry run set up. Filtering only applies to pending_messages."""
        daemon.state["active_conversation"] = {
            "conv_id": "c1",
            "participants": ["other"],
            "turns": [],
        }
        daemon._on_conversation_message(
            "conversation_message",
            {
                "conv_id": "c1",
                "from": "Test Agent",
                "from_agent_name": "testagent",
                "text": "I said this",
                "turn": 1,
            },
        )
        turns = daemon.state["active_conversation"]["turns"]
        assert any(t.get("text") == "I said this" for t in turns)

    def test_message_without_from_agent_name_falls_through_to_pending(
        self, daemon: TownDaemon
    ):
        """Older backend builds may not stamp from_agent_name. In that
        case we can't tell — default to including (avoids dropping a
        real third-party message). Defensive behavior."""
        daemon.state["active_conversation"] = {
            "conv_id": "c1",
            "participants": ["other"],
            "turns": [],
        }
        daemon._on_conversation_message(
            "conversation_message",
            {"conv_id": "c1", "from": "?", "text": "ambiguous", "turn": 1},
        )
        assert len(daemon.state.get("pending_messages", [])) == 1


# ---------------------------------------------------------------------------
# N′ — world_update normalizes location
# ---------------------------------------------------------------------------


class TestWorldUpdateLocationNormalization:
    def test_world_update_corrects_player_spawn_when_pending_destination_set(
        self, daemon: TownDaemon
    ):
        daemon._pending_move_destination = "plaza"
        daemon._handle_world_update(
            {
                "you": {
                    "id": "a",
                    "location": "player_spawn",
                    "activity": "idle",
                    "mood": 0,
                    "energy": 100,
                    "location_context": "town",
                    "onboardingPhase": "alive",
                },
                "nearby_agents": [],
                "think": False,
            }
        )
        assert daemon.state["agent"]["location"] == "plaza"

    def test_world_update_corrects_exit_when_entering_library(self, daemon: TownDaemon):
        daemon._pending_move_destination = "library"
        daemon._handle_world_update(
            {
                "you": {
                    "location": "exit",
                    "activity": "idle",
                    "mood": 0,
                    "energy": 100,
                    "location_context": "library",
                    "onboardingPhase": "alive",
                },
                "nearby_agents": [],
                "think": False,
            }
        )
        assert daemon.state["agent"]["location"] == "library"

    def test_world_update_passes_real_location_through(self, daemon: TownDaemon):
        daemon._pending_move_destination = "cafe"
        daemon._handle_world_update(
            {
                "you": {
                    "location": "cafe",
                    "activity": "idle",
                    "mood": 0,
                    "energy": 100,
                    "location_context": "town",
                    "onboardingPhase": "alive",
                },
                "nearby_agents": [],
                "think": False,
            }
        )
        assert daemon.state["agent"]["location"] == "cafe"


# ---------------------------------------------------------------------------
# X — handle_tool_command rejects move while in conversation
# ---------------------------------------------------------------------------


class TestMoveRejectedWhileInConversation:
    @pytest.mark.asyncio
    async def test_move_rejected_when_active_conversation_set(self, daemon: TownDaemon):
        """When the agent's heartbeat loop calls ``act move`` mid-conversation,
        the daemon must refuse with a structured rejection — don't yank the
        agent away from the conversation."""
        daemon.ws = AsyncMock()
        daemon._initial_state_event = asyncio.Event()
        daemon._arrived_event = asyncio.Event()
        daemon.state["active_conversation"] = {
            "conv_id": "c-active",
            "participants": ["other"],
            "turns": [],
        }

        result = await daemon.handle_tool_command(
            {"action": "act", "payload": {"action": "move", "destination": "library"}}
        )
        assert result["status"] == "rejected"
        assert result["reason"] == "in_conversation"
        assert result["conv_id"] == "c-active"
        # And we did NOT actually send the move via the WS.
        daemon.ws.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_move_proceeds_when_no_active_conversation(self, daemon: TownDaemon):
        """Sanity: with no active conversation, move proceeds normally
        (sends the WS message; eventually times out since we don't simulate
        an arrival event in this unit test)."""
        daemon.ws = AsyncMock()
        daemon._initial_state_event = asyncio.Event()
        daemon._arrived_event = asyncio.Event()
        daemon.state.pop("active_conversation", None)

        async def _fast_timeout(*_a, **_kw):
            raise asyncio.TimeoutError

        # Patch wait_for to return immediately so we don't actually wait 120s.
        import goosetown.daemon as daemon_mod

        original_wait_for = asyncio.wait_for
        asyncio.wait_for = _fast_timeout  # type: ignore[assignment]
        try:
            result = await daemon.handle_tool_command(
                {
                    "action": "act",
                    "payload": {"action": "move", "destination": "library"},
                }
            )
        finally:
            asyncio.wait_for = original_wait_for  # type: ignore[assignment]

        # Got past the in-conversation guard and actually sent.
        daemon.ws.send.assert_called_once()
        # Outcome is timeout (we never delivered an arrival), not "rejected".
        assert result.get("status") in ("timeout",)
        # Sanity: assigning daemon_mod just to keep import live.
        assert daemon_mod is not None
