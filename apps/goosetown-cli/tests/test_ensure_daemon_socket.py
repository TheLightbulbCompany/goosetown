"""Tests for ``_ensure_daemon`` waiting on the daemon's Unix socket (finding 4).

After a daemon crash, ``cmd_act`` auto-restarts it via ``_ensure_daemon`` then
immediately ``ipc_send``s to ``daemon.sock``. ``_start_daemon`` returns as soon
as the daemon writes ``state.json`` — but the socket is created *after* that, so
the act raced into "Daemon socket not found". ``_ensure_daemon`` must poll for
the socket to appear before returning.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from goosetown import core


def _config(name: str = "tessa") -> dict:
    return {
        "agent": name,
        "token": "tok",
        "ws_url": "wss://x",
        "workspace_path": "/tmp/ws",
    }


def test_ensure_daemon_returns_once_socket_exists(tmp_path: Path) -> None:
    """When _start_daemon creates the socket, _ensure_daemon returns with it present."""
    sd = tmp_path / "state"
    sd.mkdir()
    sock_path = sd / "daemon.sock"

    # No live daemon yet, so _ensure_daemon must call _start_daemon.
    def fake_start(config: dict, agent_dir: Path) -> dict:
        sock_path.write_text("")  # daemon creates its socket
        return {"status": "ok"}

    with (
        patch("goosetown.core.state_dir", return_value=sd),
        patch("goosetown.core._is_daemon_alive", return_value=False),
        patch("goosetown.core._start_daemon", side_effect=fake_start) as start_mock,
    ):
        core._ensure_daemon(_config(), tmp_path)

    start_mock.assert_called_once()
    assert sock_path.exists()


def test_ensure_daemon_waits_for_delayed_socket(tmp_path: Path) -> None:
    """The socket appears a few poll cycles after _start_daemon returns —
    _ensure_daemon must keep polling until it shows up, not return early."""
    sd = tmp_path / "state"
    sd.mkdir()
    sock_path = sd / "daemon.sock"

    state = {"polls": 0}

    def fake_start(config: dict, agent_dir: Path) -> dict:
        return {"status": "ok"}  # socket NOT created yet

    real_exists = Path.exists

    def delayed_exists(self: Path) -> bool:
        if self == sock_path:
            state["polls"] += 1
            if state["polls"] >= 3:
                sock_path.write_text("")  # socket finally created
            return real_exists(self)
        return real_exists(self)

    with (
        patch("goosetown.core.state_dir", return_value=sd),
        patch("goosetown.core._is_daemon_alive", return_value=False),
        patch("goosetown.core._start_daemon", side_effect=fake_start),
        patch("time.sleep", return_value=None),
        patch.object(Path, "exists", delayed_exists),
    ):
        core._ensure_daemon(_config(), tmp_path)

    assert sock_path.exists()
    assert state["polls"] >= 3


def test_ensure_daemon_returns_after_timeout_without_raising(tmp_path: Path) -> None:
    """If the socket never appears, _ensure_daemon returns (no exception) and lets
    the caller's error path surface 'Daemon socket not found'."""
    sd = tmp_path / "state"
    sd.mkdir()
    sock_path = sd / "daemon.sock"

    def fake_start(config: dict, agent_dir: Path) -> dict:
        return {"status": "ok"}  # never creates the socket

    with (
        patch("goosetown.core.state_dir", return_value=sd),
        patch("goosetown.core._is_daemon_alive", return_value=False),
        patch("goosetown.core._start_daemon", side_effect=fake_start),
        patch("time.sleep", return_value=None),
    ):
        # Must not raise even though the socket never appears.
        core._ensure_daemon(_config(), tmp_path)

    assert not sock_path.exists()
