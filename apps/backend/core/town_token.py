"""HMAC-signed town tokens for GooseTown agent WebSocket auth.

Format: <payload_b64url>.<signature_b64url>
Payload: JSON {"uid": user_id, "iid": instance_id, "iat": issued_at, "exp": expires_at}

The shared secret (TOWN_TOKEN_SECRET) is used by both:
- Backend: to generate tokens during opt-in / instance creation
- Lambda authorizer: to verify tokens on WebSocket $connect

Tokens must have integer issue and expiry times and pass revocation checks.
"""

import base64
import hashlib
import hmac
import json
import logging
import time

from core.config import settings

logger = logging.getLogger(__name__)

# 30 days in seconds. New tokens expire this far past their ``iat``.
TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60


def _get_town_token_secret() -> str:
    """Return the town token secret, raising if unset in non-dev environments."""
    secret = settings.TOWN_TOKEN_SECRET
    if not secret and settings.ENVIRONMENT != "dev":
        raise ValueError("TOWN_TOKEN_SECRET must be set in non-dev environments")
    if not secret:
        # Only allow empty/fallback in dev
        return "dev-town-secret-change-in-prod"
    return secret


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


def sign_town_token(
    user_id: str,
    instance_id: str,
    *,
    issued_at: int | None = None,
    ttl_seconds: int = TOKEN_TTL_SECONDS,
) -> str:
    """Create an HMAC-signed town token with a 30-day expiry.

    ``issued_at`` and ``ttl_seconds`` are exposed for testability — callers in
    production should leave them as defaults.
    """
    iat = int(issued_at if issued_at is not None else time.time())
    exp = iat + ttl_seconds
    payload = json.dumps(
        {"uid": user_id, "iid": instance_id, "iat": iat, "exp": exp},
        separators=(",", ":"),
    )
    payload_b64 = _b64url_encode(payload.encode())
    secret = _get_town_token_secret()
    sig = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest()
    sig_b64 = _b64url_encode(sig)
    return f"{payload_b64}.{sig_b64}"


def verify_town_token(token: str) -> dict | None:
    """Verify an HMAC-signed town token.

    Returns a validated payload or None. Revocation-store failures deny access.
    """
    parts = token.split(".")
    if len(parts) != 2:
        return None

    payload_b64, sig_b64 = parts
    try:
        secret = _get_town_token_secret()
        expected_sig = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest()
        actual_sig = _b64url_decode(sig_b64)
    except Exception:
        return None

    if not hmac.compare_digest(expected_sig, actual_sig):
        return None

    try:
        payload = json.loads(_b64url_decode(payload_b64))
        if "uid" not in payload or "iid" not in payload:
            return None
    except Exception:
        return None

    now = int(time.time())
    if not isinstance(payload, dict) or any(
        not isinstance(payload.get(k), str) or not payload[k] for k in ("uid", "iid")
    ):
        return None
    iat, exp = payload.get("iat"), payload.get("exp")
    if type(iat) is not int or type(exp) is not int or iat > now or exp <= now or exp <= iat:
        return None
    # Public anonymous viewer tokens have no owner to revoke.
    if payload["uid"] == "anonymous" and payload["iid"] == "anonymous-viewer":
        return payload
    try:
        from core.services.user_service import UserService

        if UserService().is_token_revoked(payload["uid"], iat * 1000):
            return None
    except Exception:
        logger.warning("Town token revocation check unavailable; denying request")
        return None
    return payload
