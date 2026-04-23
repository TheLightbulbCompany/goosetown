"""Unit tests for the WebSocket conversation handlers in ``routers/town.py``.

Covers the four action handlers that make up the agent-to-agent conversation
flow:

* ``_handle_chat`` — initiate a new conversation.
* ``_handle_say`` — speak in an active conversation (including auto-end at
  the turn limit).
* ``_handle_join_conversation`` — a third agent joins an existing active
  conversation.
* ``_handle_end_conversation`` — an agent leaves; when <2 remain the
  conversation is finalised (affinity + mood applied to participants).

Every handler locally imports ``ManagementApiClient`` and reads
``TownService`` / ``get_game_server_client`` / ``get_town_agent_ws_manager``
from the module scope of ``routers.town``, so tests patch at those source
paths to intercept correctly. Follows the mocking style established in
``test_context_transitions.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Patch targets (mirrors test_context_transitions.py).
# ---------------------------------------------------------------------------

MGMT_PATH = "core.services.management_api_client.ManagementApiClient"
# Handlers moved to core.services.conversation_service (sub-project #3,
# 2026-04-22); module-top imports there are what need patching now, not
# the re-exports in routers.town.
SERVICE_PATH = "core.services.conversation_service.TownService"
GS_CLIENT_PATH = "core.services.conversation_service.get_game_server_client"
WS_MANAGER_PATH = "core.services.conversation_service.get_town_agent_ws_manager"
# `_apply_event_with_sleep_guard` is still defined in routers.town and
# deferred-imported inside `_finalize_conversation`, so the patch source
# remains at routers.town.
APPLY_EVENT_PATH = "routers.town._apply_event_with_sleep_guard"


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _make_agent_conn(
    agent_id: str = "agent-init",
    agent_name: str = "Lucky",
) -> SimpleNamespace:
    """Build a minimal agent_conn object matching TownAgentWsManager records."""
    return SimpleNamespace(
        agent_id=agent_id,
        connection_id="conn-abc",
        user_id="user-1",
        agent_name=agent_name,
        location_context="town",
    )


def _make_agent(
    agent_id: str = "agent-init",
    display_name: str = "Lucky",
    current_location: str = "plaza",
    current_conversation_id: str | None = None,
    is_active: str = "true",
) -> dict:
    return {
        "id": agent_id,
        "displayName": display_name,
        "currentLocation": current_location,
        "currentConversationId": current_conversation_id,
        "isActive": is_active,
        "locationContext": "town",
    }


def _make_mgmt_mock(sent: list) -> MagicMock:
    mock = MagicMock()
    mock.send_message.side_effect = lambda cid, msg: sent.append((cid, msg))
    return mock


def _make_ws_manager_mock(agent_conn_by_id: dict | None = None) -> MagicMock:
    mock = MagicMock()
    mapping = agent_conn_by_id or {}
    mock.get_by_agent_id.side_effect = lambda aid: mapping.get(aid)
    return mock


def _make_gs_mock(cached_states: dict | None = None) -> MagicMock:
    mock = MagicMock()
    states = cached_states or {}
    mock.get_cached_agent_state.side_effect = lambda aid: states.get(aid)
    mock.send_command = AsyncMock()
    return mock


# ---------------------------------------------------------------------------
# _handle_chat.
# ---------------------------------------------------------------------------


class TestHandleChat:
    @pytest.mark.asyncio
    async def test_missing_target_rejects(self):
        from routers.town import _handle_chat

        sent: list = []
        svc_mock = MagicMock()

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock()),
            patch(GS_CLIENT_PATH, return_value=_make_gs_mock()),
        ):
            await _handle_chat(
                "conn-abc",
                {"target": "", "message": "hi"},
                _make_agent_conn(),
            )

        assert len(sent) == 1
        assert sent[0][1]["event"] == "chat_rejected"
        assert sent[0][1]["reason"] == "missing_target_or_message"
        svc_mock.get_town_agent_by_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_message_rejects(self):
        from routers.town import _handle_chat

        sent: list = []
        svc_mock = MagicMock()

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock()),
            patch(GS_CLIENT_PATH, return_value=_make_gs_mock()),
        ):
            await _handle_chat(
                "conn-abc",
                {"target": "agent-2", "message": "   "},
                _make_agent_conn(),
            )

        assert sent[0][1]["reason"] == "missing_target_or_message"

    @pytest.mark.asyncio
    async def test_initiator_not_found_rejects(self):
        from routers.town import _handle_chat

        sent: list = []
        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.return_value = None

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock()),
            patch(GS_CLIENT_PATH, return_value=_make_gs_mock()),
        ):
            await _handle_chat(
                "conn-abc",
                {"target": "agent-2", "message": "hi"},
                _make_agent_conn(),
            )

        assert sent[0][1]["reason"] == "initiator_not_found"
        svc_mock.create_active_conversation.assert_not_called()

    @pytest.mark.asyncio
    async def test_initiator_already_in_conversation_rejects(self):
        from routers.town import _handle_chat

        sent: list = []
        initiator = _make_agent(current_conversation_id="conv-existing")
        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.return_value = initiator

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock()),
            patch(GS_CLIENT_PATH, return_value=_make_gs_mock()),
        ):
            await _handle_chat(
                "conn-abc",
                {"target": "agent-2", "message": "hi"},
                _make_agent_conn(),
            )

        assert sent[0][1]["reason"] == "already_in_conversation"

    @pytest.mark.asyncio
    async def test_target_not_found_rejects(self):
        from routers.town import _handle_chat

        sent: list = []
        initiator = _make_agent(agent_id="agent-init")
        svc_mock = MagicMock()
        # First call returns initiator, second returns None (target missing)
        svc_mock.get_town_agent_by_id.side_effect = [initiator, None]

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock()),
            patch(GS_CLIENT_PATH, return_value=_make_gs_mock()),
        ):
            await _handle_chat(
                "conn-abc",
                {"target": "agent-2", "message": "hi"},
                _make_agent_conn(),
            )

        assert sent[0][1]["reason"] == "target_not_found"

    @pytest.mark.asyncio
    async def test_target_inactive_rejects(self):
        from routers.town import _handle_chat

        sent: list = []
        initiator = _make_agent(agent_id="agent-init")
        inactive_target = _make_agent(agent_id="agent-2", is_active="false")
        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.side_effect = [initiator, inactive_target]

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock()),
            patch(GS_CLIENT_PATH, return_value=_make_gs_mock()),
        ):
            await _handle_chat(
                "conn-abc",
                {"target": "agent-2", "message": "hi"},
                _make_agent_conn(),
            )

        assert sent[0][1]["reason"] == "target_not_found"

    @pytest.mark.asyncio
    async def test_cannot_chat_with_self(self):
        from routers.town import _handle_chat

        sent: list = []
        initiator = _make_agent(agent_id="agent-init")
        # Same id for target — target returned is the initiator itself
        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.side_effect = [initiator, initiator]

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock()),
            patch(GS_CLIENT_PATH, return_value=_make_gs_mock()),
        ):
            await _handle_chat(
                "conn-abc",
                {"target": "agent-init", "message": "hi"},
                _make_agent_conn(),
            )

        assert sent[0][1]["reason"] == "cannot_chat_with_self"

    @pytest.mark.asyncio
    async def test_target_busy_rejects(self):
        from routers.town import _handle_chat

        sent: list = []
        initiator = _make_agent(agent_id="agent-init")
        busy_target = _make_agent(agent_id="agent-2", current_conversation_id="conv-x")
        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.side_effect = [initiator, busy_target]

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock()),
            patch(GS_CLIENT_PATH, return_value=_make_gs_mock()),
        ):
            await _handle_chat(
                "conn-abc",
                {"target": "agent-2", "message": "hi"},
                _make_agent_conn(),
            )

        assert sent[0][1]["reason"] == "target_busy"

    @pytest.mark.asyncio
    async def test_not_nearby_via_cached_positions_rejects(self):
        from routers.town import _handle_chat

        sent: list = []
        initiator = _make_agent(agent_id="agent-init")
        target = _make_agent(agent_id="agent-2")
        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.side_effect = [initiator, target]
        # Far apart: dx=500 >> 96 threshold
        gs_mock = _make_gs_mock(
            cached_states={
                "agent-init": {"x": 0, "y": 0},
                "agent-2": {"x": 500, "y": 0},
            }
        )

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock()),
            patch(GS_CLIENT_PATH, return_value=gs_mock),
        ):
            await _handle_chat(
                "conn-abc",
                {"target": "agent-2", "message": "hi"},
                _make_agent_conn(),
            )

        assert sent[0][1]["reason"] == "not_nearby"
        svc_mock.create_active_conversation.assert_not_called()

    @pytest.mark.asyncio
    async def test_not_nearby_via_location_fallback_rejects(self):
        """When no cached position exists, falls back to location equality."""
        from routers.town import _handle_chat

        sent: list = []
        initiator = _make_agent(agent_id="agent-init", current_location="plaza")
        target = _make_agent(agent_id="agent-2", current_location="library")
        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.side_effect = [initiator, target]
        # No cached states → falls back to location check
        gs_mock = _make_gs_mock(cached_states={})

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock()),
            patch(GS_CLIENT_PATH, return_value=gs_mock),
        ):
            await _handle_chat(
                "conn-abc",
                {"target": "agent-2", "message": "hi"},
                _make_agent_conn(),
            )

        assert sent[0][1]["reason"] == "not_nearby"

    @pytest.mark.asyncio
    async def test_happy_path_creates_conversation_and_notifies_target(self):
        """Valid chat creates conversation, invites target, acks initiator."""
        from routers.town import _handle_chat

        sent: list = []
        initiator = _make_agent(agent_id="agent-init", current_location="cafe")
        target = _make_agent(agent_id="agent-2", display_name="Marbles", current_location="cafe")
        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.side_effect = [initiator, target]
        svc_mock.create_active_conversation.return_value = {"id": "conv-new"}

        # Target is connected; give its WS mgr entry
        target_conn = SimpleNamespace(agent_id="agent-2", connection_id="conn-target", agent_name="Marbles")
        ws_mock = _make_ws_manager_mock({"agent-2": target_conn})

        gs_mock = _make_gs_mock(
            cached_states={
                "agent-init": {"x": 10, "y": 0},
                "agent-2": {"x": 20, "y": 0},
            }
        )

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=ws_mock),
            patch(GS_CLIENT_PATH, return_value=gs_mock),
        ):
            await _handle_chat(
                "conn-abc",
                {"target": "agent-2", "message": "howdy"},
                _make_agent_conn(),
            )

        # Conversation was created with correct args
        svc_mock.create_active_conversation.assert_called_once()
        call_kwargs = svc_mock.create_active_conversation.call_args.kwargs
        assert call_kwargs["initiator_id"] == "agent-init"
        assert call_kwargs["target_id"] == "agent-2"
        assert call_kwargs["opening_message"] == "howdy"
        assert call_kwargs["location"] == "cafe"

        # Three mgmt sends: invite to target, conversation_started + act_ok to initiator.
        # conversation_started (F11) is what makes the initiator's daemon
        # populate active_conversation with the conv_id and opening line —
        # without it, the initiator has no local view of their own outgoing chat.
        recipients = [cid for cid, _ in sent]
        events = [msg["event"] for _, msg in sent]
        assert "conversation_invite" in events
        assert "conversation_started" in events
        assert "act_ok" in events
        assert "conn-target" in recipients
        assert "conn-abc" in recipients

        invite_msg = next(m for _, m in sent if m["event"] == "conversation_invite")
        assert invite_msg["conv_id"] == "conv-new"
        assert invite_msg["message"] == "howdy"

        started_msg = next(m for _, m in sent if m["event"] == "conversation_started")
        assert started_msg["conv_id"] == "conv-new"
        assert started_msg["opening_message"] == "howdy"
        assert started_msg["with_display"] == "Marbles"

    @pytest.mark.asyncio
    async def test_happy_path_target_offline_still_creates_conversation(self):
        """If target is not connected, conversation still created, no invite sent."""
        from routers.town import _handle_chat

        sent: list = []
        initiator = _make_agent(agent_id="agent-init", current_location="cafe")
        target = _make_agent(agent_id="agent-2", current_location="cafe")
        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.side_effect = [initiator, target]
        svc_mock.create_active_conversation.return_value = {"id": "conv-new"}

        ws_mock = _make_ws_manager_mock({})  # target not connected
        gs_mock = _make_gs_mock(cached_states={})

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=ws_mock),
            patch(GS_CLIENT_PATH, return_value=gs_mock),
        ):
            await _handle_chat(
                "conn-abc",
                {"target": "agent-2", "message": "hi"},
                _make_agent_conn(),
            )

        svc_mock.create_active_conversation.assert_called_once()
        events = [msg["event"] for _, msg in sent]
        assert "conversation_invite" not in events
        assert "act_ok" in events  # initiator still acknowledged


# ---------------------------------------------------------------------------
# _handle_say.
# ---------------------------------------------------------------------------


class TestHandleSay:
    @pytest.mark.asyncio
    async def test_missing_conv_id_rejects(self):
        from routers.town import _handle_say

        sent: list = []
        svc_mock = MagicMock()

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock()),
            patch(GS_CLIENT_PATH, return_value=_make_gs_mock()),
        ):
            await _handle_say(
                "conn-abc",
                {"conv_id": "", "message": "hi"},
                _make_agent_conn(),
            )

        assert sent[0][1]["event"] == "say_rejected"
        assert sent[0][1]["reason"] == "missing_conv_id_or_message"
        svc_mock.get_conversation_by_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_conversation_not_found_rejects(self):
        from routers.town import _handle_say

        sent: list = []
        svc_mock = MagicMock()
        svc_mock.get_conversation_by_id.return_value = None

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock()),
            patch(GS_CLIENT_PATH, return_value=_make_gs_mock()),
        ):
            await _handle_say(
                "conn-abc",
                {"conv_id": "conv-1", "message": "hi"},
                _make_agent_conn(),
            )

        assert sent[0][1]["reason"] == "conversation_not_found"

    @pytest.mark.asyncio
    async def test_conversation_not_active_rejects(self):
        from routers.town import _handle_say

        sent: list = []
        svc_mock = MagicMock()
        svc_mock.get_conversation_by_id.return_value = {"id": "conv-1", "status": "ended"}

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock()),
            patch(GS_CLIENT_PATH, return_value=_make_gs_mock()),
        ):
            await _handle_say(
                "conn-abc",
                {"conv_id": "conv-1", "message": "hi"},
                _make_agent_conn(),
            )

        assert sent[0][1]["reason"] == "conversation_not_active"

    @pytest.mark.asyncio
    async def test_not_a_participant_rejects(self):
        from routers.town import _handle_say

        sent: list = []
        svc_mock = MagicMock()
        svc_mock.get_conversation_by_id.return_value = {
            "id": "conv-1",
            "status": "active",
            "participants": ["agent-A", "agent-B"],
        }

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock()),
            patch(GS_CLIENT_PATH, return_value=_make_gs_mock()),
        ):
            await _handle_say(
                "conn-abc",
                {"conv_id": "conv-1", "message": "hi"},
                _make_agent_conn(agent_id="agent-X"),
            )

        assert sent[0][1]["reason"] == "not_a_participant"

    @pytest.mark.asyncio
    async def test_happy_path_forwards_to_other_participant(self):
        """Speaker's message is appended and delivered to the other agent."""
        from routers.town import _handle_say

        sent: list = []
        svc_mock = MagicMock()
        svc_mock.get_conversation_by_id.return_value = {
            "id": "conv-1",
            "status": "active",
            "participants": ["agent-A", "agent-B"],
        }
        svc_mock.get_town_agent_by_id.return_value = _make_agent(agent_id="agent-A", display_name="Alice")
        svc_mock.append_message_to_conversation.return_value = {"turnCount": 3}

        other_conn = SimpleNamespace(agent_id="agent-B", connection_id="conn-B", agent_name="Bob")
        ws_mock = _make_ws_manager_mock({"agent-B": other_conn})

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=ws_mock),
            patch(GS_CLIENT_PATH, return_value=_make_gs_mock()),
        ):
            await _handle_say(
                "conn-A",
                {"conv_id": "conv-1", "message": "hello there"},
                _make_agent_conn(agent_id="agent-A", agent_name="Alice"),
            )

        svc_mock.append_message_to_conversation.assert_called_once_with("conv-1", "agent-A", "Alice", "hello there")
        # Message forwarded to all participants, INCLUDING the speaker, so the
        # speaker's daemon can append their own turn to active_conversation.
        # F8 from 2026-04-21 dry run — without the self-echo, speakers couldn't
        # see their own messages in TOWN_STATUS.md. Since agent-A's conn isn't
        # in the ws_manager mock, only B is actually delivered to in this test
        # — but we assert the loop no longer skips the speaker by verifying
        # B's forward + checking the send path didn't filter on speaker_id.
        forwarded = [(cid, msg) for cid, msg in sent if msg["event"] == "conversation_message"]
        assert len(forwarded) == 1
        assert forwarded[0][0] == "conn-B"
        assert forwarded[0][1]["text"] == "hello there"
        assert forwarded[0][1]["turn"] == 3
        assert forwarded[0][1]["from"] == "Alice"

    @pytest.mark.asyncio
    async def test_speaker_receives_own_message_echo(self):
        """Speaker's connection gets the conversation_message echo too (F8)."""
        from routers.town import _handle_say

        sent: list = []
        svc_mock = MagicMock()
        svc_mock.get_conversation_by_id.return_value = {
            "id": "conv-1",
            "status": "active",
            "participants": ["agent-A", "agent-B"],
        }
        svc_mock.get_town_agent_by_id.return_value = _make_agent(agent_id="agent-A", display_name="Alice")
        svc_mock.append_message_to_conversation.return_value = {"turnCount": 2}

        # BOTH speaker and other connected — so we can verify speaker also
        # receives the conversation_message echo.
        speaker_conn = SimpleNamespace(agent_id="agent-A", connection_id="conn-A", agent_name="Alice")
        other_conn = SimpleNamespace(agent_id="agent-B", connection_id="conn-B", agent_name="Bob")
        ws_mock = _make_ws_manager_mock({"agent-A": speaker_conn, "agent-B": other_conn})

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=ws_mock),
            patch(GS_CLIENT_PATH, return_value=_make_gs_mock()),
        ):
            await _handle_say(
                "conn-A",
                {"conv_id": "conv-1", "message": "hi there"},
                _make_agent_conn(agent_id="agent-A", agent_name="Alice"),
            )

        forwarded = [(cid, msg) for cid, msg in sent if msg["event"] == "conversation_message"]
        recipients = {cid for cid, _ in forwarded}
        assert "conn-A" in recipients, "Speaker must receive own message echo (F8)"
        assert "conn-B" in recipients

    @pytest.mark.asyncio
    async def test_auto_end_at_turn_limit(self):
        """Turn count ≥ 20 triggers _finalize_conversation."""
        from routers.town import _MAX_CONVERSATION_TURNS, _handle_say

        sent: list = []
        svc_mock = MagicMock()
        svc_mock.get_conversation_by_id.return_value = {
            "id": "conv-1",
            "status": "active",
            "participants": ["agent-A", "agent-B"],
        }
        svc_mock.get_town_agent_by_id.return_value = _make_agent(agent_id="agent-A")
        svc_mock.append_message_to_conversation.return_value = {
            "turnCount": _MAX_CONVERSATION_TURNS,
        }
        # Finalise path
        svc_mock.end_conversation.return_value = {
            "id": "conv-1",
            "participants": ["agent-A", "agent-B"],
            "turnCount": _MAX_CONVERSATION_TURNS,
            "location": "cafe",
            "publicLog": [],
        }

        ws_mock = _make_ws_manager_mock({})

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=ws_mock),
            patch(GS_CLIENT_PATH, return_value=_make_gs_mock()),
            patch(APPLY_EVENT_PATH),  # avoid real mood-engine side effects
        ):
            await _handle_say(
                "conn-A",
                {"conv_id": "conv-1", "message": "final turn"},
                _make_agent_conn(agent_id="agent-A"),
            )

        svc_mock.end_conversation.assert_called_once_with("conv-1")


