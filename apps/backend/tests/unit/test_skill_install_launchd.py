"""Tests for the install-launchd subcommand in goosetown_cli.py."""

import plistlib
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_SKILL_DIR = Path(__file__).parents[2] / "data" / "goosetown-skill" / "skills" / "goosetown"
sys.path.insert(0, str(_SKILL_DIR))

from goosetown_cli import _render_launchd_plist, _build_parser  # noqa: E402


class TestRenderLaunchdPlist:
    def test_produces_valid_plist(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        raw = _render_launchd_plist(
            agent_name="test-agent",
            agent_workspace=workspace,
            python_path="/usr/bin/python3",
            cli_path="/home/user/.claude/skills/goosetown/goosetown_cli.py",
        )
        parsed = plistlib.loads(raw)
        assert isinstance(parsed, dict), "plist must deserialise to a dict"

    def test_label_contains_agent_name(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        raw = _render_launchd_plist(
            agent_name="marisol",
            agent_workspace=workspace,
            python_path="/usr/bin/python3",
            cli_path="/skill/goosetown_cli.py",
        )
        parsed = plistlib.loads(raw)
        assert parsed["Label"] == "com.goosetown.marisol"

    def test_program_arguments_correct(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        cli = "/skill/goosetown_cli.py"
        python = "/usr/bin/python3"
        raw = _render_launchd_plist(
            agent_name="damian",
            agent_workspace=workspace,
            python_path=python,
            cli_path=cli,
        )
        parsed = plistlib.loads(raw)
        assert parsed["ProgramArguments"] == [python, cli, "daemon-resume", "damian"]

    def test_keepalive_flags(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        raw = _render_launchd_plist(
            agent_name="yuki",
            agent_workspace=workspace,
            python_path="/usr/bin/python3",
            cli_path="/skill/goosetown_cli.py",
        )
        parsed = plistlib.loads(raw)
        assert parsed["KeepAlive"]["Crashed"] is True
        assert parsed["KeepAlive"]["NetworkState"] is True

    def test_run_at_load(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        raw = _render_launchd_plist(
            agent_name="reggie",
            agent_workspace=workspace,
            python_path="/usr/bin/python3",
            cli_path="/skill/goosetown_cli.py",
        )
        parsed = plistlib.loads(raw)
        assert parsed["RunAtLoad"] is True

    def test_stdout_stderr_paths_inside_state_dir(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        raw = _render_launchd_plist(
            agent_name="ines",
            agent_workspace=workspace,
            python_path="/usr/bin/python3",
            cli_path="/skill/goosetown_cli.py",
        )
        parsed = plistlib.loads(raw)
        assert "ines" in parsed["StandardOutPath"]
        assert "ines" in parsed["StandardErrorPath"]
        assert parsed["StandardOutPath"].endswith(".log")
        assert parsed["StandardErrorPath"].endswith(".log")

    def test_working_directory_is_workspace(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        raw = _render_launchd_plist(
            agent_name="felix",
            agent_workspace=workspace,
            python_path="/usr/bin/python3",
            cli_path="/skill/goosetown_cli.py",
        )
        parsed = plistlib.loads(raw)
        assert parsed["WorkingDirectory"] == str(workspace)

    def test_stable_output_across_two_renders(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        kwargs = dict(
            agent_name="stable-test",
            agent_workspace=workspace,
            python_path="/usr/bin/python3",
            cli_path="/skill/goosetown_cli.py",
        )
        assert _render_launchd_plist(**kwargs) == _render_launchd_plist(**kwargs)


class TestDryRun:
    def test_dry_run_writes_nothing_to_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """--dry-run must not write any file under ~/Library/LaunchAgents."""
        # Redirect HOME so we can assert nothing was written there.
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        fake_workspace = tmp_path / "workspace"
        fake_workspace.mkdir()
        # Write a GOOSETOWN.md so cmd_install_launchd can find the workspace.
        goosetown_md = fake_workspace / "GOOSETOWN.md"
        goosetown_md.write_text(
            "token: tok\nws_url: wss://example.com\napi_url: https://example.com\n"
            "agent: dry-agent\nworkspace_path: " + str(fake_workspace) + "\n"
        )

        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("AGENT_DIR", str(fake_workspace))

        # Patch Path.home() to return our fake home.
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        from goosetown_cli import cmd_install_launchd

        parser = _build_parser()
        args = parser.parse_args(["install-launchd", "--dry-run", "dry-agent"])

        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_install_launchd(args)

        assert rc == 0
        # Nothing written under fake home's LaunchAgents directory.
        launch_agents = fake_home / "Library" / "LaunchAgents"
        assert not launch_agents.exists() or not list(launch_agents.iterdir())

        # Output contains a valid plist.
        output = buf.getvalue()
        parsed = plistlib.loads(output.encode())
        assert parsed["Label"] == "com.goosetown.dry-agent"
