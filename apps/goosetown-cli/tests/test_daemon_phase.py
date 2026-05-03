"""Regression tests for the daemon's onboardingPhase handling.

The daemon used to write a "Your sprite is being generated..." welcome blurb
to TOWN_STATUS.md whenever the server reported `onboardingPhase=static`. By
the time the server sets phase=static, the agent has already been spawned in
the game server with a usable static sprite — they're alive in the world.
The blurb is therefore wrong: it tells the agent their sprite is still being
generated when in fact they're standing in the plaza. If sprite-pipeline
Phase C silently fails (no walk animations resolved), phase stays at
"static" forever and the daemon writes the blurb on every heartbeat
indefinitely — making the agent permanently blind to the live world state.

These tests pin the corrected behavior: a `world_update` carrying
`onboardingPhase=static` produces a normal live status, NOT the blurb.
"""

from pathlib import Path

import pytest

from goosetown.daemon import TownDaemon, _WELCOME_BLURB


@pytest.fixture
def daemon(tmp_path: Path) -> TownDaemon:
    """A TownDaemon bound to tmp dirs, with no real WebSocket."""
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


def _world_update(phase: str, *, location: str = "plaza") -> dict:
    """Minimal world_update payload with the given onboardingPhase."""
    return {
        "you": {
            "id": "agent-id",
            "display_name": "Test Agent",
            "location": location,
            "activity": "idle",
            "mood": "neutral",
            "energy": 100,
            "location_context": "town",
            "onboardingPhase": phase,
        },
        "nearby_agents": [],
        "think": True,
    }


def _read_status(daemon: TownDaemon) -> str:
    return (daemon._workspace_path / "TOWN_STATUS.md").read_text()


class TestPhaseStaticDoesNotBlockLiveStatus:
    """The phase=static blurb regression — the bug Pixel surfaced 2026-05-02."""

    def test_phase_static_writes_live_status_not_blurb(self, daemon: TownDaemon):
        """phase=static means agent is in the world; daemon must NOT write the blurb."""
        daemon._handle_world_update(_world_update("static"))
        status = _read_status(daemon)
        assert _WELCOME_BLURB not in status, (
            "Daemon wrote the welcome blurb when phase=static, but by phase=static "
            "the agent has already been spawned in the game server with a usable "
            "static sprite. The blurb's 'Your sprite is being generated' message "
            "is wrong here."
        )

    def test_phase_static_writes_location(self, daemon: TownDaemon):
        """phase=static live status should reflect the agent's actual location."""
        daemon._handle_world_update(_world_update("static", location="library"))
        status = _read_status(daemon)
        assert "library" in status

    def test_phase_alive_writes_live_status(self, daemon: TownDaemon):
        """Sanity: phase=alive (the post-Phase-C state) writes live status."""
        daemon._handle_world_update(_world_update("alive"))
        status = _read_status(daemon)
        assert _WELCOME_BLURB not in status
        assert "plaza" in status

    def test_phase_static_repeated_updates_never_revert_to_blurb(
        self, daemon: TownDaemon
    ):
        """If sprite-pipeline Phase C never advances phase to alive, the daemon
        must still keep writing live status on every heartbeat — not the blurb.
        This is the core 'agent permanently blind' regression."""
        for _ in range(5):
            daemon._handle_world_update(_world_update("static"))
        status = _read_status(daemon)
        assert _WELCOME_BLURB not in status
