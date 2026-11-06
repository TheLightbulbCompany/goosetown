import base64
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import dynamo
from core.auth import AuthContext, get_current_user
from core.config import settings
from core.services.town_service import TownService
from routers.runtime_binding import AUDIENCE, router


@pytest.fixture
def setup(dynamodb_tables, monkeypatch):
    monkeypatch.setattr(settings, "RUNTIME_BINDING_ENABLED", True)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/town")
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="owner")
    dynamo.agents_table().put_item(
        Item={
            "id": "actor-1",
            "userId": "owner",
            "agentName": "test",
            "displayName": "test",
            "isActive": "true",
            "instanceId": "instance",
        }
    )
    client = TestClient(app)
    private = Ed25519PrivateKey.generate()
    raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key = base64.b32encode(raw).decode().lower().rstrip("=")
    body = {"member_key": key, "session_id": key[:12] + "/codex:session1", "audience": AUDIENCE, "expected_revision": 0}
    path = "/api/v1/town/agents/actor-1/runtime-binding"
    return app, client, private, body, path


def mint(client, path, body):
    return client.post(path + "/challenge", json=body)


def finish(client, path, private, challenge):
    raw = base64.urlsafe_b64decode(
        challenge["signing_bytes_b64url"] + "=" * (-len(challenge["signing_bytes_b64url"]) % 4)
    )
    sig = base64.urlsafe_b64encode(private.sign(raw)).decode().rstrip("=")
    return client.post(path + "/complete", json={"challenge_id": challenge["challenge_id"], "signature": sig})


def test_happy_replay_unlink_and_reactivation(setup):
    app, c, k, b, p = setup
    challenge = mint(c, p, b).json()
    result = finish(c, p, k, challenge)
    assert result.status_code == 200, result.text
    assert result.json()["binding"]["revision"] == 1
    assert finish(c, p, k, challenge).status_code == 409
    service = TownService()
    assert service.opt_out("owner", "test")
    assert mint(c, p, {**b, "expected_revision": 1}).status_code == 404
    revived = service.opt_in("owner", "test", "test")
    assert revived["id"] == "actor-1"
    assert revived["runtimeBinding"]["memberKey"] == b["member_key"]
    assert revived["bindingRevision"] == 1
    assert c.request("DELETE", p, json={"expected_revision": 1}).json()["revision"] == 2
    row = service.get_town_agent_by_id("actor-1", consistent=True)
    assert "runtimeBinding" not in row and row["bindingRevision"] == 2


def test_disabled_and_account_auth(setup, monkeypatch):
    app, c, k, b, p = setup
    monkeypatch.setattr(settings, "RUNTIME_BINDING_ENABLED", False)
    assert mint(c, p, b).status_code == 404
    assert c.post(p + "/complete", json={"challenge_id": "x" * 43, "signature": "x" * 86}).status_code == 404
    assert c.request("DELETE", p, json={"expected_revision": 0}).status_code == 404
    assert "bindingChallenge" not in TownService().get_town_agent_by_id("actor-1", consistent=True)
    monkeypatch.setattr(settings, "RUNTIME_BINDING_ENABLED", True)
    app.dependency_overrides.clear()
    assert mint(c, p, b).status_code in (401, 403)
    assert c.post(p + "/complete", json={"challenge_id": "x" * 43, "signature": "x" * 86}).status_code in (401, 403)
    assert c.request("DELETE", p, json={"expected_revision": 0}).status_code in (401, 403)
    assert "bindingChallenge" not in TownService().get_town_agent_by_id("actor-1", consistent=True)


def test_wrong_owner_bad_inputs_and_tampered_signature(setup):
    app, c, k, b, p = setup
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="other")
    assert mint(c, p, b).status_code == 404
    assert c.post(p + "/complete", json={"challenge_id": "x" * 43, "signature": "x" * 86}).status_code == 404
    assert c.request("DELETE", p, json={"expected_revision": 0}).status_code == 404
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="owner")
    assert mint(c, p, {**b, "audience": "wrong"}).status_code == 422
    assert mint(c, p, {**b, "member_key": b["member_key"].upper()}).status_code == 422
    assert mint(c, p, {**b, "session_id": "aaaaaaaaaaaa/codex:session1"}).status_code == 422
    assert mint(c, p, {**b, "expected_revision": 1}).status_code == 409
    assert mint(c, p, {**b, "session_id": b["member_key"][:12] + "/external-agent"}).status_code == 200
    ch = mint(c, p, b).json()
    assert finish(c, p, Ed25519PrivateKey.generate(), ch).status_code == 403
    assert finish(c, p, k, ch).status_code == 200


