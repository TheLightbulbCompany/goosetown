"""Regression tests for the TOWN_STATUS.md template wording.

Findings I and K from the 2026-05-02 dry runs:

  K — When ``nearby`` is empty (no agents around), the template used to
     emit ``chat <agent_id> <message> (use ids shown above)`` with no
     ids visible — leaving the agent confused about what to put for
     ``<agent_id>``.

  I — TOWN_STATUS.md never referenced TOWN_EVENTS.md, so an agent
     reading status to decide what to do had no nudge to also check
     recent events for context.

Both are template-output regressions: the daemon writes a status block,
and the test asserts on what's in the rendered markdown.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from goosetown.daemon import TownDaemon


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


def _world_update_alone() -> dict:
    return {
        "you": {
            "id": "agent-id",
            "display_name": "Test Agent",
            "location": "plaza",
            "activity": "idle",
            "mood": "neutral",
            "energy": 100,
            "location_context": "town",
            "onboardingPhase": "alive",
        },
        "nearby_agents": [],
        "think": True,
    }


def _read_status(daemon: TownDaemon) -> str:
    return (daemon._workspace_path / "TOWN_STATUS.md").read_text()


def test_chat_line_omitted_when_alone(daemon: TownDaemon):
    """K — when nearby is empty, status must not advertise a chat command
    that says "use ids shown above" with no ids above. The clearest fix
    is to drop the chat line entirely in this case."""
    daemon._handle_world_update(_world_update_alone())
    status = _read_status(daemon)
    assert "use ids shown above" not in status, (
        "status still contains the misleading 'use ids shown above' phrase "
        "when no agents are nearby."
    )


def test_status_references_town_events_md(daemon: TownDaemon):
    """I — status should hint at TOWN_EVENTS.md so the agent knows where
    to find recent activity context."""
    daemon._handle_world_update(_world_update_alone())
    status = _read_status(daemon)
    assert "TOWN_EVENTS.md" in status, (
        "status does not reference TOWN_EVENTS.md; agent has no nudge to "
        "check recent activity for context."
    )
