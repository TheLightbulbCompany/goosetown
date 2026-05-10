"""Tests for the Concordia Foundation Plan 1–3 additions.

Covers:
- cmd_act dispatch routing for set_goal / complete_goal / describe_stance
- Payload shape (no agent_id in payload — daemon adds ``type``, backend
  resolves agent from auth context)
- Validation edge cases (empty text, too-long text, missing args)
- TownDaemon._handle_world_event: TOWN_EVENTS.md append + WAKE think-prompt
- TownDaemon._handle_world_update: goal block + stance lines in think-prompt
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from goosetown.daemon import TownDaemon


# ---------------------------------------------------------------------------
# Fixtures
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


# ---------------------------------------------------------------------------
# cmd_act dispatch routing — test payload shapes via mock IPC
# ---------------------------------------------------------------------------


class TestCmdActDispatch:
    """Verify cmd_act builds the right payload for each Concordia action.

    We mock ``ipc_send`` so no daemon process is needed; we just assert on
    the payload dict that cmd_act would hand to IPC.
    """

    def _run_cmd_act(self, action: str, extra_args: list[str]):
        """Build an argparse.Namespace, run cmd_act, return captured payload."""
        import argparse
        from goosetown.core import cmd_act

        captured: list[dict] = []

        def fake_ipc(sock_path, msg):
            captured.append(msg)
            return {"status": "ok"}

        ns = argparse.Namespace(action=action, args=extra_args)

        # Patch load_config, state_dir, _ensure_daemon, ipc_send, and the
        # daemon-liveness check so cmd_act doesn't touch the filesystem.
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

    def test_set_goal_payload(self):
        payload = self._run_cmd_act("set_goal", ["find", "Bob"])
        assert payload == {"action": "set_goal", "text": "find Bob"}
        assert "agent_id" not in payload

    def test_set_goal_single_word(self):
        payload = self._run_cmd_act("set_goal", ["explore"])
        assert payload["text"] == "explore"

    def test_complete_goal_payload(self):
        payload = self._run_cmd_act("complete_goal", ["done", "now"])
        assert payload == {"action": "complete_goal", "reflection": "done now"}
        assert "agent_id" not in payload

    def test_describe_stance_payload(self):
        payload = self._run_cmd_act(
            "describe_stance", ["bob-uuid-1234", "kind", "but", "distant"]
        )
        assert payload == {
            "action": "describe_stance",
            "target_agent_id": "bob-uuid-1234",
            "text": "kind but distant",
        }
        assert "agent_id" not in payload

    # ------------------------------------------------------------------
    # Validation: missing args should call _die (SystemExit in tests)
    # ------------------------------------------------------------------

    def _run_expecting_die(self, action: str, extra_args: list[str]):
        """Run cmd_act and expect it to call _die (raises SystemExit via _die)."""
        import argparse
        from goosetown.core import cmd_act

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

    def test_set_goal_no_args_dies(self):
        self._run_expecting_die("set_goal", [])

    def test_set_goal_empty_string_dies(self):
        self._run_expecting_die("set_goal", [""])

    def test_set_goal_too_long_dies(self):
        self._run_expecting_die("set_goal", ["x" * 501])

    def test_complete_goal_no_args_dies(self):
        self._run_expecting_die("complete_goal", [])

    def test_complete_goal_too_long_dies(self):
        self._run_expecting_die("complete_goal", ["x" * 2001])

    def test_describe_stance_one_arg_dies(self):
        """Need at least 2 args: target_agent_id + text."""
        self._run_expecting_die("describe_stance", ["bob-uuid"])

    def test_describe_stance_empty_text_dies(self):
        self._run_expecting_die("describe_stance", ["bob-uuid", ""])

    def test_describe_stance_text_too_long_dies(self):
        self._run_expecting_die("describe_stance", ["bob-uuid", "x" * 501])

    def test_unknown_action_dies(self):
        self._run_expecting_die("set_goal_misspelled", [])


# ---------------------------------------------------------------------------
# TownDaemon._handle_world_event
# ---------------------------------------------------------------------------


class TestHandleWorldEvent:
    def test_log_event_appends_to_town_events(self, daemon: TownDaemon):
        """A non-wake world_event should append to TOWN_EVENTS.md."""
        payload = {
            "event": {
                "tag": "arc.started",
                "summary": "A new story arc begins",
                "location": "plaza",
            },
            "wake": False,
        }
        daemon._handle_world_event(payload)

        events_file = daemon._workspace_path / "TOWN_EVENTS.md"
        assert events_file.exists()
        content = events_file.read_text()
        assert "A new story arc begins" in content
        assert "(at plaza)" in content
        # arc. prefix → 📜
        assert "\U0001f4dc" in content

    def test_event_without_location_no_suffix(self, daemon: TownDaemon):
        payload = {
            "event": {"tag": "world.rain", "summary": "Rain begins"},
            "wake": False,
        }
        daemon._handle_world_event(payload)
        events_file = daemon._workspace_path / "TOWN_EVENTS.md"
        content = events_file.read_text()
        assert "(at" not in content

    def test_wake_event_writes_think_prompt(self, daemon: TownDaemon):
        """A wake=True event must also re-render TOWN_STATUS.md with think=true."""
        daemon.state["agent"] = {
            "location": "cafe",
            "activity": "idle",
            "mood": 5,
            "energy": 80,
            "location_context": "town",
        }
        daemon.state["nearby"] = []

        payload = {
            "event": {"tag": "personal.mention", "summary": "Someone mentioned you"},
            "wake": True,
        }
        daemon._handle_world_event(payload)

        status_file = daemon._workspace_path / "TOWN_STATUS.md"
        assert status_file.exists()
        content = status_file.read_text()
        # A think-prompt has the action menu
        assert "Your turn" in content or "Where you can go" in content

    def test_icon_for_unknown_tag_is_bullet(self, daemon: TownDaemon):
        assert daemon._icon_for_tag("unknown.whatever") == "•"

    def test_icon_for_known_tags(self, daemon: TownDaemon):
        assert daemon._icon_for_tag("arc.ended") == "\U0001f4dc"
        assert daemon._icon_for_tag("social.chat") == "\U0001f4e3"
        assert daemon._icon_for_tag("ambient.weather") == "\U0001f4a1"
        assert daemon._icon_for_tag("world.event") == "\U0001f30d"
        assert daemon._icon_for_tag("personal.dm") == "\U0001f514"

    def test_fallback_to_base_tag_when_no_summary(self, daemon: TownDaemon):
        payload = {
            "event": {"tag": "arc.mystery", "baseTag": "mystery_arc"},
            "wake": False,
        }
        daemon._handle_world_event(payload)
        content = (daemon._workspace_path / "TOWN_EVENTS.md").read_text()
        assert "mystery_arc" in content


# ---------------------------------------------------------------------------
# TownDaemon._handle_world_update: goal block in think-prompt
# ---------------------------------------------------------------------------


class TestHandleWorldUpdateGoalBlock:
    def _world_update_with_goal(
        self, daemon: TownDaemon, goal: str, goal_set_at: int = 0
    ):
        """Fire _handle_world_update with a goal set on the agent."""
        data = {
            "you": {
                "location": "plaza",
                "activity": "idle",
                "mood": 10,
                "energy": 90,
                "location_context": "town",
                "currentGoal": goal,
                "goalSetAt": goal_set_at,
            },
            "nearby_agents": [],
            "think": True,
        }
        daemon._handle_world_update(data)
        return (daemon._workspace_path / "TOWN_STATUS.md").read_text()

    def test_goal_block_appears_in_think_prompt(self, daemon: TownDaemon):
        content = self._world_update_with_goal(daemon, "find the lost manuscript")
        assert "## Your current goal" in content
        assert "_find the lost manuscript_" in content
        assert "goosetown act complete_goal" in content

    def test_no_goal_block_when_goal_empty(self, daemon: TownDaemon):
        content = self._world_update_with_goal(daemon, "")
        assert "## Your current goal" not in content

    def test_stale_hint_in_think_prompt(self, daemon: TownDaemon):
        import time

        now_ms = int(time.time() * 1000)
        set_at = now_ms - 7 * 86_400_000  # 7 days ago
        content = self._world_update_with_goal(daemon, "old quest", goal_set_at=set_at)
        assert "on your mind for 7 days" in content


# ---------------------------------------------------------------------------
# TownDaemon._handle_world_update: stance + goal lines in nearby agents
# ---------------------------------------------------------------------------


class TestHandleWorldUpdateStanceLines:
    def _world_update_with_nearby(self, daemon: TownDaemon, nearby_agents: list[dict]):
        data = {
            "you": {
                "location": "plaza",
                "activity": "idle",
                "mood": 10,
                "energy": 90,
                "location_context": "town",
            },
            "nearby_agents": nearby_agents,
            "think": True,
        }
        daemon._handle_world_update(data)
        return (daemon._workspace_path / "TOWN_STATUS.md").read_text()

    def test_stance_from_me_appears(self, daemon: TownDaemon):
        content = self._world_update_with_nearby(
            daemon,
            [
                {
                    "agent_id": "bob-uuid",
                    "name": "Bob",
                    "activity": "idle",
                    "stanceFromMe": "friendly",
                }
            ],
        )
        assert "Your stance toward them: _friendly_" in content

    def test_stance_from_them_appears(self, daemon: TownDaemon):
        content = self._world_update_with_nearby(
            daemon,
            [
                {
                    "agent_id": "alice-uuid",
                    "name": "Alice",
                    "activity": "idle",
                    "stanceFromThem": "guarded",
                }
            ],
        )
        assert "Their stance toward you: _guarded_" in content

    def test_both_stances_appear(self, daemon: TownDaemon):
        content = self._world_update_with_nearby(
            daemon,
            [
                {
                    "agent_id": "carol-uuid",
                    "name": "Carol",
                    "activity": "idle",
                    "stanceFromMe": "curious",
                    "stanceFromThem": "cautious",
                }
            ],
        )
        assert "Your stance toward them: _curious_" in content
        assert "Their stance toward you: _cautious_" in content

    def test_no_stance_lines_when_absent(self, daemon: TownDaemon):
        content = self._world_update_with_nearby(
            daemon,
            [
                {
                    "agent_id": "dave-uuid",
                    "name": "Dave",
                    "activity": "idle",
                }
            ],
        )
        assert "stance" not in content.lower()

    def test_nearby_goal_appears(self, daemon: TownDaemon):
        content = self._world_update_with_nearby(
            daemon,
            [
                {
                    "agent_id": "eve-uuid",
                    "name": "Eve",
                    "activity": "idle",
                    "currentGoal": "find the treasure",
                }
            ],
        )
        assert "Goal: _find the treasure_" in content

    def test_no_goal_line_when_empty(self, daemon: TownDaemon):
        content = self._world_update_with_nearby(
            daemon,
            [
                {
                    "agent_id": "frank-uuid",
                    "name": "Frank",
                    "activity": "idle",
                    "currentGoal": "",
                }
            ],
        )
        assert "Goal:" not in content
