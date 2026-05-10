"""Unit tests for TownDaemon — welcome blurb and group conversations."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

# The skill lives outside the normal Python package tree — add it to sys.path
_SKILL_DIR = Path(__file__).parents[2] / "data" / "goosetown-skill" / "skills" / "goosetown"
sys.path.insert(0, str(_SKILL_DIR))

from goosetown_cli import (  # noqa: E402
    _WELCOME_BLURB,
    AuthFailure,
    TownDaemon,
    _is_auth_failure,
    _render_goal_block,
    _render_stance_lines,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_events(daemon: TownDaemon) -> str:
    events_file = daemon._workspace_path / "TOWN_EVENTS.md"
    if not events_file.exists():
        return ""
    return events_file.read_text()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def daemon(tmp_path):
    """Return a TownDaemon whose workspace is a real temp directory."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    d = TownDaemon(
        ws_url="wss://example.com/ws",
        token="test-token",  # nosec B106: test fixture token, not a real credential
        agent_name="test-agent",
        state_dir=state_dir,
        workspace_path=workspace,
    )
    return d


def _read_status(daemon: TownDaemon) -> str:
    status_file = daemon._workspace_path / "TOWN_STATUS.md"
    return status_file.read_text()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWelcomeBlurbDuringStaticOnboarding:
    """_handle_world_update short-circuits with _WELCOME_BLURB when onboardingPhase=static."""

    def test_static_onboarding_writes_welcome_blurb(self, daemon):
        """When agent.onboardingPhase == 'static', TOWN_STATUS.md must contain the welcome blurb."""
        # Seed agent state with onboardingPhase=static
        daemon.state["agent"] = {"onboardingPhase": "static", "location": "plaza"}

        world_data = {
            "you": {"onboardingPhase": "static", "location": "plaza"},
            "nearby_agents": [],
            "think": True,
        }
        daemon._handle_world_update(world_data)

        content = _read_status(daemon)
        assert _WELCOME_BLURB in content or content == _WELCOME_BLURB

    def test_alive_onboarding_writes_normal_status(self, daemon):
        """When onboardingPhase == 'alive', normal think-prompt logic runs (not the blurb)."""
        daemon.state["agent"] = {"onboardingPhase": "alive", "location": "plaza"}

        world_data = {
            "you": {"onboardingPhase": "alive", "location": "plaza", "mood": "neutral", "energy": 100},
            "nearby_agents": [],
            "think": True,
        }
        daemon._handle_world_update(world_data)

        content = _read_status(daemon)
        # Must NOT be the welcome blurb
        assert content != _WELCOME_BLURB
        # Must contain normal navigation options
        assert "goosetown_cli.py act move" in content

    def test_missing_onboarding_phase_treated_as_alive(self, daemon):
        """When onboardingPhase key is absent, normal think-prompt logic runs (not the blurb)."""
        daemon.state["agent"] = {"location": "plaza"}

        world_data = {
            "you": {"location": "plaza", "mood": "neutral", "energy": 100},
            "nearby_agents": [],
            "think": True,
        }
        daemon._handle_world_update(world_data)

        content = _read_status(daemon)
        # Must NOT be the welcome blurb
        assert content != _WELCOME_BLURB
        # Must contain normal navigation options
        assert "goosetown_cli.py act move" in content


# ===========================================================================
# Group Conversations — daemon event handlers
# ===========================================================================


class TestConversationStartedPopulatesInitiatorState:
    """conversation_started echoes the chat back to the initiator so their
    daemon can populate active_conversation without waiting for a reply.
    F11 from the 2026-04-21 dry run."""

    def test_started_creates_active_conversation_with_opening_turn(self, daemon):
        daemon._on_conversation_started(
            {
                "conv_id": "conv-xyz",
                "with_agent_id": "agent-target",
                "with_display": "Marbles",
                "with_agent_name": "marbles",
                "opening_message": "howdy neighbor",
                "location": "cafe",
            }
        )

        active = daemon.state.get("active_conversation")
        assert active is not None
        assert active["conv_id"] == "conv-xyz"
        assert active["participants"] == ["Marbles"]
        assert len(active["turns"]) == 1
        # Speaker is the initiator themselves — they spoke, so the turn
        # records it as "you" (matches how the daemon displays it).
        assert active["turns"][0]["speaker"] == "you"
        assert active["turns"][0]["text"] == "howdy neighbor"

    def test_started_without_opening_message_still_tracks_conv_id(self, daemon):
        """Empty opening (shouldn't happen but defensive) still sets conv_id."""
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


