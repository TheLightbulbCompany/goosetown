"""Regression tests for ``goosetown status`` accuracy.

The status command used to read ``state.json`` directly without cross-
checking whether the daemon was actually alive or whether the cached
state was fresh. Two distinct bugs surfaced from the 2026-05-02
real-user dry runs:

D1 — Broken-but-running daemon: a daemon process that fails to connect
    (bad URL, network blip, JWT expired) keeps the pidfile but never
    writes new state. Status returned the stale ``state.json`` with
    ``connected: true`` and the misleading advice "Run 'goosetown act
    idle' to auto-connect" — which would just spawn another broken
    daemon.

D2 — Cleanly-stopped daemon: ``goosetown leave`` removes the pidfile
    but leaves ``state.json`` intact, so a subsequent ``status`` returns
    ``connected: true`` even though the daemon is gone.

This test module pins the corrected behavior:

  * Status cross-checks pidfile liveness before trusting state.json.
  * Leave deletes state.json (or writes a stopped sentinel) so status
    correctly reports stopped.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from goosetown import core


@pytest.fixture
def isolated_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Create a tmp workspace with a stub GOOSETOWN.md, return (agent_dir, state_dir)."""
    agent_dir = tmp_path / "workspace"
    agent_dir.mkdir()
    (agent_dir / "GOOSETOWN.md").write_text(
        "token: t\n"
        "ws_url: wss://example.invalid\n"
        "api_url: https://example.invalid\n"
        "agent: testagent\n"
    )
    sd = tmp_path / "state"
    sd.mkdir()
    monkeypatch.setenv("AGENT_DIR", str(agent_dir))
    monkeypatch.setattr(core, "state_dir", lambda name: sd)
    return agent_dir, sd


def _run_status(capsys: pytest.CaptureFixture) -> dict:
    """Invoke cmd_status and parse the printed JSON."""
    import argparse

    core.cmd_status(argparse.Namespace())
    captured = capsys.readouterr().out.strip()
    return json.loads(captured)


def _write_alive_state(sd: Path) -> None:
    """Simulate a connected daemon: pidfile points at THIS process; state shows alive."""
    (sd / "daemon.pid").write_text(str(os.getpid()))
    (sd / "state.json").write_text(json.dumps({"connected": True, "agent": {"location": "plaza"}}))


def _write_dead_pid_state(sd: Path) -> None:
    """Simulate a dead daemon with stale state still on disk."""
    # PID 1 (init) is conventional but `os.kill(1, 0)` may succeed for root —
    # use a guaranteed-dead pid by reading any alive pid then bumping it.
    # Simplest: pick a high unused pid. Use the process group id of pid 1 +
    # an offset that's almost certainly free in tests. If the test ever
    # races into a real pid, the test is wrong by ~one in 32k.
    dead_pid = 99999
    (sd / "daemon.pid").write_text(str(dead_pid))
    (sd / "state.json").write_text(json.dumps({"connected": True, "agent": {"location": "plaza"}}))


class TestStatusReflectsDaemonLiveness:
    """``goosetown status`` must distinguish dead daemon from live."""

    def test_alive_daemon_returns_state(
        self, isolated_workspace, capsys: pytest.CaptureFixture
    ):
        _agent_dir, sd = isolated_workspace
        _write_alive_state(sd)
        result = _run_status(capsys)
        # Real state passes through.
        assert result.get("connected") is True
        assert result.get("agent", {}).get("location") == "plaza"

    def test_dead_daemon_with_stale_state_returns_stopped_not_connected(
        self, isolated_workspace, capsys: pytest.CaptureFixture
    ):
        """D1: pidfile points at a dead PID; state.json says connected. Status
        must NOT lie about it being connected."""
        _agent_dir, sd = isolated_workspace
        _write_dead_pid_state(sd)
        result = _run_status(capsys)
        assert result.get("connected") is not True, (
            "status returned connected=true even though the daemon is dead. "
            "This is the D1 bug — agents follow stale state and never reconnect."
        )
        assert result.get("status") in ("stopped", "daemon_dead", "not_connected"), (
            f"expected a stopped/dead/not_connected status, got: {result}"
        )

    def test_no_pidfile_no_state_returns_not_connected(
        self, isolated_workspace, capsys: pytest.CaptureFixture
    ):
        """Sanity: clean slate is not_connected (existing behavior)."""
        _agent_dir, _sd = isolated_workspace
        result = _run_status(capsys)
        assert result.get("status") == "not_connected"


class TestLeaveClearsStateJson:
    """D2: ``goosetown leave`` must invalidate state.json so subsequent
    status calls don't return stale connected=true."""

    def test_leave_removes_or_invalidates_state_json(
        self, isolated_workspace, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
    ):
        """After leave's daemon-shutdown phase, state.json should not say
        connected=true. The most direct test is that status no longer
        returns connected=true after a leave-style cleanup runs."""
        _agent_dir, sd = isolated_workspace
        # Simulate a leave sequence: daemon was alive, leave killed the
        # daemon process, removed the pidfile, AND should now clear stale
        # state.
        _write_alive_state(sd)

        # Manually invoke the cleanup helper that leave should call.
        # Helper is named _clear_daemon_state (added in this fix).
        if not hasattr(core, "_clear_daemon_state"):
            pytest.fail(
                "core._clear_daemon_state(sd) helper missing — leave must "
                "clear state.json on clean shutdown."
            )
        core._clear_daemon_state(sd)

        # Now status should NOT return connected=true.
        result = _run_status(capsys)
        assert result.get("connected") is not True, (
            "After leave, state.json was not cleared. Status will keep "
            "returning connected=true forever — the D2 stale-cache bug."
        )
