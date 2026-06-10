"""queue_signup_animations generates each auto_generate animation via
generate_animation (sequentially) and returns {anim_name: {direction: frames}}."""

from unittest.mock import patch

import pytest

from core.services.pixellab_service import SIGNUP_ANIMATIONS, PixelLabService


@pytest.mark.asyncio
async def test_signup_uses_generate_animation_per_anim():
    service = PixelLabService(api_key="stub")
    calls = []

    async def fake_generate(char, name, *, on_progress=None, max_attempts=30):
        calls.append(name)
        return {"south": [b"f"], "west": [b"f"], "east": [b"f"], "north": [b"f"]}

    with patch.object(service, "generate_animation", side_effect=fake_generate):
        out = await service.queue_signup_animations("char-1")

    assert set(calls) == set(SIGNUP_ANIMATIONS.keys())
    for name in SIGNUP_ANIMATIONS:
        assert name in out and "south" in out[name]
