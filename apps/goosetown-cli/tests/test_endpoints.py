"""Tests for endpoint constants and the --dev / --prod flag handling."""

import os

import pytest

from goosetown.endpoints import ENDPOINTS, DEFAULT_ENV, apply_env_override


class TestEndpointConstants:
    def test_prod_api_url(self):
        assert ENDPOINTS["prod"]["api"] == "https://api.goosetown.thelightbulb.company/api/v1"

    def test_prod_ws_url(self):
        assert ENDPOINTS["prod"]["ws"] == "wss://ws.goosetown.thelightbulb.company"

    def test_dev_api_url(self):
        assert ENDPOINTS["dev"]["api"] == "https://api-dev.goosetown.thelightbulb.company/api/v1"

    def test_dev_ws_url(self):
        assert ENDPOINTS["dev"]["ws"] == "wss://ws-dev.goosetown.thelightbulb.company"

    def test_default_env_is_prod(self):
        assert DEFAULT_ENV == "prod"

    def test_unknown_env_raises_key_error(self):
        with pytest.raises(KeyError):
            apply_env_override("staging")


class TestApplyEnvOverride:
    """apply_env_override writes the correct values into os.environ."""

    def test_dev_sets_dev_env_vars(self, monkeypatch):
        monkeypatch.delenv("TOWN_API_URL", raising=False)
        monkeypatch.delenv("TOWN_WS_URL", raising=False)
        apply_env_override("dev")
        assert os.environ["TOWN_API_URL"] == ENDPOINTS["dev"]["api"]
        assert os.environ["TOWN_WS_URL"] == ENDPOINTS["dev"]["ws"]

    def test_prod_sets_prod_env_vars(self, monkeypatch):
        monkeypatch.delenv("TOWN_API_URL", raising=False)
        monkeypatch.delenv("TOWN_WS_URL", raising=False)
        apply_env_override("prod")
        assert os.environ["TOWN_API_URL"] == ENDPOINTS["prod"]["api"]
        assert os.environ["TOWN_WS_URL"] == ENDPOINTS["prod"]["ws"]

    def test_override_replaces_existing_value(self, monkeypatch):
        monkeypatch.setenv("TOWN_API_URL", "https://some-other-url.example.com/api")
        apply_env_override("dev")
        assert os.environ["TOWN_API_URL"] == ENDPOINTS["dev"]["api"]

    def test_prod_override_after_dev(self, monkeypatch):
        """Calling apply_env_override('prod') after 'dev' switches back to prod."""
        monkeypatch.delenv("TOWN_API_URL", raising=False)
        apply_env_override("dev")
        apply_env_override("prod")
        assert os.environ["TOWN_API_URL"] == ENDPOINTS["prod"]["api"]
        assert os.environ["TOWN_WS_URL"] == ENDPOINTS["prod"]["ws"]