class TestConversationInviteTracksParticipants:
    """conversation_invite creates active_conversation with participants list."""

    def test_invite_creates_participants_list(self, daemon):
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

    def test_message_adds_new_participant(self, daemon):
        """conversation_message from a new speaker adds them to participants."""
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

    def test_message_does_not_duplicate_participant(self, daemon):
        """Same speaker speaking again shouldn't duplicate in participants."""
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


class TestParticipantJoined:
    """participant_joined adds to active conversation's participants list."""

    def test_adds_new_participant(self, daemon):
        daemon.state["active_conversation"] = {
            "conv_id": "conv-1",
            "participants": ["Alice", "Bob"],
            "turns": [],
        }

        daemon._on_participant_joined(
            {
                "conv_id": "conv-1",
                "agent_id": "c1",
                "display_name": "Charlie",
            }
        )

        assert "Charlie" in daemon.state["active_conversation"]["participants"]

    def test_ignores_wrong_conv_id(self, daemon):
        daemon.state["active_conversation"] = {
            "conv_id": "conv-1",
            "participants": ["Alice"],
            "turns": [],
        }

        daemon._on_participant_joined(
            {
                "conv_id": "conv-OTHER",
                "agent_id": "c1",
                "display_name": "Charlie",
            }
        )

        assert "Charlie" not in daemon.state["active_conversation"]["participants"]

    def test_no_duplicate_on_rejoin(self, daemon):
        daemon.state["active_conversation"] = {
            "conv_id": "conv-1",
            "participants": ["Alice", "Bob"],
            "turns": [],
        }

        daemon._on_participant_joined(
            {
                "conv_id": "conv-1",
                "agent_id": "a1",
                "display_name": "Alice",
            }
        )

        assert daemon.state["active_conversation"]["participants"].count("Alice") == 1


class TestParticipantLeft:
    """participant_left removes from active conversation's participants list."""

    def test_removes_participant(self, daemon):
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

    def test_ignores_wrong_conv_id(self, daemon):
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


class TestConversationEndedClearsState:
    """conversation_ended clears active_conversation and pending_messages."""

    def test_clears_on_end(self, daemon):
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


class TestActiveConversationStatusRendering:
    """TOWN_STATUS.md shows participants list in active conversation block."""

    def test_shows_all_participants(self, daemon):
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
            "you": {"onboardingPhase": "alive", "location": "plaza", "mood": "happy", "energy": 80},
            "nearby_agents": [],
            "think": True,
        }
        daemon._handle_world_update(world_data)

        content = _read_status(daemon)
        assert "Alice, Bob, Charlie" in content
        assert "act say conv-1" in content
        assert "act end conv-1" in content
        assert "Leave conversation" in content

    def test_shows_nearby_conversations_when_not_in_one(self, daemon):
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
            "you": {"onboardingPhase": "alive", "location": "plaza", "mood": "neutral", "energy": 100},
            "nearby_agents": [],
            "think": True,
        }
        daemon._handle_world_update(world_data)

        content = _read_status(daemon)
        assert "Nearby conversations" in content
        assert "join_conversation conv-99" in content
        assert "Alice, Bob" in content


# ===========================================================================
# Event-driven think triggers
# ===========================================================================


