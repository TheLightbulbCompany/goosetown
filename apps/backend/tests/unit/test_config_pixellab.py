"""PixelLab pool + concurrency settings exist with sane defaults."""

from core.config import settings


def test_pixellab_concurrency_defaults():
    assert settings.PIXELLAB_MAX_CONCURRENT_ANIMATIONS == 2
    assert settings.PIXELLAB_POOL_MAX_CONNECTIONS == 10
    assert settings.PIXELLAB_POOL_MAX_KEEPALIVE == 5
