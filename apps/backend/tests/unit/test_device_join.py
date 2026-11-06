"""Device redemption hands credentials only to the requesting poller."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import dynamo
from core.auth import AuthContext, get_current_user
from core.config import settings
from routers.town import router


@pytest.fixture
def device(dynamodb_tables, monkeypatch):
    from unittest.mock import MagicMock

    monkeypatch.setattr("core.services.daemon_mailbox._singleton", MagicMock())
    dynamodb_tables.create_table(
        TableName=dynamo.connections_table().name,
        KeySchema=[{"AttributeName": "connectionId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "connectionId", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    monkeypatch.setattr(settings, "RUNTIME_BINDING_ENABLED", True)
    monkeypatch.setattr(settings, "RUNTIME_SESSION_ENFORCEMENT_ENABLED", True)
    app = FastAPI()
    app.include_router(router, prefix="/town")
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="owner")
    client = TestClient(app)
    response = client.post("/town/device-join", json={"agent_name": "lucky"})
    assert response.status_code == 200, response.text
    data = response.json()
    return client, app, data, "/town/device-join/" + data["user_code"]


def test_owner_consents_and_only_device_receives_credential(device):
    c, app, data, path = device
    assert data["device_secret"] not in data["verification_uri"]
    assert c.get(path).json()["persona"]["agent_name"] == "lucky"
    assert c.post(path + "/poll", json={"device_secret": data["device_secret"]}).json() == {"state": "pending"}
    assert c.post(path + "/approve").json() == {"state": "approved"}
    from core.services.town_service import TownService

    instance = TownService().get_active_instance("owner")
    assert instance["runtimeEnrollment"]["agentName"] == "lucky"
    assert "gtj_" not in c.get(path).text
    assert c.post(path + "/approve").status_code == 409
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="another-owner")
    assert c.get(path).status_code == 404
    assert c.post(path + "/cancel").status_code == 404
    assert c.post(path + "/poll", json={"device_secret": "x" * 43}).status_code == 404
    result = c.post(path + "/poll", json={"device_secret": data["device_secret"]})
    assert result.json()["join_token"].startswith("gtj_")
    assert (
        "credential"
        not in dynamo.connections_table().get_item(Key={"connectionId": "town-device:" + data["user_code"]})["Item"]
    )
    assert "join_token" not in c.post(path + "/poll", json={"device_secret": data["device_secret"]}).json()


def test_cancel_expiry_and_concurrent_redemption(device):
    c, _, data, path = device
    assert c.post(path + "/cancel").status_code == 200
    assert c.post(path + "/approve").status_code == 409
    assert c.post(path + "/poll", json={"device_secret": data["device_secret"]}).json()["state"] == "cancelled"
    data = c.post("/town/device-join", json={}).json()
    path = "/town/device-join/" + data["user_code"]
    assert c.post(path + "/approve").status_code == 200
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: c.post(path + "/poll", json={"device_secret": data["device_secret"]}), range(2))
        )
    assert sum("join_token" in r.json() for r in results) == 1
    dynamo.connections_table().update_item(
        Key={"connectionId": "town-device:" + data["user_code"]},
        UpdateExpression="SET expiresAt = :expired",
        ExpressionAttributeValues={":expired": 1},
    )
    assert c.get(path).status_code == 404


def test_disabled_gate_and_auth(device, monkeypatch):
    c, app, _, path = device
    app.dependency_overrides.clear()
    assert c.get(path).status_code == 401
    monkeypatch.setattr(settings, "RUNTIME_BINDING_ENABLED", False)
    assert c.post("/town/device-join", json={}).status_code == 404


def test_only_one_concurrent_browser_approval_wins(device):
    c, _, _, path = device
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: c.post(path + "/approve"), range(2)))
    assert sorted(r.status_code for r in results) == [200, 409]


def redeem(device):
    import base64
    import json

    c, _, data, path = device
    assert c.post(path + "/approve").status_code == 200
    packed = c.post(path + "/poll", json={"device_secret": data["device_secret"]}).json()["join_token"][4:]
    return json.loads(base64.urlsafe_b64decode(packed + "=" * (-len(packed) % 4)))


def test_device_never_receives_owner_token_or_registration_authority(device, monkeypatch):
    from core.services.town_service import TownService
    from core.town_token import verify_town_token

    c, _, data, path = device
    monkeypatch.setattr(settings, "RUNTIME_SESSION_ENFORCEMENT_ENABLED", True)
    credential = redeem(device)
    token = credential["town_token"]
    claims = verify_town_token(token)
    instance = TownService().get_active_instance("owner")
    assert token != instance["townToken"]
    assert claims["scope"] == "actor" and claims["device_id"] == data["user_code"]
    actor = TownService().get_town_agent_by_id(claims["aid"], consistent=True)
    assert actor["agentName"] == "lucky"
    assert actor["appearance"] == "A friendly goose with a blue scarf"
    owner_before = dynamo.users_table().get_item(Key={"userId": "owner"}, ConsistentRead=True)["Item"]
    headers = {"Authorization": "Bearer " + token}
    changed = {
        "agent_name": "lucky",
        "display_name": "Private project",
        "appearance": "Changed appearance",
        "personality": "Changed persona",
    }
    assert c.post("/town/agent/register", json=changed, headers=headers).status_code == 400
    changed["agent_name"] = "unapproved-extra"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: c.post("/town/agent/register", json=changed, headers=headers), range(2)))
    assert all(r.status_code == 403 for r in results)
    assert TownService().get_agent_by_name("owner", "unapproved-extra") is None
    assert TownService().get_town_agent_by_id(claims["aid"], consistent=True) == actor
    assert dynamo.users_table().get_item(Key={"userId": "owner"}, ConsistentRead=True)["Item"] == owner_before
    assert c.post(path + "/approve").status_code == 409
    assert "actor_id" not in c.get(path).json()
    # Ownership of the account is not authority over its other existing actors.
    other = dict(actor, id="other-owned-actor", agentName="other-owned")
    dynamo.agents_table().put_item(Item=other)
    identity = {"member_key": "a" * 52, "session_id": "a" * 12 + "/goosetown"}
    assert (
        c.post(
            "/town/runtime-join",
            headers=headers,
            json={**identity, "agent_name": "other-owned", "enrollment_token": credential["enrollment_token"]},
        ).status_code
        == 403
    )
    assert (
        c.post("/town/agents/other-owned-actor/runtime-session/challenge", headers=headers, json=identity).status_code
        == 403
    )
    assert c.get("/town/agent/status?agent_name=other-owned", headers=headers).status_code == 403
    result = c.post(
        "/town/ws-message",
        json={
            "connectionId": "scope-bypass",
            "principalId": "owner",
            "body": {"type": "town_agent_connect", "agent_name": "other-owned", "token": token},
        },
    )
    assert result.json()["statusCode"] == 403


def test_previous_binding_does_not_complete_new_device_arrival(device, monkeypatch):
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from core.town_token import verify_town_token
    from routers.device_join import record_arrival

    c, _, data, path = device
    monkeypatch.setattr(settings, "RUNTIME_SESSION_ENFORCEMENT_ENABLED", True)
    credential = redeem(device)
    claims = verify_town_token(credential["town_token"])
    actor_id = claims["aid"]
    key = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    member = base64.b32encode(key).decode().lower().rstrip("=")
    old = {"memberKey": member, "sessionId": member[:12] + "/old", "revision": 1}
    dynamo.agents_table().update_item(
        Key={"id": actor_id},
        UpdateExpression="SET runtimeBinding = :binding, bindingRevision = :rev, daemonConnectionId = :conn",
        ExpressionAttributeValues={":binding": old, ":rev": 1, ":conn": "old-connection"},
    )
    dynamo.connections_table().put_item(Item={"connectionId": "old-connection"})
    assert "actor_id" not in c.get(path).json()
    # A different machine cannot bind or create the arrival marker.
    other = "a" * 52
    response = c.post(
        "/town/runtime-join",
        headers={"Authorization": "Bearer " + credential["town_token"]},
        json={
            "agent_name": "lucky",
            "member_key": other,
            "session_id": other[:12] + "/new",
            "enrollment_token": credential["enrollment_token"],
        },
    )
    assert response.status_code == 409
    assert "actor_id" not in c.get(path).json()
    runtime = {
        "actor_id": actor_id,
        "device_id": "OTHER-CODE",
        "revision": 1,
        "session": old["sessionId"],
        "jti": "proof-session",
    }
    record_arrival(claims, runtime, "old-connection")
    assert "actor_id" not in c.get(path).json()
    runtime["device_id"] = data["user_code"]
    record_arrival(claims, runtime, "old-connection")
    assert c.get(path).json()["actor_id"] == actor_id
    dynamo.connections_table().delete_item(Key={"connectionId": "old-connection"})
    assert "actor_id" not in c.get(path).json()


def test_scoped_device_binds_and_arrives_only_after_its_proven_connection(device, monkeypatch):
    import base64
    from unittest.mock import AsyncMock, MagicMock

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from core.services.town_agent_ws import TownAgentWsManager
    from core.town_token import verify_town_token
    from routers.runtime_session import verify

    c, _, data, path = device
    credential = redeem(device)
    headers = {"Authorization": "Bearer " + credential["town_token"]}
    actor = verify_town_token(credential["town_token"])["aid"]
    private = Ed25519PrivateKey.generate()
    public = (
        base64.b32encode(private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
        .decode()
        .lower()
        .rstrip("=")
    )
    identity = {"member_key": public, "session_id": public[:12] + "/goosetown"}
    start = c.post(
        "/town/runtime-join",
        headers=headers,
        json={**identity, "agent_name": "lucky", "enrollment_token": credential["enrollment_token"]},
    )
    assert start.status_code == 200, start.text
    started = start.json()

    def proof(challenge):
        encoded = challenge["signing_bytes_b64url"]
        signature = private.sign(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        return {
            "challenge_id": challenge["challenge_id"],
            "signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
        }

    bound = c.post(
        f"/town/agents/{actor}/runtime-join/{started['request_id']}/complete",
        headers=headers,
        json={**proof(started["challenge"]), "poll_secret": started["poll_secret"]},
    )
    assert bound.status_code == 200, bound.text
    assert "actor_id" not in c.get(path).json()
    challenge = c.post(f"/town/agents/{actor}/runtime-session/challenge", headers=headers, json=identity)
    assert challenge.status_code == 200, challenge.text
    issued = c.post(f"/town/agents/{actor}/runtime-session/complete", headers=headers, json=proof(challenge.json()))
    assert issued.status_code == 200, issued.text
    runtime = issued.json()["runtime_session_token"]
    assert verify(runtime)["device_id"] == data["user_code"]
    assert "actor_id" not in c.get(path).json()
    manager = TownAgentWsManager()
    monkeypatch.setattr("routers.town.get_town_agent_ws_manager", lambda: manager)
    mailbox = MagicMock()
    mailbox.notify_connection.return_value = True
    monkeypatch.setattr("core.services.daemon_mailbox._singleton", mailbox)
    gs = MagicMock()
    gs.connect_agent = AsyncMock()
    monkeypatch.setattr("core.services.agent_connect_service.get_game_server_client", lambda: gs)
    monkeypatch.setattr("core.services.sprite_jobs.enqueue_signup", lambda *_: False)
    dynamo.connections_table().put_item(Item={"connectionId": "fresh-connection"})
    connected = c.post(
        "/town/ws-message",
        json={
            "connectionId": "fresh-connection",
            "principalId": "owner",
            "body": {
                "type": "town_agent_connect",
                "agent_name": "lucky",
                "token": credential["town_token"],
                "runtime_session_token": runtime,
            },
        },
    )
    assert connected.json()["statusCode"] == 200, connected.text
    assert c.get(path).json()["actor_id"] == actor
    rejected = c.post(
        "/town/ws-message",
        json={
            "connectionId": "fresh-connection",
            "principalId": "owner",
            "body": {"type": "town_agent_act", "action": "provider_off", "request_id": "deny-job-scope"},
        },
    )
    assert rejected.json()["statusCode"] == 403
    assert (
        c.post(
            "/town/exchange/upload-url",
            headers=headers,
            json={"job_id": "job", "filename": "private.txt", "content_length": 10},
        ).status_code
        == 403
    )
