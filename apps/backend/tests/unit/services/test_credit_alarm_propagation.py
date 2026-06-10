"""Test that PixelLabCreditError is NOT swallowed by resume_sprite_generation.

Before Fix 1 the broad ``except Exception`` at the bottom of
``resume_sprite_generation`` caught ``PixelLabCreditError`` (which subclasses
``Exception``), logged it as a generic failure, and returned normally.  The
admin endpoints' ``except PixelLabCreditError`` alarm arms were therefore dead
code.

After Fix 1 an ``except PixelLabCreditError: raise`` arm sits BEFORE the broad
catch in both ``_sprite_pipeline_task`` and ``resume_sprite_generation``, so
the error propagates out to the admin endpoint and fires the alarm.

This test verifies the ``resume_sprite_generation`` path (the one consumed by
the regenerate + recomposite admin endpoints). Mocking pattern follows
``test_resume_recomposite.py``.
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services.pixellab_service import PixelLabCreditError
from core.services.sprite_pipeline import resume_sprite_generation

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
    """Patch every collaborator ``resume_sprite_generation`` touches.

    Mirrors the helper in ``test_resume_recomposite.py`` so both test
    modules stay in sync with the same patching strategy.
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
async def test_resume_propagates_credit_error_via_queue_signup():
    """PixelLabCreditError from queue_signup_animations must NOT be swallowed.

    The 'animating' resume path calls ``queue_signup_animations`` which is
    the most likely place for a 402 during a regenerate (each animation
    submission costs credits).  A 402 there must propagate out so the admin
    endpoint's ``except PixelLabCreditError`` arm fires the ALARM log.
    """
    pxl = MagicMock()
    pxl.queue_signup_animations = AsyncMock(side_effect=PixelLabCreditError("402 on animating"))

    with _patched_pipeline(pxl):
        with pytest.raises(PixelLabCreditError):
            await resume_sprite_generation("agent-1", "char-1", "appearance", "Name", "animating")


@pytest.mark.asyncio
async def test_resume_propagates_credit_error_via_fetch_spritesheets():
    """PixelLabCreditError from fetch_animation_spritesheets must NOT be swallowed.

    The 'downloading' (recomposite) resume path calls
    ``fetch_animation_spritesheets`` → ``get_character`` which can return 402
    if the account is exhausted. A 402 there must propagate.
    """
    pxl = MagicMock()
    pxl.fetch_animation_spritesheets = AsyncMock(side_effect=PixelLabCreditError("402 on downloading"))

    with _patched_pipeline(pxl):
        with pytest.raises(PixelLabCreditError):
            await resume_sprite_generation("agent-1", "char-1", "appearance", "Name", "downloading")
