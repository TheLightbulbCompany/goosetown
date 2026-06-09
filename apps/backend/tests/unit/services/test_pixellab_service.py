"""Tests for PixelLabService.fetch_all_rotations."""

import io
import json
import zipfile
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.services.pixellab_service import _RETRY_MAX_ATTEMPTS, PixelLabService


@pytest.mark.asyncio
async def test_fetch_all_rotations_returns_dict_of_bytes():
    service = PixelLabService(api_key="stub")
    stub_char = {
        "rotation_urls": {
            "south": "https://cdn.example.com/s.png",
            "west": "https://cdn.example.com/w.png",
            "east": "https://cdn.example.com/e.png",
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

    with (
        patch.object(service, "get_character", AsyncMock(return_value=stub_char)),
        patch("core.services.pixellab_service.httpx.AsyncClient", return_value=fake_client),
    ):
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

    with (
        patch.object(service, "get_character", AsyncMock(return_value=stub_char)),
        patch("core.services.pixellab_service.httpx.AsyncClient", return_value=fake_client),
    ):
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
                "animating-ghi789": {
                    "south": [
                        "animations/animating-ghi789/south/frame_000.png",
                        "animations/animating-ghi789/south/frame_001.png",
                    ]
                },
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


# --- _request retry-on-transient behavior (the grey-box reliability fix) ---


def _resp(status_code, *, json_body=None, content=None):
    """Build a fake httpx.Response. 4xx/5xx get a raise_for_status side effect."""
    r = MagicMock()
    r.status_code = status_code
    r.request = MagicMock()
    r.raise_for_status = MagicMock()
    if status_code >= 400:
        r.raise_for_status.side_effect = httpx.HTTPStatusError(f"{status_code}", request=r.request, response=r)
    if json_body is not None:
        r.json = MagicMock(return_value=json_body)
    if content is not None:
        r.content = content
    return r


def _client_yielding(side_effect):
    """A fake AsyncClient whose .request() walks a side-effect sequence across attempts."""
    client = MagicMock()
    client.request = AsyncMock(side_effect=side_effect)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


@pytest.mark.asyncio
async def test_request_retries_transient_timeout_then_succeeds():
    service = PixelLabService(api_key="stub")
    client = _client_yielding([httpx.ConnectTimeout("boom"), _resp(200, json_body={"id": "char-1"})])
    with (
        patch("core.services.pixellab_service.httpx.AsyncClient", return_value=client),
        patch("core.services.pixellab_service.asyncio.sleep", AsyncMock()) as sleep,
    ):
        result = await service.get_character("char-1")
    assert result == {"id": "char-1"}
    assert client.request.await_count == 2
    assert sleep.await_count == 1


@pytest.mark.asyncio
async def test_request_retries_on_429_then_succeeds():
    service = PixelLabService(api_key="stub")
    client = _client_yielding([_resp(429), _resp(200, json_body={"id": "char-2"})])
    with (
        patch("core.services.pixellab_service.httpx.AsyncClient", return_value=client),
        patch("core.services.pixellab_service.asyncio.sleep", AsyncMock()) as sleep,
    ):
        result = await service.get_character("char-2")
    assert result == {"id": "char-2"}
    assert client.request.await_count == 2
    assert sleep.await_count == 1


@pytest.mark.asyncio
async def test_request_does_not_retry_4xx():
    service = PixelLabService(api_key="stub")
    client = _client_yielding([_resp(404)])
    with (
        patch("core.services.pixellab_service.httpx.AsyncClient", return_value=client),
        patch("core.services.pixellab_service.asyncio.sleep", AsyncMock()) as sleep,
    ):
        with pytest.raises(httpx.HTTPStatusError):
            await service.get_character("missing")
    assert client.request.await_count == 1  # permanent failure: no retry
    assert sleep.await_count == 0


@pytest.mark.asyncio
async def test_request_raises_after_exhausting_attempts():
    service = PixelLabService(api_key="stub")
    client = _client_yielding([httpx.ConnectTimeout("boom")] * _RETRY_MAX_ATTEMPTS)
    with (
        patch("core.services.pixellab_service.httpx.AsyncClient", return_value=client),
        patch("core.services.pixellab_service.asyncio.sleep", AsyncMock()) as sleep,
    ):
        with pytest.raises(httpx.TimeoutException):
            await service.get_character("char-3")
    assert client.request.await_count == _RETRY_MAX_ATTEMPTS
    assert sleep.await_count == _RETRY_MAX_ATTEMPTS - 1


@pytest.mark.asyncio
async def test_download_zip_423_not_retried_returns_none():
    service = PixelLabService(api_key="stub")
    client = _client_yielding([_resp(423)])
    with (
        patch("core.services.pixellab_service.httpx.AsyncClient", return_value=client),
        patch("core.services.pixellab_service.asyncio.sleep", AsyncMock()) as sleep,
    ):
        result = await service.download_character_zip("char-4")
    assert result is None  # caller-handled status, not a transient error
    assert client.request.await_count == 1
    assert sleep.await_count == 0
