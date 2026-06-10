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