class TestEventDrivenThink:
    """Think triggers immediately on proximity changes, not just time throttle."""

    def test_think_triggers_on_new_nearby_agent(self, daemon):
        daemon.state["agent"] = {"onboardingPhase": "alive", "location": "plaza"}
        daemon.state["nearby"] = []

        # First agent_state — no nearby, idle, should think (first time)
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
        content1 = (daemon._workspace_path / "TOWN_STATUS.md").read_text()
        assert "act move" in content1

        # Reset think time to simulate 5s passing (under 12s throttle)
        import time

        daemon._last_think_time = time.time() - 5.0

        # New agent appears nearby — should trigger think immediately
        daemon._handle_agent_state(
            {
                "event": "agent_state",
                "location": "plaza",
                "activity": "idle",
                "speed": 0,
                "nearby_agents": [{"agent_id": "a1", "name": "Alice", "display_name": "Alice"}],
                "context": "town",
            }
        )
        content2 = (daemon._workspace_path / "TOWN_STATUS.md").read_text()
        assert "Alice" in content2

    def test_no_action_menu_when_nearby_unchanged_and_under_throttle(self, daemon):
        daemon.state["agent"] = {"onboardingPhase": "alive", "location": "plaza"}
        daemon.state["nearby"] = [{"agent_id": "a1"}]

        import time

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
        # F10: TOWN_STATUS.md is now always written so the file reflects
        # current location/mood/nearby. But the action menu ("Your turn —
        # decide what to do next") must NOT appear when think=False —
        # otherwise the agent's 2s passive updates would clobber a stale
        # action prompt with fresh ones it shouldn't yet act on.
        status_file = daemon._workspace_path / "TOWN_STATUS.md"
        assert status_file.exists()
        content = status_file.read_text()
        assert "Your turn" not in content
        assert "decide what to do next" not in content


# ---------------------------------------------------------------------------
# Move action — arrival detection via state-poll fallback
# ---------------------------------------------------------------------------
#
# Regression tests for the dry-run finding that ``move`` returned
# ``{"status": "timeout"}`` even when the agent had actually arrived.
# Root cause: the handler waited up to 120s for the discrete ``arrived``
# WebSocket event and only checked the cached state ONCE at timeout. If
# the event was dropped but ``agent_state`` broadcasts (every ~2s)
# already updated the cached location, callers sat through a full 120s
# wait before the "recovered via state cache" fallback fired.
#
# Fix: poll the cached state every 0.5s throughout the wait — return as
# soon as ``state.agent.location`` matches the requested destination.


@pytest.fixture()
async def connected_daemon(daemon):
    """Return a daemon wired with a mock ``ws`` so ``act`` commands send."""
    daemon.ws = AsyncMock()
    # The move handler pulls ``_arrived_event`` from ``asyncio.Event()``;
    # in real lifecycle that's created in ``run()``. Recreate it here so
    # handle_tool_command doesn't NPE on ``.clear()``.
    daemon._arrived_event = asyncio.Event()
    # Use fast polling on the test to keep the suite quick; the poll
    # interval is hardcoded to 0.5s in prod which is fine for the <2s
    # tests we're writing.
    yield daemon


class TestMoveActionStatePoll:
    @pytest.mark.asyncio
    async def test_returns_arrived_when_cached_state_matches_without_event(self, connected_daemon):
        """If ``_arrived_event`` never fires but ``state.agent.location``
        reaches the requested destination, the handler should return
        ``arrived`` WITHIN one poll interval — not wait the full 120s."""
        # Seed the daemon's cached state AT the destination. In prod this
        # would arrive via an ``agent_state`` broadcast; here we set it
        # directly.
        connected_daemon.state = {"agent": {"location": "cafe"}}

        cmd = {"action": "act", "payload": {"action": "move", "destination": "cafe"}}
        # Hard limit: should complete well under a second because the
        # polling loop sees the matching cached state on its first tick.
        result = await asyncio.wait_for(connected_daemon.handle_tool_command(cmd), timeout=3.0)

        assert result["status"] == "arrived"
        assert result["location"] == "cafe"
        # The fallback should log which path it returned through.
        assert "state poll" in result.get("note", "")

    @pytest.mark.asyncio
    async def test_event_fires_returns_arrived_immediately(self, connected_daemon):
        """If the discrete arrived event fires during the wait, return
        ``arrived`` via the event path — faster than waiting for the next
        poll tick."""
        connected_daemon.state = {"agent": {"location": "library"}}
        cmd = {"action": "act", "payload": {"action": "move", "destination": "library"}}

        async def _fire_event_soon():
            await asyncio.sleep(0.05)
            connected_daemon._arrived_event.set()

        fire_task = asyncio.create_task(_fire_event_soon())
        result = await asyncio.wait_for(connected_daemon.handle_tool_command(cmd), timeout=3.0)
        await fire_task

        assert result["status"] == "arrived"
        assert result["location"] == "library"
        # No "note" field on the event path — that's only for the poll
        # fallback so callers can tell apart "discrete signal fired" from
        # "signal missed but state caught up".
        assert "note" not in result

    # The 120s full-timeout path is unchanged behaviour (inlined constant
    # makes it hard to fake out without adding indirection we don't need);
    # the bug this PR fixes is the false-timeout on successful arrivals,
    # covered by ``test_returns_arrived_when_cached_state_matches_without_event``.


