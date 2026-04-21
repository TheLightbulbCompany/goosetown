"""Unit tests for handle_ws_disconnect — the API Gateway $disconnect hook.

Covers the F12 secondary fix from the 2026-04-21 agent-team dry run: the
backend must differentiate a CLEAN leave (daemon sent `town_agent_sleep`
first) from a server-initiated drop (AWS idle timeout, Lambda crash,
network flap). Only clean leaves move the agent to apartment/bed —
drops should leave the agent at their last-known location so the next
reconnect resumes in the town.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

SERVICE_PATH = "routers.town.TownService"
GS_CLIENT_PATH = "routers.town.get_game_server_client"
WS_MANAGER_PATH = "routers.town.get_town_agent_ws_manager"


def _make_agent_conn(leaving_cleanly: bool) -> SimpleNamespace:
    return SimpleNamespace(
        agent_id="agent-1",
        connection_id="conn-1",
        user_id="user-1",
        agent_name="test-agent",
        instance_id="instance-1",
        location_context="town",
        exhausted_rejections=0,
        leaving_cleanly=leaving_cleanly,
    )


def _make_ws_manager_mock(agent_conn) -> MagicMock:
    mock = MagicMock()
    mock.get_by_connection.return_value = agent_conn
    return mock


def _make_gs_mock() -> MagicMock:
    mock = MagicMock()
    mock.disconnect_agent = AsyncMock()
    return mock


@pytest.mark.asyncio
async def test_clean_leave_moves_agent_to_bed():
    """leaving_cleanly=True → update_agent_context('apartment', 'bed') called."""
    from routers.town import WsDisconnectRequest, handle_ws_disconnect

    agent_conn = _make_agent_conn(leaving_cleanly=True)
    svc_mock = MagicMock()

    with (
        patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock(agent_conn)),
        patch(SERVICE_PATH, return_value=svc_mock),
        patch(GS_CLIENT_PATH, return_value=_make_gs_mock()),
    ):
        await handle_ws_disconnect(WsDisconnectRequest(connectionId="conn-1"))

    svc_mock.update_agent_context.assert_called_once_with("agent-1", "apartment", "bed")


@pytest.mark.asyncio
async def test_unexpected_drop_does_not_move_agent_to_bed():
    """leaving_cleanly=False → update_agent_context NOT called (F12 fix).

    Without this, AWS idle-timeout disconnects silently teleported active
    agents into their apartments mid-town, confusing both the viewer and
    the agent on reconnect.
    """
    from routers.town import WsDisconnectRequest, handle_ws_disconnect

    agent_conn = _make_agent_conn(leaving_cleanly=False)
    svc_mock = MagicMock()

    with (
        patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock(agent_conn)),
        patch(SERVICE_PATH, return_value=svc_mock),
        patch(GS_CLIENT_PATH, return_value=_make_gs_mock()),
    ):
        await handle_ws_disconnect(WsDisconnectRequest(connectionId="conn-1"))

    svc_mock.update_agent_context.assert_not_called()
    # Side effects that must STILL happen on any disconnect:
    svc_mock.update_agent_state.assert_called_once_with("agent-1", daemon_connection_id="")
    svc_mock.update_agent_activity.assert_called_once_with("agent-1", None, None)


@pytest.mark.asyncio
async def test_clean_leave_still_disconnects_from_game_server():
    """Game-server detach happens on both paths — sim shouldn't keep ghosting."""
    from routers.town import WsDisconnectRequest, handle_ws_disconnect

    agent_conn = _make_agent_conn(leaving_cleanly=True)
    gs_mock = _make_gs_mock()

    with (
        patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock(agent_conn)),
        patch(SERVICE_PATH, return_value=MagicMock()),
        patch(GS_CLIENT_PATH, return_value=gs_mock),
    ):
        await handle_ws_disconnect(WsDisconnectRequest(connectionId="conn-1"))

    gs_mock.disconnect_agent.assert_awaited_once_with("agent-1")
