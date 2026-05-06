"""TDD: opt-in setup-hooks subcommand writes valid Claude Code hook config.

Ported from apps/backend/tests/unit/test_setup_hooks.py and updated to use
the goosetown package entry point rather than the standalone script path.
"""

import json
import os
import subprocess
import sys
from pathlib import Path


def _run_setup_hooks(workspace: Path) -> int:
    """Invoke the setup-hooks subcommand with cwd=<workspace>."""
    from goosetown.core import cmd_setup_hooks

    old_cwd = os.getcwd()
    os.chdir(workspace)
    try:
        return cmd_setup_hooks()
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
    """Shell-invocation path: exit code from setup-hooks must reach the shell."""
    result = subprocess.run(  # nosec B603
        [sys.executable, "-m", "goosetown", "setup-hooks"],
        cwd=tmp_path,
        capture_output=True,
    )
    assert result.returncode == 1, (
        f"expected exit 1 from missing .claude/, got {result.returncode}. "
        f"stderr: {result.stderr.decode()}"
    )


def test_setup_hooks_idempotent_when_hook_at_index_other_than_zero(tmp_path):
    """If check-town-status.sh is already at index 1+ in a matcher's hook list,
    setup-hooks must NOT append a duplicate."""
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    settings_file = settings_dir / "settings.local.json"

    seeded = {
        "hooks": {
            "Stop": [
                {
                    "matcher": "",
                    "hooks": [
                        {"type": "command", "command": "echo unrelated"},
                        {
                            "type": "command",
                            "command": "bash /path/to/check-town-status.sh",
                        },
                    ],
                }
            ]
        }
    }
    settings_file.write_text(json.dumps(seeded, indent=2))

    _run_setup_hooks(tmp_path)

    written = json.loads(settings_file.read_text())
    stop_entries = written["hooks"]["Stop"]
    assert len(stop_entries) == 1, (
        f"expected 1 Stop matcher group (deduped), got {len(stop_entries)}"
    )