class TestCliEnvFlags:
    """Integration: the --dev / --prod flags inject the right env vars via cli.main."""

    def test_dev_flag_sets_dev_env(self, monkeypatch):
        """--dev sets TOWN_API_URL to dev endpoint before the command runs."""
        monkeypatch.delenv("TOWN_API_URL", raising=False)
        monkeypatch.delenv("TOWN_WS_URL", raising=False)

        # Patch cmd_join so we don't actually hit the network
        import goosetown.core as core_mod

        joined_api_url: list[str] = []

        def fake_join(args):
            joined_api_url.append(os.environ.get("TOWN_API_URL", ""))

        monkeypatch.setattr(core_mod, "cmd_join", fake_join)

        from goosetown.cli import main

        # Simulate: goosetown --dev join <token> --name x --appearance y
        try:
            main(["--dev", "join", "tok", "--name", "x", "--appearance", "y"])
        except SystemExit:
            pass

        assert joined_api_url, "cmd_join was not called"
        assert joined_api_url[0] == ENDPOINTS["dev"]["api"]

    def test_prod_flag_sets_prod_env(self, monkeypatch):
        """--prod sets TOWN_API_URL to prod endpoint."""
        monkeypatch.delenv("TOWN_API_URL", raising=False)
        monkeypatch.delenv("TOWN_WS_URL", raising=False)

        import goosetown.core as core_mod

        joined_api_url: list[str] = []

        def fake_join(args):
            joined_api_url.append(os.environ.get("TOWN_API_URL", ""))

        monkeypatch.setattr(core_mod, "cmd_join", fake_join)

        from goosetown.cli import main

        try:
            main(["--prod", "join", "tok", "--name", "x", "--appearance", "y"])
        except SystemExit:
            pass

        assert joined_api_url
        assert joined_api_url[0] == ENDPOINTS["prod"]["api"]

    def test_no_flag_leaves_env_untouched(self, monkeypatch):
        """Without an explicit --dev / --prod flag, ``cli.main`` must NOT
        rewrite ``TOWN_API_URL`` / ``TOWN_WS_URL``.

        Regression guard for the bug Jim hit during the live agent-team
        test on 2026-05-02: when ``cmd_join`` spawned the daemon
        subprocess (``goosetown _daemon``), the spawned child's
        ``cli.main`` was applying ``apply_env_override("prod")``
        unconditionally, overwriting the dev URLs the parent
        (``goosetown --dev join …``) had set in the child's environment.
        That made the daemon connect to prod instead of dev.

        Default-to-prod for first-time users is handled by
        ``_load_config`` further downstream — not by this env layer.
        """
        monkeypatch.setenv("TOWN_API_URL", "https://from-parent.example.com/api/v1")
        monkeypatch.setenv("TOWN_WS_URL", "wss://from-parent.example.com")

        import goosetown.core as core_mod

        seen_api_url: list[str] = []
        seen_ws_url: list[str] = []

        def fake_join(args):
            seen_api_url.append(os.environ.get("TOWN_API_URL", ""))
            seen_ws_url.append(os.environ.get("TOWN_WS_URL", ""))

        monkeypatch.setattr(core_mod, "cmd_join", fake_join)

        from goosetown.cli import main

        try:
            main(["join", "tok", "--name", "x", "--appearance", "y"])
        except SystemExit:
            pass

        assert seen_api_url, "cmd_join was not called"
        # Env values from the parent must be preserved verbatim.
        assert seen_api_url[0] == "https://from-parent.example.com/api/v1"
        assert seen_ws_url[0] == "wss://from-parent.example.com"

    def test_daemon_subcommand_does_not_override_parent_env(self, monkeypatch):
        """``goosetown _daemon`` (the subprocess spawned by _start_daemon)
        must inherit TOWN_API_URL / TOWN_WS_URL from its parent without
        re-overriding to prod. This is the exact path Jim's daemon went
        through during the agent-team test.
        """
        monkeypatch.setenv("TOWN_API_URL", ENDPOINTS["dev"]["api"])
        monkeypatch.setenv("TOWN_WS_URL", ENDPOINTS["dev"]["ws"])

        import goosetown.daemon as daemon_mod

        seen_api_url: list[str] = []

        def fake_daemon_main():
            seen_api_url.append(os.environ.get("TOWN_API_URL", ""))

        monkeypatch.setattr(daemon_mod, "daemon_main", fake_daemon_main)

        from goosetown.cli import main

        try:
            main(["_daemon"])
        except SystemExit:
            pass

        assert seen_api_url, "daemon_main was not invoked"
        assert seen_api_url[0] == ENDPOINTS["dev"]["api"], (
            "daemon child overrode parent's TOWN_API_URL — would have connected to prod"
        )

    def test_both_flags_error(self):
        """Passing --dev and --prod together must exit with code 2 (argparse error)."""
        from goosetown.cli import main

        with pytest.raises(SystemExit) as exc_info:
            main(["--dev", "--prod", "status"])
        assert exc_info.value.code == 2
