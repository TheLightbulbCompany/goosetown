"""Tests for TownDaemon.run(serve_socket=False).

Test A: run(serve_socket=False) must NOT call run_socket_server.
Test B: run() default must call run_socket_server exactly once.

The bar: both tests must fail if someone deletes the `if serve_socket` guard.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from goosetown.daemon import TownDaemon


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
# Helper: build a patched listen_ws that sets _initial_state_event then returns
# ---------------------------------------------------------------------------


def _make_listen_ws(daemon: TownDaemon):
    """Return an async coroutine that satisfies the 60s wait then exits cleanly."""

    async def _fake_listen_ws():
        # run() creates _initial_state_event before creating the ws_task so
        # it is safe to set it here.
        daemon._initial_state_event.set()
        # Return immediately — this makes ws_task complete so run() exits.

    return _fake_listen_ws


# ---------------------------------------------------------------------------
# Test A — serve_socket=False must NOT start the socket server
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_serve_socket_false_does_not_start_socket_server(
    daemon: TownDaemon,
) -> None:
    socket_call_count = 0

    async def _fake_socket_server():
        nonlocal socket_call_count
        socket_call_count += 1

    async def _noop_connect_ws():
        pass

    async def _noop_check_alarm():
        pass

    async def _noop_keepalive():
        # Yield once so the task is running, then suspend until cancelled.
        await asyncio.sleep(0)
        # Wait until cancelled.
        await asyncio.Future()

    with (
        patch.object(daemon, "connect_ws", _noop_connect_ws),
        patch.object(daemon, "_check_alarm", _noop_check_alarm),
        patch.object(daemon, "_keepalive_loop", _noop_keepalive),
        patch.object(daemon, "run_socket_server", _fake_socket_server),
        patch.object(daemon, "listen_ws", _make_listen_ws(daemon)),
    ):
        await daemon.run(serve_socket=False)

    assert socket_call_count == 0, (
        "run(serve_socket=False) must not call run_socket_server, "
        f"but it was called {socket_call_count} time(s)"
    )


# ---------------------------------------------------------------------------
# Test B — default (serve_socket=True) MUST start the socket server once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_serve_socket_default_starts_socket_server(
    daemon: TownDaemon,
) -> None:
    """Assert run_socket_server is called (coroutine created) when serve_socket=True.

    We track invocations at coroutine-creation time (not inside the body) because
    listen_ws completes immediately — the event loop may cancel the socket task
    before it gets a chance to execute its first line.  Counting at creation time
    is the correct bar: "was run_socket_server() called to produce a task?"
    """
    socket_call_count = 0

    # Capture the real method so we can wrap it.
    # We replace it with a function that increments the counter when called
    # (i.e. when asyncio.create_task(self.run_socket_server()) is evaluated),
    # then suspends until cancelled.
    def _tracking_socket_server_factory():
        """Returns an async fn that increments count at call time, then sleeps."""

        async def _inner():
            await asyncio.Future()  # suspend until cancelled

        def _call_and_count():
            nonlocal socket_call_count
            socket_call_count += 1
            return _inner()

        return _call_and_count

    tracking_socket_server = _tracking_socket_server_factory()

    async def _noop_connect_ws():
        pass

    async def _noop_check_alarm():
        pass

    async def _noop_keepalive():
        await asyncio.sleep(0)
        await asyncio.Future()

    with (
        patch.object(daemon, "connect_ws", _noop_connect_ws),
        patch.object(daemon, "_check_alarm", _noop_check_alarm),
        patch.object(daemon, "_keepalive_loop", _noop_keepalive),
        patch.object(daemon, "run_socket_server", tracking_socket_server),
        patch.object(daemon, "listen_ws", _make_listen_ws(daemon)),
    ):
        await daemon.run()  # default: serve_socket=True

    assert socket_call_count == 1, (
        "run() with default serve_socket must call run_socket_server exactly once, "
        f"but it was called {socket_call_count} time(s)"
    )
