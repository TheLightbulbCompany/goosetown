"""Unit tests for context transition logic (town ↔ apartment).

Tests ``_handle_context_transition`` and the ``_transition_to_apartment`` shim
in routers/town.py by mocking DaemonMailbox, TownService, GameServerClient,
and TownAgentWsManager at their source modules so
local-import statements in the router functions are intercepted correctly.

The two-phase transition flow:
1. Validate source context and busy state.
2. Set ``locationContext: "transitioning"`` + update ws manager.
3. Call ``transition_agent`` on the game server (awaited with timeout=15s).
4. On success: commit destination state, update ws manager, send
   ``context_changed``.
5. On failure/timeout: roll back DynamoDB + ws manager, send
   ``move_rejected``.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent_conn(agent_id: str = "agent-uuid-1") -> SimpleNamespace:
    """Build a minimal agent_conn object matching TownAgentWsManager records."""
    return SimpleNamespace(
        agent_id=agent_id,
        connection_id="conn-abc",
        user_id="user-1",
        agent_name="Lucky",
        location_context="town",
    )


def _make_agent(
    agent_id: str = "agent-uuid-1",
    location_context: str = "town",
    current_activity: str | None = None,
) -> dict:
    """Build a minimal agent DB record."""
    return {
        "id": agent_id,
        "locationContext": location_context,
        "currentLocation": "plaza",
        "currentActivity": current_activity,
        "mood": "neutral",
        "energy": 100,
        "displayName": "Lucky",
        "spriteUrl": "https://cdn.example.com/sprite.png",
    }


def _make_mgmt_mock(sent_messages: list) -> MagicMock:
    """Return a DaemonMailbox mock that captures sent messages."""
    mock = MagicMock()
    mock.notify_connection.side_effect = lambda cid, msg: sent_messages.append(msg)
    mock.reject.side_effect = lambda _cid, event, reason, **extra: sent_messages.append(
        {"event": event, "reason": reason, **extra}
    )
    return mock


def _make_ws_manager_mock() -> MagicMock:
    """Return a TownAgentWsManager mock with a no-op update_context."""
    mock = MagicMock()
    mock.update_context = MagicMock()
    return mock


# `_handle_context_transition` was extracted to
# ``core.services.context_transition_service`` in sub-project #3 sixth slice
# (2026-04-23). Its module-top imports (``TownService``,
# ``get_game_server_client``, ``get_town_agent_ws_manager``) are what need
# patching now — patching at the old ``routers.town.*`` names would only
# affect the re-exports, not the bindings the function actually reads.
MGMT_PATH = "core.services.context_transition_service.get_daemon_mailbox"
SERVICE_PATH = "core.services.context_transition_service.TownService"
GS_CLIENT_PATH = "core.services.context_transition_service.get_game_server_client"
WS_MANAGER_PATH = "core.services.context_transition_service.get_town_agent_ws_manager"


# ---------------------------------------------------------------------------
# town → apartment transition (residence)
# ---------------------------------------------------------------------------


class TestHandleContextTransitionToApartment:
    """Tests for ``town_act move residence`` (town → apartment)."""

    @pytest.mark.asyncio
    async def test_rejected_when_already_in_apartment(self):
        """Agent already in apartment receives move_rejected:already_in_apartment."""
        from routers.town import _handle_context_transition

        agent_conn = _make_agent_conn()
        agent = _make_agent(location_context="apartment")
        sent_messages: list = []

        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.return_value = agent

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent_messages)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock()),
        ):
            await _handle_context_transition("conn-abc", "residence", agent_conn)

        assert len(sent_messages) == 1
        assert sent_messages[0]["event"] == "move_rejected"
        assert sent_messages[0]["reason"] == "already_in_apartment"

    @pytest.mark.asyncio
    async def test_rejected_when_busy(self):
        """Agent with an active activity receives move_rejected:busy."""
        from routers.town import _handle_context_transition

        agent_conn = _make_agent_conn()
        agent = _make_agent(location_context="town", current_activity="read")
        sent_messages: list = []

        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.return_value = agent

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent_messages)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock()),
        ):
            await _handle_context_transition("conn-abc", "residence", agent_conn)

        assert len(sent_messages) == 1
        assert sent_messages[0]["event"] == "move_rejected"
        assert sent_messages[0]["reason"] == "busy"

    @pytest.mark.asyncio
    async def test_rejected_when_agent_not_found(self):
        """Unknown agent_id receives move_rejected:agent_not_found."""
        from routers.town import _handle_context_transition

        agent_conn = _make_agent_conn()
        sent_messages: list = []

        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.return_value = None

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent_messages)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock()),
        ):
            await _handle_context_transition("conn-abc", "residence", agent_conn)

        assert len(sent_messages) == 1
        assert sent_messages[0]["event"] == "move_rejected"
        assert sent_messages[0]["reason"] == "agent_not_found"

    @pytest.mark.asyncio
    async def test_valid_transition_sends_context_changed(self):
        """Valid town→apartment transition calls transition_agent and sends context_changed."""
        from routers.town import _handle_context_transition

        agent_conn = _make_agent_conn()
        agent = _make_agent(location_context="town")
        updated_agent = dict(agent, locationContext="apartment", currentLocation="exit")
        sent_messages: list = []

        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.return_value = agent
        # update_agent_context called twice: once to set "transitioning", once to commit
        svc_mock.update_agent_context.side_effect = [
            dict(agent, locationContext="transitioning"),
            updated_agent,
        ]

        gs_mock = MagicMock()
        gs_mock.transition_agent = AsyncMock()

        ws_mock = _make_ws_manager_mock()

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent_messages)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(GS_CLIENT_PATH, return_value=gs_mock),
            patch(WS_MANAGER_PATH, return_value=ws_mock),
        ):
            await _handle_context_transition("conn-abc", "residence", agent_conn)

        # transition_agent must have been called
        gs_mock.transition_agent.assert_called_once()
        call_kwargs = gs_mock.transition_agent.call_args
        assert call_kwargs.args[0] == "agent-uuid-1"
        assert call_kwargs.kwargs["to_context"] == "apartment"

        # context_changed event sent to daemon
        assert len(sent_messages) == 1
        msg = sent_messages[0]
        assert msg["event"] == "context_changed"
        assert msg["context"] == "apartment"
        assert msg["location"] == "exit"

    @pytest.mark.asyncio
    async def test_sets_transitioning_state_before_game_server_call(self):
        """Phase 1: update_agent_context is called with 'transitioning' before transition_agent."""
        from routers.town import _handle_context_transition

        agent_conn = _make_agent_conn()
        agent = _make_agent(location_context="town")
        updated_agent = dict(agent, locationContext="apartment", currentLocation="exit")
        sent_messages: list = []
        call_order: list = []

        # Responses keyed to which call number we're on
        db_responses = [
            dict(agent, locationContext="transitioning"),
            updated_agent,
        ]

        def _recording_update(aid, ctx, loc):
            call_order.append(f"update_context:{ctx}")
            return db_responses.pop(0)

        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.return_value = agent
        svc_mock.update_agent_context.side_effect = _recording_update

        async def _record_transition(*args, **kwargs):
            call_order.append("transition_agent")

        gs_mock = MagicMock()
        gs_mock.transition_agent = AsyncMock(side_effect=_record_transition)

        ws_mock = _make_ws_manager_mock()

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent_messages)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(GS_CLIENT_PATH, return_value=gs_mock),
            patch(WS_MANAGER_PATH, return_value=ws_mock),
        ):
            await _handle_context_transition("conn-abc", "residence", agent_conn)

        # "transitioning" DB write must precede transition_agent call
        assert call_order[0] == "update_context:transitioning"
        assert call_order[1] == "transition_agent"
        assert call_order[2] == "update_context:apartment"

    @pytest.mark.asyncio
    async def test_rollback_on_game_server_timeout(self):
        """Game server timeout rolls back to town context and sends move_rejected."""
        from routers.town import _handle_context_transition

        agent_conn = _make_agent_conn()
        agent = _make_agent(location_context="town")
        sent_messages: list = []

        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.return_value = agent
        # First call sets "transitioning", subsequent calls are rollback
        svc_mock.update_agent_context.return_value = dict(agent, locationContext="transitioning")

        gs_mock = MagicMock()
        gs_mock.transition_agent = AsyncMock(side_effect=asyncio.TimeoutError())

        ws_mock = _make_ws_manager_mock()

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent_messages)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(GS_CLIENT_PATH, return_value=gs_mock),
            patch(WS_MANAGER_PATH, return_value=ws_mock),
        ):
            await _handle_context_transition("conn-abc", "residence", agent_conn)

        assert len(sent_messages) == 1
        assert sent_messages[0]["event"] == "move_rejected"
        assert sent_messages[0]["reason"] == "navigation_timeout"

        # ws manager rolled back to original context
        rollback_calls = [c for c in ws_mock.update_context.call_args_list]
        contexts_set = [c.args[1] for c in rollback_calls]
        assert "transitioning" in contexts_set
        assert "town" in contexts_set

    @pytest.mark.asyncio
    async def test_rollback_on_game_server_error(self):
        """Game server connection error rolls back and sends move_rejected:navigation_error."""
        from routers.town import _handle_context_transition

        agent_conn = _make_agent_conn()
        agent = _make_agent(location_context="town")
        sent_messages: list = []

        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.return_value = agent
        svc_mock.update_agent_context.return_value = dict(agent, locationContext="transitioning")

        gs_mock = MagicMock()
        gs_mock.transition_agent = AsyncMock(side_effect=ConnectionError("game server down"))

        ws_mock = _make_ws_manager_mock()

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent_messages)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(GS_CLIENT_PATH, return_value=gs_mock),
            patch(WS_MANAGER_PATH, return_value=ws_mock),
        ):
            await _handle_context_transition("conn-abc", "residence", agent_conn)

        assert len(sent_messages) == 1
        assert sent_messages[0]["event"] == "move_rejected"
        assert sent_messages[0]["reason"] == "navigation_error"


# ---------------------------------------------------------------------------
# apartment → town transition (exit)
# ---------------------------------------------------------------------------


class TestHandleContextTransitionToTown:
    """Tests for ``town_act move exit`` (apartment → town)."""

    @pytest.mark.asyncio
    async def test_rejected_when_in_town(self):
        """Agent already in town receives move_rejected:not_in_apartment."""
        from routers.town import _handle_context_transition

        agent_conn = _make_agent_conn()
        agent = _make_agent(location_context="town")
        sent_messages: list = []

        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.return_value = agent

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent_messages)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock()),
        ):
            await _handle_context_transition("conn-abc", "exit", agent_conn)

        assert len(sent_messages) == 1
        assert sent_messages[0]["event"] == "move_rejected"
        # Reason is "not_in_interior" — generalized after library context was
        # added (an "exit" command from town no longer specifically means
        # "leave apartment"; could be either interior).
        assert sent_messages[0]["reason"] == "not_in_interior"

    @pytest.mark.asyncio
    async def test_transition_sends_context_changed(self):
        """Valid apartment→town transition sends context_changed with town context."""
        from routers.town import _handle_context_transition

        agent_conn = _make_agent_conn()
        agent = _make_agent(location_context="apartment")
        updated_agent = dict(agent, locationContext="town", currentLocation="residence")
        sent_messages: list = []

        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.return_value = agent
        svc_mock.update_agent_context.side_effect = [
            dict(agent, locationContext="transitioning"),
            updated_agent,
        ]

        gs_mock = MagicMock()
        gs_mock.transition_agent = AsyncMock()

        ws_mock = _make_ws_manager_mock()

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent_messages)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(GS_CLIENT_PATH, return_value=gs_mock),
            patch(WS_MANAGER_PATH, return_value=ws_mock),
        ):
            await _handle_context_transition("conn-abc", "exit", agent_conn)

        assert len(sent_messages) == 1
        msg = sent_messages[0]
        assert msg["event"] == "context_changed"
        assert msg["context"] == "town"
        assert msg["location"] == "residence"

    @pytest.mark.asyncio
    async def test_exit_rejects_when_busy(self):
        """Busy agent in apartment cannot exit."""
        from routers.town import _handle_context_transition

        agent_conn = _make_agent_conn()
        agent = _make_agent(location_context="apartment", current_activity="sleep")
        sent_messages: list = []

        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.return_value = agent

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent_messages)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock()),
        ):
            await _handle_context_transition("conn-abc", "exit", agent_conn)

        assert len(sent_messages) == 1
        assert sent_messages[0]["event"] == "move_rejected"
        assert sent_messages[0]["reason"] == "busy"

    @pytest.mark.asyncio
    async def test_exit_internal_error_when_db_update_fails(self):
        """If final update_agent_context returns None, agent receives move_rejected:internal_error."""
        from routers.town import _handle_context_transition

        agent_conn = _make_agent_conn()
        agent = _make_agent(location_context="apartment")
        sent_messages: list = []

        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.return_value = agent
        # Call 1: set transitioning; call 2: commit returns None (simulates DB failure);
        # call 3: rollback to original context (called by the internal_error path)
        svc_mock.update_agent_context.side_effect = [
            dict(agent, locationContext="transitioning"),
            None,
            dict(agent, locationContext="apartment"),  # rollback
        ]

        gs_mock = MagicMock()
        gs_mock.transition_agent = AsyncMock()

        ws_mock = _make_ws_manager_mock()

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent_messages)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(GS_CLIENT_PATH, return_value=gs_mock),
            patch(WS_MANAGER_PATH, return_value=ws_mock),
        ):
            await _handle_context_transition("conn-abc", "exit", agent_conn)

        assert len(sent_messages) == 1
        assert sent_messages[0]["event"] == "move_rejected"
        assert sent_messages[0]["reason"] == "internal_error"

    @pytest.mark.asyncio
    async def test_update_agent_context_called_with_correct_args(self):
        """update_agent_context is called with town context and residence location."""
        from routers.town import _handle_context_transition

        agent_conn = _make_agent_conn()
        agent = _make_agent(location_context="apartment")
        updated_agent = dict(agent, locationContext="town", currentLocation="residence")
        sent_messages: list = []

        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.return_value = agent
        svc_mock.update_agent_context.side_effect = [
            dict(agent, locationContext="transitioning"),
            updated_agent,
        ]

        gs_mock = MagicMock()
        gs_mock.transition_agent = AsyncMock()

        ws_mock = _make_ws_manager_mock()

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent_messages)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(GS_CLIENT_PATH, return_value=gs_mock),
            patch(WS_MANAGER_PATH, return_value=ws_mock),
        ):
            await _handle_context_transition("conn-abc", "exit", agent_conn)

        # Check the commit call (second call) uses the correct destination args
        commit_call = svc_mock.update_agent_context.call_args_list[1]
        assert commit_call.args == (agent_conn.agent_id, "town", "residence")


# ---------------------------------------------------------------------------
# _transition_to_apartment shim
# ---------------------------------------------------------------------------


class TestTransitionToApartment:
    """Tests for the ``_transition_to_apartment`` backward-compat shim.

    The shim delegates to ``_handle_context_transition("residence", ...)``.
    These tests verify the shim calls the full two-phase flow.
    """

    @pytest.mark.asyncio
    async def test_sends_context_changed_on_successful_arrival(self):
        """After a successful transition, context_changed is sent with apartment context."""
        from routers.town import _transition_to_apartment

        agent_conn = _make_agent_conn()
        agent_id = "agent-uuid-1"
        agent = _make_agent(agent_id=agent_id, location_context="town")
        updated_agent = {"id": agent_id, "locationContext": "apartment", "currentLocation": "exit"}
        sent_messages: list = []

        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.return_value = agent
        svc_mock.update_agent_context.side_effect = [
            dict(agent, locationContext="transitioning"),
            updated_agent,
        ]

        gs_mock = MagicMock()
        gs_mock.transition_agent = AsyncMock()

        ws_mock = _make_ws_manager_mock()

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent_messages)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(GS_CLIENT_PATH, return_value=gs_mock),
            patch(WS_MANAGER_PATH, return_value=ws_mock),
        ):
            await _transition_to_apartment("conn-abc", agent_id, agent_conn)

        assert len(sent_messages) == 1
        msg = sent_messages[0]
        assert msg["event"] == "context_changed"
        assert msg["context"] == "apartment"
        assert msg["location"] == "exit"

    @pytest.mark.asyncio
    async def test_update_context_called_with_apartment_args(self):
        """update_agent_context is called with apartment and exit location."""
        from routers.town import _transition_to_apartment

        agent_conn = _make_agent_conn()
        agent_id = "agent-uuid-1"
        agent = _make_agent(agent_id=agent_id, location_context="town")
        updated_agent = {"id": agent_id, "locationContext": "apartment", "currentLocation": "exit"}
        sent_messages: list = []

        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.return_value = agent
        svc_mock.update_agent_context.side_effect = [
            dict(agent, locationContext="transitioning"),
            updated_agent,
        ]

        gs_mock = MagicMock()
        gs_mock.transition_agent = AsyncMock()

        ws_mock = _make_ws_manager_mock()

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent_messages)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(GS_CLIENT_PATH, return_value=gs_mock),
            patch(WS_MANAGER_PATH, return_value=ws_mock),
        ):
            await _transition_to_apartment("conn-abc", agent_id, agent_conn)

        # Commit call (second) must use apartment / exit
        commit_call = svc_mock.update_agent_context.call_args_list[1]
        assert commit_call.args == (agent_id, "apartment", "exit")

    @pytest.mark.asyncio
    async def test_sends_move_rejected_on_navigation_timeout(self):
        """Game server timeout sends move_rejected:navigation_timeout."""
        from routers.town import _transition_to_apartment

        agent_conn = _make_agent_conn()
        agent_id = "agent-uuid-1"
        agent = _make_agent(agent_id=agent_id, location_context="town")
        sent_messages: list = []

        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.return_value = agent
        svc_mock.update_agent_context.return_value = dict(agent, locationContext="transitioning")

        gs_mock = MagicMock()
        gs_mock.transition_agent = AsyncMock(side_effect=asyncio.TimeoutError())

        ws_mock = _make_ws_manager_mock()

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent_messages)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(GS_CLIENT_PATH, return_value=gs_mock),
            patch(WS_MANAGER_PATH, return_value=ws_mock),
        ):
            await _transition_to_apartment("conn-abc", agent_id, agent_conn)

        assert len(sent_messages) == 1
        assert sent_messages[0]["event"] == "move_rejected"
        assert sent_messages[0]["reason"] == "navigation_timeout"

    @pytest.mark.asyncio
    async def test_sends_move_rejected_on_game_server_send_error(self):
        """Game server connection failure sends move_rejected:navigation_error."""
        from routers.town import _transition_to_apartment

        agent_conn = _make_agent_conn()
        agent_id = "agent-uuid-1"
        agent = _make_agent(agent_id=agent_id, location_context="town")
        sent_messages: list = []

        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.return_value = agent
        svc_mock.update_agent_context.return_value = dict(agent, locationContext="transitioning")

        gs_mock = MagicMock()
        gs_mock.transition_agent = AsyncMock(side_effect=ConnectionError("game server down"))

        ws_mock = _make_ws_manager_mock()

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent_messages)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(GS_CLIENT_PATH, return_value=gs_mock),
            patch(WS_MANAGER_PATH, return_value=ws_mock),
        ):
            await _transition_to_apartment("conn-abc", agent_id, agent_conn)

        assert len(sent_messages) == 1
        assert sent_messages[0]["event"] == "move_rejected"
        assert sent_messages[0]["reason"] == "navigation_error"

    @pytest.mark.asyncio
    async def test_sends_move_rejected_on_arrival_non_timeout_error(self):
        """Non-timeout game server error sends move_rejected:navigation_error."""
        from routers.town import _transition_to_apartment

        agent_conn = _make_agent_conn()
        agent_id = "agent-uuid-1"
        agent = _make_agent(agent_id=agent_id, location_context="town")
        sent_messages: list = []

        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.return_value = agent
        svc_mock.update_agent_context.return_value = dict(agent, locationContext="transitioning")

        gs_mock = MagicMock()
        gs_mock.transition_agent = AsyncMock(side_effect=RuntimeError("ws closed"))

        ws_mock = _make_ws_manager_mock()

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent_messages)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(GS_CLIENT_PATH, return_value=gs_mock),
            patch(WS_MANAGER_PATH, return_value=ws_mock),
        ):
            await _transition_to_apartment("conn-abc", agent_id, agent_conn)

        assert len(sent_messages) == 1
        assert sent_messages[0]["event"] == "move_rejected"
        assert sent_messages[0]["reason"] == "navigation_error"