# ---------------------------------------------------------------------------
# Auth-failure classification — keeps the daemon from churning on a
# revoked/expired token forever. Network-level closes still bubble to the
# generic retry path.
# ---------------------------------------------------------------------------


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
    """Smoke-test that the AuthFailure type is exported and is a proper Exception."""

    def test_auth_failure_is_exception(self):
        assert issubclass(AuthFailure, Exception)
        # The message is preserved so run_forever can log it verbatim.
        try:
            raise AuthFailure("expired")
        except AuthFailure as exc:
            assert str(exc) == "expired"


# ===========================================================================
# _render_goal_block — unit tests for the goal helper
# ===========================================================================


class TestRenderGoalBlock:
    """_render_goal_block renders currentGoal state into a markdown section."""

    _NOW_MS = 1_700_000_000_000  # fixed "now" for deterministic age calculations

    def test_returns_empty_when_no_goal(self):
        """No currentGoal → empty string (nothing added to status)."""
        assert _render_goal_block({}, self._NOW_MS) == ""

    def test_returns_empty_when_goal_is_none(self):
        assert _render_goal_block({"currentGoal": None}, self._NOW_MS) == ""

    def test_returns_empty_when_goal_is_blank(self):
        assert _render_goal_block({"currentGoal": "   "}, self._NOW_MS) == ""

    def test_renders_goal_block_when_set(self):
        """A set goal produces a ## Your current goal section."""
        out = _render_goal_block({"currentGoal": "finish the library mural"}, self._NOW_MS)
        assert "## Your current goal" in out
        assert "_finish the library mural_" in out
        assert "complete_goal" in out

    def test_no_stale_hint_for_fresh_goal(self):
        """A goal set 2 days ago must NOT show the stale hint."""
        two_days_ago = self._NOW_MS - 2 * 86_400_000
        out = _render_goal_block(
            {"currentGoal": "meet everyone at the cafe", "goalSetAt": two_days_ago},
            self._NOW_MS,
        )
        assert "days" not in out
        assert "still pursuing" not in out

    def test_stale_hint_appears_at_five_days(self):
        """A goal that is exactly 5 days old triggers the stale hint."""
        five_days_ago = self._NOW_MS - 5 * 86_400_000
        out = _render_goal_block(
            {"currentGoal": "explore every location", "goalSetAt": five_days_ago},
            self._NOW_MS,
        )
        assert "5 days" in out
        assert "still pursuing" in out
        assert "set_goal" in out

    def test_stale_hint_shows_correct_age(self):
        """Age in the hint matches the actual elapsed days."""
        ten_days_ago = self._NOW_MS - 10 * 86_400_000
        out = _render_goal_block(
            {"currentGoal": "write a novel", "goalSetAt": ten_days_ago},
            self._NOW_MS,
        )
        assert "10 days" in out

    def test_missing_goal_set_at_still_renders_without_stale(self):
        """goalSetAt absent → renders goal block with no staleness hint."""
        out = _render_goal_block({"currentGoal": "find a best friend"}, self._NOW_MS)
        assert "## Your current goal" in out
        assert "days" not in out


# ===========================================================================
# Goal rendering in TOWN_STATUS.md (integration via _handle_world_update)
# ===========================================================================


