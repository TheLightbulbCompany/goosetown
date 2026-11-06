"""Real startup call sites must yield while synchronous database calls are slow."""

import asyncio
import threading
import time
from unittest.mock import AsyncMock, patch

import pytest

import main
from core.services.viewer_relay import ViewerRelay


def slow_empty(*args, **kwargs):
    time.sleep(0.3)
    return {"Items": []}


@pytest.mark.asyncio
async def test_lifespan_reaches_yield_without_waiting_for_viewer_seed():
    relay = ViewerRelay("ws://127.0.0.1:9")
    table = type("SlowTable", (), {"scan": slow_empty})()
    with (
        patch.object(main.settings, "validate", lambda: None),
        patch("core.services.viewer_relay.get_viewer_relay", return_value=relay),
        patch("core.dynamo.connections_table", return_value=table),
        patch.object(relay, "_run_loop", AsyncMock()),
        patch.object(relay, "_broadcast_worker", AsyncMock()),
        patch.object(main, "_startup_recovery", AsyncMock()),
        patch("core.services.exchange_service.exchange_sweeper_loop", AsyncMock()),
        patch("core.services.pixellab_service.close_pixellab_client", AsyncMock()),
    ):
        manager = main.lifespan(main.app)
        started = time.monotonic()
        try:
            await manager.__aenter__()
            assert time.monotonic() - started < 0.15, "slow viewer seed blocked lifespan startup"
        finally:
            await manager.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_background_agent_query_does_not_block_event_loop():
    def slow_agents():
        time.sleep(0.3)
        return []

    with patch("core.services.town_service.TownService.get_all_active_agents", side_effect=slow_agents):
        started = time.monotonic()
        task = asyncio.create_task(main._reconnect_active_agents())
        try:
            await asyncio.sleep(0.02)
            assert time.monotonic() - started < 0.15, "background agent query blocked liveness"
        finally:
            await task


@pytest.mark.asyncio
async def test_timer_recovery_query_does_not_block_event_loop():
    from core.services.conversation_service import restore_inactivity_timers

    def slow_conversations():
        time.sleep(0.3)
        return []

    with patch("core.services.town_service.TownService.get_active_conversations", side_effect=slow_conversations):
        started = time.monotonic()
        task = asyncio.create_task(restore_inactivity_timers())
        try:
            await asyncio.sleep(0.02)
            assert time.monotonic() - started < 0.15, "timer recovery query blocked liveness"
        finally:
            await task


@pytest.mark.asyncio
async def test_viewer_snapshot_preserves_registration_and_disconnects_during_seed():
    started, release = threading.Event(), threading.Event()

    def read_snapshot():
        started.set()
        assert release.wait(2), "test did not release the snapshot reader"
        return {"disconnected", "still-connected", "reconnected"}

    relay = ViewerRelay("ws://127.0.0.1:9")
    with (
        patch.object(relay, "_read_viewer_connections", side_effect=read_snapshot),
        patch.object(relay, "_run_loop", AsyncMock()),
        patch.object(relay, "_broadcast_worker", AsyncMock()),
    ):
        await relay.start()
        try:
            assert await asyncio.to_thread(started.wait, 2)
            relay.unregister_viewer("disconnected")
            relay.register_viewer("fresh")
            relay.unregister_viewer("reconnected")
            relay.register_viewer("reconnected")
            release.set()
            await relay._seed_task
            assert relay._viewer_connections == {"still-connected", "fresh", "reconnected"}
        finally:
            release.set()
            await relay.stop()


@pytest.mark.asyncio
async def test_stop_discards_late_snapshot_without_waiting_for_database():
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def read_snapshot():
        started.set()
        try:
            assert release.wait(2), "test did not release the snapshot reader"
            return {"stale"}
        finally:
            finished.set()

    relay = ViewerRelay("ws://127.0.0.1:9")
    with (
        patch.object(relay, "_read_viewer_connections", side_effect=read_snapshot),
        patch.object(relay, "_run_loop", AsyncMock()),
        patch.object(relay, "_broadcast_worker", AsyncMock()),
    ):
        await relay.start()
        try:
            assert await asyncio.to_thread(started.wait, 2)
            await asyncio.wait_for(relay.stop(), timeout=0.2)
            assert not finished.is_set(), "stop waited for the database reader"
        finally:
            release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        await asyncio.sleep(0)
        assert relay._viewer_connections == set()


@pytest.mark.asyncio
async def test_gone_viewer_pruned_during_seed_is_not_resurrected():
    started, release = threading.Event(), threading.Event()

    def read_snapshot():
        started.set()
        assert release.wait(2), "test did not release the snapshot reader"
        return {"gone", "still-connected"}

    relay = ViewerRelay("ws://127.0.0.1:9")
    relay.register_viewer("gone")
    with (
        patch.object(relay, "_read_viewer_connections", side_effect=read_snapshot),
        patch.object(relay, "_run_loop", AsyncMock()),
        patch.object(relay, "_broadcast_worker", AsyncMock()),
        patch("core.services.management_api_client.ManagementApiClient") as management,
        patch("core.dynamo.delete_connection") as delete_connection,
    ):
        management.return_value.send_message.return_value = False
        await relay.start()
        try:
            assert await asyncio.to_thread(started.wait, 2)
            await relay._fan_out(["gone"], {"event": "world_state", "agents": []})
            delete_connection.assert_called_once_with("gone")
            assert "gone" not in relay._viewer_connections
            release.set()
            await relay._seed_task
            assert relay._viewer_connections == {"still-connected"}
        finally:
            release.set()
            await relay.stop()


def test_seed_reader_keeps_pages_loaded_before_database_failure():
    from unittest.mock import MagicMock

    table = MagicMock()
    table.scan.side_effect = [
        {"Items": [{"connectionId": "first"}], "LastEvaluatedKey": {"connectionId": "first"}},
        RuntimeError("synthetic database outage"),
    ]
    with patch("core.dynamo.connections_table", return_value=table):
        assert ViewerRelay._read_viewer_connections() == {"first"}
    assert table.scan.call_args.kwargs["ExclusiveStartKey"] == {"connectionId": "first"}
