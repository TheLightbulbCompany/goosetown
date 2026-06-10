"""generate_animation acquires the global semaphore, submits one batched call,
and returns the collected per-direction frames."""

from unittest.mock import AsyncMock, patch

import pytest

from core.services.pixellab_service import PixelLabService


@pytest.mark.asyncio
async def test_generate_animation_happy_path():
    service = PixelLabService(api_key="stub")
    with (
        patch.object(service, "queue_animation", AsyncMock(return_value=(["south", "west"], ["j1", "j2"]))),
        patch.object(service, "_poll_and_collect", AsyncMock(return_value={"south": [b"png"], "west": [b"png"]})),
    ):
        out = await service.generate_animation("char-1", "walk")
    assert set(out.keys()) == {"south", "west"}


@pytest.mark.asyncio
async def test_generate_animation_no_jobs_returns_empty():
    service = PixelLabService(api_key="stub")
    with patch.object(service, "queue_animation", AsyncMock(return_value=([], []))):
        out = await service.generate_animation("char-1", "walk")
    assert out == {}
