"""Unit tests for context transition logic (town ↔ apartment).

Tests ``_handle_context_transition`` and ``_transition_to_apartment`` in
routers/town.py by mocking ManagementApiClient, TownService, and
GameServerClient at their source modules so local-import statements in the
router functions are intercepted correctly.
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
    }


def _make_mgmt_mock(sent_messages: list) -> MagicMock:
    """Return a ManagementApiClient mock that captures sent messages."""
    mock = MagicMock()
    mock.send_message.side_effect = lambda cid, msg: sent_messages.append(msg)
    return mock


# The transition functions use ``from core.services.management_api_client import
# ManagementApiClient`` and ``from core.services.town_service import TownService``
# inside the function body.  We must patch at those source paths.
# ManagementApiClient is locally imported inside functions → patch at source
MGMT_PATH = "core.services.management_api_client.ManagementApiClient"
# TownService is imported at top of routers/town.py → patch where it's used
SERVICE_PATH = "routers.town.TownService"
GS_CLIENT_PATH = "routers.town.get_game_server_client"


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
        ):
            await _handle_context_transition("conn-abc", "residence", agent_conn)

        assert len(sent_messages) == 1
        assert sent_messages[0]["event"] == "move_rejected"
        assert sent_messages[0]["reason"] == "agent_not_found"

    @pytest.mark.asyncio
    async def test_spawns_background_task_on_valid_transition(self):
        """Valid town→apartment transition spawns a background task; no immediate event."""
        from routers.town import _handle_context_transition

        agent_conn = _make_agent_conn()
        agent = _make_agent(location_context="town")
        sent_messages: list = []

        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.return_value = agent

        task_created = []

        def _capture_task(coro):
            # Close the coroutine to avoid ResourceWarning; record it was called.
            coro.close()
            task_created.append(True)
            return MagicMock()

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent_messages)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch("routers.town.asyncio.create_task", side_effect=_capture_task),
        ):
            await _handle_context_transition("conn-abc", "residence", agent_conn)

        # A background task was spawned, no direct message sent.
        assert len(task_created) == 1
        assert len(sent_messages) == 0


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
        ):
            await _handle_context_transition("conn-abc", "exit", agent_conn)

        assert len(sent_messages) == 1
        assert sent_messages[0]["event"] == "move_rejected"
        assert sent_messages[0]["reason"] == "not_in_apartment"

    @pytest.mark.asyncio
    async def test_immediate_transition_sends_context_changed(self):
        """Valid apartment→town transition fires context_changed immediately."""
        from routers.town import _handle_context_transition

        agent_conn = _make_agent_conn()
        agent = _make_agent(location_context="apartment")
        updated_agent = dict(agent, locationContext="town", currentLocation="residence")
        sent_messages: list = []

        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.return_value = agent
        svc_mock.update_agent_context.return_value = updated_agent

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent_messages)),
            patch(SERVICE_PATH, return_value=svc_mock),
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
        ):
            await _handle_context_transition("conn-abc", "exit", agent_conn)

        assert len(sent_messages) == 1
        assert sent_messages[0]["event"] == "move_rejected"
        assert sent_messages[0]["reason"] == "busy"

    @pytest.mark.asyncio
    async def test_exit_internal_error_when_db_update_fails(self):
        """If update_agent_context returns None, agent receives move_rejected:internal_error."""
        from routers.town import _handle_context_transition

        agent_conn = _make_agent_conn()
        agent = _make_agent(location_context="apartment")
        sent_messages: list = []

        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.return_value = agent
        svc_mock.update_agent_context.return_value = None

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent_messages)),
            patch(SERVICE_PATH, return_value=svc_mock),
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
        svc_mock.update_agent_context.return_value = updated_agent

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent_messages)),
            patch(SERVICE_PATH, return_value=svc_mock),
        ):
            await _handle_context_transition("conn-abc", "exit", agent_conn)

        svc_mock.update_agent_context.assert_called_once_with(
            agent_conn.agent_id, "town", "residence"
        )


# ---------------------------------------------------------------------------
# _transition_to_apartment background task
# ---------------------------------------------------------------------------


class TestTransitionToApartment:
    """Tests for the ``_transition_to_apartment`` background task."""

    @pytest.mark.asyncio
    async def test_sends_context_changed_on_successful_arrival(self):
        """After navigation arrival, context_changed is sent with apartment context."""
        from routers.town import _transition_to_apartment

        agent_conn = _make_agent_conn()
        agent_id = "agent-uuid-1"
        updated_agent = {"id": agent_id, "locationContext": "apartment", "currentLocation": "exit"}
        sent_messages: list = []

        svc_mock = MagicMock()
        svc_mock.update_agent_context.return_value = updated_agent

        gs_mock = MagicMock()
        gs_mock.send_command = AsyncMock()
        gs_mock.wait_for_arrival = AsyncMock()

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent_messages)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(GS_CLIENT_PATH, return_value=gs_mock),
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
        updated_agent = {"id": agent_id, "locationContext": "apartment", "currentLocation": "exit"}
        sent_messages: list = []

        svc_mock = MagicMock()
        svc_mock.update_agent_context.return_value = updated_agent

        gs_mock = MagicMock()
        gs_mock.send_command = AsyncMock()
        gs_mock.wait_for_arrival = AsyncMock()

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent_messages)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(GS_CLIENT_PATH, return_value=gs_mock),
        ):
            await _transition_to_apartment("conn-abc", agent_id, agent_conn)

        svc_mock.update_agent_context.assert_called_once_with(agent_id, "apartment", "exit")

    @pytest.mark.asyncio
    async def test_sends_move_rejected_on_navigation_timeout(self):
        """Navigation timeout sends move_rejected:navigation_timeout."""
        from routers.town import _transition_to_apartment

        agent_conn = _make_agent_conn()
        agent_id = "agent-uuid-1"
        sent_messages: list = []

        gs_mock = MagicMock()
        gs_mock.send_command = AsyncMock()
        # wait_for_arrival is called inside asyncio.wait_for — raising TimeoutError
        # directly simulates the timeout path in _transition_to_apartment.
        gs_mock.wait_for_arrival = AsyncMock(side_effect=asyncio.TimeoutError())

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent_messages)),
            patch(SERVICE_PATH, return_value=MagicMock()),
            patch(GS_CLIENT_PATH, return_value=gs_mock),
        ):
            await _transition_to_apartment("conn-abc", agent_id, agent_conn)

        assert len(sent_messages) == 1
        assert sent_messages[0]["event"] == "move_rejected"
        assert sent_messages[0]["reason"] == "navigation_timeout"

    @pytest.mark.asyncio
    async def test_sends_move_rejected_on_game_server_send_error(self):
        """game server send_command failure sends move_rejected:navigation_error."""
        from routers.town import _transition_to_apartment

        agent_conn = _make_agent_conn()
        agent_id = "agent-uuid-1"
        sent_messages: list = []

        gs_mock = MagicMock()
        gs_mock.send_command = AsyncMock(side_effect=ConnectionError("game server down"))

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent_messages)),
            patch(SERVICE_PATH, return_value=MagicMock()),
            patch(GS_CLIENT_PATH, return_value=gs_mock),
        ):
            await _transition_to_apartment("conn-abc", agent_id, agent_conn)

        assert len(sent_messages) == 1
        assert sent_messages[0]["event"] == "move_rejected"
        assert sent_messages[0]["reason"] == "navigation_error"

    @pytest.mark.asyncio
    async def test_sends_move_rejected_on_arrival_non_timeout_error(self):
        """Non-timeout arrival error sends move_rejected:navigation_error."""
        from routers.town import _transition_to_apartment

        agent_conn = _make_agent_conn()
        agent_id = "agent-uuid-1"
        sent_messages: list = []

        gs_mock = MagicMock()
        gs_mock.send_command = AsyncMock()
        gs_mock.wait_for_arrival = AsyncMock(side_effect=RuntimeError("ws closed"))

        with (
            patch(MGMT_PATH, return_value=_make_mgmt_mock(sent_messages)),
            patch(SERVICE_PATH, return_value=MagicMock()),
            patch(GS_CLIENT_PATH, return_value=gs_mock),
        ):
            await _transition_to_apartment("conn-abc", agent_id, agent_conn)

        assert len(sent_messages) == 1
        assert sent_messages[0]["event"] == "move_rejected"
        assert sent_messages[0]["reason"] == "navigation_error"
