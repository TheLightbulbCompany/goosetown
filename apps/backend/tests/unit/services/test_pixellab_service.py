"""Tests for PixelLabService.fetch_all_rotations."""
import io
import json
import zipfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services.pixellab_service import PixelLabService


@pytest.mark.asyncio
async def test_fetch_all_rotations_returns_dict_of_bytes():
    service = PixelLabService(api_key="stub")
    stub_char = {
        "rotation_urls": {
            "south": "https://cdn.example.com/s.png",
            "west":  "https://cdn.example.com/w.png",
            "east":  "https://cdn.example.com/e.png",
            "north": "https://cdn.example.com/n.png",
        },
        "size": {"width": 96, "height": 96},
    }

    fake_resp = MagicMock()
    fake_resp.content = b"\x89PNGfakebytes"
    fake_resp.raise_for_status = MagicMock()

    fake_client = MagicMock()
    fake_client.get = AsyncMock(return_value=fake_resp)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch.object(service, "get_character", AsyncMock(return_value=stub_char)), \
         patch("core.services.pixellab_service.httpx.AsyncClient", return_value=fake_client):
        result = await service.fetch_all_rotations("char-123")

    assert set(result.keys()) == {"south", "west", "east", "north"}
    for png in result.values():
        assert png == b"\x89PNGfakebytes"


@pytest.mark.asyncio
async def test_fetch_all_rotations_skips_missing_direction_urls():
    service = PixelLabService(api_key="stub")
    stub_char = {
        "rotation_urls": {
            "south": "https://cdn.example.com/s.png",
        },
        "size": {"width": 96, "height": 96},
    }

    fake_resp = MagicMock()
    fake_resp.content = b"\x89PNGfakebytes"
    fake_resp.raise_for_status = MagicMock()

    fake_client = MagicMock()
    fake_client.get = AsyncMock(return_value=fake_resp)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch.object(service, "get_character", AsyncMock(return_value=stub_char)), \
         patch("core.services.pixellab_service.httpx.AsyncClient", return_value=fake_client):
        result = await service.fetch_all_rotations("char-123")

    assert set(result.keys()) == {"south"}


@pytest.mark.asyncio
async def test_fetch_all_rotations_returns_empty_when_get_character_fails():
    service = PixelLabService(api_key="stub")

    with patch.object(service, "get_character", AsyncMock(side_effect=RuntimeError("boom"))):
        result = await service.fetch_all_rotations("char-123")

    assert result == {}


def _make_test_zip() -> bytes:
    from PIL import Image

    def _png(w=96, h=96):
        img = Image.new("RGBA", (w, h), (255, 0, 0, 128))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    metadata = {
        "character": {"id": "char-1"},
        "frames": {
            "rotations": {},
            "animations": {
                "walking-abc123": {"south": ["animations/walking-abc123/south/frame_000.png"]},
                "walking-def456": {"west": ["animations/walking-def456/west/frame_000.png"]},
                "animating-ghi789": {"south": ["animations/animating-ghi789/south/frame_000.png",
                                                "animations/animating-ghi789/south/frame_001.png"]},
            },
        },
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("metadata.json", json.dumps(metadata))
        zf.writestr("animations/walking-abc123/south/frame_000.png", _png())
        zf.writestr("animations/walking-def456/west/frame_000.png", _png())
        zf.writestr("animations/animating-ghi789/south/frame_000.png", _png())
        zf.writestr("animations/animating-ghi789/south/frame_001.png", _png())
    return buf.getvalue()


@pytest.mark.asyncio
async def test_fetch_animation_spritesheets_resolves_names():
    pxl = PixelLabService(api_key="test")
    mock_char = {
        "animations": [
            {"name": "walk", "direction": "south", "num_frames": 1},
            {"name": "walk", "direction": "west", "num_frames": 1},
            {"name": "breathing-idle", "direction": "south", "num_frames": 2},
        ]
    }
    pxl.get_character = AsyncMock(return_value=mock_char)
    pxl.download_character_zip = AsyncMock(return_value=_make_test_zip())

    result = await pxl.fetch_animation_spritesheets("char-1")

    assert "walk" in result
    assert "breathing-idle" in result
    assert "south" in result["walk"]
    assert "west" in result["walk"]
    assert len(result["walk"]["south"]) == 1
    assert "south" in result["breathing-idle"]
    assert len(result["breathing-idle"]["south"]) == 2


@pytest.mark.asyncio
async def test_fetch_animation_spritesheets_no_zip_returns_empty():
    pxl = PixelLabService(api_key="test")
    pxl.get_character = AsyncMock(return_value={"animations": []})
    pxl.download_character_zip = AsyncMock(return_value=None)
    result = await pxl.fetch_animation_spritesheets("char-1")
    assert result == {}