class TestGoalInStatusRendering:
    """TOWN_STATUS.md includes the goal block when currentGoal is set."""

    def test_goal_block_in_think_status(self, daemon):
        """Goal block appears in the think-prompt output."""
        world_data = {
            "you": {
                "onboardingPhase": "alive",
                "location": "plaza",
                "mood": "neutral",
                "energy": 100,
                "currentGoal": "explore every corner of town",
            },
            "nearby_agents": [],
            "think": True,
        }
        daemon._handle_world_update(world_data)

        content = _read_status(daemon)
        assert "## Your current goal" in content
        assert "_explore every corner of town_" in content

    def test_no_goal_block_when_goal_absent(self, daemon):
        """No goal block when currentGoal is not set."""
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
        assert "## Your current goal" not in content

    def test_nearby_agent_goal_sub_bullet_rendered(self, daemon):
        """Nearby agent with a goal shows a '- Goal: ...' sub-bullet."""
        world_data = {
            "you": {
                "onboardingPhase": "alive",
                "location": "cafe",
                "mood": "happy",
                "energy": 80,
            },
            "nearby_agents": [
                {
                    "agent_id": "agent-bob",
                    "name": "Bob",
                    "display_name": "Bob",
                    "activity": "idle",
                    "currentGoal": "make peace with Alice",
                }
            ],
            "think": True,
        }
        daemon._handle_world_update(world_data)

        content = _read_status(daemon)
        assert "Goal: _make peace with Alice_" in content

    def test_nearby_agent_without_goal_no_sub_bullet(self, daemon):
        """Nearby agent with no goal must NOT show a goal sub-bullet."""
        world_data = {
            "you": {
                "onboardingPhase": "alive",
                "location": "cafe",
                "mood": "neutral",
                "energy": 100,
            },
            "nearby_agents": [
                {
                    "agent_id": "agent-carol",
                    "name": "Carol",
                    "display_name": "Carol",
                    "activity": "idle",
                }
            ],
            "think": True,
        }
        daemon._handle_world_update(world_data)

        content = _read_status(daemon)
        # Carol is listed but no goal sub-line
        assert "Carol" in content
        assert "Goal:" not in content


# ---------------------------------------------------------------------------
# Tests for _render_stance_lines
# ---------------------------------------------------------------------------


class TestRenderStanceLines:
    """_render_stance_lines returns formatted sub-bullet lines for nearby agents."""

    def test_both_directions_present(self):
        near = {
            "stanceFromMe": "warm and curious",
            "stanceFromThem": "reserved but friendly",
        }
        lines = _render_stance_lines(near)
        assert len(lines) == 2
        assert "Your stance toward them: _warm and curious_" in lines[0]
        assert "Their stance toward you: _reserved but friendly_" in lines[1]

    def test_only_my_stance_set(self):
        near = {
            "stanceFromMe": "kind but distant",
            "stanceFromThem": None,
        }
        lines = _render_stance_lines(near)
        assert len(lines) == 1
        assert "Your stance toward them: _kind but distant_" in lines[0]

    def test_only_their_stance_set(self):
        near = {
            "stanceFromMe": None,
            "stanceFromThem": "enthusiastic and talkative",
        }
        lines = _render_stance_lines(near)
        assert len(lines) == 1
        assert "Their stance toward you: _enthusiastic and talkative_" in lines[0]

    def test_neither_stance_set(self):
        near = {
            "stanceFromMe": None,
            "stanceFromThem": None,
        }
        lines = _render_stance_lines(near)
        assert lines == []

    def test_neither_stance_keys_absent(self):
        near = {"agent_id": "alice-1", "name": "Alice"}
        lines = _render_stance_lines(near)
        assert lines == []

    def test_blank_strings_treated_as_absent(self):
        near = {
            "stanceFromMe": "   ",
            "stanceFromThem": "",
        }
        lines = _render_stance_lines(near)
        assert lines == []


# ===========================================================================
# WorldGM world_event daemon handler (Phase E1)
# ===========================================================================


