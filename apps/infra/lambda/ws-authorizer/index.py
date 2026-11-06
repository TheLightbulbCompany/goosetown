import base64
import hashlib
import hmac
import json
import logging
import os
import struct
import time
import urllib.request

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

secrets_client = boto3.client("secretsmanager")
_cache = {}

def _get_secret(name):
    if name not in _cache:
        resp = secrets_client.get_secret_value(SecretId=name)
        _cache[name] = resp["SecretString"]
    return _cache[name]

def _b64url_decode(s):
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)

def _verify_town_token(token, secret):
    """Verify HMAC-signed town token. Returns payload dict or None.

    Requires nonempty identity and integer issue/expiry times.
    """
    parts = token.split(".")
    if len(parts) != 2:
        return None
    payload_b64, sig_b64 = parts
    try:
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
    if not isinstance(payload, dict) or any(
        not isinstance(payload.get(k), str) or not payload[k] for k in ("uid", "iid")
    ):
        return None
    now = int(time.time())
    iat, exp = payload.get("iat"), payload.get("exp")
    if type(iat) is not int or type(exp) is not int or iat > now or exp <= now or exp <= iat:
        return None
    return payload


def _town_token_not_revoked(payload):
    # Anonymous viewers have no owner or revocation record.
    if payload["uid"] == "anonymous" and payload["iid"] == "anonymous-viewer":
        return True
    response = boto3.client("dynamodb").get_item(
        TableName=os.environ["USERS_TABLE"],
        Key={"userId": {"S": payload["uid"]}},
        ConsistentRead=True,
        ProjectionExpression="tokenRevokedAt",
    )
    revoked = response.get("Item", {}).get("tokenRevokedAt")
    return revoked is None or payload["iat"] * 1000 >= int(revoked["N"])

def _b64url_to_int(s):
    """Decode a base64url-encoded big-endian unsigned integer."""
    data = _b64url_decode(s)
    return int.from_bytes(data, "big")

def _verify_rs256(message_bytes, signature_bytes, n_int, e_int):
    """Verify RS256 signature using raw math (no external crypto libs)."""
    sig_int = int.from_bytes(signature_bytes, "big")
    key_len = (n_int.bit_length() + 7) // 8
    if len(signature_bytes) != key_len:
        return False
    decrypted = pow(sig_int, e_int, n_int)
    em = decrypted.to_bytes(key_len, "big")
    # PKCS#1 v1.5: 0x00 0x01 [padding 0xFF] 0x00 [DigestInfo]
    # SHA-256 DigestInfo prefix
    sha256_prefix = bytes.fromhex(
        "3031300d060960864801650304020105000420"
    )
    digest = hashlib.sha256(message_bytes).digest()
    expected_suffix = sha256_prefix + digest
    # em should be: 00 01 FF...FF 00 <expected_suffix>
    if len(em) < len(expected_suffix) + 11:
        return False
    if em[0] != 0 or em[1] != 1:
        return False
    suffix_start = len(em) - len(expected_suffix)
    if em[suffix_start - 1] != 0:
        return False
    if em[suffix_start:] != expected_suffix:
        return False
    # Check padding bytes are all 0xFF
    for i in range(2, suffix_start - 1):
        if em[i] != 0xFF:
            return False
    return True

_jwks_cache = {}
JWKS_CACHE_TTL_SECONDS = 300

