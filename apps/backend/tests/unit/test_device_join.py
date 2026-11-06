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
    dynamodb_tables.create_table(
        TableName=dynamo.connections_table().name,
        KeySchema=[{"AttributeName": "connectionId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "connectionId", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    monkeypatch.setattr(settings, "RUNTIME_BINDING_ENABLED", True)
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
