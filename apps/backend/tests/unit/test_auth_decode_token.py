"""End-to-end RS256 verification through core.auth._decode_token.

Signs real Clerk-shaped tokens with a throwaway RSA key and serves its JWK
from a patched JWKS cache, so a PyJWT upgrade that changes PyJWK, algorithm
or claim handling fails here instead of in production sign-in.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from core import auth
from core.config import settings

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_JWK = {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key())), "kid": "k1", "use": "sig"}


def _token(**overrides) -> str:
    now = int(time.time())
    claims = {"sub": "user_1", "iss": settings.CLERK_ISSUER, "iat": now, "nbf": now, "exp": now + 60}
    if settings.CLERK_AUDIENCE:
        claims["aud"] = settings.CLERK_AUDIENCE
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, _KEY, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture(autouse=True)
def _jwks():
    with patch("core.auth._get_cached_jwks", AsyncMock(return_value={"keys": [_JWK]})):
        yield


async def test_valid_token_decodes():
    assert (await auth._decode_token(_token()))["sub"] == "user_1"


async def test_expired_token_rejected():
    with pytest.raises(jwt.ExpiredSignatureError):
        await auth._decode_token(_token(exp=int(time.time()) - 10))


async def test_wrong_issuer_rejected():
    with pytest.raises(jwt.InvalidIssuerError):
        await auth._decode_token(_token(iss="https://evil.example"))


async def test_future_nbf_rejected():
    with pytest.raises(jwt.ImmatureSignatureError):
        await auth._decode_token(_token(nbf=int(time.time()) + 3600))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


async def test_hs256_signed_with_public_key_rejected():
    """Algorithm confusion: HS256 token HMAC'd with the RS256 public key PEM must not verify."""
    pem = _KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "k1"}).encode())
    payload = _b64(json.dumps({"sub": "attacker", "iss": settings.CLERK_ISSUER, "exp": int(time.time()) + 60}).encode())
    sig = _b64(hmac.new(pem, f"{header}.{payload}".encode(), hashlib.sha256).digest())
    with pytest.raises(jwt.InvalidAlgorithmError):
        await auth._decode_token(f"{header}.{payload}.{sig}")


@pytest.mark.parametrize("aud", ["https://wrong.example", None])
async def test_wrong_or_missing_audience_rejected(aud):
    with patch.object(settings, "CLERK_AUDIENCE", "https://goosetown.example"):
        token = _token(aud=aud)  # aud=None drops the claim
        with pytest.raises((jwt.InvalidAudienceError, jwt.MissingRequiredClaimError)):
            await auth._decode_token(token)


async def test_unknown_kid_rejected():
    forged = jwt.encode({"sub": "x"}, _KEY, algorithm="RS256", headers={"kid": "nope"})
    with pytest.raises(HTTPException):
        await auth._decode_token(forged)
