"""HMAC-signed town tokens for GooseTown agent WebSocket auth.

Format: <payload_b64url>.<signature_b64url>
Payload: JSON {"uid": user_id, "iid": instance_id, "iat": issued_at, "exp": expires_at}

The shared secret (TOWN_TOKEN_SECRET) is used by both:
- Backend: to generate tokens during opt-in / instance creation
- Lambda authorizer: to verify tokens on WebSocket $connect

New tokens carry a 30-day ``exp`` claim. Legacy tokens without ``exp`` still
verify successfully (logged at WARNING) so we can roll out gradually; the
fail-closed flip will land once the in-the-wild population has rotated.
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

    Returns the payload dict (``uid``, ``iid``, plus optional ``iat``/``exp``)
    if valid, ``None`` otherwise.

    Tokens with an ``exp`` in the past are rejected. Tokens missing ``exp``
    entirely are accepted for backward compatibility but logged at WARNING so
    we can monitor the legacy population shrinking before flipping to
    fail-closed.
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

    exp = payload.get("exp")
    if exp is None:
        logger.warning(
            "legacy token used, no exp (uid=%s iid=%s)",
            payload.get("uid"),
            payload.get("iid"),
        )
    else:
        try:
            if int(exp) < int(time.time()):
                return None
        except (TypeError, ValueError):
            # Malformed exp claim — treat as invalid.
            return None

    # User-level token revocation. Stateless: every verify call loads the
    # user record and rejects tokens whose iat is before the revoke timestamp.
    # Returning users mint a fresh token via POST /town/instance.
    iat = payload.get("iat")
    if iat is not None:
        try:
            from core.services.user_service import UserService  # local to avoid circular import

            iat_ms = int(iat) * 1000
            if UserService().is_token_revoked(payload["uid"], iat_ms):
                logger.warning(
                    "token rejected: issued before revocation (uid=%s iat=%s)",
                    payload.get("uid"),
                    iat,
                )
                return None
        except Exception as exc:
            # Don't fail-closed on transient DynamoDB errors. The user's
            # session continues as if not revoked, which is the same
            # behavior we had before this change. Logged for ops visibility.
            logger.warning(
                "token revocation check failed (uid=%s): %s — passing through",
                payload.get("uid"),
                exc,
            )

    return payload
