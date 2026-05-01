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

    async def add_contact(self, email: str) -> None:
        """Add a contact to the configured audience.

        Idempotent: a 422 response with 'already exists' is treated as success
        (Resend's signal for duplicate). Other non-2xx responses raise.
        """
        url = f"{RESEND_API_BASE}/audiences/{self.audience_id}/contacts"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url,
                json={"email": email, "unsubscribed": False},
                headers={"Authorization": f"Bearer {self.api_key}"},
            )

        if 200 <= response.status_code < 300:
            return

        # Treat duplicate-email 422 as idempotent success.
        if response.status_code == 422:
            try:
                body_json = response.json()
                if "already exists" in body_json.get("message", "").lower():
                    logger.info("Resend contact already exists for %s — treating as success", email)
                    return
            except Exception:
                pass

        raise ResendError(response.status_code, response.text)