def test_superseded_and_expired(setup, monkeypatch):
    app, c, k, b, p = setup
    old = mint(c, p, b).json()
    fresh = mint(c, p, b).json()
    assert finish(c, p, k, old).status_code == 409
    monkeypatch.setattr("routers.runtime_binding.time.time", lambda: fresh["expires_at"])
    assert finish(c, p, k, fresh).status_code == 409


def test_two_completions_only_one_wins(setup):
    app, c, k, b, p = setup
    ch = mint(c, p, b).json()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: finish(c, p, k, ch).status_code, range(2)))
    assert sorted(results) == [200, 409]


def test_cross_actor_and_tampered_stored_challenge(setup):
    app, c, k, b, p = setup
    ch = mint(c, p, b).json()
    dynamo.agents_table().put_item(Item={"id": "actor-2", "userId": "owner", "isActive": "true"})
    other = p.replace("actor-1", "actor-2")
    assert finish(c, other, k, ch).status_code == 409
    dynamo.agents_table().update_item(
        Key={"id": "actor-1"},
        UpdateExpression="SET bindingChallenge.session_id = :s",
        ExpressionAttributeValues={":s": b["member_key"][:12] + "/codex:other"},
    )
    assert finish(c, p, k, ch).status_code == 403


def test_complete_races_unlink(setup):
    app, c, k, b, p = setup
    ch = mint(c, p, b).json()
    with ThreadPoolExecutor(max_workers=2) as pool:
        complete = pool.submit(finish, c, p, k, ch)
        unlink = pool.submit(c.request, "DELETE", p, json={"expected_revision": 0})
        assert sorted([complete.result().status_code, unlink.result().status_code]) == [200, 409]
    assert TownService().get_town_agent_by_id("actor-1", consistent=True)["bindingRevision"] == 1


def test_expiry_advances_between_verification_and_update(setup, monkeypatch):
    app, c, k, b, p = setup
    ch = mint(c, p, b).json()
    ticks = iter([ch["expires_at"] - 1, ch["expires_at"]])
    monkeypatch.setattr("routers.runtime_binding._now", lambda: next(ticks))
    assert finish(c, p, k, ch).status_code == 409
    assert "runtimeBinding" not in TownService().get_town_agent_by_id("actor-1", consistent=True)


def test_competing_rebindings_same_revision(setup):
    app, c, k, b, p = setup
    assert finish(c, p, k, mint(c, p, b).json()).status_code == 200
    proposal = {**b, "expected_revision": 1}
    old = mint(c, p, proposal).json()
    current = mint(c, p, proposal).json()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda challenge: finish(c, p, k, challenge).status_code, [old, current]))
    assert sorted(results) == [200, 409]
    assert TownService().get_town_agent_by_id("actor-1", consistent=True)["bindingRevision"] == 2


def test_strict_input_limits(setup):
    app, c, k, b, p = setup
    assert mint(c, p, {**b, "extra": "x"}).status_code == 422
    assert mint(c, p, {**b, "expected_revision": "0"}).status_code == 422
    assert mint(c, p, {**b, "session_id": "x" * 5000}).status_code == 422
    assert c.post(p + "/complete", json={"challenge_id": "x" * 43, "signature": "x" * 87}).status_code == 422
    assert c.request("DELETE", p, json={"expected_revision": 0, "extra": True}).status_code == 422


def test_signature_wrong_domain_and_payload(setup):
    app, c, k, b, p = setup
    ch = mint(c, p, b).json()
    raw = base64.urlsafe_b64decode(ch["signing_bytes_b64url"] + "=" * (-len(ch["signing_bytes_b64url"]) % 4))
    for changed in (
        b"other-purpose\n" + raw,
        raw.replace(b"actor-1", b"actor-2"),
        raw.replace(b"codex:session1", b"codex:session2"),
        raw.replace(b"goosetown-runtime-binding-v1", b"goosetown-runtime-binding-v2"),
    ):
        sig = base64.urlsafe_b64encode(k.sign(changed)).decode().rstrip("=")
        assert c.post(p + "/complete", json={"challenge_id": ch["challenge_id"], "signature": sig}).status_code == 403
    assert finish(c, p, k, ch).status_code == 200


def test_nonconditional_storage_error_is_not_conflict(setup, monkeypatch):
    from botocore.exceptions import ClientError

    import routers.runtime_binding as binding

    app, c, k, b, p = setup

    class Broken:
        def update_item(self, **kwargs):
            raise ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "throttled"}}, "UpdateItem"
            )

    monkeypatch.setattr(binding.dynamo, "agents_table", lambda: Broken())
    with pytest.raises(ClientError) as err:
        binding._update("actor-1", UpdateExpression="SET x=:x", ExpressionAttributeValues={":x": 1})
    assert err.value.response["Error"]["Code"] == "ProvisionedThroughputExceededException"


