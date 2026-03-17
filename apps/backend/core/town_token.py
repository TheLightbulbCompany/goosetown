"""HMAC-signed town tokens for GooseTown agent WebSocket auth.

Format: <payload_b64url>.<signature_b64url>
Payload: JSON {"uid": user_id, "iid": instance_id}

The shared secret (TOWN_TOKEN_SECRET) is used by both:
- Backend: to generate tokens during opt-in / instance creation
- Lambda authorizer: to verify tokens on WebSocket $connect
"""

import base64
import hashlib
import hmac
import json
import os

TOWN_TOKEN_SECRET = os.environ.get("TOWN_TOKEN_SECRET", "dev-town-secret-change-in-prod")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


def sign_town_token(user_id: str, instance_id: str) -> str:
    """Create an HMAC-signed town token."""
    payload = json.dumps({"uid": user_id, "iid": instance_id}, separators=(",", ":"))
    payload_b64 = _b64url_encode(payload.encode())
    sig = hmac.new(TOWN_TOKEN_SECRET.encode(), payload_b64.encode(), hashlib.sha256).digest()
    sig_b64 = _b64url_encode(sig)
    return f"{payload_b64}.{sig_b64}"


def verify_town_token(token: str) -> dict | None:
    """Verify an HMAC-signed town token.

    Returns the payload dict {"uid": ..., "iid": ...} if valid, None otherwise.
    """
    parts = token.split(".")
    if len(parts) != 2:
        return None

    payload_b64, sig_b64 = parts
    try:
        expected_sig = hmac.new(TOWN_TOKEN_SECRET.encode(), payload_b64.encode(), hashlib.sha256).digest()
        actual_sig = _b64url_decode(sig_b64)
    except Exception:
        return None

    if not hmac.compare_digest(expected_sig, actual_sig):
        return None

    try:
        payload = json.loads(_b64url_decode(payload_b64))
        if "uid" not in payload or "iid" not in payload:
            return None
        return payload
    except Exception:
        return None
