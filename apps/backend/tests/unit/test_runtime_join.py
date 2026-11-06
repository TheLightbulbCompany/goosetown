import base64
import json
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
from routers.town import router


@pytest.fixture
def enrollment(dynamodb_tables, monkeypatch):
    monkeypatch.setattr(settings, "RUNTIME_BINDING_ENABLED", True)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/town")
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="owner")
    client = TestClient(app)
    response = client.post("/api/v1/town/instance")
    assert response.status_code == 200, response.text
    packed = response.json()["join_token"][4:]
    credential = json.loads(base64.urlsafe_b64decode(packed + "=" * (-len(packed) % 4)))
    instance = TownService().get_active_instance("owner")
    # The legacy registration service can omit instanceId; onboarding attaches
    # only this authenticated owner's instance under an atomic owner check.
    dynamo.agents_table().put_item(
        Item={
            "id": "actor-1",
            "userId": "owner",
            "agentName": "visitor",
            "isActive": "true",
            "joinedAt": "2026-01-01",
            "spriteReady": True,
        }
    )
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    public = base64.b32encode(raw).decode().lower().rstrip("=")
    body = {
        "agent_name": "visitor",
        "member_key": public,
        "session_id": public[:12] + "/visitor",
        "enrollment_token": credential["enrollment_token"],
    }
    headers = {"Authorization": "Bearer " + credential["town_token"]}
    return client, key, body, headers, instance


def begin(fixture, body=None):
    c, _, original, headers, _ = fixture
    return c.post("/api/v1/town/runtime-join", json=body or original, headers=headers)


def proof(start, key):
    challenge = start["challenge"]
    encoded = challenge["signing_bytes_b64url"]
    signature = key.sign(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    return {
        "poll_secret": start["poll_secret"],
        "challenge_id": challenge["challenge_id"],
        "signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def endpoint(start):
    return f"/api/v1/town/agents/{start['actor_id']}/runtime-join/{start['request_id']}"


def test_automatic_initial_binding_and_ready_rejoin(enrollment):
    c, key, body, headers, instance = enrollment
    response = begin(enrollment)
    assert response.status_code == 200, response.text
    start = response.json()
    assert start["state"] == "approved" and "approval_url" not in start
    assert (
        c.post(endpoint(start) + "/poll", json={"poll_secret": start["poll_secret"]}, headers=headers).status_code
        == 200
    )
    result = c.post(endpoint(start) + "/complete", json=proof(start, key), headers=headers)
    assert result.status_code == 200, result.text
    assert result.json() == {"state": "ready", "actor_id": "actor-1", "revision": 1, "runtime_session_required": False}
    actor = TownService().get_town_agent_by_id("actor-1", consistent=True)
    assert actor["instanceId"] == instance["id"]
    assert actor["runtimeBinding"]["memberKey"] == body["member_key"]
    assert "runtimeJoin" not in actor and "bindingChallenge" not in actor
    row = dynamo.instances_table().get_item(Key={"id": instance["id"]}, ConsistentRead=True)["Item"]
    assert "runtimeEnrollment" not in row
    assert c.post(endpoint(start) + "/complete", json=proof(start, key), headers=headers).status_code == 404
    returning = {k: v for k, v in body.items() if k != "enrollment_token"}
    assert begin(enrollment, returning).json()["state"] == "ready"


def test_no_implicit_authority_from_legacy_token_or_wrong_grant(enrollment):
    c, _, body, headers, _ = enrollment
    missing = {k: v for k, v in body.items() if k != "enrollment_token"}
    assert begin(enrollment, missing).status_code == 403
    assert begin(enrollment, {**body, "enrollment_token": "x" * 43}).status_code == 403
    assert c.post("/api/v1/town/runtime-join", json=body).status_code == 401
    assert c.post("/api/v1/town/runtime-join", json=body, headers={"Authorization": "Bearer fake"}).status_code == 401
    assert "runtimeJoin" not in TownService().get_town_agent_by_id("actor-1", consistent=True)


def test_wrong_poll_secret_signature_and_expiry(enrollment, monkeypatch):
    c, key, _, headers, instance = enrollment
    start = begin(enrollment).json()
    assert c.post(endpoint(start) + "/poll", json={"poll_secret": "x" * 43}, headers=headers).status_code == 404
    wrong = proof(start, Ed25519PrivateKey.generate())
    assert c.post(endpoint(start) + "/complete", json=wrong, headers=headers).status_code == 403
    assert "runtimeEnrollment" in dynamo.instances_table().get_item(Key={"id": instance["id"]})["Item"]
    monkeypatch.setattr("routers.runtime_binding._now", lambda: start["expires_at"])
    assert c.post(endpoint(start) + "/complete", json=proof(start, key), headers=headers).status_code == 404


def test_reissued_grant_invalidates_inflight(enrollment):
    c, key, _, headers, _ = enrollment
    start = begin(enrollment).json()
    assert c.post("/api/v1/town/instance").status_code == 200
    result = c.post(endpoint(start) + "/complete", json=proof(start, key), headers=headers)
    assert result.status_code == 409
    assert "runtimeBinding" not in TownService().get_town_agent_by_id("actor-1", consistent=True)


def test_retry_same_runtime_supersedes_failed_attempt(enrollment):
    c, key, _, headers, _ = enrollment
    old = begin(enrollment).json()
    response = begin(enrollment)
    assert response.status_code == 200, response.text
    new = response.json()
    assert new["request_id"] != old["request_id"]
    assert c.post(endpoint(old) + "/complete", json=proof(old, key), headers=headers).status_code == 404
    assert c.post(endpoint(new) + "/complete", json=proof(new, key), headers=headers).status_code == 200


def test_binding_cannot_be_replaced_by_new_onboarding(enrollment):
    c, key, body, headers, _ = enrollment
    start = begin(enrollment).json()
    assert c.post(endpoint(start) + "/complete", json=proof(start, key), headers=headers).status_code == 200
    assert begin(enrollment, {**body, "session_id": body["member_key"][:12] + "/another"}).status_code == 409


def test_expired_grant_and_authorization_storage_failure(enrollment, monkeypatch):
    c, _, _, _, instance = enrollment
    dynamo.instances_table().update_item(
        Key={"id": instance["id"]},
        UpdateExpression="SET runtimeEnrollment.expiresAt=:expired",
        ExpressionAttributeValues={":expired": 1},
    )
    assert begin(enrollment).status_code == 403

    class Unavailable:
        def get_item(self, **kwargs):
            raise RuntimeError("storage down")

    monkeypatch.setattr(dynamo, "users_table", lambda: Unavailable())
    assert begin(enrollment).status_code == 503
    assert "runtimeBinding" not in TownService().get_town_agent_by_id("actor-1", consistent=True)


def test_revocation_owner_transfer_and_deactivation(enrollment):
    c, key, _, headers, instance = enrollment
    start = begin(enrollment).json()
    dynamo.users_table().put_item(Item={"userId": "owner", "tokenRevokedAt": 9999999999999})
    assert c.post(endpoint(start) + "/complete", json=proof(start, key), headers=headers).status_code == 401
    dynamo.users_table().delete_item(Key={"userId": "owner"})
    dynamo.agents_table().update_item(
        Key={"id": "actor-1"}, UpdateExpression="SET isActive=:v", ExpressionAttributeValues={":v": "false"}
    )
    assert c.post(endpoint(start) + "/complete", json=proof(start, key), headers=headers).status_code == 404
    dynamo.agents_table().update_item(
        Key={"id": "actor-1"},
        UpdateExpression="SET isActive=:v, userId=:u",
        ExpressionAttributeValues={":v": "true", ":u": "other"},
    )
    assert c.post(endpoint(start) + "/complete", json=proof(start, key), headers=headers).status_code == 404
    assert "runtimeEnrollment" in dynamo.instances_table().get_item(Key={"id": instance["id"]})["Item"]


def test_grant_can_bind_only_one_actor_transactionally(enrollment):
    c, key, body, headers, instance = enrollment
    first = begin(enrollment).json()
    dynamo.agents_table().put_item(
        Item={
            "id": "actor-2",
            "userId": "owner",
            "agentName": "other",
            "isActive": "true",
            "instanceId": instance["id"],
        }
    )
    other = begin(enrollment, {**body, "agent_name": "other"}).json()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda start: (
                    c.post(endpoint(start) + "/complete", json=proof(start, key), headers=headers).status_code
                ),
                (first, other),
            )
        )
    assert sorted(results) == [200, 409], results
    assert (
        sum("runtimeBinding" in TownService().get_town_agent_by_id(a, consistent=True) for a in ("actor-1", "actor-2"))
        == 1
    )


