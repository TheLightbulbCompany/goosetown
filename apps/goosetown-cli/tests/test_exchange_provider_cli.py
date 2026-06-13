"""Tests for the Wave 2 provider-onboarding CLI (Task 8, spec §3.4).

Covers:
- ``goosetown provider register/update/off`` payload construction + flag
  parsing (cmd_provider → daemon act, mirroring the cmd_act IPC harness).
- ``post_job --category=`` / ``--tags=`` (position-independent via ``_pop_flag``,
  trailing + leading, like the 0.4.1 fix).
- ``join --provider ...`` one-shot setup sends a provider_register after join.
- ``join --harness=`` records ``harnessType`` in GOOSETOWN.md.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

import pytest

from goosetown.core import cmd_act, cmd_join, cmd_provider


# ---------------------------------------------------------------------------
# provider register / update / off — payload construction
# ---------------------------------------------------------------------------


def _run_cmd_provider(subcommand: str, extra_args: list[str]) -> dict:
    """Build the provider Namespace, run cmd_provider, return the act payload."""
    captured: list[dict] = []

    def fake_ipc(sock_path, msg):
        captured.append(msg)
        return {"status": "ok"}

    ns = argparse.Namespace(provider_command=subcommand, args=extra_args)

    with (
        patch("goosetown.core.load_config", return_value={"agent": "tester"}),
        patch("goosetown.core.state_dir", return_value=Path("/tmp/fake")),
        patch("goosetown.core._ensure_daemon"),
        patch("goosetown.core._ensure_websockets"),
        patch("goosetown.core._require_websockets"),
        patch("goosetown.core.ipc_send", side_effect=fake_ipc),
        patch("goosetown.core._out"),
    ):
        cmd_provider(ns)

    assert captured, "ipc_send was never called"
    return captured[0]["payload"]


def _run_provider_expecting_die(subcommand: str, extra_args: list[str]) -> None:
    ns = argparse.Namespace(provider_command=subcommand, args=extra_args)
    with (
        patch("goosetown.core.load_config", return_value={"agent": "tester"}),
        patch("goosetown.core.state_dir", return_value=Path("/tmp/fake")),
        patch("goosetown.core._ensure_daemon"),
        patch("goosetown.core._ensure_websockets"),
        patch("goosetown.core._require_websockets"),
        pytest.raises(SystemExit),
    ):
        cmd_provider(ns)


class TestProviderRegister:
    def test_full_payload(self):
        payload = _run_cmd_provider(
            "register",
            [
                "--skills=haiku,research",
                "--categories=writing,research",
                "--bio=I write things.",
                "--min-price=5",
            ],
        )
        assert payload == {
            "action": "provider_register",
            "skills": ["haiku", "research"],
            "categories": ["writing", "research"],
            "bio": "I write things.",
            "min_price": 5,
        }

    def test_min_price_optional(self):
        payload = _run_cmd_provider(
            "register",
            ["--skills=haiku", "--categories=writing", "--bio=hi"],
        )
        assert "min_price" not in payload
        assert payload["skills"] == ["haiku"]
        assert payload["categories"] == ["writing"]

    def test_skills_optional(self):
        # Categories are the only hard requirement (the provider must subscribe
        # to something to be discoverable). Skills/bio may be omitted.
        payload = _run_cmd_provider("register", ["--categories=writing"])
        assert payload["action"] == "provider_register"
        assert payload["categories"] == ["writing"]
        assert payload["skills"] == []
        assert payload["bio"] == ""

    def test_flags_position_independent(self):
        # Order of flags must not matter (each is position-independent _pop_flag).
        payload = _run_cmd_provider(
            "register",
            ["--bio=hi", "--min-price=2", "--categories=writing", "--skills=a,b"],
        )
        assert payload["skills"] == ["a", "b"]
        assert payload["categories"] == ["writing"]
        assert payload["bio"] == "hi"
        assert payload["min_price"] == 2

    def test_blank_skill_items_dropped(self):
        payload = _run_cmd_provider(
            "register", ["--skills=a, ,b,", "--categories=writing"]
        )
        assert payload["skills"] == ["a", "b"]

    def test_missing_categories_dies(self):
        _run_provider_expecting_die("register", ["--skills=a", "--bio=hi"])

    def test_empty_categories_dies(self):
        _run_provider_expecting_die("register", ["--categories="])

    def test_non_int_min_price_dies(self):
        _run_provider_expecting_die(
            "register", ["--categories=writing", "--min-price=lots"]
        )

    def test_negative_min_price_dies(self):
        _run_provider_expecting_die(
            "register", ["--categories=writing", "--min-price=-1"]
        )


class TestProviderUpdate:
    def test_update_is_alias_for_register(self):
        # `provider update` replaces the profile — identical payload to register.
        payload = _run_cmd_provider(
            "update", ["--skills=a", "--categories=research", "--bio=new bio"]
        )
        assert payload["action"] == "provider_register"
        assert payload["categories"] == ["research"]
        assert payload["bio"] == "new bio"


class TestProviderOff:
    def test_off_payload(self):
        payload = _run_cmd_provider("off", [])
        assert payload == {"action": "provider_off"}

    def test_off_ignores_extra_flags(self):
        payload = _run_cmd_provider("off", ["--skills=ignored"])
        assert payload == {"action": "provider_off"}


# ---------------------------------------------------------------------------
# post_job --category / --tags (position-independent)
# ---------------------------------------------------------------------------


def _run_cmd_act(action: str, extra_args: list[str]) -> dict:
    captured: list[dict] = []

    def fake_ipc(sock_path, msg):
        captured.append(msg)
        return {"status": "ok"}

    ns = argparse.Namespace(action=action, args=extra_args)
    with (
        patch("goosetown.core.load_config", return_value={"agent": "tester"}),
        patch("goosetown.core.state_dir", return_value=Path("/tmp/fake")),
        patch("goosetown.core._ensure_daemon"),
        patch("goosetown.core._ensure_websockets"),
        patch("goosetown.core._require_websockets"),
        patch("goosetown.core.ipc_send", side_effect=fake_ipc),
        patch("goosetown.core._out"),
    ):
        cmd_act(ns)
    assert captured, "ipc_send was never called"
    return captured[0]["payload"]


class TestPostJobCategoryTags:
    def test_category_trailing(self):
        payload = _run_cmd_act(
            "post_job", ["20", "title", "Write five bullets.", "--category=writing"]
        )
        assert payload["category"] == "writing"
        assert payload["description"] == "Write five bullets."

    def test_category_leading(self):
        payload = _run_cmd_act(
            "post_job", ["--category=research", "20", "title", "the description"]
        )
        assert payload["category"] == "research"
        assert payload["max_price"] == 20
        assert payload["title"] == "title"
        assert payload["description"] == "the description"

    def test_tags_trailing(self):
        payload = _run_cmd_act("post_job", ["20", "title", "desc", "--tags=fast,cheap"])
        assert payload["tags"] == ["fast", "cheap"]

    def test_category_and_tags_together(self):
        payload = _run_cmd_act(
            "post_job",
            [
                "--category=technology.code-review",
                "20",
                "title",
                "desc",
                "--tags=python,rust",
            ],
        )
        assert payload["category"] == "technology.code-review"
        assert payload["tags"] == ["python", "rust"]

    def test_category_and_strategy_and_criteria_coexist(self):
        payload = _run_cmd_act(
            "post_job",
            [
                "--strategy=best_quality",
                "20",
                "title",
                "desc",
                "--category=writing",
                "--criteria=a;b",
            ],
        )
        assert payload["strategy"] == "best_quality"
        assert payload["category"] == "writing"
        assert payload["success_criteria"] == ["a", "b"]

    def test_category_omitted_not_in_payload(self):
        payload = _run_cmd_act("post_job", ["20", "title", "desc"])
        assert "category" not in payload
        assert "tags" not in payload

    def test_blank_tag_items_dropped(self):
        payload = _run_cmd_act("post_job", ["20", "title", "desc", "--tags=a, ,b,"])
        assert payload["tags"] == ["a", "b"]


# ---------------------------------------------------------------------------
# join --provider one-shot + --harness recording
# ---------------------------------------------------------------------------


def _join_namespace(tmp_path: Path, **overrides) -> argparse.Namespace:
    base = dict(
        token="tok-123",
        api_url=None,
        name="prov",
        appearance="a tiny pixel goose",
        personality="curious",
        traits="studious",
        harness=None,
        provider=False,
        skills=None,
        categories=None,
        bio=None,
        min_price=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _register_response() -> dict:
    return {
        "agent_id": "uuid-prov",
        "ws_url": "wss://example.invalid",
        "api_url": "https://api.example.invalid/api/v1",
        "agent_name": "prov",
    }


def _run_join(tmp_path: Path, ns: argparse.Namespace, provider_calls: list[dict]):
    """Run cmd_join with the register HTTP mocked + daemon/provider mocked.

    provider_calls collects any provider_register act payloads emitted by the
    one-shot --provider path.
    """
    import io
    import json

    class _Resp:
        def __init__(self, body: dict):
            self._b = json.dumps(body).encode()

        def read(self):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=30):
        return _Resp(_register_response())

    def fake_send_provider(config, agent_dir, payload):
        provider_calls.append(payload)
        return {"status": "ok"}

    with (
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
        patch("goosetown.core._ensure_websockets"),
        patch("goosetown.core._require_websockets"),
        patch("goosetown.core._start_daemon", return_value={"status": "connected"}),
        patch("goosetown.core._is_daemon_alive", return_value=False),
        patch("goosetown.core._send_provider_register", side_effect=fake_send_provider),
        patch("goosetown.core._out"),
        patch.dict("os.environ", {"AGENT_DIR": str(tmp_path)}, clear=False),
    ):
        # Ensure io import path exists (no-op; keeps lints from flagging io unused)
        _ = io
        cmd_join(ns)


class TestJoinHarness:
    def test_harness_recorded_in_config(self, tmp_path: Path):
        ns = _join_namespace(tmp_path, harness="openclaw")
        _run_join(tmp_path, ns, [])
        cfg = (tmp_path / "GOOSETOWN.md").read_text()
        assert "harness: openclaw" in cfg

    def test_harness_defaults_to_claude_code(self, tmp_path: Path):
        ns = _join_namespace(tmp_path, harness=None)
        _run_join(tmp_path, ns, [])
        cfg = (tmp_path / "GOOSETOWN.md").read_text()
        assert "harness: claude-code" in cfg

    def test_invalid_harness_dies(self, tmp_path: Path):
        ns = _join_namespace(tmp_path, harness="banana")
        with pytest.raises(SystemExit):
            _run_join(tmp_path, ns, [])


class TestJoinProviderOneShot:
    def test_provider_flag_sends_register_after_join(self, tmp_path: Path):
        ns = _join_namespace(
            tmp_path,
            provider=True,
            skills="haiku,research",
            categories="writing,research",
            bio="I write things.",
            min_price="5",
        )
        calls: list[dict] = []
        _run_join(tmp_path, ns, calls)
        assert len(calls) == 1
        payload = calls[0]
        assert payload["action"] == "provider_register"
        assert payload["skills"] == ["haiku", "research"]
        assert payload["categories"] == ["writing", "research"]
        assert payload["bio"] == "I write things."
        assert payload["min_price"] == 5

    def test_no_provider_flag_does_not_register(self, tmp_path: Path):
        ns = _join_namespace(tmp_path, provider=False)
        calls: list[dict] = []
        _run_join(tmp_path, ns, calls)
        assert calls == []

    def test_provider_flag_without_categories_dies(self, tmp_path: Path):
        ns = _join_namespace(tmp_path, provider=True, skills="haiku", categories=None)
        with pytest.raises(SystemExit):
            _run_join(tmp_path, ns, [])
