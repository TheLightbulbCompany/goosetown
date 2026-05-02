"""Tests for soak-report.py snapshot collection logic."""

import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# soak-report.py has a hyphen so it can't be imported with a bare ``import``.
# Load it via importlib and bind to the name ``soak_report``.
_SOAK_REPORT_PATH = Path(__file__).parent / "soak-report.py"
_spec = importlib.util.spec_from_file_location("soak_report", _SOAK_REPORT_PATH)
soak_report = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules["soak_report"] = soak_report
_spec.loader.exec_module(soak_report)  # type: ignore[union-attr]


class TestSnapshotSchema:
    def test_snapshot_has_required_fields(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """_collect_snapshot must return a dict with all required fields."""
        monkeypatch.setattr(soak_report, "_SOAK_DIR", tmp_path)
        monkeypatch.setattr(soak_report, "_fetch_json", lambda url: None)

        snap = soak_report._collect_snapshot()

        required = {
            "timestamp_utc",
            "active_agent_count",
            "silent_agents",
            "daemon_alive_count",
            "active_conversations",
            "mood_distribution",
        }
        assert required <= set(snap.keys()), f"Missing fields: {required - set(snap.keys())}"

    def test_snapshot_serialises_to_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The snapshot dict must round-trip through JSON without error."""
        monkeypatch.setattr(soak_report, "_SOAK_DIR", tmp_path)
        monkeypatch.setattr(soak_report, "_fetch_json", lambda url: None)

        snap = soak_report._collect_snapshot()
        serialised = json.dumps(snap)
        parsed = json.loads(serialised)
        assert parsed["active_agent_count"] == snap["active_agent_count"]


class TestEmptyWorld:
    def test_empty_world_produces_zero_counts(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the API returns empty state and no soak dirs exist, all counts are 0."""
        monkeypatch.setattr(soak_report, "_SOAK_DIR", tmp_path)
        monkeypatch.setattr(
            soak_report,
            "_fetch_json",
            lambda url: {"agents": [], "conversations": []} if "state" in url else [],
        )

        snap = soak_report._collect_snapshot()

        assert snap["active_agent_count"] == 0
        assert snap["daemon_alive_count"] == 0
        assert snap["active_conversations"] == 0
        assert snap["silent_agents"] == []
        assert snap["mood_distribution"] == {}

    def test_api_error_does_not_raise(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """If the API is unreachable, _collect_snapshot returns zeros not an exception."""
        monkeypatch.setattr(soak_report, "_SOAK_DIR", tmp_path)
        monkeypatch.setattr(soak_report, "_fetch_json", lambda url: None)

        snap = soak_report._collect_snapshot()
        assert snap["active_agent_count"] == 0


class TestSilentAgentDetection:
    def test_agent_with_stale_log_is_silent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An agent whose daemon.log mtime is older than the silence threshold appears in silent_agents."""
        agent_name = "test-agent"
        agent_dir = tmp_path / agent_name
        agent_dir.mkdir()
        (agent_dir / "GOOSETOWN.md").write_text("token: tok\nagent: test-agent\n")

        # Place a daemon.log with a deliberately old mtime.
        log_dir = tmp_path / "tmp_goosetown" / agent_name
        log_dir.mkdir(parents=True)
        log_path = log_dir / "daemon.log"
        log_path.write_text("old entry\n")

        old_mtime = time.time() - (soak_report._SILENCE_THRESHOLD_SEC + 60)
        os.utime(log_path, (old_mtime, old_mtime))

        monkeypatch.setattr(soak_report, "_SOAK_DIR", tmp_path)
        monkeypatch.setattr(soak_report, "_fetch_json", lambda url: None)
        monkeypatch.setattr(
            soak_report,
            "_daemon_log_path",
            lambda name: log_dir / "daemon.log",
        )
        monkeypatch.setattr(soak_report, "_daemon_alive", lambda name: False)

        snap = soak_report._collect_snapshot()
        assert agent_name in snap["silent_agents"]

    def test_agent_with_fresh_log_is_not_silent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An agent whose daemon.log was touched recently should NOT appear in silent_agents."""
        agent_name = "fresh-agent"
        agent_dir = tmp_path / agent_name
        agent_dir.mkdir()
        (agent_dir / "GOOSETOWN.md").write_text("token: tok\nagent: fresh-agent\n")

        log_dir = tmp_path / "tmp_goosetown" / agent_name
        log_dir.mkdir(parents=True)
        log_path = log_dir / "daemon.log"
        log_path.write_text("recent entry\n")
        # mtime defaults to now, well within the 30-minute window.

        monkeypatch.setattr(soak_report, "_SOAK_DIR", tmp_path)
        monkeypatch.setattr(soak_report, "_fetch_json", lambda url: None)
        monkeypatch.setattr(
            soak_report,
            "_daemon_log_path",
            lambda name: log_dir / "daemon.log",
        )
        monkeypatch.setattr(soak_report, "_daemon_alive", lambda name: False)

        snap = soak_report._collect_snapshot()
        assert agent_name not in snap["silent_agents"]

    def test_agent_without_log_and_with_config_is_silent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An agent that has GOOSETOWN.md but no daemon.log at all counts as silent."""
        agent_name = "no-log-agent"
        agent_dir = tmp_path / agent_name
        agent_dir.mkdir()
        (agent_dir / "GOOSETOWN.md").write_text("token: tok\nagent: no-log-agent\n")

        monkeypatch.setattr(soak_report, "_SOAK_DIR", tmp_path)
        monkeypatch.setattr(soak_report, "_fetch_json", lambda url: None)
        # Point log path to a file that doesn't exist.
        monkeypatch.setattr(
            soak_report,
            "_daemon_log_path",
            lambda name: tmp_path / "nonexistent" / "daemon.log",
        )
        monkeypatch.setattr(soak_report, "_daemon_alive", lambda name: False)

        snap = soak_report._collect_snapshot()
        assert agent_name in snap["silent_agents"]


class TestMoodDistribution:
    def test_mood_distribution_populated_from_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """mood_distribution should count agents by their mood label."""
        monkeypatch.setattr(soak_report, "_SOAK_DIR", tmp_path)
        monkeypatch.setattr(
            soak_report,
            "_fetch_json",
            lambda url: {
                "agents": [
                    {"mood": "happy"},
                    {"mood": "happy"},
                    {"mood": "neutral"},
                ]
            } if "state" in url else [],
        )

        snap = soak_report._collect_snapshot()
        assert snap["mood_distribution"].get("happy") == 2
        assert snap["mood_distribution"].get("neutral") == 1
