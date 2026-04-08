"""Tests for PixelLabService.fetch_all_rotations."""
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
