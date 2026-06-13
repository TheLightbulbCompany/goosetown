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
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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


# ---------------------------------------------------------------------------
# End-to-end keepalive: pong/reconnect handling + pong-timeout reconnect
# (CLI 0.5.2 — closes the #402 gap where idle agents didn't recover from a
# backend-side drop, e.g. after a deploy).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pong_event_marks_liveness(daemon: TownDaemon):
    """A backend ``{"event":"pong"}`` resets the pong deadline and records that
    the backend supports pong (enables the timeout-reconnect guard)."""
    daemon._ever_ponged = False
    daemon._last_pong_at = 0.0
    calls = {"n": 0}

    async def fake_recv():
        calls["n"] += 1
        if calls["n"] == 1:
            return json.dumps({"event": "pong"})
        daemon.running = False
        raise ConnectionError("stop")

    daemon.ws = MagicMock()
    daemon.ws.recv = fake_recv
    daemon.running = True
    await daemon.listen_ws()

    assert daemon._ever_ponged is True
    assert daemon._last_pong_at > 0


@pytest.mark.asyncio
async def test_reconnect_event_exits_listen_loop(daemon: TownDaemon):
    """A backend ``{"event":"reconnect"}`` breaks listen_ws so the outer loop
    reconnects and re-sends town_agent_connect (re-register)."""
    calls = {"n": 0}

    async def fake_recv():
        calls["n"] += 1
        if calls["n"] == 1:
            return json.dumps({"event": "reconnect"})
        return json.dumps({"event": "world_state"})  # must NOT be reached

    daemon.ws = MagicMock()
    daemon.ws.recv = fake_recv
    daemon.running = True
    with patch.object(daemon, "_handle_world_update") as handled:
        await daemon.listen_ws()

    assert calls["n"] == 1  # loop exited on the reconnect event
    assert not handled.called


@pytest.mark.asyncio
async def test_keepalive_reconnects_on_pong_timeout(daemon: TownDaemon):
    """Once the backend has ponged at least once, a stale pong deadline means the
    connection is half-open → close it to force a reconnect (don't keep pinging)."""
    daemon._PING_INTERVAL_SEC = 0.01
    daemon._PONG_TIMEOUT_SEC = 0.05
    daemon._ever_ponged = True
    daemon._last_pong_at = time.time() - 100  # very stale
    closed = {"v": False}

    async def fake_close():
        closed["v"] = True
        daemon.running = False  # stop the loop after the close

    daemon.ws = MagicMock()
    daemon.ws.close = fake_close
    daemon.ws.send = AsyncMock()
    daemon.running = True
    await daemon._keepalive_loop()

    assert closed["v"] is True
    daemon.ws.send.assert_not_called()  # reconnected instead of pinging


@pytest.mark.asyncio
async def test_keepalive_does_not_reconnect_against_pongless_backend(
    daemon: TownDaemon,
):
    """If the backend has never ponged (older backend), the timeout-reconnect is
    disabled — the daemon just keeps pinging (no regression vs the pre-keepalive
    build)."""
    daemon._PING_INTERVAL_SEC = 0.01
    daemon._PONG_TIMEOUT_SEC = 0.05
    daemon._ever_ponged = False  # never seen a pong
    daemon._last_pong_at = time.time() - 100  # stale, but the guard is off
    sent = {"n": 0}

    async def fake_send(_payload):
        sent["n"] += 1
        daemon.running = False  # stop after the first ping

    daemon.ws = MagicMock()
    daemon.ws.close = AsyncMock()
    daemon.ws.send = fake_send
    daemon.running = True
    await daemon._keepalive_loop()

    daemon.ws.close.assert_not_called()
    assert sent["n"] == 1  # pinged, did not reconnect


@pytest.mark.asyncio
async def test_connect_resets_ever_ponged_per_connection(daemon: TownDaemon):
    """Each new connection re-arms pong detection from scratch (codex P2): a
    daemon that saw a pong on a prior connection but reconnects to a pongless
    backend (rollback / mixed-version) must NOT inherit the armed guard, or it
    would churn an otherwise-healthy socket every _PONG_TIMEOUT_SEC."""
    daemon._ever_ponged = True  # stale from a previous connection
    daemon._last_pong_at = 0.0

    mock_ws = MagicMock()
    mock_ws.send = AsyncMock()
    with patch("websockets.connect", new=AsyncMock(return_value=mock_ws)):
        await daemon.connect_ws()

    assert daemon.ws is mock_ws
    assert daemon._ever_ponged is False  # re-armed only after a pong on THIS conn
    assert daemon._last_pong_at > 0  # deadline started at connect
