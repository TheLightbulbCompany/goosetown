"""Unit tests for ``core.town_token`` — sign + verify with expiry.

New tokens carry a 30-day ``exp`` claim. Legacy tokens without ``exp`` still
verify (logged at WARNING) so we can roll out gradually before flipping to
fail-closed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from unittest.mock import patch

import pytest

from core.town_token import (
    TOKEN_TTL_SECONDS,
    _b64url_decode,
    _b64url_encode,
    _get_town_token_secret,
    sign_town_token,
    verify_town_token,
)


@pytest.fixture(autouse=True)
def revocation_store_available():
    with patch("core.services.user_service.UserService.is_token_revoked", return_value=False):
        yield


def _decode_payload(token: str) -> dict:
    payload_b64, _ = token.split(".")
    return json.loads(_b64url_decode(payload_b64))


def _make_legacy_token(uid: str, iid: str) -> str:
    """Sign a token in the pre-exp format (no ``iat``/``exp``)."""
    payload = json.dumps({"uid": uid, "iid": iid}, separators=(",", ":"))
    payload_b64 = _b64url_encode(payload.encode())
    secret = _get_town_token_secret()
    sig = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64url_encode(sig)}"


# ---------------------------------------------------------------------------
# sign_town_token
# ---------------------------------------------------------------------------


def test_sign_includes_iat_and_exp_30_days_in_future():
    before = int(time.time())
    token = sign_town_token("user-1", "instance-1")
    after = int(time.time())

    payload = _decode_payload(token)
    assert payload["uid"] == "user-1"
    assert payload["iid"] == "instance-1"
    assert before <= payload["iat"] <= after
    # exp should be ~30 days from iat (allow fudge for execution time).
    assert payload["exp"] - payload["iat"] == TOKEN_TTL_SECONDS
    assert payload["exp"] >= before + TOKEN_TTL_SECONDS
    assert payload["exp"] <= after + TOKEN_TTL_SECONDS + 1


def test_sign_accepts_explicit_issued_at_for_testability():
    token = sign_town_token("u", "i", issued_at=1_000_000)
    payload = _decode_payload(token)
    assert payload["iat"] == 1_000_000
    assert payload["exp"] == 1_000_000 + TOKEN_TTL_SECONDS


# ---------------------------------------------------------------------------
# verify_town_token — happy + expiry paths
# ---------------------------------------------------------------------------


def test_verify_fresh_token_succeeds():
    token = sign_town_token("user-1", "instance-1")
    payload = verify_town_token(token)

    assert payload is not None
    assert payload["uid"] == "user-1"
    assert payload["iid"] == "instance-1"
    assert "exp" in payload


def test_verify_expired_token_returns_none():
    # Issue a token in the past such that exp is also in the past.
    expired_iat = int(time.time()) - TOKEN_TTL_SECONDS - 60
    token = sign_town_token("u", "i", issued_at=expired_iat)

    assert verify_town_token(token) is None


def test_verify_token_one_second_before_expiry_still_succeeds():
    # exp = now + 1s, should still verify.
    iat = int(time.time()) - TOKEN_TTL_SECONDS + 5
    token = sign_town_token("u", "i", issued_at=iat)

    payload = verify_town_token(token)
    assert payload is not None


def test_verify_token_with_malformed_exp_returns_none():
    # Hand-craft a token with a non-numeric ``exp``.
    secret = _get_town_token_secret()
    payload = json.dumps(
        {"uid": "u", "iid": "i", "iat": int(time.time()), "exp": "not-a-number"},
        separators=(",", ":"),
    )
    payload_b64 = _b64url_encode(payload.encode())
    sig = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest()
    token = f"{payload_b64}.{_b64url_encode(sig)}"

    assert verify_town_token(token) is None


# ---------------------------------------------------------------------------
# verify_town_token — legacy (no exp) path
# ---------------------------------------------------------------------------


def test_verify_legacy_token_without_exp_is_rejected():
    assert verify_town_token(_make_legacy_token("user-legacy", "instance-legacy")) is None


# ---------------------------------------------------------------------------
# Tamper resistance — make sure expiry checks didn't open a verification gap.
# ---------------------------------------------------------------------------


def test_verify_rejects_tampered_signature():
    token = sign_town_token("u", "i")
    payload_b64, sig_b64 = token.split(".")
    # Flip a byte in the signature.
    sig_bytes = bytearray(_b64url_decode(sig_b64))
    sig_bytes[0] ^= 0x01
    bad_sig = base64.urlsafe_b64encode(bytes(sig_bytes)).rstrip(b"=").decode()
    tampered = f"{payload_b64}.{bad_sig}"

    assert verify_town_token(tampered) is None


def test_verify_rejects_tampered_exp_payload():
    # Forge a payload with a far-future exp but keep the original signature.
    token = sign_town_token("u", "i", issued_at=int(time.time()) - TOKEN_TTL_SECONDS - 100)
    _, sig_b64 = token.split(".")
    # Brand-new payload claiming a fresh exp — signature won't match.
    forged_payload = json.dumps(
        {"uid": "u", "iid": "i", "iat": 0, "exp": int(time.time()) + 999_999},
        separators=(",", ":"),
    )
    forged_b64 = _b64url_encode(forged_payload.encode())
    forged_token = f"{forged_b64}.{sig_b64}"

    assert verify_town_token(forged_token) is None


def test_verify_malformed_token_returns_none():
    assert verify_town_token("not-a-token") is None
    assert verify_town_token("only.one.dot.too.many") is None
    assert verify_town_token("") is None


# ---------------------------------------------------------------------------
# Time-mock variant — proves ``time.time`` is the gate, not just ``iat``.
# ---------------------------------------------------------------------------


def test_verify_uses_current_time_for_expiry_check():
    # Issue a token now, then jump time forward past the expiry.
    token = sign_town_token("u", "i")

    fake_now = int(time.time()) + TOKEN_TTL_SECONDS + 10
    with patch("core.town_token.time.time", return_value=fake_now):
        assert verify_town_token(token) is None


@pytest.mark.parametrize("uid,iid", [("a", "b"), ("user-with-dashes", "00000000-uuid")])
def test_round_trip_preserves_identity(uid, iid):
    token = sign_town_token(uid, iid)
    payload = verify_town_token(token)
    assert payload is not None
    assert payload["uid"] == uid
    assert payload["iid"] == iid
