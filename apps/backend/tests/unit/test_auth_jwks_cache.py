"""Unit tests for JWKS caching in core/auth.py.

Specifically covers the stale-cache grace window — on Clerk outage we
serve the last-known JWKS for up to ``JWKS_STALE_GRACE`` past its normal
TTL expiry, then fail loudly rather than silently trusting ancient keys
forever.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest


@pytest.fixture(autouse=True)
def _reset_jwks_cache():
    """Each test starts with a clean cache."""
    from core import auth

    auth._jwks_cache.update({"data": None, "expires_at": None})
    yield
    auth._jwks_cache.update({"data": None, "expires_at": None})


@pytest.mark.asyncio
async def test_fetches_and_caches_on_first_call():
    from core import auth

    fake_jwks = {"keys": [{"kid": "k1"}]}

    mock_client = AsyncMock()
    mock_response = AsyncMock()
    mock_response.json = lambda: fake_jwks
    mock_response.raise_for_status = lambda: None
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("core.auth.httpx.AsyncClient", return_value=mock_client):
        result = await auth._get_cached_jwks("https://issuer/.well-known/jwks.json")
    assert result == fake_jwks
    assert auth._jwks_cache["data"] == fake_jwks


@pytest.mark.asyncio
async def test_returns_cached_within_ttl_without_refetching():
    from core import auth

    fake_jwks = {"keys": [{"kid": "cached"}]}
    auth._jwks_cache["data"] = fake_jwks
    auth._jwks_cache["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)

    with patch("core.auth.httpx.AsyncClient") as mock_client:
        result = await auth._get_cached_jwks("https://issuer/.well-known/jwks.json")
    assert result == fake_jwks
    # No network call — cache hit.
    mock_client.assert_not_called()


@pytest.mark.asyncio
async def test_serves_stale_cache_within_grace_on_fetch_failure():
    """When Clerk is down and cache expired recently, we keep serving the
    stale keys so users don't immediately see 5xx from a flaky edge."""
    from core import auth

    fake_jwks = {"keys": [{"kid": "stale"}]}
    # Expired 2 minutes ago — well within the 5-minute grace.
    auth._jwks_cache["data"] = fake_jwks
    auth._jwks_cache["expires_at"] = datetime.now(timezone.utc) - timedelta(minutes=2)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("clerk unreachable"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("core.auth.httpx.AsyncClient", return_value=mock_client):
        result = await auth._get_cached_jwks("https://issuer/.well-known/jwks.json")
    assert result == fake_jwks


@pytest.mark.asyncio
async def test_raises_when_stale_cache_exceeds_grace():
    """Once the stale cache is older than the grace window, we should fail
    rather than indefinitely trust ancient keys."""
    from core import auth

    fake_jwks = {"keys": [{"kid": "ancient"}]}
    # Expired 30 minutes ago — well past the 5-minute grace.
    auth._jwks_cache["data"] = fake_jwks
    auth._jwks_cache["expires_at"] = datetime.now(timezone.utc) - timedelta(minutes=30)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("clerk still down"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("core.auth.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(httpx.HTTPError):
            await auth._get_cached_jwks("https://issuer/.well-known/jwks.json")


@pytest.mark.asyncio
async def test_raises_when_no_cache_and_fetch_fails():
    """Cold start + Clerk outage → propagate the error."""
    from core import auth

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("clerk"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("core.auth.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(httpx.HTTPError):
            await auth._get_cached_jwks("https://issuer/.well-known/jwks.json")


@pytest.mark.asyncio
async def test_concurrent_cache_misses_coalesce_to_single_fetch():
    """10 tasks all hit the cache-miss path at once; the refresh lock
    should ensure only ONE hits Clerk. The rest wait, see the newly-
    populated cache under the lock, and return the shared result.

    Regression test for the JWKS-refresh-has-no-lock concurrency bug
    (fixed by PR adding ``_jwks_refresh_lock``).
    """
    import asyncio

    from core import auth

    fake_jwks = {"keys": [{"kid": "shared"}]}
    fetch_calls = 0
    fetch_in_progress = asyncio.Event()
    release_fetch = asyncio.Event()

    async def _slow_get(url, timeout=None):
        nonlocal fetch_calls
        fetch_calls += 1
        fetch_in_progress.set()
        # Block until all concurrent callers are queued on the lock,
        # then release this fetch to populate the cache.
        await release_fetch.wait()
        resp = AsyncMock()
        resp.json = lambda: fake_jwks
        resp.raise_for_status = lambda: None
        return resp

    mock_client = AsyncMock()
    mock_client.get = _slow_get
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("core.auth.httpx.AsyncClient", return_value=mock_client):
        # Fire 10 concurrent tasks — all start on cold cache.
        tasks = [asyncio.create_task(auth._get_cached_jwks("https://issuer/.well-known/jwks.json")) for _ in range(10)]
        # Wait for the first task to reach the HTTP call (it has the lock).
        await fetch_in_progress.wait()
        # Give the remaining 9 a chance to queue on the lock.
        await asyncio.sleep(0)
        # Now release the fetch — it populates the cache, drops the lock,
        # and the queued tasks see the fresh cache instead of re-fetching.
        release_fetch.set()
        results = await asyncio.gather(*tasks)

    assert fetch_calls == 1, f"expected 1 fetch, got {fetch_calls}"
    assert all(r == fake_jwks for r in results)
