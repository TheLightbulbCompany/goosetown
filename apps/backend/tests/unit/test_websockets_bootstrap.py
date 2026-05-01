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
