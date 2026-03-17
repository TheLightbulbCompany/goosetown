"""Clerk JWT validation for GooseTown API.

Ported from Isol8 backend -- validates Clerk-issued JWTs using JWKS endpoint.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.config import settings

logger = logging.getLogger(__name__)
security = HTTPBearer()
security_optional = HTTPBearer(auto_error=False)

# JWKS cache with TTL
_jwks_cache: dict = {"data": None, "expires_at": None}
JWKS_CACHE_TTL = timedelta(hours=1)


async def _get_cached_jwks(jwks_url: str) -> dict:
    """Fetch JWKS with TTL-based caching to avoid hitting Clerk on every request."""
    now = datetime.now(timezone.utc)

    # Return cached data if still valid
    if _jwks_cache["data"] and _jwks_cache["expires_at"] and now < _jwks_cache["expires_at"]:
        return _jwks_cache["data"]

    # Fetch fresh JWKS
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(jwks_url, timeout=10.0)
            response.raise_for_status()
            jwks = response.json()

        # Update cache
        _jwks_cache["data"] = jwks
        _jwks_cache["expires_at"] = now + JWKS_CACHE_TTL
        logger.info("JWKS cache refreshed")
        return jwks
    except httpx.HTTPError as e:
        # If fetch fails but we have stale cached data, use it as fallback
        if _jwks_cache["data"]:
            logger.warning(f"JWKS fetch failed, using stale cache: {e}")
            return _jwks_cache["data"]
        raise


@dataclass
class AuthContext:
    """Structured auth context from JWT claims."""

    user_id: str


def _find_rsa_key(jwks: dict, kid: str) -> dict | None:
    """Find RSA key in JWKS by key ID."""
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return {
                "kty": key["kty"],
                "kid": key["kid"],
                "use": key["use"],
                "n": key["n"],
                "e": key["e"],
            }
    return None


async def _decode_token(token: str) -> dict:
    """Fetch JWKS, find the matching RSA key, and decode the JWT.

    Raises jwt/httpx exceptions on failure -- callers handle error mapping.
    """
    jwks_url = f"{settings.CLERK_ISSUER}/.well-known/jwks.json"
    jwks = await _get_cached_jwks(jwks_url)

    unverified_header = jwt.get_unverified_header(token)
    rsa_key = _find_rsa_key(jwks, unverified_header["kid"])
    if not rsa_key:
        raise HTTPException(status_code=401, detail="Invalid token headers")

    public_key = jwt.PyJWK(rsa_key).key
    return jwt.decode(
        token,
        public_key,
        algorithms=["RS256"],
        audience=settings.CLERK_AUDIENCE,
        issuer=settings.CLERK_ISSUER,
    )


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> AuthContext:
    """Validate JWT and return AuthContext with user_id."""
    try:
        token = credentials.credentials
        # Diagnostic: log unverified claims to trace auth failures (DEBUG only)
        try:
            unverified = jwt.decode(token, options={"verify_signature": False}, algorithms=["RS256"])
            logger.debug(
                "JWT claims: iss=%s sub=%s aud=%s", unverified.get("iss"), unverified.get("sub"), unverified.get("aud")
            )
        except Exception as parse_err:
            logger.debug("Could not decode unverified JWT claims: %s", parse_err)
        payload = await _decode_token(token)

        return AuthContext(user_id=payload["sub"])

    except jwt.ExpiredSignatureError:
        logger.warning("AUTH FAIL: JWT expired")
        raise HTTPException(status_code=401, detail="Token expired")
    except (jwt.InvalidAudienceError, jwt.InvalidIssuerError, jwt.MissingRequiredClaimError) as e:
        logger.warning("AUTH FAIL: JWT claims error: %s", e)
        raise HTTPException(status_code=401, detail="Invalid claims")
    except httpx.HTTPError as e:
        logger.error(f"Failed to fetch JWKS: {e}")
        raise HTTPException(status_code=503, detail="Authentication service unavailable")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"JWT validation error: {e}")
        raise HTTPException(status_code=401, detail="Could not validate credentials")


async def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security_optional),
) -> AuthContext | None:
    """Like get_current_user but returns None instead of raising on missing/invalid auth."""
    if credentials is None:
        return None

    try:
        payload = await _decode_token(credentials.credentials)
        return AuthContext(user_id=payload["sub"])
    except Exception:
        return None