# ---------------------------------------------------------------------------
# _handle_join_conversation.
# ---------------------------------------------------------------------------


class TestHandleJoinConversation:
    @pytest.mark.asyncio
    async def test_missing_conv_id_rejects(self):
        from routers.town import _handle_join_conversation

        sent: list = []
        svc_mock = MagicMock()

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock()),
        ):
            await _handle_join_conversation("conn-abc", {"conv_id": ""}, _make_agent_conn())

        assert sent[0][1]["event"] == "join_rejected"
        assert sent[0][1]["reason"] == "missing_conv_id"

    @pytest.mark.asyncio
    async def test_joiner_not_found_rejects(self):
        from routers.town import _handle_join_conversation

        sent: list = []
        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.return_value = None

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock()),
        ):
            await _handle_join_conversation("conn-abc", {"conv_id": "conv-1"}, _make_agent_conn())

        assert sent[0][1]["reason"] == "joiner_not_found"

    @pytest.mark.asyncio
    async def test_joiner_already_in_conversation_rejects(self):
        from routers.town import _handle_join_conversation

        sent: list = []
        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.return_value = _make_agent(current_conversation_id="conv-other")

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock()),
        ):
            await _handle_join_conversation("conn-abc", {"conv_id": "conv-1"}, _make_agent_conn())

        assert sent[0][1]["reason"] == "already_in_conversation"
        svc_mock.join_conversation.assert_not_called()

    @pytest.mark.asyncio
    async def test_conversation_not_joinable_rejects(self):
        from routers.town import _handle_join_conversation

        sent: list = []
        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.return_value = _make_agent()
        svc_mock.join_conversation.return_value = None  # service says nope

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock()),
        ):
            await _handle_join_conversation("conn-abc", {"conv_id": "conv-1"}, _make_agent_conn())

        assert sent[0][1]["reason"] == "conversation_not_joinable"

    @pytest.mark.asyncio
    async def test_happy_path_notifies_others_and_acks_joiner(self):
        """Joiner gets act_ok; existing participants get participant_joined."""
        from routers.town import _handle_join_conversation

        sent: list = []
        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.return_value = _make_agent(agent_id="agent-C", display_name="Charlie")
        svc_mock.join_conversation.return_value = {
            "id": "conv-1",
            "participants": ["agent-A", "agent-B", "agent-C"],
        }

        a_conn = SimpleNamespace(agent_id="agent-A", connection_id="conn-A")
        b_conn = SimpleNamespace(agent_id="agent-B", connection_id="conn-B")
        ws_mock = _make_ws_manager_mock({"agent-A": a_conn, "agent-B": b_conn})

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=ws_mock),
        ):
            await _handle_join_conversation(
                "conn-C",
                {"conv_id": "conv-1"},
                _make_agent_conn(agent_id="agent-C", agent_name="Charlie"),
            )

        joined_events = [(cid, msg) for cid, msg in sent if msg["event"] == "participant_joined"]
        assert len(joined_events) == 2
        recipients = {cid for cid, _ in joined_events}
        assert recipients == {"conn-A", "conn-B"}
        for _, msg in joined_events:
            assert msg["agent_id"] == "agent-C"
            assert msg["display_name"] == "Charlie"

        ack = [msg for _, msg in sent if msg["event"] == "act_ok"]
        assert len(ack) == 1
        assert ack[0]["action"] == "join_conversation"


