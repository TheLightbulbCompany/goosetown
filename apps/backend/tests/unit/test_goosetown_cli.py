"""Unit tests for TownDaemon — welcome blurb and group conversations."""
import sys
from pathlib import Path

import pytest

# The skill lives outside the normal Python package tree — add it to sys.path
_SKILL_DIR = Path(__file__).parents[2] / "data" / "goosetown-skill"
sys.path.insert(0, str(_SKILL_DIR))

from goosetown_cli import _WELCOME_BLURB, TownDaemon  # noqa: E402

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
        token="test-token",
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


class TestConversationInviteTracksParticipants:
    """conversation_invite creates active_conversation with participants list."""

    def test_invite_creates_participants_list(self, daemon):
        daemon._on_conversation_message("conversation_invite", {
            "conv_id": "conv-1",
            "from": "Alice",
            "from_agent_name": "alice",
            "message": "Hey there!",
        })

        active = daemon.state.get("active_conversation")
        assert active is not None
        assert active["conv_id"] == "conv-1"
        assert active["participants"] == ["Alice"]
        assert len(active["turns"]) == 1
        assert active["turns"][0]["speaker"] == "Alice"

    def test_message_adds_new_participant(self, daemon):
        """conversation_message from a new speaker adds them to participants."""
        daemon._on_conversation_message("conversation_invite", {
            "conv_id": "conv-1",
            "from": "Alice",
            "from_agent_name": "alice",
            "message": "Hey!",
        })
        daemon._on_conversation_message("conversation_message", {
            "conv_id": "conv-1",
            "from": "Bob",
            "from_agent_name": "bob",
            "text": "Hi Alice!",
            "turn": 2,
        })

        active = daemon.state["active_conversation"]
        assert "Alice" in active["participants"]
        assert "Bob" in active["participants"]
        assert len(active["turns"]) == 2

    def test_message_does_not_duplicate_participant(self, daemon):
        """Same speaker speaking again shouldn't duplicate in participants."""
        daemon._on_conversation_message("conversation_invite", {
            "conv_id": "conv-1",
            "from": "Alice",
            "from_agent_name": "alice",
            "message": "Hey!",
        })
        daemon._on_conversation_message("conversation_message", {
            "conv_id": "conv-1",
            "from": "Alice",
            "from_agent_name": "alice",
            "text": "Hello again",
            "turn": 2,
        })

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

        daemon._on_participant_joined({
            "conv_id": "conv-1",
            "agent_id": "c1",
            "display_name": "Charlie",
        })

        assert "Charlie" in daemon.state["active_conversation"]["participants"]

    def test_ignores_wrong_conv_id(self, daemon):
        daemon.state["active_conversation"] = {
            "conv_id": "conv-1",
            "participants": ["Alice"],
            "turns": [],
        }

        daemon._on_participant_joined({
            "conv_id": "conv-OTHER",
            "agent_id": "c1",
            "display_name": "Charlie",
        })

        assert "Charlie" not in daemon.state["active_conversation"]["participants"]

    def test_no_duplicate_on_rejoin(self, daemon):
        daemon.state["active_conversation"] = {
            "conv_id": "conv-1",
            "participants": ["Alice", "Bob"],
            "turns": [],
        }

        daemon._on_participant_joined({
            "conv_id": "conv-1",
            "agent_id": "a1",
            "display_name": "Alice",
        })

        assert daemon.state["active_conversation"]["participants"].count("Alice") == 1


class TestParticipantLeft:
    """participant_left removes from active conversation's participants list."""

    def test_removes_participant(self, daemon):
        daemon.state["active_conversation"] = {
            "conv_id": "conv-1",
            "participants": ["Alice", "Bob", "Charlie"],
            "turns": [],
        }

        daemon._on_participant_left({
            "conv_id": "conv-1",
            "agent_id": "b1",
            "display_name": "Bob",
            "remaining": 2,
        })

        assert "Bob" not in daemon.state["active_conversation"]["participants"]
        assert len(daemon.state["active_conversation"]["participants"]) == 2

    def test_ignores_wrong_conv_id(self, daemon):
        daemon.state["active_conversation"] = {
            "conv_id": "conv-1",
            "participants": ["Alice", "Bob"],
            "turns": [],
        }

        daemon._on_participant_left({
            "conv_id": "conv-OTHER",
            "agent_id": "b1",
            "display_name": "Bob",
            "remaining": 1,
        })

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

        daemon._on_conversation_ended({
            "conv_id": "conv-1",
            "turn_count": 3,
            "location": "plaza",
            "log_summary": "They chatted about the weather.",
        })

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