def test_revoke_between_read_and_commit_is_rejected(enrollment, monkeypatch):
    from routers import runtime_binding

    c, key, _, headers, _ = enrollment
    start = begin(enrollment).json()
    real = runtime_binding._complete_for_owner

    def race(*args, **kwargs):
        dynamo.users_table().put_item(Item={"userId": "owner", "tokenRevokedAt": 9999999999999})
        return real(*args, **kwargs)

    monkeypatch.setattr(runtime_binding, "_complete_for_owner", race)
    result = c.post(endpoint(start) + "/complete", json=proof(start, key), headers=headers)
    assert result.status_code == 409, result.text
    assert "runtimeBinding" not in TownService().get_town_agent_by_id("actor-1", consistent=True)


def test_disabled_all_routes_and_public_projection(enrollment, monkeypatch):
    c, key, _, headers, _ = enrollment
    start = begin(enrollment).json()
    world = c.get("/api/v1/town/state")
    assert world.status_code == 200
    for secret in (start["poll_secret"], start["request_id"], "runtimeJoin", "enrollmentHash"):
        assert secret not in world.text
    monkeypatch.setattr(settings, "RUNTIME_BINDING_ENABLED", False)
    assert begin(enrollment).status_code == 404
    assert (
        c.post(endpoint(start) + "/poll", json={"poll_secret": start["poll_secret"]}, headers=headers).status_code
        == 404
    )
    assert c.post(endpoint(start) + "/complete", json=proof(start, key), headers=headers).status_code == 404


def test_device_consent_cannot_bind_another_character(enrollment):
    c, _, _, _, instance = enrollment
    dynamo.instances_table().update_item(
        Key={"id": instance["id"]}, UpdateExpression="SET runtimeEnrollment.agentName = :name",
        ExpressionAttributeValues={":name": "someone-else"},
    )
    assert begin(enrollment).status_code == 403
