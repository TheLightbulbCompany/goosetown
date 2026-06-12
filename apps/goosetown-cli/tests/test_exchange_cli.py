"""Tests for the Exchange CLI verbs (Task 11, ADR-0007).

Covers cmd_act payload construction for post_job / bid / cancel_job /
deliver / review, including --confidence parsing, --file= deliverable
loading, and validation deaths. Mirrors the mock-IPC harness from
test_concordia_actions.py.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

import pytest

from goosetown.core import cmd_act


def _run_cmd_act(action: str, extra_args: list[str]) -> dict:
    """Build an argparse.Namespace, run cmd_act, return captured payload."""
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


def _run_expecting_die(action: str, extra_args: list[str]) -> None:
    """Run cmd_act and expect it to call _die (raises SystemExit)."""
    ns = argparse.Namespace(action=action, args=extra_args)

    with (
        patch("goosetown.core.load_config", return_value={"agent": "tester"}),
        patch("goosetown.core.state_dir", return_value=Path("/tmp/fake")),
        patch("goosetown.core._ensure_daemon"),
        patch("goosetown.core._ensure_websockets"),
        patch("goosetown.core._require_websockets"),
        pytest.raises(SystemExit),
    ):
        cmd_act(ns)


class TestPostJob:
    def test_payload(self):
        payload = _run_cmd_act(
            "post_job", ["40", "Write a haiku", "A", "haiku", "about", "dusk."]
        )
        assert payload == {
            "action": "post_job",
            "title": "Write a haiku",
            "description": "A haiku about dusk.",
            "max_price": 40,
        }

    def test_title_truncated_to_120(self):
        payload = _run_cmd_act("post_job", ["10", "t" * 200, "desc"])
        assert payload["title"] == "t" * 120

    def test_description_truncated_to_4096(self):
        payload = _run_cmd_act("post_job", ["10", "title", "d" * 5000])
        assert len(payload["description"]) == 4096

    def test_non_int_price_dies(self):
        _run_expecting_die("post_job", ["lots", "title", "desc"])

    def test_price_zero_dies(self):
        _run_expecting_die("post_job", ["0", "title", "desc"])

    def test_price_negative_dies(self):
        _run_expecting_die("post_job", ["-1", "title", "desc"])

    def test_price_above_million_dies(self):
        _run_expecting_die("post_job", ["1000001", "title", "desc"])

    def test_price_at_million_ok(self):
        payload = _run_cmd_act("post_job", ["1000000", "title", "desc"])
        assert payload["max_price"] == 1_000_000

    def test_missing_description_dies(self):
        _run_expecting_die("post_job", ["10", "title"])

    def test_no_args_dies(self):
        _run_expecting_die("post_job", [])

    def test_empty_title_dies(self):
        _run_expecting_die("post_job", ["10", "  ", "desc"])

    def test_empty_description_dies(self):
        _run_expecting_die("post_job", ["10", "title", "  "])


class TestBid:
    def test_payload_minimal(self):
        payload = _run_cmd_act("bid", ["job-1", "25"])
        assert payload == {"action": "bid", "job_id": "job-1", "price": 25}

    def test_payload_with_confidence_and_pitch(self):
        payload = _run_cmd_act(
            "bid", ["--confidence=0.8", "job-1", "25", "I", "write", "fast"]
        )
        assert payload == {
            "action": "bid",
            "job_id": "job-1",
            "price": 25,
            "confidence": 0.8,
            "pitch": "I write fast",
        }

    def test_pitch_truncated_to_500(self):
        payload = _run_cmd_act("bid", ["job-1", "25", "p" * 600])
        assert len(payload["pitch"]) == 500

    def test_confidence_out_of_range_dies(self):
        _run_expecting_die("bid", ["--confidence=1.5", "job-1", "25"])

    def test_confidence_negative_dies(self):
        _run_expecting_die("bid", ["--confidence=-0.1", "job-1", "25"])

    def test_confidence_not_a_number_dies(self):
        _run_expecting_die("bid", ["--confidence=high", "job-1", "25"])

    def test_non_int_price_dies(self):
        _run_expecting_die("bid", ["job-1", "cheap"])

    def test_price_negative_dies(self):
        _run_expecting_die("bid", ["job-1", "-1"])

    def test_price_above_million_dies(self):
        _run_expecting_die("bid", ["job-1", "1000001"])

    def test_price_zero_ok(self):
        payload = _run_cmd_act("bid", ["job-1", "0"])
        assert payload["price"] == 0

    def test_price_at_million_ok(self):
        payload = _run_cmd_act("bid", ["job-1", "1000000"])
        assert payload["price"] == 1_000_000

    def test_missing_price_dies(self):
        _run_expecting_die("bid", ["job-1"])


class TestCancelJob:
    def test_payload(self):
        payload = _run_cmd_act("cancel_job", ["job-9"])
        assert payload == {"action": "cancel_job", "job_id": "job-9"}

    def test_no_args_dies(self):
        _run_expecting_die("cancel_job", [])


class TestDeliver:
    def test_inline_text_payload(self):
        payload = _run_cmd_act("deliver", ["job-1", "here", "is", "the", "work"])
        assert payload == {
            "action": "deliver_job",
            "job_id": "job-1",
            "deliverable": "here is the work",
        }

    def test_file_payload(self, tmp_path: Path):
        f = tmp_path / "work.md"
        f.write_text("the deliverable\nline two\n")
        payload = _run_cmd_act("deliver", ["job-1", f"--file={f}"])
        assert payload["action"] == "deliver_job"
        assert payload["deliverable"] == "the deliverable\nline two"

    def test_missing_file_dies(self, tmp_path: Path):
        _run_expecting_die("deliver", ["job-1", f"--file={tmp_path}/nope.md"])

    def test_deliverable_truncated_to_16384(self):
        payload = _run_cmd_act("deliver", ["job-1", "x" * 20000])
        assert len(payload["deliverable"]) == 16384

    def test_missing_deliverable_dies(self):
        _run_expecting_die("deliver", ["job-1"])

    def test_empty_deliverable_dies(self):
        _run_expecting_die("deliver", ["job-1", "  "])


class TestReview:
    def test_accept_payload(self):
        payload = _run_cmd_act("review", ["job-1", "accept"])
        assert payload == {"action": "review_job", "job_id": "job-1", "accept": True}

    def test_reject_with_reason_payload(self):
        payload = _run_cmd_act("review", ["job-1", "reject", "not", "a", "haiku"])
        assert payload == {
            "action": "review_job",
            "job_id": "job-1",
            "accept": False,
            "reason": "not a haiku",
        }

    def test_reason_truncated_to_500(self):
        payload = _run_cmd_act("review", ["job-1", "reject", "r" * 600])
        assert len(payload["reason"]) == 500

    def test_invalid_verdict_dies(self):
        _run_expecting_die("review", ["job-1", "maybe"])

    def test_missing_verdict_dies(self):
        _run_expecting_die("review", ["job-1"])
