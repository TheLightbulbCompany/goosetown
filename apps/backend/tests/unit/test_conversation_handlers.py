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
# Test UUIDs — agent IDs must be UUIDs (fix #1: names are not globally unique)
# ---------------------------------------------------------------------------

# A valid UUID used as the "target agent" in _handle_chat tests.
TARGET_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

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
# `_apply_event_with_sleep_guard` was extracted to core.services.daemon_bridge
# and is now imported at module-top of conversation_service (sub-project #3
# fourth slice, 2026-04-22). Patch the module-top binding in conversation_service,
# not the re-export or the defining module.
APPLY_EVENT_PATH = "core.services.conversation_service._apply_event_with_sleep_guard"


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
                {"target": TARGET_UUID, "message": "   "},
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
                {"target": TARGET_UUID, "message": "hi"},
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
                {"target": TARGET_UUID, "message": "hi"},
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
                {"target": TARGET_UUID, "message": "hi"},
                _make_agent_conn(),
            )

        assert sent[0][1]["reason"] == "target_not_found"

    @pytest.mark.asyncio
    async def test_target_inactive_rejects(self):
        from routers.town import _handle_chat

        sent: list = []
        initiator = _make_agent(agent_id="agent-init")
        inactive_target = _make_agent(agent_id=TARGET_UUID, is_active="false")
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
                {"target": TARGET_UUID, "message": "hi"},
                _make_agent_conn(),
            )

        assert sent[0][1]["reason"] == "target_not_found"

    @pytest.mark.asyncio
    async def test_cannot_chat_with_self(self):
        from routers.town import _handle_chat

        # Use a UUID as the agent_id so the UUID check passes before reaching
        # the self-chat check.
        self_uuid = "11111111-2222-3333-4444-555555555555"
        sent: list = []
        initiator = _make_agent(agent_id=self_uuid)
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
                {"target": self_uuid, "message": "hi"},
                _make_agent_conn(agent_id=self_uuid),
            )

        assert sent[0][1]["reason"] == "cannot_chat_with_self"

    @pytest.mark.asyncio
    async def test_target_busy_rejects(self):
        from routers.town import _handle_chat

        sent: list = []
        initiator = _make_agent(agent_id="agent-init")
        busy_target = _make_agent(agent_id=TARGET_UUID, current_conversation_id="conv-x")
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
                {"target": TARGET_UUID, "message": "hi"},
                _make_agent_conn(),
            )

        assert sent[0][1]["reason"] == "target_busy"

    @pytest.mark.asyncio
    async def test_not_nearby_via_cached_positions_rejects(self):
        from routers.town import _handle_chat

        sent: list = []
        initiator = _make_agent(agent_id="agent-init")
        target = _make_agent(agent_id=TARGET_UUID)
        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.side_effect = [initiator, target]
        # Far apart: dx=500 >> 96 threshold. Target conn present so
        # target_not_connected check passes; not_nearby fires instead.
        target_conn = SimpleNamespace(agent_id=TARGET_UUID, connection_id="conn-target", agent_name="T")
        ws_mock = _make_ws_manager_mock({TARGET_UUID: target_conn})
        gs_mock = _make_gs_mock(
            cached_states={
                "agent-init": {"x": 0, "y": 0},
                TARGET_UUID: {"x": 500, "y": 0},
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
                {"target": TARGET_UUID, "message": "hi"},
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
        target = _make_agent(agent_id=TARGET_UUID, current_location="library")
        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.side_effect = [initiator, target]
        # Target connected — so target_not_connected check passes.
        target_conn = SimpleNamespace(agent_id=TARGET_UUID, connection_id="conn-target", agent_name="T")
        ws_mock = _make_ws_manager_mock({TARGET_UUID: target_conn})
        # No cached positions → falls back to location check
        gs_mock = _make_gs_mock(cached_states={})

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=ws_mock),
            patch(GS_CLIENT_PATH, return_value=gs_mock),
        ):
            await _handle_chat(
                "conn-abc",
                {"target": TARGET_UUID, "message": "hi"},
                _make_agent_conn(),
            )

        assert sent[0][1]["reason"] == "not_nearby"

    @pytest.mark.asyncio
    async def test_happy_path_creates_conversation_and_notifies_target(self):
        """Valid chat creates conversation, invites target, acks initiator."""
        from routers.town import _handle_chat

        sent: list = []
        initiator = _make_agent(agent_id="agent-init", current_location="cafe")
        target = _make_agent(agent_id=TARGET_UUID, display_name="Marbles", current_location="cafe")
        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.side_effect = [initiator, target]
        svc_mock.create_active_conversation.return_value = {"id": "conv-new"}

        # Target is connected; give its WS mgr entry
        target_conn = SimpleNamespace(agent_id=TARGET_UUID, connection_id="conn-target", agent_name="Marbles")
        ws_mock = _make_ws_manager_mock({TARGET_UUID: target_conn})

        gs_mock = _make_gs_mock(
            cached_states={
                "agent-init": {"x": 10, "y": 0},
                TARGET_UUID: {"x": 20, "y": 0},
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
                {"target": TARGET_UUID, "message": "howdy"},
                _make_agent_conn(),
            )

        # Conversation was created with correct args
        svc_mock.create_active_conversation.assert_called_once()
        call_kwargs = svc_mock.create_active_conversation.call_args.kwargs
        assert call_kwargs["initiator_id"] == "agent-init"
        assert call_kwargs["target_id"] == TARGET_UUID
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
        """If target is not connected, conversation still created, no invite sent.

        NOTE: This test exercises the OLD behaviour (pre-fix #2) via a legacy
        path where ws_manager still has no entry. After fix #2 the real handler
        now rejects if target is offline; this test is intentionally
        kept as documentation but is superseded by
        ``test_target_not_connected_rejects``.
        """
        from routers.town import _handle_chat

        sent: list = []
        initiator = _make_agent(agent_id="agent-init", current_location="cafe")
        # Use a valid UUID so the UUID-check passes
        target_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        target = _make_agent(agent_id=target_uuid, current_location="cafe")
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
                {"target": target_uuid, "message": "hi"},
                _make_agent_conn(),
            )

        # Fix #2: offline target is now rejected before conversation creation.
        svc_mock.create_active_conversation.assert_not_called()
        assert sent[0][1]["reason"] == "target_not_connected"

    # ------------------------------------------------------------------
    # Fix #1: UUID-only target validation.
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_target_name_instead_of_uuid_rejects(self):
        """Passing an agent name (not a UUID) as target is synchronously rejected.

        This is the new behaviour introduced by fix #1: names are not globally
        unique across accounts, so the backend now requires a UUID and rejects
        with ``target_must_be_uuid`` before any DB lookup.
        """
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
                {"target": "pam", "message": "Hey Pam!"},
                _make_agent_conn(),
            )

        assert len(sent) == 1
        assert sent[0][1]["event"] == "chat_rejected"
        assert sent[0][1]["reason"] == "target_must_be_uuid"
        # No DB calls — the UUID check is a pure string validation that fires
        # before any service lookup.
        svc_mock.get_town_agent_by_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_target_not_connected_rejects(self):
        """If target's daemon is offline, chat is rejected with target_not_connected.

        Fix #2: previously the backend created the conversation silently and the
        CLI got {ok}, but the invite was never delivered because the target had
        no WS connection.  Now we reject synchronously.
        """
        from routers.town import _handle_chat

        sent: list = []
        initiator = _make_agent(agent_id="agent-init", current_location="cafe")
        target_uuid = "12345678-1234-1234-1234-123456789abc"
        target = _make_agent(agent_id=target_uuid, current_location="cafe")
        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.side_effect = [initiator, target]

        # Target not connected — ws_manager returns None for their ID
        ws_mock = _make_ws_manager_mock({})
        gs_mock = _make_gs_mock(
            cached_states={
                "agent-init": {"x": 10, "y": 0},
                target_uuid: {"x": 20, "y": 0},
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
                {"target": target_uuid, "message": "hello"},
                _make_agent_conn(),
            )

        assert sent[0][1]["event"] == "chat_rejected"
        assert sent[0][1]["reason"] == "target_not_connected"
        svc_mock.create_active_conversation.assert_not_called()


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

        # Even when the conversation continues (≥2 remain), the leaver
        # MUST get a ``conversation_ended`` so their daemon clears its
        # local ``active_conversation`` cache. Otherwise their next
        # ``chat`` blocks with ``already_in_conversation``. Finding W
        # from the 2026-05-04 5-agent soak — confirmed by 5/5 agents.
        leaver_ended = [msg for cid, msg in sent if cid == "conn-A" and msg["event"] == "conversation_ended"]
        assert len(leaver_ended) == 1, f"leaver did not receive conversation_ended; got {[m['event'] for _, m in sent]}"
        assert leaver_ended[0]["reason"] == "left_by_self"

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

        # conversation_ended delivered to BOTH the leaver AND the remaining
        # participant. Pre-fix this only went to the remaining participant
        # (the leaver was filtered out of finalize's notify-loop because
        # ``leave_conversation`` had already removed them from the
        # post-leave participants list). Without this event the leaver's
        # daemon never cleared its local ``active_conversation`` and would
        # block all future ``chat`` calls with ``already_in_conversation``
        # for 2-10+ minutes (5/5 of the 5-agent soak hit this — finding W).
        ended_events = [(cid, msg) for cid, msg in sent if msg["event"] == "conversation_ended"]
        assert len(ended_events) == 2, (
            f"expected conversation_ended to both leaver and remaining; got {len(ended_events)}"
        )
        recipient_conns = {cid for cid, _ in ended_events}
        assert recipient_conns == {"conn-A", "conn-B"}
        # Leaver sees ``left_by_self`` (their own action's perspective);
        # remaining participant sees ``ended_by_participant`` (from
        # _finalize_conversation's notify-loop). Both clear the daemon's
        # local active_conversation cache via _on_conversation_ended.
        leaver_msg = next(msg for cid, msg in ended_events if cid == "conn-A")
        remaining_msg = next(msg for cid, msg in ended_events if cid == "conn-B")
        assert leaver_msg["reason"] == "left_by_self"
        assert remaining_msg["reason"] == "ended_by_participant"

        # Relationship affinity bumped
        svc_mock.update_relationship.assert_called_once_with("agent-A", "agent-B", affinity_delta=5)


# ---------------------------------------------------------------------------
# Silence auto-end timer (``_schedule_inactivity_timeout`` +
# ``_inactivity_timeout``). These tests verify that the silence window
# tracks the "last activity" on a conversation and auto-finalises when
# nobody has spoken for ``_INACTIVITY_TIMEOUT_SEC``.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=False)
def _clear_inactivity_tasks():
    """Reset the module-level inactivity task registry before and after."""
    from core.services import conversation_service as cs

    for t in list(cs._inactivity_tasks.values()):
        t.cancel()
    cs._inactivity_tasks.clear()
    yield
    for t in list(cs._inactivity_tasks.values()):
        t.cancel()
    cs._inactivity_tasks.clear()


class TestInactivityTimeout:
    """The silence-auto-end timer schedules on every chat/say/join, cancels
    on end, and fires ``_finalize_conversation`` with reason ``inactivity``
    when the window elapses with no new activity."""

    @pytest.mark.asyncio
    async def test_chat_schedules_timer(self, _clear_inactivity_tasks):
        """A successful ``chat`` should leave an entry in
        ``_inactivity_tasks`` keyed by the new conv_id."""
        from core.services import conversation_service as cs
        from routers.town import _handle_chat

        sent: list = []
        initiator = _make_agent(agent_id="agent-init", current_location="cafe")
        target = _make_agent(agent_id=TARGET_UUID, display_name="M", current_location="cafe")
        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.side_effect = [initiator, target]
        svc_mock.create_active_conversation.return_value = {"id": "conv-new"}

        target_conn = SimpleNamespace(agent_id=TARGET_UUID, connection_id="conn-target", agent_name="M")
        ws_mock = _make_ws_manager_mock({TARGET_UUID: target_conn})

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=ws_mock),
            patch(GS_CLIENT_PATH, return_value=_make_gs_mock()),
        ):
            await _handle_chat(
                "conn-abc",
                {"target": TARGET_UUID, "message": "hey"},
                _make_agent_conn(),
            )

        assert "conv-new" in cs._inactivity_tasks
        task = cs._inactivity_tasks["conv-new"]
        assert not task.done(), "timer should be scheduled and running"

    @pytest.mark.asyncio
    async def test_say_replaces_timer(self, _clear_inactivity_tasks):
        """Every ``say`` should cancel the previous timer and schedule a new
        one so the silence window is measured from the most recent turn."""
        from core.services import conversation_service as cs
        from routers.town import _handle_say

        sent: list = []
        svc_mock = MagicMock()
        svc_mock.get_conversation_by_id.return_value = {
            "id": "conv-1",
            "status": "active",
            "participants": ["agent-A", "agent-B"],
            "participantAId": "agent-A",
            "participantBId": "agent-B",
        }
        svc_mock.append_message_to_conversation.return_value = {"turnCount": 3}
        svc_mock.get_town_agent_by_id.return_value = _make_agent()

        # Pre-seed a timer so we can verify it gets replaced, not stacked.
        cs._schedule_inactivity_timeout("conv-1")
        first_task = cs._inactivity_tasks["conv-1"]

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(
                WS_MANAGER_PATH,
                return_value=_make_ws_manager_mock(
                    {"agent-B": SimpleNamespace(agent_id="agent-B", connection_id="c-b")}
                ),
            ),
            patch(GS_CLIENT_PATH, return_value=_make_gs_mock()),
        ):
            await _handle_say(
                "conn-A",
                {"conv_id": "conv-1", "message": "still here"},
                _make_agent_conn(agent_id="agent-A"),
            )

        # The OLD task was cancelled and replaced by a fresh one.
        import asyncio

        await asyncio.sleep(0)  # let cancel propagate
        assert first_task.cancelled() or first_task.done()
        new_task = cs._inactivity_tasks["conv-1"]
        assert new_task is not first_task
        assert not new_task.done()

    @pytest.mark.asyncio
    async def test_end_conversation_cancels_timer(self, _clear_inactivity_tasks):
        """Explicit ``end_conversation`` should cancel the silence timer so
        the auto-end path doesn't fire after the agent has already left."""
        from core.services import conversation_service as cs
        from routers.town import _handle_end_conversation

        sent: list = []
        svc_mock = MagicMock()
        svc_mock.get_conversation_by_id.return_value = {
            "id": "conv-1",
            "status": "active",
            "participants": ["agent-A", "agent-B"],
            "participantAId": "agent-A",
            "participantBId": "agent-B",
        }
        svc_mock.leave_conversation.return_value = 1  # 1 remaining → triggers finalise
        svc_mock.end_conversation.return_value = {
            "id": "conv-1",
            "participants": ["agent-A", "agent-B"],
            "participantAId": "agent-A",
            "participantBId": "agent-B",
            "publicLog": [{"speaker": "A", "text": "hi"}],
        }
        svc_mock.get_town_agent_by_id.return_value = _make_agent()

        b_conn = SimpleNamespace(agent_id="agent-B", connection_id="conn-B")
        ws_mock = _make_ws_manager_mock({"agent-B": b_conn})

        # Pre-seed a live timer.
        cs._schedule_inactivity_timeout("conv-1")
        scheduled_task = cs._inactivity_tasks["conv-1"]

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=ws_mock),
            patch(GS_CLIENT_PATH, return_value=_make_gs_mock()),
            patch(APPLY_EVENT_PATH),
        ):
            await _handle_end_conversation(
                "conn-A",
                {"conv_id": "conv-1"},
                _make_agent_conn(agent_id="agent-A"),
            )

        assert "conv-1" not in cs._inactivity_tasks
        # Give the cancelled coroutine a tick to notice cancellation
        import asyncio

        await asyncio.sleep(0)
        assert scheduled_task.cancelled() or scheduled_task.done()

    @pytest.mark.asyncio
    async def test_timeout_fires_finalises_with_inactivity_reason(self, _clear_inactivity_tasks, monkeypatch):
        """When the silence window elapses, the timer should call
        ``_finalize_conversation`` with ``end_reason_override="inactivity"``."""
        from core.services import conversation_service as cs

        # Shrink the window so the test doesn't actually wait 60s.
        monkeypatch.setattr(cs, "_INACTIVITY_TIMEOUT_SEC", 0.01)

        finalize_calls: list = []

        async def _fake_finalize(*args, **kwargs) -> None:
            finalize_calls.append((args, kwargs))

        monkeypatch.setattr(cs, "_finalize_conversation", _fake_finalize)

        # get_conversation_by_id must report the conv as still active at
        # fire-time so _inactivity_timeout proceeds to finalise.
        svc_mock = MagicMock()
        svc_mock.get_conversation_by_id.return_value = {
            "id": "conv-1",
            "status": "active",
        }

        with (
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock()),
            patch(MGMT_PATH, return_value=_make_mgmt_mock([])),
        ):
            cs._schedule_inactivity_timeout("conv-1")
            task = cs._inactivity_tasks["conv-1"]
            await task

        assert len(finalize_calls) == 1
        _args, kwargs = finalize_calls[0]
        assert kwargs.get("end_reason_override") == "inactivity"
        assert kwargs.get("auto_ended") is True

    @pytest.mark.asyncio
    async def test_timeout_noop_if_conv_already_ended(self, _clear_inactivity_tasks, monkeypatch):
        """If the conversation was finalised by another path (explicit end,
        turn limit) before the timer fires, the timer should do nothing —
        no second ``_finalize_conversation`` call."""
        from core.services import conversation_service as cs

        monkeypatch.setattr(cs, "_INACTIVITY_TIMEOUT_SEC", 0.01)

        finalize_calls: list = []

        async def _fake_finalize(*args, **kwargs) -> None:
            finalize_calls.append((args, kwargs))

        monkeypatch.setattr(cs, "_finalize_conversation", _fake_finalize)

        svc_mock = MagicMock()
        # Conversation already ended in DB by the time the timer fires.
        svc_mock.get_conversation_by_id.return_value = {
            "id": "conv-1",
            "status": "ended",
        }

        with (
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock()),
            patch(MGMT_PATH, return_value=_make_mgmt_mock([])),
        ):
            cs._schedule_inactivity_timeout("conv-1")
            task = cs._inactivity_tasks["conv-1"]
            await task

        assert finalize_calls == [], "must not double-finalise an ended conv"


