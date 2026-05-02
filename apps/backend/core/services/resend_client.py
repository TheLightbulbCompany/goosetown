"""Thin client for the Resend Audiences API.

Used by the /landing/signup endpoint to add email contacts to a Resend
audience (the launch list). No SDK — direct httpx POST per the existing
pixellab_service.py pattern.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

RESEND_API_BASE = "https://api.resend.com"


class ResendError(Exception):
    """Raised on non-2xx response from Resend that we don't treat as success."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Resend API {status}: {body[:200]}")
        self.status = status


class ResendClient:
    def __init__(self, api_key: str, audience_id: str) -> None:
        self.api_key = api_key
        self.audience_id = audience_id

    async def add_contact(self, email: str) -> str:
        """Add a contact to the configured audience.

        Returns:
            ``"created"`` for new subscriptions, ``"already_subscribed"`` for
            idempotent retries (Resend's 422 + ``already exists``).

        Raises:
            ``ResendError`` for any other non-2xx response.
        """
        url = f"{RESEND_API_BASE}/audiences/{self.audience_id}/contacts"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url,
                json={"email": email, "unsubscribed": False},
                headers={"Authorization": f"Bearer {self.api_key}"},
            )

        if 200 <= response.status_code < 300:
            return "created"

        # Treat duplicate-email 422 as idempotent — surface the distinction
        # so the UI can tell the user "you're already on the list" instead
        # of a generic "got it".
        if response.status_code == 422:
            try:
                body_json = response.json()
                if "already exists" in body_json.get("message", "").lower():
                    logger.info("Resend contact already exists for %s", email)
                    return "already_subscribed"
            except Exception:  # nosec B110: malformed body falls through to ResendError below; not a silent swallow
                pass

        raise ResendError(response.status_code, response.text)
