import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException

from core.config import settings
from core.runtime_origin import audience


@pytest.mark.parametrize("origin", [
    "https://town.example", "https://town.example:8443", "http://localhost:8000",
    "http://127.0.0.1:8000", "http://[::1]:8000", "https://[::1]",
])
def test_canonical_origins(monkeypatch, origin):
    monkeypatch.setattr(settings, "RUNTIME_BINDING_ORIGIN", origin)
    assert audience() == "goosetown-runtime-binding-v1:" + origin


@pytest.mark.parametrize("origin", [
    "", "http://town.example", "https://town.example/", "https://TOWN.example",
    "https://town.example:443", "https://user@town.example", "https://town.example?q=1",
    "https://town.example#x", "http://127.0.0.2:8000", "https://[0:0:0:0:0:0:0:1]",
    "https://" + ".".join(["a" * 60] * 8),
])
def test_bad_origin_is_configuration_error(monkeypatch, origin):
    monkeypatch.setattr(settings, "RUNTIME_BINDING_ORIGIN", origin)
    with pytest.raises(HTTPException) as error:
        audience()
    assert error.value.status_code == 503


def test_cross_language_origin_scoped_vector():
    from routers.runtime_binding import _bytes

    key = Ed25519PrivateKey.from_private_bytes(bytes([42]) * 32)
    from cryptography.hazmat.primitives import serialization
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    member = base64.b32encode(public).decode().lower().rstrip("=")
    payload = {
        "actor_id": "12345678-1234-1234-1234-123456789abc",
        "audience": "goosetown-runtime-binding-v1:https://town.example",
        "challenge_id": base64.urlsafe_b64encode(bytes([7]) * 32).decode().rstrip("="),
        "expires_at": 1120, "issued_at": 1000, "member_key": member,
        "owner_id": "user_example", "revision": 0, "session_id": member[:12] + "/synthetic", "version": 1,
    }
    # The Rust test pins this same externally generated payload and signature.
    signature = base64.urlsafe_b64encode(key.sign(_bytes(payload))).decode().rstrip("=")
    assert signature == "Ht6Qxgo-DT9kIfeSJqBr3MZ7cN_0MhOZyzOcjka4ZxqIoaYIkhFvS1-V3ppLCvzqhsF_FlHVK0PHMNWQfvwdDw"
    assert json.loads(_bytes(payload).split(b"\n", 1)[1])["audience"].endswith(":https://town.example")
