"""HTTP sink: worker-side sprite events -> backend internal endpoint."""

from __future__ import annotations

import asyncio
import logging

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

_RETRIES = 3


class HttpSpriteEventSink:
    """SpriteEventSink impl that POSTs /town/internal/sprite-event on the
    backend. Best-effort: never raises — a missed progress event only stales
    a status line, and a missed sprite_updated self-heals on the daemon's
    next reconnect / the frontend's 2s /apartment poll."""

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._url = f"{settings.BACKEND_INTERNAL_URL}/api/v1/town/internal/sprite-event"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, payload: dict) -> None:
        headers = {}
        if settings.WS_LAMBDA_SECRET:
            headers["x-ws-lambda-secret"] = settings.WS_LAMBDA_SECRET
        for attempt in range(_RETRIES):
            try:
                resp = await self._client.post(self._url, json=payload, headers=headers)
                if resp.status_code < 500:
                    return
                logger.warning(
                    "sprite-event POST -> %d (attempt %d/%d)",
                    resp.status_code,
                    attempt + 1,
                    _RETRIES,
                )
            except httpx.HTTPError as exc:
                logger.warning(
                    "sprite-event POST failed: %s (attempt %d/%d)",
                    exc,
                    attempt + 1,
                    _RETRIES,
                )
            if attempt + 1 < _RETRIES:
                await asyncio.sleep(1.5 * (attempt + 1))
        logger.error(
            "sprite-event POST gave up: type=%s agent=%s",
            payload.get("type"),
            str(payload.get("agent_id"))[:8],
        )

    async def progress(self, agent_id: str, status: str) -> None:
        await self._post({"agent_id": agent_id, "type": "progress", "status": status})

    async def sprite_updated(self, agent_id: str, kind: str) -> None:
        await self._post({"agent_id": agent_id, "type": kind})