def _get_jwks(jwks_url):
    cached = _jwks_cache.get(jwks_url)
    if cached and time.monotonic() < cached[0]:
        return cached[1]
    req = urllib.request.Request(jwks_url, headers={"User-Agent": "GooseTown-Authorizer/1.0"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read())
    _jwks_cache[jwks_url] = (time.monotonic() + JWKS_CACHE_TTL_SECONDS, data)
    return data

def _verify_clerk_jwt(token, clerk_issuer):
    """Verify a Clerk JWT using JWKS. Returns subject or None."""
    import time as _time

    parts = token.split(".")
    if len(parts) != 3:
        return None
    header_b64, payload_b64, sig_b64 = parts
    try:
        header = json.loads(_b64url_decode(header_b64))
        payload = json.loads(_b64url_decode(payload_b64))
        signature = _b64url_decode(sig_b64)
    except Exception:
        return None

    if not isinstance(header, dict) or not isinstance(payload, dict):
        return None
    if header.get("alg") != "RS256":
        logger.warning("Unsupported JWT algorithm: %s", header.get("alg"))
        return None

    kid = header.get("kid")
    if not kid:
        return None

    # Verify expiry
    now = int(_time.time())
    if type(payload.get("exp")) is not int or payload["exp"] <= now:
        logger.warning("JWT expired")
        return None

    if type(payload.get("nbf")) is not int or payload["nbf"] > now:
        return None
    if "iat" in payload and (type(payload["iat"]) is not int or payload["iat"] > now):
        return None
    if not isinstance(payload.get("sub"), str) or not payload["sub"]:
        return None
    allowed = {x.strip() for x in os.environ.get("CLERK_AUTHORIZED_PARTIES", "").split(",") if x.strip()}
    if not allowed or not isinstance(payload.get("azp"), str) or payload["azp"] not in allowed:
        return None
    if payload.get("aud"):
        return None

    # Verify issuer
    if payload.get("iss") != clerk_issuer:
        logger.warning("JWT issuer mismatch: %s != %s", payload.get("iss"), clerk_issuer)
        return None

    # Fetch JWKS and find key
    jwks_url = clerk_issuer.rstrip("/") + "/.well-known/jwks.json"
    try:
        jwks = _get_jwks(jwks_url)
    except Exception as e:
        logger.error("Failed to fetch JWKS: %s", e)
        return None

    rsa_key = None
    if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
        return None
    for key in jwks["keys"]:
        if isinstance(key, dict) and key.get("kid") == kid and key.get("kty") == "RSA":
            rsa_key = key
            break
    if not rsa_key:
        logger.warning("No matching RSA key found for kid=%s", kid)
        return None

    # Verify signature
    try:
        n_int = _b64url_to_int(rsa_key["n"])
        e_int = _b64url_to_int(rsa_key["e"])
        if n_int <= 0 or e_int <= 0:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    message = (header_b64 + "." + payload_b64).encode("ascii")
    if not _verify_rs256(message, signature, n_int, e_int):
        logger.warning("JWT signature verification failed")
        return None

    return payload.get("sub")

def _extract_token(event):
    # Prefer "Authorization: Bearer <token>" header (kept out of access logs).
    # Fall back to the legacy ?token= query param so daemons that still ship
    # the older skill bundle keep working through the transition.
    headers = event.get("headers") or {}
    for k, v in headers.items():
        if k.lower() == "authorization" and isinstance(v, str):
            if v.lower().startswith("bearer "):
                return v[7:].strip(), "header"
            return v.strip(), "header"
    qs = event.get("queryStringParameters") or {}
    legacy_token = qs.get("token") or ""
    if legacy_token:
        return legacy_token, "query"
    return "", "none"

def handler(event, context):
    method_arn = event.get("methodArn", "")
    # Don't log the full event — it contains the token in queryStringParameters
    # or headers depending on the auth path. Log only the route + source.
    route_key = event.get("requestContext", {}).get("routeKey", "")
    token, source = _extract_token(event)
    logger.info("Authorizer route=%s token_source=%s", route_key, source)

    if not token:
        logger.warning("No token provided")
        return generate_policy("anonymous", "Deny", method_arn)

    # Try town token first (HMAC-signed)
    try:
        town_secret = _get_secret(os.environ["TOWN_TOKEN_SECRET"])
        payload = _verify_town_token(token, town_secret)
        if payload and _town_token_not_revoked(payload):
            user_id = payload.get("uid", "town-agent")
            logger.info("Town token verified for user %s", user_id)
            return generate_policy(user_id, "Allow", method_arn)
    except Exception as e:
        logger.warning("Town token check failed: %s", e)

    # Try Clerk JWT
    try:
        clerk_issuer = _get_secret(os.environ["CLERK_ISSUER"])
        subject = _verify_clerk_jwt(token, clerk_issuer)
        if subject:
            logger.info("Clerk JWT verified for user %s", subject)
            return generate_policy(subject, "Allow", method_arn)
    except Exception as e:
        logger.warning("Clerk JWT check failed: %s", e)

    logger.warning("All token verification methods failed")
    return generate_policy("anonymous", "Deny", method_arn)

def generate_policy(principal_id, effect, method_arn):
    # Allow all routes on this API once authorized
    arn_parts = method_arn.split(":")
    region = arn_parts[3]
    account_id = arn_parts[4]
    api_gw_arn = arn_parts[5].split("/")
    api_id = api_gw_arn[0]
    stage = api_gw_arn[1] if len(api_gw_arn) > 1 else "*"
    resource_arn = f"arn:aws:execute-api:{region}:{account_id}:{api_id}/{stage}/*"

    return {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    "Resource": resource_arn,
                }
            ],
        },
    }
