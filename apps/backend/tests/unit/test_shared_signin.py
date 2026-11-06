import importlib.util
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from core import auth
from core.config import settings

ORIGIN = "https://goosetown.thelightbulb.company"
ISSUER = "https://clerk.thelightbulb.company"


@pytest.fixture
def verifier(monkeypatch):
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("CLERK_AUTHORIZED_PARTIES", ORIGIN)
    path = Path(__file__).resolve().parents[3] / "infra/lambda/ws-authorizer/index.py"
    spec = importlib.util.spec_from_file_location("goose_ws_authorizer_test", path)
    ws = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ws)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk.update(kid="synthetic", use="sig")

    async def backend_keys(_url):
        return {"keys": [jwk]}

    monkeypatch.setattr(auth, "_get_cached_jwks", backend_keys)
    monkeypatch.setattr(ws, "_get_jwks", lambda _: {"keys": [jwk]})
    monkeypatch.setattr(settings, "CLERK_ISSUER", ISSUER)
    monkeypatch.setattr(settings, "CLERK_AUTHORIZED_PARTIES", ORIGIN)
    monkeypatch.setattr(settings, "CLERK_AUDIENCE", None)
    return key, ws


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,valid",
    [
        ({}, True),
        ({"azp": "https://thelightbulb.company"}, False),
        ({"azp": None}, False),
        ({"azp": []}, False),
        ({"sub": ""}, False),
        ({"exp": "4102444800"}, False),
        ({"nbf": True}, False),
        ({"exp": float("nan")}, False),
        ({"nbf": float("inf")}, False),
        ({"iat": "1"}, False),
        ({"iat": True}, False),
        ({"aud": "unexpected"}, False),
        ({"iss": "https://clerk.isol8.co"}, False),
        ({"exp": 1}, False),
        ({"nbf": 4102444800}, False),
    ],
)
async def test_backend_and_websocket_claims_match(verifier, change, valid):
    key, ws = verifier
    now = int(time.time())
    claims = {"sub": "user_synthetic", "iss": ISSUER, "azp": ORIGIN, "nbf": now - 1, "exp": now + 60}
    claims.update(change)
    token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "synthetic"})
    if valid:
        assert (await auth._decode_token(token))["sub"] == "user_synthetic"
    else:
        with pytest.raises(jwt.InvalidTokenError):
            await auth._decode_token(token)
    assert (ws._verify_clerk_jwt(token, ISSUER) == "user_synthetic") is valid


def test_ws_rejects_bad_signature_and_key(verifier, monkeypatch):
    key, ws = verifier
    now = int(time.time())
    claims = {"sub": "u", "iss": ISSUER, "azp": ORIGIN, "nbf": now - 1, "exp": now + 60}
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(claims, other, algorithm="RS256", headers={"kid": "synthetic"})
    assert ws._verify_clerk_jwt(token, ISSUER) is None
    token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "unknown"})
    assert ws._verify_clerk_jwt(token, ISSUER) is None
    assert ws._verify_clerk_jwt("not.a.jwt", ISSUER) is None
    monkeypatch.setattr(ws, "_get_jwks", lambda _: {"keys": [{"kid": "synthetic", "kty": "RSA"}]})
    token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "synthetic"})
    assert ws._verify_clerk_jwt(token, ISSUER) is None


def test_ws_jwks_cache_expires(verifier, monkeypatch):
    import io
    import json

    _, ws = verifier
    # Restore the implementation replaced by the synthetic verifier fixture.
    spec = importlib.util.spec_from_file_location("goose_ws_rotation_test", Path(ws.__file__))
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    clock = [10.0]
    values = [{"keys": [{"kid": "old"}]}, {"keys": [{"kid": "new"}]}]

    class Response(io.BytesIO):
        pass

    monkeypatch.setattr(fresh.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        fresh.urllib.request, "urlopen", lambda *_args, **_kwargs: Response(json.dumps(values.pop(0)).encode())
    )
    assert fresh._get_jwks("https://synthetic/jwks")["keys"][0]["kid"] == "old"
    clock[0] += 301
    assert fresh._get_jwks("https://synthetic/jwks")["keys"][0]["kid"] == "new"


@pytest.mark.parametrize("missing", ["exp", "nbf", "sub"])
def test_ws_missing_required_claims(verifier, missing):
    key, ws = verifier
    now = int(time.time())
    claims = {"sub": "u", "iss": ISSUER, "azp": ORIGIN, "nbf": now - 1, "exp": now + 60}
    del claims[missing]
    token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "synthetic"})
    assert ws._verify_clerk_jwt(token, ISSUER) is None


def test_ws_wrong_algorithm_and_nonobject(verifier):
    import base64

    _, ws = verifier
    now = int(time.time())
    claims = {"sub": "u", "iss": ISSUER, "azp": ORIGIN, "nbf": now - 1, "exp": now + 60}
    token = jwt.encode(claims, "synthetic", algorithm="HS256", headers={"kid": "synthetic"})
    assert ws._verify_clerk_jwt(token, ISSUER) is None

    def part(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    assert ws._verify_clerk_jwt(part(b"[]") + "." + part(b"{}") + ".AA", ISSUER) is None
