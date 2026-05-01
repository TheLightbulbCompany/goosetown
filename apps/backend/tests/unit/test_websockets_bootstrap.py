"""TDD: daemon auto-installs websockets on first run if missing."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

_SKILL_DIR = Path(__file__).parents[2] / "data" / "goosetown-skill" / "skills" / "goosetown"
sys.path.insert(0, str(_SKILL_DIR))


def test_ensure_websockets_already_imported_returns_silently(capsys):
    """When websockets is already importable, the bootstrap is a no-op."""
    from goosetown_cli import _ensure_websockets

    _ensure_websockets()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_ensure_websockets_pip_installs_if_missing(monkeypatch):
    """When websockets is missing, the bootstrap calls pip install --user."""
    from goosetown_cli import _ensure_websockets

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__
    state = {"first_call": True}

    def fake_import(name, *args, **kwargs):
        if name == "websockets" and state["first_call"]:
            state["first_call"] = False
            raise ImportError("simulated missing websockets")
        return real_import(name, *args, **kwargs)

    fake_run = MagicMock(return_value=0)
    monkeypatch.setattr("subprocess.check_call", fake_run)
    monkeypatch.setattr("builtins.__import__", fake_import)

    _ensure_websockets()
    fake_run.assert_called_once()
    args = fake_run.call_args.args[0]
    assert args[0] == sys.executable
    assert args[1:4] == ["-m", "pip", "install"]
    assert "--user" in args
    assert "websockets" in args


def test_ensure_websockets_exits_on_pip_failure(monkeypatch, capsys):
    """When pip install fails, the daemon exits with code 2 and a clear message."""
    import subprocess

    import pytest as _pytest
    from goosetown_cli import _ensure_websockets

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def fake_import(name, *args, **kwargs):
        if name == "websockets":
            raise ImportError("simulated missing websockets")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    monkeypatch.setattr(
        "subprocess.check_call",
        MagicMock(side_effect=subprocess.CalledProcessError(1, "pip")),
    )

    with _pytest.raises(SystemExit) as exc:
        _ensure_websockets()
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "FATAL" in captured.err
    assert "pip3 install --user websockets" in captured.err


def test_status_command_does_not_invoke_bootstrap(monkeypatch):
    """status is documented as offline/stdlib-only; it must not trigger
    pip install of websockets even when websockets is missing."""
    import importlib

    # Tracking: did _ensure_websockets get called?
    called = {"count": 0}

    # Reload goosetown_cli with the spy
    if "goosetown_cli" in sys.modules:
        del sys.modules["goosetown_cli"]

    import goosetown_cli  # noqa: F401 — re-imports module fresh

    original = goosetown_cli._ensure_websockets

    def spy() -> None:
        called["count"] += 1
        original()

    monkeypatch.setattr(goosetown_cli, "_ensure_websockets", spy)

    # Invoke status via main() with patched argv
    monkey_argv = ["goosetown_cli.py", "status"]
    monkeypatch.setattr(sys, "argv", monkey_argv)
    try:
        rc = goosetown_cli.main()
    except SystemExit as exc:
        rc = exc.code

    # status should NOT call _ensure_websockets — it's offline by design
    assert called["count"] == 0, (
        f"status invoked _ensure_websockets {called['count']}x; "
        "it must stay offline-capable per docs."
    )


def test_setup_hooks_command_does_not_invoke_bootstrap(monkeypatch, tmp_path):
    """setup-hooks doesn't open any WS — must not trigger pip install."""
    if "goosetown_cli" in sys.modules:
        del sys.modules["goosetown_cli"]
    import goosetown_cli  # noqa: F401

    called = {"count": 0}
    original = goosetown_cli._ensure_websockets

    def spy() -> None:
        called["count"] += 1
        original()

    monkeypatch.setattr(goosetown_cli, "_ensure_websockets", spy)

    (tmp_path / ".claude").mkdir()
    import os
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        monkeypatch.setattr(sys, "argv", ["goosetown_cli.py", "setup-hooks"])
        try:
            rc = goosetown_cli.main()
        except SystemExit as exc:
            rc = exc.code
    finally:
        os.chdir(old_cwd)

    assert called["count"] == 0, (
        f"setup-hooks invoked _ensure_websockets {called['count']}x; "
        "it doesn't need a WebSocket connection."
    )
