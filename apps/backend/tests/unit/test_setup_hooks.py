"""TDD: opt-in setup-hooks subcommand writes valid Claude Code hook config."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

_SKILL_DIR = Path(__file__).parents[2] / "data" / "goosetown-skill" / "skills" / "goosetown"
sys.path.insert(0, str(_SKILL_DIR))


def _run_setup_hooks(workspace: Path) -> int:
    """Invoke the setup-hooks subcommand with cwd=<workspace>."""
    from goosetown_cli import main

    monkey_argv = ["goosetown_cli.py", "setup-hooks"]
    with patch.object(sys, "argv", monkey_argv):
        old_cwd = os.getcwd()
        os.chdir(workspace)
        try:
            return main()
        finally:
            os.chdir(old_cwd)


def test_setup_hooks_writes_settings_local_json(tmp_path):
    """First-run creates .claude/settings.local.json with Stop and TeammateIdle hooks."""
    (tmp_path / ".claude").mkdir()
    rc = _run_setup_hooks(tmp_path)
    assert rc == 0

    settings = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
    assert "hooks" in settings
    for event in ("Stop", "TeammateIdle"):
        assert event in settings["hooks"]
        cmd = settings["hooks"][event][0]["hooks"][0]["command"]
        assert "check-town-status.sh" in cmd


def test_setup_hooks_is_idempotent(tmp_path):
    """Re-running setup-hooks doesn't duplicate entries."""
    (tmp_path / ".claude").mkdir()
    _run_setup_hooks(tmp_path)
    _run_setup_hooks(tmp_path)

    settings = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
    assert len(settings["hooks"]["Stop"]) == 1
    assert len(settings["hooks"]["TeammateIdle"]) == 1


def test_setup_hooks_refuses_when_dot_claude_missing(tmp_path, capsys):
    """If .claude/ doesn't exist, setup-hooks exits non-zero with a clear message."""
    rc = _run_setup_hooks(tmp_path)
    assert rc != 0
    captured = capsys.readouterr()
    assert ".claude" in captured.err.lower()


def test_setup_hooks_exit_code_propagates_to_shell(tmp_path):
    """Shell-invocation path: exit code from cmd_setup_hooks() must reach
    sys.exit, not be swallowed by main()."""
    import subprocess

    cli = Path(__file__).parents[2] / "data" / "goosetown-skill" / "skills" / "goosetown" / "goosetown_cli.py"
    # Workspace without .claude/ — setup-hooks must exit 1
    result = subprocess.run(
        [sys.executable, str(cli), "setup-hooks"],
        cwd=tmp_path,
        capture_output=True,
    )
    assert result.returncode == 1, (
        f"expected exit 1 from missing .claude/, got {result.returncode}. stderr: {result.stderr.decode()}"
    )
