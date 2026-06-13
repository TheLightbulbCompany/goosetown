"""Tests for W4: job-scoped messaging CLI (job_msg verb).

Covers:
- cmd_act payload construction: job_id + msg_text extracted correctly.
- Die on missing job_id or missing text.
- Die on msg_text > 2000 chars.
- Daemon _on_job_message: event line + cache with trim to 5.
- TOWN_JOBS.md renders messages under job entries.
- Outcome future resolves on exchange_ok for job_msg action.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from goosetown.core import cmd_act
from goosetown.daemon import TownDaemon


# ---------------------------------------------------------------------------
# CLI payload tests
# ---------------------------------------------------------------------------


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


class TestJobMsgCli:
    def test_basic_payload(self):
        payload = _run_cmd_act(
            "job_msg", ["job-123", "are", "you", "making", "progress?"]
        )
        assert payload == {
            "action": "job_msg",
            "job_id": "job-123",
            "msg_text": "are you making progress?",
        }

    def test_single_word_text(self):
        payload = _run_cmd_act("job_msg", ["job-abc", "hello"])
        assert payload["msg_text"] == "hello"
        assert payload["job_id"] == "job-abc"

    def test_missing_job_id_dies(self):
        _run_expecting_die("job_msg", [])

    def test_missing_text_dies(self):
        _run_expecting_die("job_msg", ["job-123"])

    def test_text_too_long_dies(self):
        _run_expecting_die("job_msg", ["job-123", "x" * 2001])

    def test_text_at_max_length_ok(self):
        payload = _run_cmd_act("job_msg", ["job-123", "x" * 2000])
        assert len(payload["msg_text"]) == 2000

    def test_multi_word_text_joined(self):
        payload = _run_cmd_act("job_msg", ["job-xyz", "hello", "world", "foo"])
        assert payload["msg_text"] == "hello world foo"


# ---------------------------------------------------------------------------
# Daemon _on_job_message handler tests
# ---------------------------------------------------------------------------


@pytest.fixture
def daemon(tmp_path: Path) -> TownDaemon:
    state_dir = tmp_path / "state"
    workspace = tmp_path / "workspace"
    state_dir.mkdir()
    workspace.mkdir()
    d = TownDaemon(
        ws_url="wss://example.invalid",
        token="t",
        agent_name="testagent",
        state_dir=state_dir,
        workspace_path=workspace,
    )
    return d


def _events(daemon: TownDaemon) -> str:
    return (daemon._workspace_path / "TOWN_EVENTS.md").read_text()


def _jobs_md(daemon: TownDaemon) -> str:
    return (daemon._workspace_path / "TOWN_JOBS.md").read_text()


class TestDaemonJobMessage:
    def test_event_line_appended(self, daemon: TownDaemon):
        daemon._handle_event(
            {
                "event": "job_message",
                "job_id": "j1",
                "title": "Test Job",
                "from_id": "provider-id",
                "from_name": "dave",
                "text": "I'm making good progress!",
            }
        )
        events = _events(daemon)
        assert "dave" in events
        assert "j1" in events
        assert "I'm making good progress!" in events
        assert "goosetown act job_msg j1" in events

    def test_message_cached_on_job(self, daemon: TownDaemon):
        daemon._handle_event(
            {
                "event": "job_message",
                "job_id": "j2",
                "title": "Test Job",
                "from_name": "dave",
                "text": "message one",
            }
        )
        ex = daemon.state["exchange"]
        job = ex["jobs"]["j2"]
        assert len(job["messages"]) == 1
        assert job["messages"][0]["from_name"] == "dave"
        assert job["messages"][0]["text"] == "message one"

    def test_messages_trimmed_to_5(self, daemon: TownDaemon):
        """After 6 messages the cache holds only the last 5."""
        for i in range(6):
            daemon._handle_event(
                {
                    "event": "job_message",
                    "job_id": "j3",
                    "title": "T",
                    "from_name": "dave",
                    "text": f"message {i}",
                }
            )
        ex = daemon.state["exchange"]
        job = ex["jobs"]["j3"]
        msgs = job["messages"]
        assert len(msgs) == 5
        # Oldest message (0) should have been dropped; last is message 5.
        assert msgs[-1]["text"] == "message 5"
        assert msgs[0]["text"] == "message 1"

    def test_town_jobs_renders_messages(self, daemon: TownDaemon):
        """Messages appear in TOWN_JOBS.md under the job entry."""
        # Prime the job cache as a consumer-posted job.
        daemon._exchange_state()["jobs"]["j4"] = {
            "job_id": "j4",
            "role": "consumer",
            "status": "AWARDED",
            "title": "Render Test",
            "max_price": 20,
        }
        daemon._handle_event(
            {
                "event": "job_message",
                "job_id": "j4",
                "title": "Render Test",
                "from_name": "dave",
                "text": "Working on it now.",
            }
        )
        md = _jobs_md(daemon)
        assert "> dave: Working on it now." in md

    def test_town_jobs_renders_messages_on_provider_job(self, daemon: TownDaemon):
        """Messages appear in TOWN_JOBS.md for provider-side jobs too."""
        daemon._exchange_state()["jobs"]["j5"] = {
            "job_id": "j5",
            "role": "provider",
            "status": "AWARDED",
            "title": "Provider Job",
            "agreed_price": 15,
        }
        daemon._handle_event(
            {
                "event": "job_message",
                "job_id": "j5",
                "title": "Provider Job",
                "from_name": "carol",
                "text": "Please hurry!",
            }
        )
        md = _jobs_md(daemon)
        assert "> carol: Please hurry!" in md

    def test_long_text_truncated_in_event_line(self, daemon: TownDaemon):
        """Event line truncates text at 200 chars."""
        long_text = "A" * 300
        daemon._handle_event(
            {
                "event": "job_message",
                "job_id": "j6",
                "title": "T",
                "from_name": "dave",
                "text": long_text,
            }
        )
        events = _events(daemon)
        # The 300-char text should appear truncated in the event line.
        assert "A" * 200 in events
        assert "A" * 201 not in events

    def test_long_text_truncated_in_jobs_md(self, daemon: TownDaemon):
        """TOWN_JOBS.md renders messages truncated at 200 chars."""
        daemon._exchange_state()["jobs"]["j7"] = {
            "job_id": "j7",
            "role": "consumer",
            "status": "AWARDED",
            "title": "Long msg job",
            "max_price": 10,
        }
        long_text = "B" * 300
        daemon._handle_event(
            {
                "event": "job_message",
                "job_id": "j7",
                "title": "Long msg job",
                "from_name": "dave",
                "text": long_text,
            }
        )
        md = _jobs_md(daemon)
        assert "B" * 200 in md
        assert "B" * 201 not in md


# ---------------------------------------------------------------------------
# Outcome future resolves for job_msg
# ---------------------------------------------------------------------------


class TestJobMsgOutcomeFuture:
    async def test_exchange_ok_resolves_future_for_job_msg(self, daemon: TownDaemon):
        """exchange_ok with verb=job_msg resolves the _exchange_outcome future."""
        daemon._exchange_outcome = asyncio.get_running_loop().create_future()
        daemon._handle_event(
            {
                "event": "exchange_ok",
                "verb": "job_msg",
                "status": "ok",
                "job_id": "j8",
            }
        )
        assert daemon._exchange_outcome.done()
        result = daemon._exchange_outcome.result()
        assert result["status"] == "ok"
        assert result["verb"] == "job_msg"

    async def test_exchange_rejected_resolves_future_for_job_msg(
        self, daemon: TownDaemon
    ):
        """exchange_rejected with verb=job_msg resolves the _exchange_outcome future."""
        daemon._exchange_outcome = asyncio.get_running_loop().create_future()
        daemon._handle_event(
            {
                "event": "exchange_rejected",
                "verb": "job_msg",
                "status": "rejected",
                "reason": "not_participant",
            }
        )
        assert daemon._exchange_outcome.done()
        result = daemon._exchange_outcome.result()
        assert result["status"] == "rejected"
        assert result["reason"] == "not_participant"
