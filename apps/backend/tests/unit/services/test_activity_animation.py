"""Activity animations generate via the unified ``generate_animation`` path.

Regression for the template-gap bug: ``_generate_activity_animation`` used to
early-return for any animation not in ``CUSTOM_ANIMATIONS``, silently skipping
template-based activity animations. It must now generate for ANY registry entry
(template or v3) by delegating to ``PixelLabService.generate_animation`` and
compositing/persisting the result.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import core.services.activity_service as act


@pytest.mark.asyncio
async def test_generate_activity_animation_uses_generate_animation():
    gen = AsyncMock(return_value={"south": [b"f"], "west": [b"f"], "east": [b"f"], "north": [b"f"]})
    fake_service = MagicMock()
    fake_service.get_town_agent_by_id.return_value = {"spriteAnimations": {}}

    with (
        patch("core.services.activity_service.PixelLabService") as PxlCls,
        patch("core.services.activity_service.TownService", return_value=fake_service),
        patch(
            "core.services.sprite_storage.composite_v2_animation",
            return_value=(b"png", 4),
        ),
        patch(
            "core.services.sprite_storage.upload_sprites_to_s3",
            return_value={"reading": {"key": "sprites/a/reading.png", "frame_count": 4}},
        ),
        patch("core.services.activity_service.get_game_server_client"),
        patch("core.services.activity_service.get_daemon_mailbox"),
    ):
        PxlCls.return_value.generate_animation = gen
        await act._generate_activity_animation("agent-1", "char-1", "reading")

    gen.assert_awaited_once()
    assert gen.await_args.args[1] == "reading"
    # Result is persisted via update_agent_state(sprite_animations=...)
    persisted = [c for c in fake_service.update_agent_state.call_args_list if "sprite_animations" in c.kwargs]
    assert persisted, "expected a sprite_animations persistence call"
    assert "reading" in persisted[-1].kwargs["sprite_animations"]
