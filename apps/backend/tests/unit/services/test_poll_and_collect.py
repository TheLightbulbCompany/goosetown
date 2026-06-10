"""Frames are collected as each direction completes; a vanished (404) job is
terminal, not an infinite wait."""

import base64
import io
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from PIL import Image

from core.services.pixellab_service import PixelLabService


def _rgba_b64(w=8, h=8):
    img = Image.new("RGBA", (w, h), (10, 20, 30, 255))
    return base64.b64encode(img.tobytes()).decode()


def _completed_job(n_frames=2, w=8, h=8):
    images = [{"type": "rgba_bytes", "width": w, "height": h, "base64": _rgba_b64(w, h)} for _ in range(n_frames)]
    return {"status": "completed", "last_response": {"images": images}}


def test_frames_from_job_decodes_rgba_bytes():
    service = PixelLabService(api_key="stub")
    frames = service._frames_from_job(_completed_job(n_frames=3))
    assert len(frames) == 3
    Image.open(io.BytesIO(frames[0]))


@pytest.mark.asyncio
async def test_poll_collects_per_direction_and_handles_404():
    service = PixelLabService(api_key="stub")
    statuses = {
        "j1": _completed_job(n_frames=2),
        "j2": httpx.HTTPStatusError("404", request=None, response=type("R", (), {"status_code": 404})()),
    }

    async def fake_get_job_status(jid):
        v = statuses[jid]
        if isinstance(v, Exception):
            raise v
        return v

    with (
        patch.object(service, "get_job_status", side_effect=fake_get_job_status),
        patch("core.services.pixellab_service.asyncio.sleep", AsyncMock()),
    ):
        out = await service._poll_and_collect(["south", "west"], ["j1", "j2"], None, "walk", max_attempts=3)

    assert "south" in out and len(out["south"]) == 2
    assert "west" not in out
