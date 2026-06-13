"""Regression tests for the WS listen loop's connection-liveness handling.

Bug: ``listen_ws`` used a 30s ``recv()`` timeout as a death signal and forced a
reconnect whenever no *application* message arrived for 30s. Idle apartment
agents legitimately receive no broadcasts (town agents get ``world_state`` every
~2s; idle agents get nothing), so the daemon reconnected every ~30s and the
backend flipped the agent's ``isActive`` off/on. Liveness is the websockets
library's ping/pong job; an idle recv gap must NOT be treated as a dead
connection.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from goosetown.daemon import TownDaemon


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


@pytest.mark.asyncio
async def test_idle_recv_timeout_does_not_disconnect(daemon: TownDaemon):
    """A recv() timeout (no application traffic) must continue the loop, NOT
    reconnect — and a later message is still processed."""
    calls = {"n": 0}

    async def fake_recv():
        calls["n"] += 1
        if calls["n"] == 1:
            # The safety-net timeout firing surfaces as asyncio.TimeoutError.
            raise asyncio.TimeoutError
        if calls["n"] == 2:
            return json.dumps({"event": "world_state", "agents": []})
        # End the loop deterministically on the 3rd call.
        daemon.running = False
        raise ConnectionError("server closed")

    daemon.ws = MagicMock()
    daemon.ws.recv = fake_recv
    daemon.running = True

    with patch.object(daemon, "_handle_world_update") as handled:
        await daemon.listen_ws()

    # The message AFTER the idle timeout was handled → the loop continued past
    # the timeout instead of bailing out to reconnect.
    assert handled.called, "message after an idle recv timeout was not processed"
    assert calls["n"] >= 2


@pytest.mark.asyncio
async def test_recv_timeout_is_not_a_reconnect_signal(daemon: TownDaemon):
    """Many consecutive idle timeouts never raise out of listen_ws — they just
    re-loop. (A genuine dead peer raises ConnectionClosed from recv(), which the
    library produces via ping/pong; that path is the outer handler's job.)"""
    calls = {"n": 0}

    async def fake_recv():
        calls["n"] += 1
        if calls["n"] <= 3:
            raise asyncio.TimeoutError  # three idle gaps in a row
        daemon.running = False
        raise ConnectionError("stop")

    daemon.ws = MagicMock()
    daemon.ws.recv = fake_recv
    daemon.running = True

    # Must return cleanly (outer handler swallows the final close); no
    # "Inactivity timeout" ConnectionError escapes mid-idle.
    await daemon.listen_ws()
    assert calls["n"] == 4
