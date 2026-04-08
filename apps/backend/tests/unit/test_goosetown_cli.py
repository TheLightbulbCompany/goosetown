"""Unit tests for TownDaemon welcome blurb during onboarding=static."""
import json
import sys
from pathlib import Path

import pytest

# The skill lives outside the normal Python package tree — add it to sys.path
_SKILL_DIR = Path(__file__).parents[2] / "data" / "goosetown-skill"
sys.path.insert(0, str(_SKILL_DIR))

import goosetown_cli  # noqa: E402  (import after path manipulation)
from goosetown_cli import TownDaemon, _WELCOME_BLURB  # noqa: E402


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
