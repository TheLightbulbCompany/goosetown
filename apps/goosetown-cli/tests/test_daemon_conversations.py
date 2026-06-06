"""Group-conversation, participant-tracking, status-rendering, event-driven
think, and auth-failure tests for TownDaemon.

These behaviors used to be exercised only by the (now-deleted) skill-bundle
daemon's test suite (``apps/backend/tests/unit/test_goosetown_cli.py``). They
were genuinely uncovered by the canonical PyPI test suite, so they are ported
here against the ``goosetown`` package (NOT the bundle). The bundle-only drift
(static-phase welcome blurb, ``weather.*`` icon, 0.5s move state-poll) is
deliberately NOT ported — that behavior does not exist in the canonical daemon.
"""

from __future__ import annotations

import plistlib
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from goosetown.daemon import (
    AuthFailure,
    TownDaemon,
    _is_auth_failure,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def daemon(tmp_path: Path) -> TownDaemon:
    state_dir = tmp_path / "state"
    workspace = tmp_path / "workspace"
    state_dir.mkdir()
    workspace.mkdir()
    return TownDaemon(
        ws_url="wss://example.invalid",
        token="t",  # nosec B106: test fixture token, not a real credential
        agent_name="testagent",
        state_dir=state_dir,
        workspace_path=workspace,
    )


def _read_status(daemon: TownDaemon) -> str:
    return (daemon._workspace_path / "TOWN_STATUS.md").read_text()


# ===========================================================================
# Participant tracking in _on_conversation_message
# ===========================================================================


class TestConversationInviteTracksParticipants:
    """conversation_invite/message build and grow the participants list."""

    def test_invite_creates_participants_list(self, daemon: TownDaemon):
        daemon._on_conversation_message(
            "conversation_invite",
            {
                "conv_id": "conv-1",
                "from": "Alice",
                "from_agent_name": "alice",
                "message": "Hey there!",
            },
        )

        active = daemon.state.get("active_conversation")
        assert active is not None
        assert active["conv_id"] == "conv-1"
        assert active["participants"] == ["Alice"]
        assert len(active["turns"]) == 1
        assert active["turns"][0]["speaker"] == "Alice"

    def test_message_adds_new_participant(self, daemon: TownDaemon):
        daemon._on_conversation_message(
            "conversation_invite",
            {
                "conv_id": "conv-1",
                "from": "Alice",
                "from_agent_name": "alice",
                "message": "Hey!",
            },
        )
        daemon._on_conversation_message(
            "conversation_message",
            {
                "conv_id": "conv-1",
                "from": "Bob",
                "from_agent_name": "bob",
                "text": "Hi Alice!",
                "turn": 2,
            },
        )

        active = daemon.state["active_conversation"]
        assert "Alice" in active["participants"]
        assert "Bob" in active["participants"]
        assert len(active["turns"]) == 2

    def test_message_does_not_duplicate_participant(self, daemon: TownDaemon):
        daemon._on_conversation_message(
            "conversation_invite",
            {
                "conv_id": "conv-1",
                "from": "Alice",
                "from_agent_name": "alice",
                "message": "Hey!",
            },
        )
        daemon._on_conversation_message(
            "conversation_message",
            {
                "conv_id": "conv-1",
                "from": "Alice",
                "from_agent_name": "alice",
                "text": "Hello again",
                "turn": 2,
            },
        )

        active = daemon.state["active_conversation"]
        assert active["participants"].count("Alice") == 1


# ===========================================================================
# participant_joined / participant_left
# ===========================================================================


class TestParticipantJoined:
    """participant_joined adds to the active conversation's participants."""

    def test_adds_new_participant(self, daemon: TownDaemon):
        daemon.state["active_conversation"] = {
            "conv_id": "conv-1",
            "participants": ["Alice", "Bob"],
            "turns": [],
        }
        daemon._on_participant_joined(
            {"conv_id": "conv-1", "agent_id": "c1", "display_name": "Charlie"}
        )
        assert "Charlie" in daemon.state["active_conversation"]["participants"]

    def test_ignores_wrong_conv_id(self, daemon: TownDaemon):
        daemon.state["active_conversation"] = {
            "conv_id": "conv-1",
            "participants": ["Alice"],
            "turns": [],
        }
        daemon._on_participant_joined(
            {"conv_id": "conv-OTHER", "agent_id": "c1", "display_name": "Charlie"}
        )
        assert "Charlie" not in daemon.state["active_conversation"]["participants"]

    def test_no_duplicate_on_rejoin(self, daemon: TownDaemon):
        daemon.state["active_conversation"] = {
            "conv_id": "conv-1",
            "participants": ["Alice", "Bob"],
            "turns": [],
        }
        daemon._on_participant_joined(
            {"conv_id": "conv-1", "agent_id": "a1", "display_name": "Alice"}
        )
        assert daemon.state["active_conversation"]["participants"].count("Alice") == 1


class TestParticipantLeft:
    """participant_left removes from the active conversation's participants."""

    def test_removes_participant(self, daemon: TownDaemon):
        daemon.state["active_conversation"] = {
            "conv_id": "conv-1",
            "participants": ["Alice", "Bob", "Charlie"],
            "turns": [],
        }
        daemon._on_participant_left(
            {
                "conv_id": "conv-1",
                "agent_id": "b1",
                "display_name": "Bob",
                "remaining": 2,
            }
        )
        assert "Bob" not in daemon.state["active_conversation"]["participants"]
        assert len(daemon.state["active_conversation"]["participants"]) == 2

    def test_ignores_wrong_conv_id(self, daemon: TownDaemon):
        daemon.state["active_conversation"] = {
            "conv_id": "conv-1",
            "participants": ["Alice", "Bob"],
            "turns": [],
        }
        daemon._on_participant_left(
            {
                "conv_id": "conv-OTHER",
                "agent_id": "b1",
                "display_name": "Bob",
                "remaining": 1,
            }
        )
        assert "Bob" in daemon.state["active_conversation"]["participants"]


# ===========================================================================
# conversation_ended clears state
# ===========================================================================


class TestConversationEndedClearsState:
    """conversation_ended clears active_conversation and pending_messages."""

    def test_clears_on_end(self, daemon: TownDaemon):
        daemon.state["active_conversation"] = {
            "conv_id": "conv-1",
            "participants": ["Alice"],
            "turns": [{"speaker": "Alice", "text": "hi"}],
        }
        daemon.state["pending_messages"] = [{"event": "conversation_invite"}]

        daemon._on_conversation_ended(
            {
                "conv_id": "conv-1",
                "turn_count": 3,
                "location": "plaza",
                "log_summary": "They chatted about the weather.",
            }
        )

        assert "active_conversation" not in daemon.state
        assert daemon.state["pending_messages"] == []


# ===========================================================================
# Active / nearby conversation rendering in TOWN_STATUS.md
# ===========================================================================


class TestActiveConversationStatusRendering:
    """TOWN_STATUS.md surfaces the participants list and nearby conversations."""

    def test_shows_all_participants(self, daemon: TownDaemon):
        daemon.state["agent"] = {"onboardingPhase": "alive", "location": "plaza"}
        daemon.state["active_conversation"] = {
            "conv_id": "conv-1",
            "participants": ["Alice", "Bob", "Charlie"],
            "turns": [
                {"speaker": "Alice", "text": "Hey everyone!"},
                {"speaker": "Bob", "text": "Hi!"},
            ],
        }
        world_data = {
            "you": {
                "onboardingPhase": "alive",
                "location": "plaza",
                "mood": "happy",
                "energy": 80,
            },
            "nearby_agents": [],
            "think": True,
        }
        daemon._handle_world_update(world_data)

        content = _read_status(daemon)
        assert "Alice, Bob, Charlie" in content
        assert "act say conv-1" in content
        assert "act end conv-1" in content

    def test_shows_nearby_conversations_when_not_in_one(self, daemon: TownDaemon):
        daemon.state["agent"] = {"onboardingPhase": "alive", "location": "plaza"}
        daemon.state["nearby_conversations"] = [
            {
                "conv_id": "conv-99",
                "participants": ["Alice", "Bob"],
                "last_message": {"speaker": "Alice", "text": "Nice weather today"},
                "location": "plaza",
            },
        ]
        world_data = {
            "you": {
                "onboardingPhase": "alive",
                "location": "plaza",
                "mood": "neutral",
                "energy": 100,
            },
            "nearby_agents": [],
            "think": True,
        }
        daemon._handle_world_update(world_data)

        content = _read_status(daemon)
        assert "Nearby conversations" in content
        assert "join_conversation conv-99" in content
        assert "Alice, Bob" in content


# ===========================================================================
# Event-driven think — proximity change triggers think under the throttle
# ===========================================================================


class TestEventDrivenThink:
    """Think triggers immediately on proximity changes, not just time throttle."""

    def test_think_triggers_on_new_nearby_agent(self, daemon: TownDaemon):
        daemon.state["agent"] = {"onboardingPhase": "alive", "location": "plaza"}
        daemon.state["nearby"] = []

        # First agent_state — no nearby, idle, should think (first time).
        daemon._handle_agent_state(
            {
                "event": "agent_state",
                "location": "plaza",
                "activity": "idle",
                "speed": 0,
                "nearby_agents": [],
                "context": "town",
            }
        )
        content1 = _read_status(daemon)
        assert "act move" in content1

        # Reset think time to simulate 5s passing (under the 12s throttle).
        daemon._last_think_time = time.time() - 5.0

        # New agent appears nearby — should trigger think immediately even
        # though we're under the throttle window.
        daemon._handle_agent_state(
            {
                "event": "agent_state",
                "location": "plaza",
                "activity": "idle",
                "speed": 0,
                "nearby_agents": [
                    {"agent_id": "a1", "name": "Alice", "display_name": "Alice"}
                ],
                "context": "town",
            }
        )
        content2 = _read_status(daemon)
        assert "Alice" in content2

    def test_no_action_menu_when_nearby_unchanged_and_under_throttle(
        self, daemon: TownDaemon
    ):
        daemon.state["agent"] = {"onboardingPhase": "alive", "location": "plaza"}
        daemon.state["nearby"] = [{"agent_id": "a1"}]

        daemon._last_think_time = time.time() - 5.0

        daemon._handle_agent_state(
            {
                "event": "agent_state",
                "location": "plaza",
                "activity": "idle",
                "speed": 0,
                "nearby_agents": [{"agent_id": "a1", "name": "Alice"}],
                "context": "town",
            }
        )
        # TOWN_STATUS.md is always written (it reflects current state), but
        # the action menu ("Your turn — decide what to do next") must NOT
        # appear when think=False — otherwise passive 2s updates clobber a
        # stale action prompt the agent shouldn't act on yet.
        status_file = daemon._workspace_path / "TOWN_STATUS.md"
        assert status_file.exists()
        content = status_file.read_text()
        assert "Your turn" not in content
        assert "decide what to do next" not in content


# ===========================================================================
# Auth-failure classification — keeps the daemon from churning on a
# revoked/expired token forever. Network-level closes still bubble to the
# generic retry path.
# ===========================================================================


class _FakeStatus:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeInvalidStatus(Exception):
    """Mimics websockets.exceptions.InvalidStatus (>= v14)."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"server rejected WebSocket connection: HTTP {status_code}")
        self.response = _FakeStatus(status_code)


class _FakeInvalidStatusCode(Exception):
    """Mimics websockets.exceptions.InvalidStatusCode (< v14)."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"server rejected WebSocket connection: HTTP {status_code}")
        self.status_code = status_code


class _FakeRcvd:
    def __init__(self, code: int) -> None:
        self.code = code


class _FakeConnectionClosed(Exception):
    """Mimics websockets.exceptions.ConnectionClosed."""

    def __init__(self, code: int, *, via_rcvd: bool = False) -> None:
        super().__init__(f"connection closed with code {code}")
        if via_rcvd:
            self.rcvd = _FakeRcvd(code)
        else:
            self.code = code


class TestIsAuthFailure:
    @pytest.mark.parametrize("status", [401, 403])
    def test_handshake_http_rejection_is_auth_failure(self, status):
        assert _is_auth_failure(_FakeInvalidStatus(status))
        assert _is_auth_failure(_FakeInvalidStatusCode(status))

    @pytest.mark.parametrize("status", [400, 404, 500, 502, 503])
    def test_other_http_statuses_are_network_failures(self, status):
        assert not _is_auth_failure(_FakeInvalidStatus(status))
        assert not _is_auth_failure(_FakeInvalidStatusCode(status))

    @pytest.mark.parametrize("code", [1008, 4401, 4403])
    def test_auth_close_codes_are_auth_failure(self, code):
        assert _is_auth_failure(_FakeConnectionClosed(code))
        assert _is_auth_failure(_FakeConnectionClosed(code, via_rcvd=True))

    @pytest.mark.parametrize("code", [1000, 1001, 1006, 1011, 4000])
    def test_network_close_codes_are_not_auth_failure(self, code):
        assert not _is_auth_failure(_FakeConnectionClosed(code))

    def test_plain_exception_is_not_auth_failure(self):
        assert not _is_auth_failure(ConnectionError("network blip"))
        assert not _is_auth_failure(TimeoutError("inactivity"))
        assert not _is_auth_failure(RuntimeError("???"))


class TestAuthFailureSurface:
    """AuthFailure is exported and is a proper Exception."""

    def test_auth_failure_is_exception(self):
        assert issubclass(AuthFailure, Exception)
        try:
            raise AuthFailure("expired")
        except AuthFailure as exc:
            assert str(exc) == "expired"


# ===========================================================================
# _on_conversation_started populates initiator state (echo-to-self)
# ===========================================================================


class TestConversationStartedPopulatesInitiatorState:
    """conversation_started echoes the chat back to the initiator so their
    daemon can populate active_conversation without waiting for a reply."""

    def test_started_without_opening_message_still_tracks_conv_id(
        self, daemon: TownDaemon
    ):
        daemon._on_conversation_started(
            {
                "conv_id": "conv-xyz",
                "with_agent_id": "agent-target",
                "with_display": "Marbles",
                "opening_message": "",
                "location": "cafe",
            }
        )
        active = daemon.state.get("active_conversation")
        assert active is not None
        assert active["conv_id"] == "conv-xyz"
        assert active["turns"] == []


# ===========================================================================
# _icon_for_tag — empty-tag fallback (the unknown-tag case is covered in
# test_concordia_actions.py; the empty-string edge case is not).
# ===========================================================================


class TestIconForEmptyTag:
    def test_empty_tag_returns_bullet(self, daemon: TownDaemon):
        assert daemon._icon_for_tag("") == "•"


# ===========================================================================
# cmd_install_launchd — macOS LaunchAgent installer
#
# The deleted skill-bundle test suite covered a *different*, drifted launchd
# implementation (a `_render_launchd_plist` helper, a `com.goosetown.*` label,
# a `daemon-resume` ProgramArguments shape, and a `--dry-run` flag) — none of
# which exist in the canonical package. Those bundle-only tests are NOT ported.
# Instead we cover the canonical `cmd_install_launchd` (in goosetown.core),
# which had zero test coverage: it uses the `co.isol8.goosetown.<name>` label
# and a `goosetown _daemon` ProgramArguments shape.
# ===========================================================================


class TestInstallLaunchd:
    def test_non_macos_returns_error_without_writing(self, monkeypatch, tmp_path):
        """On a non-darwin platform the installer must refuse and write nothing."""
        from goosetown import core

        monkeypatch.setattr(core.sys, "platform", "linux")
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(core.Path, "home", staticmethod(lambda: fake_home))

        rc = core.cmd_install_launchd("test-agent")

        assert rc == 1
        assert not (fake_home / "Library" / "LaunchAgents").exists()

    def test_macos_writes_valid_plist_with_isol8_label(self, monkeypatch, tmp_path):
        """On macOS the installer writes a valid plist labeled co.isol8.goosetown.<name>."""
        from goosetown import core

        monkeypatch.setattr(core.sys, "platform", "darwin")
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(core.Path, "home", staticmethod(lambda: fake_home))

        # Stub subprocess: `which goosetown` resolves to a path; `launchctl
        # load` succeeds. Both go through core.subprocess.run.
        def fake_run(argv, *args, **kwargs):
            result = MagicMock()
            if argv[:1] == ["which"]:
                result.stdout = "/usr/local/bin/goosetown\n"
            else:  # launchctl load
                result.returncode = 0
                result.stderr = ""
            return result

        monkeypatch.setattr(core.subprocess, "run", fake_run)

        rc = core.cmd_install_launchd("marisol")
        assert rc == 0

        plist_path = (
            fake_home / "Library" / "LaunchAgents" / "co.isol8.goosetown.marisol.plist"
        )
        assert plist_path.exists()
        parsed = plistlib.loads(plist_path.read_bytes())
        assert parsed["Label"] == "co.isol8.goosetown.marisol"
        assert parsed["RunAtLoad"] is True
        assert parsed["KeepAlive"] is True
        assert parsed["ProgramArguments"] == ["/usr/local/bin/goosetown", "_daemon"]
        assert parsed["EnvironmentVariables"]["TOWN_AGENT"] == "marisol"
        assert "marisol" in parsed["StandardOutPath"]


# ===========================================================================
# _ensure_websockets — first-run dependency bootstrap.
#
# In the PyPI package `websockets` is a declared dependency, but the bootstrap
# is kept for compat with skill-bundle-variant agents. It was only ever
# *patched away* in the PyPI suite, never exercised — porting the real
# behavior here (ported from the deleted backend test_websockets_bootstrap.py,
# with assertions updated to the canonical failure message).
# ===========================================================================


class TestEnsureWebsockets:
    def test_no_op_when_already_importable(self, capsys):
        """websockets is a declared dep, so the bootstrap is a silent no-op."""
        from goosetown.core import _ensure_websockets

        _ensure_websockets()
        captured = capsys.readouterr()
        # The success path returns before printing anything.
        assert captured.out == ""
        assert captured.err == ""

    def test_pip_installs_when_missing(self, monkeypatch):
        """When websockets is missing, the bootstrap shells out to pip install --user."""
        from goosetown import core

        real_import = (
            __builtins__["__import__"]
            if isinstance(__builtins__, dict)
            else __builtins__.__import__
        )
        state = {"first_call": True}

        def fake_import(name, *args, **kwargs):
            if name == "websockets" and state["first_call"]:
                state["first_call"] = False
                raise ImportError("simulated missing websockets")
            return real_import(name, *args, **kwargs)

        fake_run = MagicMock(return_value=0)
        monkeypatch.setattr(core.subprocess, "check_call", fake_run)
        monkeypatch.setattr("builtins.__import__", fake_import)

        core._ensure_websockets()

        fake_run.assert_called_once()
        argv = fake_run.call_args.args[0]
        assert argv[0] == core.sys.executable
        assert argv[1:4] == ["-m", "pip", "install"]
        assert "--user" in argv
        assert "websockets" in argv

    def test_exits_on_pip_failure(self, monkeypatch, capsys):
        """When pip install fails, the bootstrap exits with code 2 and a clear message."""
        import subprocess

        from goosetown import core

        real_import = (
            __builtins__["__import__"]
            if isinstance(__builtins__, dict)
            else __builtins__.__import__
        )

        def fake_import(name, *args, **kwargs):
            if name == "websockets":
                raise ImportError("simulated missing websockets")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", fake_import)
        monkeypatch.setattr(
            core.subprocess,
            "check_call",
            MagicMock(side_effect=subprocess.CalledProcessError(1, "pip")),
        )

        with pytest.raises(SystemExit) as exc:
            core._ensure_websockets()
        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert "FATAL" in captured.err
        assert "pip install websockets" in captured.err
