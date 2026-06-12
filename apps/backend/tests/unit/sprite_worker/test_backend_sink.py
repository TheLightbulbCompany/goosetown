"""Tests for sprite_worker.backend_sink.HttpSpriteEventSink."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sink(transport: httpx.AsyncBaseTransport, secret: str = "test-secret"):
    """Build an HttpSpriteEventSink with a mock transport and injected secret."""
    from sprite_worker.backend_sink import HttpSpriteEventSink

    client = httpx.AsyncClient(transport=transport)
    with patch("sprite_worker.backend_sink.settings") as mock_settings:
        mock_settings.BACKEND_INTERNAL_URL = "http://backend:8000"
        mock_settings.WS_LAMBDA_SECRET = secret
        sink = HttpSpriteEventSink(client=client)
    return sink


class _MockTransport(httpx.AsyncBaseTransport):
    """Minimal mock transport that returns a preset response."""

    def __init__(self, status_code: int = 200, side_effect=None):
        self.requests: list[httpx.Request] = []
        self._status_code = status_code
        self._side_effect = side_effect

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._side_effect is not None:
            raise self._side_effect
        return httpx.Response(self._status_code, content=b"ok")


# ---------------------------------------------------------------------------
# Correct URL, payload, and secret header
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_progress_posts_correct_payload():
    transport = _MockTransport(status_code=200)
    sink = _make_sink(transport, secret="my-secret")

    with patch("sprite_worker.backend_sink.settings") as mock_settings:
        mock_settings.BACKEND_INTERNAL_URL = "http://backend:8000"
        mock_settings.WS_LAMBDA_SECRET = "my-secret"
        # Re-create so the URL is baked in with the patched settings
        from sprite_worker.backend_sink import HttpSpriteEventSink

        client = httpx.AsyncClient(transport=transport)
        sink = HttpSpriteEventSink(client=client)
        await sink.progress("agent-abc-123", "Creating your character...")

    assert len(transport.requests) == 1
    req = transport.requests[0]
    assert req.method == "POST"
    assert "/api/v1/town/internal/sprite-event" in str(req.url)
    import json

    body = json.loads(req.content)
    assert body["agent_id"] == "agent-abc-123"
    assert body["type"] == "progress"
    assert body["status"] == "Creating your character..."
    assert req.headers.get("x-ws-lambda-secret") == "my-secret"


@pytest.mark.asyncio
async def test_sprite_updated_posts_correct_payload():
    transport = _MockTransport(status_code=200)

    with patch("sprite_worker.backend_sink.settings") as mock_settings:
        mock_settings.BACKEND_INTERNAL_URL = "http://backend:8000"
        mock_settings.WS_LAMBDA_SECRET = "sec"
        from sprite_worker.backend_sink import HttpSpriteEventSink

        client = httpx.AsyncClient(transport=transport)
        sink = HttpSpriteEventSink(client=client)
        await sink.sprite_updated("agent-def-456", "static_ready")

    assert len(transport.requests) == 1
    import json

    body = json.loads(transport.requests[0].content)
    assert body["agent_id"] == "agent-def-456"
    assert body["type"] == "static_ready"
    assert "status" not in body


# ---------------------------------------------------------------------------
# 500 -> retried then gives up without raising
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_500_retries_then_gives_up_without_raising():
    transport = _MockTransport(status_code=500)

    with patch("sprite_worker.backend_sink.settings") as mock_settings:
        mock_settings.BACKEND_INTERNAL_URL = "http://backend:8000"
        mock_settings.WS_LAMBDA_SECRET = ""
        with patch("sprite_worker.backend_sink.asyncio.sleep", new_callable=AsyncMock):
            from sprite_worker.backend_sink import _RETRIES, HttpSpriteEventSink

            client = httpx.AsyncClient(transport=transport)
            sink = HttpSpriteEventSink(client=client)
            # Must not raise
            await sink.progress("agent-001", "test")

    assert len(transport.requests) == _RETRIES


# ---------------------------------------------------------------------------
# Transport error -> retried, never raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transport_error_retries_never_raises():
    transport = _MockTransport(side_effect=httpx.ConnectError("connection refused"))

    with patch("sprite_worker.backend_sink.settings") as mock_settings:
        mock_settings.BACKEND_INTERNAL_URL = "http://backend:8000"
        mock_settings.WS_LAMBDA_SECRET = ""
        with patch("sprite_worker.backend_sink.asyncio.sleep", new_callable=AsyncMock):
            from sprite_worker.backend_sink import _RETRIES, HttpSpriteEventSink

            client = httpx.AsyncClient(transport=transport)
            sink = HttpSpriteEventSink(client=client)
            # Must not raise
            await sink.progress("agent-002", "test")

    assert len(transport.requests) == _RETRIES


# ---------------------------------------------------------------------------
# 4xx -> returns after first attempt (no retry) but logs ERROR loudly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_4xx_returns_after_first_attempt_no_retry_and_logs_error(caplog):
    transport = _MockTransport(status_code=403)

    with patch("sprite_worker.backend_sink.settings") as mock_settings:
        mock_settings.BACKEND_INTERNAL_URL = "http://backend:8000"
        mock_settings.WS_LAMBDA_SECRET = ""
        with patch("sprite_worker.backend_sink.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            from sprite_worker.backend_sink import HttpSpriteEventSink

            client = httpx.AsyncClient(transport=transport)
            sink = HttpSpriteEventSink(client=client)
            with caplog.at_level("ERROR", logger="sprite_worker.backend_sink"):
                await sink.progress("agent-003", "test")

    # Only one request, no retries for 4xx
    assert len(transport.requests) == 1
    mock_sleep.assert_not_awaited()
    # ...but a rejection is loud: silent 4xx would invisibly drop all events.
    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(error_records) == 1
    assert "rejected with 403" in error_records[0].getMessage()
    assert "x-ws-lambda-secret" in error_records[0].getMessage()


# ---------------------------------------------------------------------------
# No secret header when WS_LAMBDA_SECRET is empty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_secret_header_when_empty():
    transport = _MockTransport(status_code=200)

    with patch("sprite_worker.backend_sink.settings") as mock_settings:
        mock_settings.BACKEND_INTERNAL_URL = "http://backend:8000"
        mock_settings.WS_LAMBDA_SECRET = ""
        from sprite_worker.backend_sink import HttpSpriteEventSink

        client = httpx.AsyncClient(transport=transport)
        sink = HttpSpriteEventSink(client=client)
        await sink.progress("agent-004", "test")

    req = transport.requests[0]
    assert "x-ws-lambda-secret" not in req.headers
