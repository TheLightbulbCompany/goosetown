"""Regression guard for #429 — no public API schema or docs in prod.

`/openapi.json` served the complete route inventory in prod (~36 KB): every
path, method, and declared security scheme. That is a recon map, and because
the declared schemes don't always match runtime behaviour it misleads
defenders reading it too.

`openapi_url=None` makes FastAPI skip registering the route entirely, so these
must **404** (not 403) in prod, while staying available in dev.

The app object is built at import time from ``settings.ENVIRONMENT``, so these
tests reload ``main`` under a patched environment and restore it afterwards.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

DOC_ROUTES = ["/openapi.json", "/docs", "/redoc"]


def _reload_main_with_env(env: str):
    """Re-import main with ENVIRONMENT=env and return the fresh module."""
    import main as main_module
    from core.config import settings

    settings.ENVIRONMENT = env
    return importlib.reload(main_module)


@pytest.fixture
def restore_main():
    """Restore the module-level app other tests import, whatever happens."""
    from core.config import settings

    original = settings.ENVIRONMENT
    yield
    settings.ENVIRONMENT = original
    import main as main_module

    importlib.reload(main_module)


@pytest.mark.parametrize("route", DOC_ROUTES)
def test_docs_routes_404_in_prod(restore_main, route):
    main_module = _reload_main_with_env("prod")
    client = TestClient(main_module.app)

    resp = client.get(route)
    assert resp.status_code == 404, f"{route} returned {resp.status_code} in prod"


@pytest.mark.parametrize("route", DOC_ROUTES)
def test_docs_routes_available_in_dev(restore_main, route):
    main_module = _reload_main_with_env("dev")
    client = TestClient(main_module.app)

    resp = client.get(route)
    assert resp.status_code == 200, f"{route} returned {resp.status_code} in dev"


def test_prod_schema_does_not_leak_route_inventory(restore_main):
    """The point of the fix: route paths must not be enumerable in prod."""
    main_module = _reload_main_with_env("prod")
    client = TestClient(main_module.app)

    body = client.get("/openapi.json").text
    assert "/town/exchange/upload-url" not in body
    assert "/town/ws-message" not in body
    assert "/town/admin/" not in body


def test_health_still_served_in_prod(restore_main):
    """Disabling docs must not disturb real routes (ALB health check depends on this)."""
    main_module = _reload_main_with_env("prod")
    client = TestClient(main_module.app)

    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy"}
