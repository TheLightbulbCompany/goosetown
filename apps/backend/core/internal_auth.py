"""Shared-secret gates for machine-to-machine seams.

These routes are reachable on the public ALB but are only ever *meant* to be
called by another piece of our own infrastructure (the API Gateway WebSocket
Lambdas, the sprite worker). A shared secret in a header is the authentication
boundary; there is no user identity involved.

Two properties matter here, and both were missing on the ws-* seam (#427):

1. **The check must be a FastAPI dependency, not an inline check inside the
   handler.** Dependencies resolve *before* request-body validation, so an
   unauthenticated caller gets 403. With an inline check, Pydantic rejects a
   malformed body with 422 first and the auth check never runs — which is
   exactly why external probing read these routes as "no auth gate".

2. **It must fail closed.** The previous ws-message check compared
   ``request.headers.get(...) or ""`` against an unset ``WS_LAMBDA_SECRET``,
   i.e. ``"" == ""`` → authenticated. An unset secret outside dev is a
   deployment fault, not permission to accept anonymous calls.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Callable

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)


def require_shared_secret(header_name: str, setting_name: str) -> Callable[[Request], None]:
    """Build a dependency that gates a route on a shared-secret header.

    ``setting_name`` is read from ``core.config.settings`` at *request* time,
    not at import time, so tests (and a secret rotation) can monkeypatch it.

    Fails closed: if the secret is unconfigured, the route is refused outside
    dev rather than silently accepting everyone. Dev with no secret set stays
    open for local ergonomics (no Secrets Manager on a laptop).
    """

    def _dependency(request: Request) -> None:
        from core.config import settings

        secret = getattr(settings, setting_name, "") or ""
        provided = request.headers.get(header_name, "")

        if not secret:
            if settings.ENVIRONMENT != "dev":
                logger.error(
                    "%s is not configured in env=%s — refusing %s. This is a "
                    "deployment fault: the seam is unauthenticated.",
                    setting_name,
                    settings.ENVIRONMENT,
                    request.url.path,
                )
                raise HTTPException(status_code=403, detail="forbidden")
            return  # dev, no secret configured — local-dev ergonomics

        # compare_digest: constant-time, and never compares against "" by
        # accident the way the old `provided != settings.X` check did.
        if not provided or not hmac.compare_digest(provided, secret):
            logger.warning("Rejected %s: missing/invalid %s", request.url.path, header_name)
            raise HTTPException(status_code=403, detail="forbidden")

    return _dependency


#: Gate for the API Gateway WebSocket Lambdas → backend seam
#: (``/town/ws-message``, ``/town/ws-disconnect``).
require_ws_lambda_secret = require_shared_secret("x-ws-lambda-secret", "WS_LAMBDA_SECRET")

#: Gate for the sprite worker → backend seam (``/town/internal/sprite-event``).
require_sprite_event_secret = require_shared_secret("x-sprite-event-secret", "SPRITE_EVENT_SECRET")
