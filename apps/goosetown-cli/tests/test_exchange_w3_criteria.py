"""Tests for W3 CLI: --criteria flag on post_job and review, daemon rendering.

Covers:
- _parse_criteria: valid semicolon-separated, >8 items dies, empty items dropped.
- _parse_criteria_verdicts: valid 0/1 comma-separated, invalid chars die.
- cmd_act post_job: --criteria flag included in payload as success_criteria list.
- cmd_act review: --criteria=1,0,1 flag included as criteria_verdicts bools.
- _write_jobs: TOWN_JOBS.md renders criteria as '- [ ] item' checklist when AWARDED.
- _on_job_completed / _on_job_failed: event lines include verdict summary.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

import pytest

from goosetown.core import _parse_criteria, _parse_criteria_verdicts, cmd_act
from goosetown.daemon import TownDaemon


# ---------------------------------------------------------------------------
# Helpers (mirror test_exchange_cli.py)
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


def _run_expecting_die(action: str, extra_args: list[str]) -> None:
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


@pytest.fixture
def daemon(tmp_path: Path) -> TownDaemon:
    state_dir = tmp_path / "state"
    workspace = tmp_path / "workspace"
    state_dir.mkdir()
    workspace.mkdir()
    return TownDaemon(
        ws_url="wss://example.invalid",
        token="t",
        agent_name="testagent",
        state_dir=state_dir,
        workspace_path=workspace,
    )


def _jobs_md(daemon: TownDaemon) -> str:
    return (daemon._workspace_path / "TOWN_JOBS.md").read_text()


def _events(daemon: TownDaemon) -> str:
    return (daemon._workspace_path / "TOWN_EVENTS.md").read_text()


# ---------------------------------------------------------------------------
# _parse_criteria flag helper
# ---------------------------------------------------------------------------


class TestParseCriteria:
    def test_valid_semicolon_separated(self):
        criteria, rest = _parse_criteria(["--criteria=a;b;c", "other"])
        assert criteria == ["a", "b", "c"]
        assert rest == ["other"]

    def test_items_stripped(self):
        criteria, _ = _parse_criteria(["--criteria=  alpha ;  beta  "])
        assert criteria == ["alpha", "beta"]

    def test_items_truncated_to_200(self):
        long_item = "x" * 250
        criteria, _ = _parse_criteria([f"--criteria={long_item}"])
        assert criteria is not None
        assert all(len(c) <= 200 for c in criteria)

    def test_empty_items_dropped(self):
        criteria, _ = _parse_criteria(["--criteria=a;;b; ;c"])
        assert criteria == ["a", "b", "c"]

    def test_all_empty_returns_none(self):
        criteria, _ = _parse_criteria(["--criteria=; ;"])
        assert criteria is None

    def test_no_flag_returns_none(self):
        criteria, rest = _parse_criteria(["10", "title", "desc"])
        assert criteria is None
        assert rest == ["10", "title", "desc"]

    def test_more_than_8_items_dies(self):
        items = ";".join(f"item{i}" for i in range(9))
        with pytest.raises(SystemExit):
            _parse_criteria([f"--criteria={items}"])

    def test_exactly_8_items_ok(self):
        items = ";".join(f"item{i}" for i in range(8))
        criteria, _ = _parse_criteria([f"--criteria={items}"])
        assert criteria is not None
        assert len(criteria) == 8


# ---------------------------------------------------------------------------
# _parse_criteria_verdicts flag helper
# ---------------------------------------------------------------------------


class TestParseCriteriaVerdicts:
    def test_valid_mixed_verdicts(self):
        verdicts, rest = _parse_criteria_verdicts(["--criteria=1,0,1", "other"])
        assert verdicts == [True, False, True]
        assert rest == ["other"]

    def test_all_true(self):
        verdicts, _ = _parse_criteria_verdicts(["--criteria=1,1,1"])
        assert verdicts == [True, True, True]

    def test_all_false(self):
        verdicts, _ = _parse_criteria_verdicts(["--criteria=0,0"])
        assert verdicts == [False, False]

    def test_single_verdict(self):
        verdicts, _ = _parse_criteria_verdicts(["--criteria=1"])
        assert verdicts == [True]

    def test_no_flag_returns_none(self):
        verdicts, rest = _parse_criteria_verdicts(["job-1", "accept"])
        assert verdicts is None
        assert rest == ["job-1", "accept"]

    def test_invalid_chars_die(self):
        with pytest.raises(SystemExit):
            _parse_criteria_verdicts(["--criteria=1,2,0"])

    def test_letters_die(self):
        with pytest.raises(SystemExit):
            _parse_criteria_verdicts(["--criteria=yes,no"])

    def test_empty_value_returns_none(self):
        verdicts, _ = _parse_criteria_verdicts(["--criteria="])
        assert verdicts is None


# ---------------------------------------------------------------------------
# cmd_act post_job with --criteria flag
# ---------------------------------------------------------------------------


class TestPostJobCriteriaFlag:
    def test_criteria_flag_included_in_payload(self):
        payload = _run_cmd_act(
            "post_job",
            [
                "--criteria=must be haiku;no rhyme",
                "40",
                "Write a haiku",
                "A haiku about dusk.",
            ],
        )
        assert payload["success_criteria"] == ["must be haiku", "no rhyme"]
        assert payload["action"] == "post_job"

    def test_criteria_flag_after_strategy(self):
        payload = _run_cmd_act(
            "post_job",
            ["--strategy=balanced", "--criteria=a;b", "40", "title", "desc"],
        )
        assert payload["success_criteria"] == ["a", "b"]
        assert payload["strategy"] == "balanced"

    def test_criteria_omitted_not_in_payload(self):
        payload = _run_cmd_act("post_job", ["40", "title", "desc"])
        assert "success_criteria" not in payload

    def test_more_than_8_criteria_dies(self):
        items = ";".join(f"item{i}" for i in range(9))
        _run_expecting_die("post_job", [f"--criteria={items}", "40", "title", "desc"])


# ---------------------------------------------------------------------------
# cmd_act review with --criteria flag
# ---------------------------------------------------------------------------


class TestReviewCriteriaVerdicts:
    def test_accept_with_verdicts(self):
        payload = _run_cmd_act("review", ["job-1", "accept", "--criteria=1,0,1"])
        assert payload["accept"] is True
        assert payload["criteria_verdicts"] == [True, False, True]
        assert payload["action"] == "review_job"

    def test_reject_with_verdicts(self):
        payload = _run_cmd_act(
            "review", ["job-1", "reject", "bad work", "--criteria=0,0"]
        )
        assert payload["accept"] is False
        assert payload["criteria_verdicts"] == [False, False]
        assert payload["reason"] == "bad work"

    def test_verdicts_before_reason(self):
        payload = _run_cmd_act(
            "review", ["job-1", "accept", "--criteria=1,1", "well done"]
        )
        assert payload["criteria_verdicts"] == [True, True]
        assert payload.get("reason") == "well done"

    def test_no_verdicts_not_in_payload(self):
        payload = _run_cmd_act("review", ["job-1", "accept"])
        assert "criteria_verdicts" not in payload

    def test_invalid_verdict_chars_die(self):
        _run_expecting_die("review", ["job-1", "accept", "--criteria=yes,no"])


# ---------------------------------------------------------------------------
# daemon _write_jobs: criteria checklist rendering
# ---------------------------------------------------------------------------


class TestWriteJobsCriteriaChecklist:
    def test_criteria_rendered_as_checklist_when_awarded(self, daemon: TownDaemon):
        daemon._update_job(
            "j1",
            role="provider",
            status="AWARDED",
            title="Write a haiku",
            description="A haiku about the fountain.",
            success_criteria=["must be 5-7-5", "must mention water"],
        )
        daemon._write_jobs()
        md = _jobs_md(daemon)
        assert "  Success criteria:" in md
        assert "  - [ ] must be 5-7-5" in md
        assert "  - [ ] must mention water" in md

    def test_no_criteria_no_checklist_section(self, daemon: TownDaemon):
        daemon._update_job(
            "j1",
            role="provider",
            status="AWARDED",
            title="T",
            description="D",
        )
        daemon._write_jobs()
        md = _jobs_md(daemon)
        assert "Success criteria:" not in md
        assert "- [ ]" not in md

    def test_criteria_not_rendered_when_completed(self, daemon: TownDaemon):
        """Criteria checklist only shows while AWARDED; vanishes on settlement."""
        daemon._update_job(
            "j1",
            role="provider",
            status="COMPLETED",
            title="T",
            success_criteria=["criterion A"],
        )
        daemon._write_jobs()
        md = _jobs_md(daemon)
        assert "Success criteria:" not in md

    def test_criteria_with_verdicts_rendered_checked(self, daemon: TownDaemon):
        daemon._update_job(
            "j1",
            role="provider",
            status="AWARDED",
            title="T",
            description="D",
            success_criteria=["A", "B", "C"],
            criteria_verdicts=[True, False, True],
        )
        daemon._write_jobs()
        md = _jobs_md(daemon)
        assert "  - [x] A" in md
        assert "  - [ ] B" in md
        assert "  - [x] C" in md


# ---------------------------------------------------------------------------
# daemon _on_job_completed / _on_job_failed verdict summary in event line
# ---------------------------------------------------------------------------


class TestJobCompletedVerdictSummary:
    def test_completed_event_with_verdicts_shows_summary(self, daemon: TownDaemon):
        daemon._update_job("j1", role="provider", status="DELIVERED", title="T")
        daemon._handle_event(
            {
                "event": "job_completed",
                "job_id": "j1",
                "title": "T",
                "payout": 27,
                "fee": 3,
                "criteria_verdicts": [True, False, True],
            }
        )
        content = _events(daemon)
        assert "criteria: 2/3 met" in content

    def test_completed_event_without_verdicts_no_summary(self, daemon: TownDaemon):
        daemon._update_job("j1", role="provider", status="DELIVERED", title="T")
        daemon._handle_event(
            {
                "event": "job_completed",
                "job_id": "j1",
                "title": "T",
                "payout": 27,
                "fee": 3,
            }
        )
        content = _events(daemon)
        assert "criteria:" not in content

    def test_failed_event_with_verdicts_shows_summary(self, daemon: TownDaemon):
        daemon._update_job("j1", role="provider", status="AWARDED", title="T")
        daemon._handle_event(
            {
                "event": "job_failed",
                "job_id": "j1",
                "title": "T",
                "reason": "rejected",
                "criteria_verdicts": [False, False],
            }
        )
        content = _events(daemon)
        assert "criteria: 0/2 met" in content

    def test_failed_event_without_verdicts_no_summary(self, daemon: TownDaemon):
        daemon._update_job("j1", role="provider", status="AWARDED", title="T")
        daemon._handle_event(
            {
                "event": "job_failed",
                "job_id": "j1",
                "title": "T",
                "reason": "delivery_timeout",
            }
        )
        content = _events(daemon)
        assert "criteria:" not in content
