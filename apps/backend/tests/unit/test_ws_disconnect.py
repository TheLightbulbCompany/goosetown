"""Unit tests for handle_ws_disconnect + the sleep-grace window.

The invariant the backend preserves:

* Clean leave (`town_agent_sleep`): move to apartment/bed immediately.
* Server-initiated drop: schedule a delayed move-to-bed. If the daemon
  reconnects within the grace window the task is cancelled and the agent
  resumes at their last-known location. If not, the task fires and the
  agent ends up in bed as before.

The grace window fixes the 9-second network flap case observed in the
2026-04-21 dry run (F12): Dwight's daemon auto-reconnected within 5s of
a 1011 close, but the backend had already teleported him to bed mid-flap.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

SERVICE_PATH = "routers.town.TownService"
GS_CLIENT_PATH = "routers.town.get_game_server_client"
WS_MANAGER_PATH = "routers.town.get_town_agent_ws_manager"


def _make_agent_conn(leaving_cleanly: bool, agent_id: str = "agent-1") -> SimpleNamespace:
    return SimpleNamespace(
        agent_id=agent_id,
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


@pytest.fixture(autouse=True)
def _clear_pending_tasks():
    """Each test starts with an empty grace-task registry."""
    from routers import town

    town._pending_sleep_tasks.clear()
    yield
    # Drain whatever tasks the test left behind.
    for t in list(town._pending_sleep_tasks.values()):
        t.cancel()
    town._pending_sleep_tasks.clear()


@pytest.mark.asyncio
async def test_clean_leave_moves_agent_to_bed_immediately():
    """leaving_cleanly=True → bed IMMEDIATELY, no grace task."""
    from routers import town
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
    assert "agent-1" not in town._pending_sleep_tasks, "clean leave must not schedule grace"


@pytest.mark.asyncio
async def test_drop_schedules_grace_does_not_move_immediately():
    """leaving_cleanly=False → no immediate location change, grace scheduled."""
    from routers import town
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
    svc_mock.update_agent_state.assert_called_once_with("agent-1", daemon_connection_id="")
    svc_mock.update_agent_activity.assert_called_once_with("agent-1", None, None)

    task = town._pending_sleep_tasks.get("agent-1")
    assert task is not None and not task.done(), "grace task must be registered and running"


@pytest.mark.asyncio
async def test_drop_then_grace_expiry_moves_to_bed():
    """If the daemon never reconnects, the task fires and moves to bed."""
    from routers import town
    from routers.town import WsDisconnectRequest, handle_ws_disconnect

    agent_conn = _make_agent_conn(leaving_cleanly=False)
    svc_mock = MagicMock()
    # After expiry _delayed_sleep calls get_town_agent_by_id to re-check
    # daemonConnectionId. Return an agent with empty id so it proceeds.
    svc_mock.get_town_agent_by_id.return_value = {
        "id": "agent-1",
        "daemonConnectionId": "",
    }

    with (
        patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock(agent_conn)),
        patch(SERVICE_PATH, return_value=svc_mock),
        patch(GS_CLIENT_PATH, return_value=_make_gs_mock()),
        # Shrink the grace so the test doesn't actually sleep 90s.
        patch.object(town, "_SLEEP_GRACE_SECONDS", 0.01),
    ):
        await handle_ws_disconnect(WsDisconnectRequest(connectionId="conn-1"))
        task = town._pending_sleep_tasks.get("agent-1")
        assert task is not None
        # Wait for the task to run its course.
        await task

    svc_mock.update_agent_context.assert_called_once_with("agent-1", "apartment", "bed")
    assert "agent-1" not in town._pending_sleep_tasks


@pytest.mark.asyncio
async def test_drop_then_reconnect_within_grace_cancels_task():
    """_cancel_sleep_grace returns True; location is NOT moved to bed."""
    from routers import town
    from routers.town import WsDisconnectRequest, handle_ws_disconnect

    agent_conn = _make_agent_conn(leaving_cleanly=False)
    svc_mock = MagicMock()
    svc_mock.get_town_agent_by_id.return_value = {
        "id": "agent-1",
        "daemonConnectionId": "",  # irrelevant since task gets cancelled
    }

    with (
        patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock(agent_conn)),
        patch(SERVICE_PATH, return_value=svc_mock),
        patch(GS_CLIENT_PATH, return_value=_make_gs_mock()),
        patch.object(town, "_SLEEP_GRACE_SECONDS", 0.5),
    ):
        await handle_ws_disconnect(WsDisconnectRequest(connectionId="conn-1"))

        # Simulate a reconnect landing inside the grace window by calling
        # _cancel_sleep_grace directly.
        cancelled = town._cancel_sleep_grace("agent-1")

    assert cancelled is True, "_cancel_sleep_grace should report it cancelled a live task"
    # Give the cancelled coroutine a chance to propagate CancelledError.
    await asyncio.sleep(0)
    svc_mock.update_agent_context.assert_not_called()
    assert "agent-1" not in town._pending_sleep_tasks


@pytest.mark.asyncio
async def test_cancel_sleep_grace_on_no_pending_task_returns_false():
    """Fresh connects (no prior drop) must not claim they were in-grace."""
    from routers import town

    assert town._cancel_sleep_grace("agent-never-seen") is False


@pytest.mark.asyncio
async def test_schedule_replaces_existing_task():
    """Two drops in a row (e.g. double $disconnect from API Gateway) —
    last-writer-wins; old task gets cancelled, new one replaces."""
    from routers import town

    with patch.object(town, "_SLEEP_GRACE_SECONDS", 1.0):
        town._schedule_sleep_grace("agent-1")
        first = town._pending_sleep_tasks["agent-1"]
        town._schedule_sleep_grace("agent-1")
        second = town._pending_sleep_tasks["agent-1"]
        # Let the cancelled coroutine reach its CancelledError handler
        await asyncio.sleep(0)

    assert first is not second
    assert first.cancelled() or first.done()
    second.cancel()
    await asyncio.sleep(0)


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