# ---------------------------------------------------------------------------
# _handle_end_conversation.
# ---------------------------------------------------------------------------


class TestHandleEndConversation:
    @pytest.mark.asyncio
    async def test_missing_conv_id_rejects(self):
        from routers.town import _handle_end_conversation

        sent: list = []
        svc_mock = MagicMock()

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock()),
            patch(GS_CLIENT_PATH, return_value=_make_gs_mock()),
        ):
            await _handle_end_conversation("conn-abc", {"conv_id": ""}, _make_agent_conn())

        assert sent[0][1]["event"] == "end_rejected"
        assert sent[0][1]["reason"] == "missing_conv_id"
        svc_mock.leave_conversation.assert_not_called()

    @pytest.mark.asyncio
    async def test_conversation_not_found_rejects(self):
        from routers.town import _handle_end_conversation

        sent: list = []
        svc_mock = MagicMock()
        svc_mock.get_conversation_by_id.return_value = None

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock()),
            patch(GS_CLIENT_PATH, return_value=_make_gs_mock()),
        ):
            await _handle_end_conversation("conn-abc", {"conv_id": "conv-1"}, _make_agent_conn())

        assert sent[0][1]["reason"] == "conversation_not_found"

    @pytest.mark.asyncio
    async def test_not_a_participant_rejects(self):
        from routers.town import _handle_end_conversation

        sent: list = []
        svc_mock = MagicMock()
        svc_mock.get_conversation_by_id.return_value = {
            "id": "conv-1",
            "status": "active",
            "participants": ["agent-A", "agent-B"],
        }

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock()),
            patch(GS_CLIENT_PATH, return_value=_make_gs_mock()),
        ):
            await _handle_end_conversation(
                "conn-abc",
                {"conv_id": "conv-1"},
                _make_agent_conn(agent_id="agent-X"),
            )

        assert sent[0][1]["reason"] == "not_a_participant"

    @pytest.mark.asyncio
    async def test_conversation_not_active_rejects(self):
        from routers.town import _handle_end_conversation

        sent: list = []
        svc_mock = MagicMock()
        svc_mock.get_conversation_by_id.return_value = {
            "id": "conv-1",
            "status": "ended",
            "participants": ["agent-A", "agent-B"],
        }

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock()),
            patch(GS_CLIENT_PATH, return_value=_make_gs_mock()),
        ):
            await _handle_end_conversation(
                "conn-abc",
                {"conv_id": "conv-1"},
                _make_agent_conn(agent_id="agent-A"),
            )

        assert sent[0][1]["reason"] == "conversation_not_active"

    @pytest.mark.asyncio
    async def test_leave_with_remaining_notifies_but_does_not_finalise(self):
        """If ≥2 agents remain after leave, no finalisation runs."""
        from routers.town import _handle_end_conversation

        sent: list = []
        svc_mock = MagicMock()
        svc_mock.get_conversation_by_id.return_value = {
            "id": "conv-1",
            "status": "active",
            "participants": ["agent-A", "agent-B", "agent-C"],
        }
        svc_mock.leave_conversation.return_value = 2  # still 2 remaining

        b_conn = SimpleNamespace(agent_id="agent-B", connection_id="conn-B")
        c_conn = SimpleNamespace(agent_id="agent-C", connection_id="conn-C")
        ws_mock = _make_ws_manager_mock({"agent-B": b_conn, "agent-C": c_conn})

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=ws_mock),
            patch(GS_CLIENT_PATH, return_value=_make_gs_mock()),
        ):
            await _handle_end_conversation(
                "conn-A",
                {"conv_id": "conv-1"},
                _make_agent_conn(agent_id="agent-A", agent_name="Alice"),
            )

        svc_mock.leave_conversation.assert_called_once_with("conv-1", "agent-A")
        svc_mock.end_conversation.assert_not_called()

        left_events = [(cid, msg) for cid, msg in sent if msg["event"] == "participant_left"]
        assert len(left_events) == 2
        assert {cid for cid, _ in left_events} == {"conn-B", "conn-C"}
        for _, msg in left_events:
            assert msg["agent_id"] == "agent-A"
            assert msg["remaining"] == 2

        ack = [msg for _, msg in sent if msg["event"] == "act_ok"]
        assert len(ack) == 1
        assert ack[0]["action"] == "end_conversation"

    @pytest.mark.asyncio
    async def test_leave_triggers_finalise_when_under_two_remain(self):
        """When leave_conversation returns <2, _finalize_conversation runs."""
        from routers.town import _handle_end_conversation

        sent: list = []
        svc_mock = MagicMock()
        svc_mock.get_conversation_by_id.return_value = {
            "id": "conv-1",
            "status": "active",
            "participants": ["agent-A", "agent-B"],
        }
        svc_mock.leave_conversation.return_value = 1  # 1 remaining → finalise
        svc_mock.end_conversation.return_value = {
            "id": "conv-1",
            "participants": ["agent-A", "agent-B"],
            "turnCount": 4,
            "location": "cafe",
            "publicLog": [{"speaker": "Alice", "text": "hi"}],
        }
        svc_mock.get_town_agent_by_id.return_value = _make_agent()

        b_conn = SimpleNamespace(agent_id="agent-B", connection_id="conn-B")
        ws_mock = _make_ws_manager_mock({"agent-B": b_conn})

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=ws_mock),
            patch(GS_CLIENT_PATH, return_value=_make_gs_mock()),
            patch(APPLY_EVENT_PATH) as apply_event_mock,
        ):
            await _handle_end_conversation(
                "conn-A",
                {"conv_id": "conv-1"},
                _make_agent_conn(agent_id="agent-A"),
            )

        svc_mock.end_conversation.assert_called_once_with("conv-1")
        # Mood event applied once per participant (2 participants)
        assert apply_event_mock.call_count == 2
        # conversation_ended delivered to remaining participant
        ended_events = [msg for _, msg in sent if msg["event"] == "conversation_ended"]
        assert len(ended_events) == 1
        assert ended_events[0]["reason"] == "ended_by_participant"
        # Relationship affinity bumped
        svc_mock.update_relationship.assert_called_once_with("agent-A", "agent-B", affinity_delta=5)
