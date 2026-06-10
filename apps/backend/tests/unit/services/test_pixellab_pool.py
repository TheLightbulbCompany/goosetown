"""The PixelLab service reuses a single pooled httpx client across calls."""

import httpx
import pytest

from core.services import pixellab_service as pxl_mod


def test_get_client_returns_same_instance():
    c1 = pxl_mod._get_pixellab_client()
    c2 = pxl_mod._get_pixellab_client()
    assert c1 is c2
    assert isinstance(c1, httpx.AsyncClient)


@pytest.mark.asyncio
async def test_close_client_lets_a_fresh_one_be_made():
    c1 = pxl_mod._get_pixellab_client()
    await pxl_mod.close_pixellab_client()
    c2 = pxl_mod._get_pixellab_client()
    assert c1 is not c2
    await pxl_mod.close_pixellab_client()


def test_animation_semaphore_bound_matches_setting():
    from core.config import settings
    from core.services.pixellab_service import _get_animation_semaphore

    sem = _get_animation_semaphore()
    assert _get_animation_semaphore() is sem  # singleton
    assert sem._value == settings.PIXELLAB_MAX_CONCURRENT_ANIMATIONS
