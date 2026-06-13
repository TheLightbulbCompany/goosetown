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


@pytest.mark.asyncio
async def test_signup_animation_failure_propagates():
    """A failing signup animation must RAISE, not yield {} for that name.

    With SIGNUP_ANIMATIONS == {"walk"} there are no "others" to continue
    with — swallowing the failure would let run_signup_pipeline complete
    on the Phase B static placeholder (spriteStatus="complete", no SQS
    retry, no DLQ). The SQS worker's retry/DLQ owns failures now.
    """
    service = PixelLabService(api_key="stub")

    async def fake_generate(char, name, *, on_progress=None, max_attempts=30):
        raise RuntimeError("PixelLab animation persistently failing")

    with patch.object(service, "generate_animation", side_effect=fake_generate):
        with pytest.raises(RuntimeError, match="persistently failing"):
            await service.queue_signup_animations("char-1")