class TestIconForTag:
    """_icon_for_tag maps tag prefixes to emoji icons."""

    def test_weather_prefix_returns_cloud_rain(self, daemon):
        assert daemon._icon_for_tag("weather.rain") == "\U0001f327"

    def test_ambient_prefix_returns_bulb(self, daemon):
        assert daemon._icon_for_tag("ambient.noise.market") == "\U0001f4a1"

    def test_arc_prefix_returns_scroll(self, daemon):
        assert daemon._icon_for_tag("arc.festival.started") == "\U0001f4dc"

    def test_social_prefix_returns_megaphone(self, daemon):
        assert daemon._icon_for_tag("social.announcement") == "\U0001f4e3"

    def test_personal_prefix_returns_bell(self, daemon):
        assert daemon._icon_for_tag("personal.agent-uuid.goal") == "\U0001f514"

    def test_unknown_tag_returns_bullet(self, daemon):
        assert daemon._icon_for_tag("system.heartbeat") == "•"

    def test_empty_tag_returns_bullet(self, daemon):
        assert daemon._icon_for_tag("") == "•"


class TestHandleWorldEventLog:
    """LOG world_event: appends to TOWN_EVENTS.md, does NOT modify TOWN_STATUS.md."""

    def test_log_event_appends_to_events_file(self, daemon):
        """A non-wake world_event must appear in TOWN_EVENTS.md."""
        daemon.state["agent"] = {"onboardingPhase": "alive", "location": "plaza"}

        payload = {
            "type": "world_event",
            "event": {
                "tag": "weather.rain",
                "summary": "A light rain begins to fall",
                "location": "plaza",
                "timestamp": "2026-05-10T10:00:00Z",
            },
            "wake": False,
        }
        daemon._handle_world_event(payload)

        content = _read_events(daemon)
        assert "A light rain begins to fall" in content
        assert "at plaza" in content
        assert "\U0001f327" in content  # 🌧

    def test_log_event_does_not_write_status(self, daemon):
        """A wake=False world_event must NOT create TOWN_STATUS.md (no think trigger)."""
        daemon.state["agent"] = {"onboardingPhase": "alive", "location": "plaza"}
        status_file = daemon._workspace_path / "TOWN_STATUS.md"

        payload = {
            "type": "world_event",
            "event": {
                "tag": "ambient.market.busy",
                "summary": "Market bustle fills the plaza",
                "location": "plaza",
                "timestamp": "2026-05-10T10:00:00Z",
            },
            "wake": False,
        }
        daemon._handle_world_event(payload)

        assert not status_file.exists()

    def test_log_event_without_location_no_suffix(self, daemon):
        """When location is None, the '(at X)' suffix must not appear."""
        daemon.state["agent"] = {"onboardingPhase": "alive", "location": "library"}
        payload = {
            "type": "world_event",
            "event": {
                "tag": "arc.festival.started",
                "summary": "The Goose Festival kicks off",
                "location": None,
                "timestamp": "2026-05-10T10:00:00Z",
            },
            "wake": False,
        }
        daemon._handle_world_event(payload)

        content = _read_events(daemon)
        assert "The Goose Festival kicks off" in content
        assert "(at" not in content

    def test_log_event_uses_basetag_when_summary_missing(self, daemon):
        """Falls back to event.baseTag if summary is absent."""
        daemon.state["agent"] = {"onboardingPhase": "alive", "location": "cafe"}
        payload = {
            "type": "world_event",
            "event": {
                "tag": "personal.agent-x.observed",
                "baseTag": "ambient.birdsong",
                "location": None,
                "timestamp": "2026-05-10T10:01:00Z",
            },
            "wake": False,
        }
        daemon._handle_world_event(payload)

        content = _read_events(daemon)
        assert "ambient.birdsong" in content

    def test_log_event_uses_tag_when_summary_and_basetag_missing(self, daemon):
        """Falls back to raw tag if both summary and baseTag are absent."""
        daemon.state["agent"] = {"onboardingPhase": "alive", "location": "cafe"}
        payload = {
            "type": "world_event",
            "event": {
                "tag": "social.festival.stage_open",
                "timestamp": "2026-05-10T10:01:00Z",
            },
            "wake": False,
        }
        daemon._handle_world_event(payload)

        content = _read_events(daemon)
        assert "social.festival.stage_open" in content


