"""Regression tests for move outcome handling.

Two findings from 2026-05-02 dry runs surfaced in `act move`:

  G — Cross-building moves silently succeed-as-rejected. From inside
     the library, `move cafe` returns `MOVE_REJECTED ... busy` from
     the game server (the library scene only handles in-library
     moves). The daemon's `_on_move_rejected` and `_on_arrived` both
     set the same `_arrived_event`, so `handle_tool_command` returned
     `{"status": "arrived"}` even when the move was rejected. Agent
     thinks it succeeded; nothing happens; confusion.

  H — Reported location can be a hidden marker. The game server's
     `_get_agent_location` (apps/godot/scripts/server/agent_controller.gd)
     returns the nearest marker name. When the `player_spawn` marker
     happens to overlap or sit near `plaza`, the arrival reports
     `location: "player_spawn"` even though the user requested
     `plaza` and the agent is functionally there. The daemon can
     correct by preferring the requested destination when the
     reported location is the spawn-only marker.

These tests pin the corrected behavior — the daemon now uses a
`_move_outcome` future analogous to `_chat_outcome` (#281) so move
rejections surface to the CLI as ``{"status": "rejected", ...}``,
and the spawn-marker quirk is normalized away.
"""

from __future__ import annotations

import asyncio
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


class TestMoveRejectedSurfacedToOutcome:
    """G — move_rejected must populate _move_outcome with the reason."""

    @pytest.mark.asyncio
    async def test_move_rejected_sets_outcome_with_reason(self, daemon: TownDaemon):
        daemon._move_outcome = asyncio.get_running_loop().create_future()
        daemon._on_move_rejected(
            {"event": "move_rejected", "reason": "busy", "destination": "cafe"}
        )
        assert daemon._move_outcome.done()
        outcome = daemon._move_outcome.result()
        assert outcome["status"] == "rejected"
        assert outcome["reason"] == "busy"
        assert outcome["destination"] == "cafe"

    @pytest.mark.asyncio
    async def test_move_rejected_tolerates_no_outcome_future(self, daemon: TownDaemon):
        """A passive move rejection (e.g., from a server-pushed event) must
        not crash when no outgoing CLI invocation is awaiting the result."""
        daemon._move_outcome = None
        # Should not raise:
        daemon._on_move_rejected(
            {"event": "move_rejected", "reason": "busy", "destination": "cafe"}
        )

    @pytest.mark.asyncio
    async def test_move_arrived_sets_outcome_with_location(self, daemon: TownDaemon):
        daemon._move_outcome = asyncio.get_running_loop().create_future()
        daemon._on_arrived({"event": "arrived", "location": "plaza"})
        outcome = daemon._move_outcome.result()
        assert outcome["status"] == "arrived"
        assert outcome["location"] == "plaza"


class TestPlayerSpawnLocationCorrection:
    """H — when arrived reports `player_spawn` but the agent meant somewhere
    else, normalize away the spawn-marker false reading."""

    @pytest.mark.asyncio
    async def test_arrived_player_spawn_corrected_to_requested_destination(
        self, daemon: TownDaemon
    ):
        """If the daemon has a pending move with destination=plaza and the
        arrival reports player_spawn, surface plaza in the CLI return — the
        agent IS at plaza, the marker overlap is a Godot positional quirk."""
        daemon._move_outcome = asyncio.get_running_loop().create_future()
        # Simulate: a move to plaza was requested.
        daemon._pending_move_destination = "plaza"
        daemon._on_arrived({"event": "arrived", "location": "player_spawn"})
        outcome = daemon._move_outcome.result()
        assert outcome["status"] == "arrived"
        # Reported location was player_spawn, but the daemon corrects to
        # the requested destination. The agent is at plaza by intent.
        assert outcome["location"] == "plaza"

    @pytest.mark.asyncio
    async def test_arrived_player_spawn_kept_when_no_pending_destination(
        self, daemon: TownDaemon
    ):
        """If there's no requested destination (e.g., agent just spawned and
        idled), `player_spawn` is the honest answer — keep it."""
        daemon._move_outcome = asyncio.get_running_loop().create_future()
        daemon._pending_move_destination = None
        daemon._on_arrived({"event": "arrived", "location": "player_spawn"})
        outcome = daemon._move_outcome.result()
        assert outcome["location"] == "player_spawn"

    @pytest.mark.asyncio
    async def test_arrived_real_location_passes_through(self, daemon: TownDaemon):
        """Sanity: real (non-spawn) reported locations pass through unchanged."""
        daemon._move_outcome = asyncio.get_running_loop().create_future()
        daemon._pending_move_destination = "cafe"
        daemon._on_arrived({"event": "arrived", "location": "cafe"})
        outcome = daemon._move_outcome.result()
        assert outcome["location"] == "cafe"
