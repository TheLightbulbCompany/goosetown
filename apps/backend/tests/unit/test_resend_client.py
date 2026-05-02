"""TDD: ResendClient.add_contact wraps the Resend Audiences API."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from core.services.resend_client import ResendClient, ResendError


@pytest.mark.asyncio
async def test_add_contact_posts_to_resend_audiences_endpoint():
    client = ResendClient(api_key="test-key", audience_id="aud-123")
    mock_response = httpx.Response(200, json={"id": "contact-abc"})

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_response)) as mock_post:
        await client.add_contact("user@example.com")

    mock_post.assert_called_once()
    call_args = mock_post.call_args
    assert "audiences/aud-123/contacts" in call_args.args[0]
    assert call_args.kwargs["headers"]["Authorization"] == "Bearer test-key"
    assert call_args.kwargs["json"]["email"] == "user@example.com"
    assert call_args.kwargs["json"]["unsubscribed"] is False


@pytest.mark.asyncio
async def test_add_contact_raises_on_4xx():
    client = ResendClient(api_key="test-key", audience_id="aud-123")
    mock_response = httpx.Response(400, json={"message": "Invalid email"})

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(ResendError) as exc:
            await client.add_contact("bad-email")
    assert exc.value.status == 400


@pytest.mark.asyncio
async def test_add_contact_raises_on_5xx():
    client = ResendClient(api_key="test-key", audience_id="aud-123")
    mock_response = httpx.Response(503, text="Service Unavailable")

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(ResendError) as exc:
            await client.add_contact("user@example.com")
    assert exc.value.status == 503


@pytest.mark.asyncio
async def test_add_contact_treats_already_exists_as_success():
    """Resend returns a specific 'contact already exists' shape; treat as success.

    This makes the endpoint idempotent — re-submitting the same email doesn't
    surface as an error to the user.
    """
    client = ResendClient(api_key="test-key", audience_id="aud-123")
    mock_response = httpx.Response(422, json={"name": "validation_error", "message": "Contact already exists."})

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_response)):
        # Should NOT raise
        await client.add_contact("dup@example.com")
