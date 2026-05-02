"""Tests for endpoint constants and the --dev / --prod flag handling."""

import os

import pytest

from goosetown.endpoints import ENDPOINTS, DEFAULT_ENV, apply_env_override


class TestEndpointConstants:
    def test_prod_api_url(self):
        assert ENDPOINTS["prod"]["api"] == "https://api.goosetown.isol8.co/api/v1"

    def test_prod_ws_url(self):
        assert ENDPOINTS["prod"]["ws"] == "wss://ws.goosetown.isol8.co"

    def test_dev_api_url(self):
        assert ENDPOINTS["dev"]["api"] == "https://api-dev.goosetown.isol8.co/api/v1"

    def test_dev_ws_url(self):
        assert ENDPOINTS["dev"]["ws"] == "wss://ws-dev.goosetown.isol8.co"

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

    def test_no_flag_defaults_to_prod(self, monkeypatch):
        """Without --dev or --prod, the prod endpoint is used."""
        monkeypatch.delenv("TOWN_API_URL", raising=False)
        monkeypatch.delenv("TOWN_WS_URL", raising=False)

        import goosetown.core as core_mod

        joined_api_url: list[str] = []

        def fake_join(args):
            joined_api_url.append(os.environ.get("TOWN_API_URL", ""))

        monkeypatch.setattr(core_mod, "cmd_join", fake_join)

        from goosetown.cli import main

        try:
            main(["join", "tok", "--name", "x", "--appearance", "y"])
        except SystemExit:
            pass

        assert joined_api_url
        assert joined_api_url[0] == ENDPOINTS["prod"]["api"]

    def test_both_flags_error(self):
        """Passing --dev and --prod together must exit with code 2 (argparse error)."""
        from goosetown.cli import main

        with pytest.raises(SystemExit) as exc_info:
            main(["--dev", "--prod", "status"])
        assert exc_info.value.code == 2