def test_get_binding_authorization_revision_and_sleep(setup, monkeypatch):
    app, c, k, b, p = setup
    assert c.get(p).json() == {"actor_id": "actor-1", "revision": 0, "binding": None}
    ch = mint(c, p, b).json()
    # Even the authenticated owner status endpoint does not expose a pending challenge.
    assert c.get(p).json() == {"actor_id": "actor-1", "revision": 0, "binding": None}
    assert finish(c, p, k, ch).status_code == 200
    assert c.get(p).json()["binding"]["memberKey"] == b["member_key"]
    assert c.request("DELETE", p, json={"expected_revision": 1}).status_code == 200
    assert c.get(p).json() == {"actor_id": "actor-1", "revision": 2, "binding": None}
    assert TownService().opt_out("owner", "test")
    assert c.get(p).json()["revision"] == 2
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="other")
    assert c.get(p).status_code == 404
    app.dependency_overrides.clear()
    assert c.get(p).status_code in (401, 403)
    monkeypatch.setattr(settings, "RUNTIME_BINDING_ENABLED", False)
    assert c.get(p).status_code == 404


def test_valid_signature_for_unissued_owner_or_origin_is_refused(setup):
    from routers.runtime_binding import DOMAIN

    _, client, private, body, path = setup
    challenge = mint(client, path, body).json()
    encoded = challenge["signing_bytes_b64url"]
    raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    import json

    payload = json.loads(raw[len(DOMAIN):])
    for name, changed in (
        ("owner_id", "other-owner"),
        ("audience", "goosetown-runtime-binding-v1:https://other.example"),
        ("challenge_id", "a" * 43),
    ):
        modified = {**payload, name: changed}
        signed = DOMAIN + json.dumps(modified, sort_keys=True, separators=(",", ":")).encode()
        signature = base64.urlsafe_b64encode(private.sign(signed)).decode().rstrip("=")
        result = client.post(
            path + "/complete", json={"challenge_id": challenge["challenge_id"], "signature": signature}
        )
        assert result.status_code == 403
    assert finish(client, path, private, challenge).status_code == 200


def test_public_and_existing_actor_responses_exclude_binding_internals(setup):
    from routers.town import get_town_token_user
    from routers.town import router as town_router

    app, c, k, b, p = setup
    ch = mint(c, p, b).json()
    dynamo.agents_table().update_item(
        Key={"id": "actor-1"},
        UpdateExpression="SET joinedAt=:j, spriteReady=:ready, locationContext=:ctx, isProvider=:provider",
        ExpressionAttributeValues={":j": "2026-01-01", ":ready": True, ":ctx": "town", ":provider": True},
    )
    existing = FastAPI()
    existing.include_router(town_router, prefix="/api/v1/town")
    existing.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="owner")
    existing.dependency_overrides[get_town_token_user] = lambda: ("owner", "synthetic")
    client = TestClient(existing)
    instance = TownService().create_instance("owner")
    dynamo.agents_table().update_item(
        Key={"id": "actor-1"},
        UpdateExpression="SET instanceId = :instance",
        ExpressionAttributeValues={":instance": instance["id"]},
    )
    instance_response = client.post("/api/v1/town/instance")
    assert instance_response.status_code == 200
    existing.dependency_overrides[get_town_token_user] = lambda: ("owner", instance_response.json()["town_token"])
    assert ch["challenge_id"] not in instance_response.text
    assert b["member_key"] not in instance_response.text
    assert "bindingChallenge" not in instance_response.text
    for url in (
        "/api/v1/town/state",
        "/api/v1/town/descriptions",
        "/api/v1/town/apartment",
        "/api/v1/town/exchange/providers",
        "/api/v1/town/agent/status?agent_name=test",
    ):
        response = client.get(url)
        assert response.status_code == 200, (url, response.text)
        assert ch["challenge_id"] not in response.text
        assert "bindingChallenge" not in response.text and "runtimeBinding" not in response.text
        assert b["member_key"] not in response.text and b["session_id"] not in response.text
    assert finish(c, p, k, ch).status_code == 200
    for url in (
        "/api/v1/town/state",
        "/api/v1/town/descriptions",
        "/api/v1/town/apartment",
        "/api/v1/town/exchange/providers",
        "/api/v1/town/agent/status?agent_name=test",
    ):
        response = client.get(url)
        assert response.status_code == 200, (url, response.text)
        assert "bindingChallenge" not in response.text and "runtimeBinding" not in response.text
        assert b["member_key"] not in response.text and b["session_id"] not in response.text
