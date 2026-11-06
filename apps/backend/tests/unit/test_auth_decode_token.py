"""End-to-end RS256 verification through core.auth._decode_token.

Signs real Clerk-shaped tokens with a throwaway RSA key and serves its JWK
from a patched JWKS cache, so a PyJWT upgrade that changes PyJWK, algorithm
or claim handling fails here instead of in production sign-in.
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, patch

import jwt
import pytest
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


async def test_hs256_with_public_key_rejected():
    """Algorithm-confusion: an HS256 token must not verify against the RS256 key."""
    forged = jwt.encode({"sub": "attacker"}, "s" * 32, algorithm="HS256", headers={"kid": "k1"})
    with pytest.raises(jwt.InvalidAlgorithmError):
        await auth._decode_token(forged)


async def test_unknown_kid_rejected():
    forged = jwt.encode({"sub": "x"}, _KEY, algorithm="RS256", headers={"kid": "nope"})
    with pytest.raises(HTTPException):
        await auth._decode_token(forged)