# ---------------------------------------------------------------------------
# Inactivity-timeout env-var override (``CONVERSATION_INACTIVITY_TIMEOUT_SEC``).
# Read once at module load. The reader is tested directly to avoid having to
# reimport the module under monkeypatched env, which races with other tests
# using ``cs._INACTIVITY_TIMEOUT_SEC`` directly.
# ---------------------------------------------------------------------------


class TestInactivityTimeoutEnvVar:
    """``_read_inactivity_timeout`` parses the env override safely."""

    def test_default_when_unset(self, monkeypatch):
        from core.services import conversation_service as cs

        monkeypatch.delenv("CONVERSATION_INACTIVITY_TIMEOUT_SEC", raising=False)
        assert cs._read_inactivity_timeout() == cs._DEFAULT_INACTIVITY_TIMEOUT_SEC

    def test_default_is_180_seconds(self):
        from core.services import conversation_service as cs

        # 60s was too short for slow-model two-turn exchanges. 180s is the new
        # baseline; if this regresses, the soak window will start auto-closing
        # mid-conversation again.
        assert cs._DEFAULT_INACTIVITY_TIMEOUT_SEC == 180.0

    def test_env_override_accepts_positive_float(self, monkeypatch):
        from core.services import conversation_service as cs

        monkeypatch.setenv("CONVERSATION_INACTIVITY_TIMEOUT_SEC", "240.5")
        assert cs._read_inactivity_timeout() == 240.5

    def test_env_override_falls_back_on_garbage(self, monkeypatch):
        from core.services import conversation_service as cs

        monkeypatch.setenv("CONVERSATION_INACTIVITY_TIMEOUT_SEC", "not-a-number")
        assert cs._read_inactivity_timeout() == cs._DEFAULT_INACTIVITY_TIMEOUT_SEC

    def test_env_override_falls_back_on_zero(self, monkeypatch):
        from core.services import conversation_service as cs

        # A zero or negative timeout would cause the inactivity coroutine to
        # fire immediately on every turn — bad. Treat as garbage and fall back.
        monkeypatch.setenv("CONVERSATION_INACTIVITY_TIMEOUT_SEC", "0")
        assert cs._read_inactivity_timeout() == cs._DEFAULT_INACTIVITY_TIMEOUT_SEC