class TestHandleWorldEventWake:
    """WAKE world_event: appends to TOWN_EVENTS.md AND writes think=True TOWN_STATUS.md."""

    def test_wake_event_appends_to_events_file(self, daemon):
        """wake=True still writes to TOWN_EVENTS.md."""
        daemon.state["agent"] = {"onboardingPhase": "alive", "location": "plaza", "mood": "neutral", "energy": 90}
        payload = {
            "type": "world_event",
            "event": {
                "tag": "personal.agent-me.goal_opportunity",
                "summary": "A goal opportunity has appeared",
                "location": "library",
                "timestamp": "2026-05-10T10:02:00Z",
            },
            "wake": True,
        }
        daemon._handle_world_event(payload)

        content = _read_events(daemon)
        assert "A goal opportunity has appeared" in content

    def test_wake_event_writes_think_status(self, daemon):
        """wake=True must write TOWN_STATUS.md with think content (action menu)."""
        daemon.state["agent"] = {
            "onboardingPhase": "alive",
            "location": "plaza",
            "mood": "neutral",
            "energy": 90,
        }
        payload = {
            "type": "world_event",
            "event": {
                "tag": "personal.agent-me.goal_opportunity",
                "summary": "An opportunity aligned with your goal",
                "location": None,
                "timestamp": "2026-05-10T10:02:00Z",
            },
            "wake": True,
        }
        daemon._handle_world_event(payload)

        content = _read_status(daemon)
        # think=True triggers the action menu in _handle_world_update
        assert "GooseTown Status" in content
        assert "goosetown_cli.py act move" in content


class TestStanceInStatusRendering:
    """Stance sub-bullets appear in the think-prompt nearby-agent block."""

    def test_stance_sub_bullets_rendered_in_status(self, daemon):
        """When a nearby agent has stanceFromMe set, it appears under their chat line."""
        daemon.state["agent"] = {"onboardingPhase": "alive", "location": "plaza"}
        world_data = {
            "you": {
                "onboardingPhase": "alive",
                "location": "plaza",
                "mood": "neutral",
                "energy": 80,
            },
            "nearby_agents": [
                {
                    "agent_id": "bob-uuid",
                    "name": "Bob",
                    "display_name": "Bob",
                    "activity": "idle",
                    "stanceFromMe": "thoughtful and a bit guarded",
                    "stanceFromThem": None,
                }
            ],
            "think": True,
        }
        daemon._handle_world_update(world_data)
        content = _read_status(daemon)
        assert "Your stance toward them: _thoughtful and a bit guarded_" in content

    def test_their_stance_rendered(self, daemon):
        """stanceFromThem appears when set."""
        daemon.state["agent"] = {"onboardingPhase": "alive", "location": "plaza"}
        world_data = {
            "you": {
                "onboardingPhase": "alive",
                "location": "plaza",
                "mood": "neutral",
                "energy": 80,
            },
            "nearby_agents": [
                {
                    "agent_id": "carol-uuid",
                    "name": "Carol",
                    "display_name": "Carol",
                    "activity": "idle",
                    "stanceFromMe": None,
                    "stanceFromThem": "cheerful and direct",
                }
            ],
            "think": True,
        }
        daemon._handle_world_update(world_data)
        content = _read_status(daemon)
        assert "Their stance toward you: _cheerful and direct_" in content

    def test_no_stance_lines_when_absent(self, daemon):
        """No stance lines when both stances are absent."""
        daemon.state["agent"] = {"onboardingPhase": "alive", "location": "plaza"}
        world_data = {
            "you": {
                "onboardingPhase": "alive",
                "location": "plaza",
                "mood": "neutral",
                "energy": 80,
            },
            "nearby_agents": [
                {
                    "agent_id": "dave-uuid",
                    "name": "Dave",
                    "display_name": "Dave",
                    "activity": "idle",
                }
            ],
            "think": True,
        }
        daemon._handle_world_update(world_data)
        content = _read_status(daemon)
        assert "Dave" in content
        assert "stance" not in content.lower()
