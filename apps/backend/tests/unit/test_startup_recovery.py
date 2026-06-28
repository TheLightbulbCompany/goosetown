"""Startup must not block on the network-bound recovery work.

Regression test for the 2026-06-26 dev crash loop: the lifespan awaited
``_reconnect_active_agents`` (which connects each active agent to the game
server sequentially, with a per-connect open_timeout) on the startup
critical path. uvicorn does not start serving — including ``/health`` — until
the lifespan generator reaches ``yield``. When game-server connects were slow
or timing out, startup blocked, ``/health`` starved, the ALB marked the task
unhealthy, and ECS replaced it in a loop.

The fix runs the recovery (``_reconnect_active_agents`` then
``restore_inactivity_timers``) in a background task so startup completes
immediately, and cancels it on shutdown.
"""

import asyncio
import unittest.mock as mock

import pytest

import main


@pytest.mark.asyncio
async def test_lifespan_does_not_block_on_reconnect():
    reconnect_started = asyncio.Event()
    release_reconnect = asyncio.Event()

    async def _hung_reconnect() -> None:
        reconnect_started.set()
        await release_reconnect.wait()  # simulates slow/timing-out game-server connects

    fake_relay = mock.AsyncMock()

    with (
        mock.patch.object(main, "_reconnect_active_agents", _hung_reconnect),
        mock.patch.object(main.settings, "validate", lambda: None),
        mock.patch("core.services.viewer_relay.get_viewer_relay", return_value=fake_relay),
        mock.patch("core.services.conversation_service.restore_inactivity_timers", mock.AsyncMock()),
        mock.patch("core.services.exchange_service.exchange_sweeper_loop", return_value=asyncio.sleep(3600)),
        mock.patch("core.services.pixellab_service.close_pixellab_client", mock.AsyncMock()),
    ):
        cm = main.lifespan(main.app)
        try:
            # Startup must complete promptly even though the reconnect is hung.
            # If it were still awaited on the critical path, this times out.
            await asyncio.wait_for(cm.__aenter__(), timeout=2.0)

            # Recovery was kicked off in the background, still running.
            recovery = main.app.state.startup_recovery
            assert recovery is not None
            assert not recovery.done()
            # And it really started (backgrounded, not silently dropped).
            await asyncio.wait_for(reconnect_started.wait(), timeout=2.0)
        finally:
            release_reconnect.set()
            await cm.__aexit__(None, None, None)

        # Shutdown cancels the still-pending recovery task.
        assert main.app.state.startup_recovery.done()
