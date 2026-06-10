"""Tests for resume_sprite_generation's two resume entry points.

The recomposite admin endpoint (POST /town/admin/recomposite-sprites) resumes
every agent at ``current_status="downloading"``. Its documented contract is:
re-download the agent's EXISTING PixelLab frames and re-composite — **no new
generation credits**. So the "downloading" resume path must call
``fetch_animation_spritesheets`` (re-download) and must NOT call
``queue_signup_animations`` (re-generate → costs credits).

The "animating" resume path (reached after a fresh "creating" regenerate) is
the one that legitimately re-generates via ``queue_signup_animations``.
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services.sprite_pipeline import resume_sprite_generation

_FOUR_DIR_FRAMES = {
    "south": [b"f"],
    "west": [b"f"],
    "east": [b"f"],
    "north": [b"f"],
}

_AGENT_RECORD = {
    "spriteReady": True,
    "spriteUrl": "https://cdn/old.png",
    "currentLocation": "plaza",
    "locationContext": "town",
    "userId": "u1",
    "spriteAnimations": {},
}


@contextlib.contextmanager
def _patched_pipeline(pxl):
    """Patch every collaborator resume_sprite_generation touches.

    PixelLabService is patched at its source module (it's imported locally
    inside the function); the daemon/game-server/lattice helpers are patched
    at the sprite_pipeline module where they're bound at import time.
    """
    ts = MagicMock()
    ts.get_town_agent_by_id.return_value = dict(_AGENT_RECORD)
    gs = MagicMock()
    gs.connect_agent = AsyncMock()

    with (
        patch("core.services.pixellab_service.PixelLabService", return_value=pxl),
        patch(
            "core.services.sprite_storage.composite_animation_spritesheet",
            return_value=(b"png", 4),
        ),
        patch(
            "core.services.sprite_storage.upload_sprite_to_s3",
            return_value="sprites/agent-1/walk.png",
        ),
        patch("core.services.sprite_pipeline.TownService", return_value=ts),
        patch("core.services.sprite_pipeline.get_game_server_client", return_value=gs),
        patch("core.services.sprite_pipeline.advance_phase"),
        patch("core.services.sprite_pipeline._send_to_daemon"),
        patch("core.services.sprite_pipeline._make_agent_event_callback"),
    ):
        yield


@pytest.mark.asyncio
async def test_resume_downloading_redownloads_not_regenerates():
    """Recomposite (resume at 'downloading') must re-download existing frames,
    never re-generate (which would burn PixelLab credits)."""
    pxl = MagicMock()
    pxl.fetch_animation_spritesheets = AsyncMock(return_value={"walk": dict(_FOUR_DIR_FRAMES)})
    pxl.queue_signup_animations = AsyncMock(return_value={})  # must NOT be called

    with _patched_pipeline(pxl):
        await resume_sprite_generation("agent-1", "char-1", "", "Name", "downloading")

    pxl.fetch_animation_spritesheets.assert_awaited_once()
    pxl.queue_signup_animations.assert_not_called()


@pytest.mark.asyncio
async def test_resume_animating_regenerates():
    """A fresh regenerate (resume at 'animating') legitimately re-generates via
    queue_signup_animations and must NOT re-download."""
    pxl = MagicMock()
    pxl.queue_signup_animations = AsyncMock(return_value={"walk": dict(_FOUR_DIR_FRAMES)})
    pxl.fetch_animation_spritesheets = AsyncMock(return_value={})  # must NOT be called

    with _patched_pipeline(pxl):
        await resume_sprite_generation("agent-1", "char-1", "", "Name", "animating")

    pxl.queue_signup_animations.assert_awaited_once()
    pxl.fetch_animation_spritesheets.assert_not_called()
