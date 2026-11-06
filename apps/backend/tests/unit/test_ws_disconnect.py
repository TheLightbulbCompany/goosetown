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

# The `handle_ws_disconnect` route body still lives in routers.town and
# looks up these names there — so the route-body patches target routers.town.
SERVICE_PATH = "routers.town.TownService"
GS_CLIENT_PATH = "routers.town.get_game_server_client"
WS_MANAGER_PATH = "routers.town.get_town_agent_ws_manager"

# The sleep-grace trio (`_delayed_sleep`, `_schedule_sleep_grace`,
# `_cancel_sleep_grace`) + ``_SLEEP_GRACE_SECONDS`` moved to
# ``core.services.agent_connect_service`` in sub-project #3 fifth slice
# (2026-04-23). Background tasks spawned by ``_delayed_sleep`` look up
# ``TownService`` from that module's namespace, so tests that await the
# background task must patch at the new home.
AGENT_CONNECT_SERVICE_PATH = "core.services.agent_connect_service.TownService"
GRACE_MODULE_PATH = "core.services.agent_connect_service"


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
    from core.services import agent_connect_service
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
        # `_delayed_sleep` runs in the service namespace and must see the
        # same mock that the route body does.
        patch(AGENT_CONNECT_SERVICE_PATH, return_value=svc_mock),
        patch(GS_CLIENT_PATH, return_value=_make_gs_mock()),
        # Shrink the grace so the test doesn't actually sleep 90s. Patched at
        # the service module — the routers.town re-export is a bound copy.
        patch.object(agent_connect_service, "_SLEEP_GRACE_SECONDS", 0.01),
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


@pytest.mark.asyncio
async def test_clean_leave_flips_is_active_false_to_free_quota_slot():
    """Quota-leak regression: clean leave must set ``isActive='false'`` so the
    per-user agent quota releases.

    Surfaced by the 2026-05-06 integration test — sleeping agents that had
    cleanly disconnected days earlier still showed ``isActive="true"`` and
    occupied quota slots, blocking new registrations with HTTP 429. The
    register path (``opt_in``) flips the flag back to ``"true"`` on
    re-registration so the round-trip is symmetric.
    """
    from routers.town import WsDisconnectRequest, handle_ws_disconnect

    agent_conn = _make_agent_conn(leaving_cleanly=True)
    svc_mock = MagicMock()

    with (
        patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock(agent_conn)),
        patch(SERVICE_PATH, return_value=svc_mock),
        patch(GS_CLIENT_PATH, return_value=_make_gs_mock()),
    ):
        await handle_ws_disconnect(WsDisconnectRequest(connectionId="conn-1"))

    # ``update_agent_state`` is now called twice on clean leave:
    #   1. clear daemon_connection_id (existing behaviour)
    #   2. set is_active="false" (NEW — frees quota slot)
    svc_mock.set_agent_active.assert_any_call("agent-1", False)


@pytest.mark.asyncio
async def test_clean_leave_still_unregisters_when_quota_release_fails():
    """Review F2: a contended set_agent_active must not skip ws_manager.unregister."""
    from routers.town import WsDisconnectRequest, handle_ws_disconnect

    ws_mock = _make_ws_manager_mock(_make_agent_conn(leaving_cleanly=True))
    svc_mock = MagicMock()
    svc_mock.set_agent_active.side_effect = RuntimeError("TransactionConflict")

    with (
        patch(WS_MANAGER_PATH, return_value=ws_mock),
        patch(SERVICE_PATH, return_value=svc_mock),
        patch(GS_CLIENT_PATH, return_value=_make_gs_mock()),
    ):
        await handle_ws_disconnect(WsDisconnectRequest(connectionId="conn-1"))

    ws_mock.unregister.assert_called_once_with("conn-1")


@pytest.mark.asyncio
async def test_grace_expiry_flips_is_active_false_to_free_quota_slot():
    """Quota-leak regression on the drop path: when the grace window
    expires without a reconnect, the delayed-sleep task must also set
    ``isActive='false'`` for the same reason as the clean-leave path. A
    drop that resolves into permanent disconnection is functionally
    indistinguishable from an explicit leave.
    """
    from core.services import agent_connect_service
    from routers import town
    from routers.town import WsDisconnectRequest, handle_ws_disconnect

    agent_conn = _make_agent_conn(leaving_cleanly=False)
    svc_mock = MagicMock()
    svc_mock.get_town_agent_by_id.return_value = {
        "id": "agent-1",
        "daemonConnectionId": "",
    }

    with (
        patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock(agent_conn)),
        patch(SERVICE_PATH, return_value=svc_mock),
        patch(AGENT_CONNECT_SERVICE_PATH, return_value=svc_mock),
        patch(GS_CLIENT_PATH, return_value=_make_gs_mock()),
        patch.object(agent_connect_service, "_SLEEP_GRACE_SECONDS", 0.01),
    ):
        await handle_ws_disconnect(WsDisconnectRequest(connectionId="conn-1"))
        task = town._pending_sleep_tasks.get("agent-1")
        assert task is not None
        await task

    svc_mock.set_agent_active.assert_any_call("agent-1", False)


@pytest.mark.asyncio
async def test_reconnect_within_grace_does_not_flip_is_active():
    """Negative case: a network flap that reconnects inside the grace
    window must NOT flip ``isActive`` — from the quota's perspective the
    agent never went away. This guards against a regression where the
    grace task would race past ``_cancel_sleep_grace`` and mistakenly free
    the slot under a still-connected daemon.
    """
    from routers import town
    from routers.town import WsDisconnectRequest, handle_ws_disconnect

    agent_conn = _make_agent_conn(leaving_cleanly=False)
    svc_mock = MagicMock()

    with (
        patch(WS_MANAGER_PATH, return_value=_make_ws_manager_mock(agent_conn)),
        patch(SERVICE_PATH, return_value=svc_mock),
        patch(GS_CLIENT_PATH, return_value=_make_gs_mock()),
        patch.object(town, "_SLEEP_GRACE_SECONDS", 0.5),
    ):
        await handle_ws_disconnect(WsDisconnectRequest(connectionId="conn-1"))
        cancelled = town._cancel_sleep_grace("agent-1")

    assert cancelled is True
    # Let the cancelled coroutine raise CancelledError before we inspect calls.
    await asyncio.sleep(0)

    # In-grace reconnect must not free the quota slot.
    svc_mock.set_agent_active.assert_not_called()
