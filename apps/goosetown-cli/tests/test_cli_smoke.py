"""Smoke tests for the goosetown CLI entry point."""

import subprocess
import sys


from goosetown import __version__


class TestVersionFlag:
    def test_version_matches_package_version(self):
        """goosetown --version should print the package __version__."""
        result = subprocess.run(
            [sys.executable, "-m", "goosetown", "--version"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert __version__ in result.stdout

    def test_version_format(self):
        """Version output must be 'goosetown/<version>'."""
        result = subprocess.run(
            [sys.executable, "-m", "goosetown", "--version"],
            capture_output=True,
            text=True,
        )
        assert f"goosetown/{__version__}" in result.stdout


class TestHelpFlag:
    def test_help_exits_zero(self):
        result = subprocess.run(
            [sys.executable, "-m", "goosetown", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

    def test_help_lists_dev_flag(self):
        result = subprocess.run(
            [sys.executable, "-m", "goosetown", "--help"],
            capture_output=True,
            text=True,
        )
        assert "--dev" in result.stdout

    def test_help_lists_prod_flag(self):
        result = subprocess.run(
            [sys.executable, "-m", "goosetown", "--help"],
            capture_output=True,
            text=True,
        )
        assert "--prod" in result.stdout

    def test_help_lists_subcommands(self):
        result = subprocess.run(
            [sys.executable, "-m", "goosetown", "--help"],
            capture_output=True,
            text=True,
        )
        for cmd in ("join", "status", "act", "leave", "setup-hooks"):
            assert cmd in result.stdout, (
                f"subcommand '{cmd}' missing from --help output"
            )

    def test_join_subcommand_help(self):
        """goosetown join --help should show join-specific flags."""
        result = subprocess.run(
            [sys.executable, "-m", "goosetown", "join", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "--name" in result.stdout
        assert "--appearance" in result.stdout
        assert "--personality" in result.stdout
        assert "--traits" in result.stdout

    def test_dev_join_help(self):
        """goosetown --dev join --help should work (flags before subcommand)."""
        result = subprocess.run(
            [sys.executable, "-m", "goosetown", "--dev", "join", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "--name" in result.stdout


class TestNonexistentSubcommand:
    def test_nonexistent_subcommand_exits_2(self):
        """An unknown subcommand should exit with code 2 (argparse convention)."""
        result = subprocess.run(
            [sys.executable, "-m", "goosetown", "nonexistent"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2

    def test_no_subcommand_exits_nonzero(self):
        """No subcommand at all should exit non-zero."""
        result = subprocess.run(
            [sys.executable, "-m", "goosetown"],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0


class TestMainEntrypoint:
    """Test the python -m goosetown module entry point."""

    def test_main_module_is_importable(self):
        result = subprocess.run(
            [sys.executable, "-c", "import goosetown; print(goosetown.__version__)"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert __version__ in result.stdout

    def test_main_module_run_version(self):
        result = subprocess.run(
            [sys.executable, "-m", "goosetown", "--version"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
