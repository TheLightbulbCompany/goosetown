"""Regression tests for findings M, N, P, R from dry-run #3.

  M — Move-rejection's ``destination`` field echoed the navmesh node
     name (``"exit"``) instead of the user-requested destination.
  N — Arrival into a building reported ``location: "exit"`` (same
     class as the player_spawn quirk fixed in PR #286, different
     marker).
  P — ``Mood:`` rendered as raw integer (``"Mood: 10"``) leaving
     agents to guess if 10 is good or bad.
  R — ``Activity:`` rendered blank when idle (``"Activity: "``)
     instead of ``"Activity: idle"``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from goosetown.daemon import (
    TownDaemon,
    mood_label,
    render_activity,
    render_mood,
)


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


# ---------------------------------------------------------------------------
# M / N — internal-marker normalization in rejection AND arrival
# ---------------------------------------------------------------------------


class TestMoveRejectedDestinationNormalization:
    @pytest.mark.asyncio
    async def test_rejected_destination_exit_corrected_to_requested(
        self, daemon: TownDaemon
    ):
        """M — server says destination=exit; daemon should echo cafe."""
        daemon._move_outcome = asyncio.get_running_loop().create_future()
        daemon._pending_move_destination = "cafe"
        daemon._on_move_rejected(
            {"event": "move_rejected", "reason": "busy", "destination": "exit"}
        )
        outcome = daemon._move_outcome.result()
        assert outcome["destination"] == "cafe"

    @pytest.mark.asyncio
    async def test_rejected_destination_player_spawn_corrected_to_requested(
        self, daemon: TownDaemon
    ):
        daemon._move_outcome = asyncio.get_running_loop().create_future()
        daemon._pending_move_destination = "plaza"
        daemon._on_move_rejected(
            {"event": "move_rejected", "reason": "busy", "destination": "player_spawn"}
        )
        outcome = daemon._move_outcome.result()
        assert outcome["destination"] == "plaza"

    @pytest.mark.asyncio
    async def test_rejected_real_destination_passes_through(self, daemon: TownDaemon):
        daemon._move_outcome = asyncio.get_running_loop().create_future()
        daemon._pending_move_destination = "cafe"
        daemon._on_move_rejected(
            {"event": "move_rejected", "reason": "busy", "destination": "library"}
        )
        outcome = daemon._move_outcome.result()
        assert outcome["destination"] == "library"


class TestArrivedExitNormalization:
    @pytest.mark.asyncio
    async def test_arrived_exit_corrected_to_library(self, daemon: TownDaemon):
        """N — server reports arrival at "exit" when entering library."""
        daemon._move_outcome = asyncio.get_running_loop().create_future()
        daemon._pending_move_destination = "library"
        daemon._on_arrived({"event": "arrived", "location": "exit"})
        outcome = daemon._move_outcome.result()
        assert outcome["location"] == "library"

    @pytest.mark.asyncio
    async def test_arrived_real_location_still_passes_through(self, daemon: TownDaemon):
        daemon._move_outcome = asyncio.get_running_loop().create_future()
        daemon._pending_move_destination = "cafe"
        daemon._on_arrived({"event": "arrived", "location": "cafe"})
        outcome = daemon._move_outcome.result()
        assert outcome["location"] == "cafe"


# ---------------------------------------------------------------------------
# P — render_mood
# ---------------------------------------------------------------------------


class TestRenderMood:
    def test_neutral_zero(self):
        assert render_mood(0) == "neutral (0)"

    def test_happy_positive(self):
        assert render_mood(15) == "happy (15)"

    def test_elated_high(self):
        assert render_mood(40) == "elated (40)"

    def test_sad_negative(self):
        assert render_mood(-15) == "sad (-15)"

    def test_miserable_low(self):
        assert render_mood(-40) == "miserable (-40)"

    def test_string_fallback_passes_through(self):
        """Defensive: pre-labeled string from older payloads still renders."""
        assert render_mood("happy") == "happy"

    def test_empty_string_falls_back_to_neutral(self):
        assert render_mood("") == "neutral"

    def test_mood_label_boundaries(self):
        # Boundary points from the spec.
        assert mood_label(-30) == "miserable"
        assert mood_label(-10) == "sad"
        assert mood_label(10) == "neutral"
        assert mood_label(30) == "happy"
        assert mood_label(50) == "elated"


# ---------------------------------------------------------------------------
# R — render_activity
# ---------------------------------------------------------------------------


class TestRenderActivity:
    def test_blank_string_renders_idle(self):
        assert render_activity("") == "idle"

    def test_none_renders_idle(self):
        assert render_activity(None) == "idle"

    def test_real_activity_passes_through(self):
        assert render_activity("read") == "read"

    def test_idle_explicit_passes_through(self):
        assert render_activity("idle") == "idle"
