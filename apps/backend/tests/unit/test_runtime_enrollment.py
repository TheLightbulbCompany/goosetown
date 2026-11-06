import base64
import hashlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import dynamo
from core.auth import AuthContext, get_current_user
from core.config import settings
from core.runtime_enrollment import EnrollmentStateChanged, mint_join_token
from core.runtime_origin import audience
from core.services.town_service import TownService
from routers.town import router


def unpack(token):
    assert token.startswith("gtj_")
    data = token[4:]
    return json.loads(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)))


def test_mint_scoped_expiring_and_replaced(dynamodb_tables, monkeypatch):
    instance = TownService().create_instance("owner")
    monkeypatch.setattr("core.runtime_enrollment.time.time", lambda: 1000)
    joined = mint_join_token(instance["id"], "owner", instance["townToken"])
    payload = unpack(joined)
    assert payload["town_token"] == instance["townToken"]
    secret = payload["enrollment_token"]
    assert len(secret) == 43
    row = dynamo.instances_table().get_item(Key={"id": instance["id"]}, ConsistentRead=True)["Item"]
    assert row["runtimeEnrollment"] == {
        "ownerId": "owner",
        "secretHash": hashlib.sha256(secret.encode()).hexdigest(),
        "expiresAt": 1600,
        "audience": audience(),
    }
    second = unpack(mint_join_token(instance["id"], "owner", instance["townToken"]))
    assert second["enrollment_token"] != secret
    row = dynamo.instances_table().get_item(Key={"id": instance["id"]}, ConsistentRead=True)["Item"]
    assert row["runtimeEnrollment"]["secretHash"] == hashlib.sha256(second["enrollment_token"].encode()).hexdigest()
    with pytest.raises(EnrollmentStateChanged):
        mint_join_token(instance["id"], "other", instance["townToken"])


def test_account_endpoint_flag_and_owner(dynamodb_tables, monkeypatch):
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/town")
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="owner")
    client = TestClient(app)
    monkeypatch.setattr(settings, "RUNTIME_BINDING_ENABLED", False)
    response = client.post("/api/v1/town/instance")
    assert response.status_code == 200
    legacy = response.json()
    assert set(legacy) == {"town_token", "apartment_unit", "agents"}
    instance = TownService().get_active_instance("owner")
    row = dynamo.instances_table().get_item(Key={"id": instance["id"]})["Item"]
    assert "runtimeEnrollment" not in row
    monkeypatch.setattr(settings, "RUNTIME_BINDING_ENABLED", True)
    response = client.post("/api/v1/town/instance")
    assert response.status_code == 200
    payload = unpack(response.json()["join_token"])
    assert payload["town_token"] == response.json()["town_token"]
    enrollment = dynamo.instances_table().get_item(Key={"id": instance["id"]})["Item"]["runtimeEnrollment"]
    assert enrollment["ownerId"] == "owner"
    app.dependency_overrides.clear()
    assert client.post("/api/v1/town/instance").status_code in (401, 403)


def test_onboarding_refreshes_expired_and_revoked_town_token(dynamodb_tables, monkeypatch):
    from core.town_token import sign_town_token, verify_town_token

    app = FastAPI()
    app.include_router(router, prefix="/api/v1/town")
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="owner")
    client = TestClient(app)
    instance = TownService().create_instance("owner")
    old = sign_town_token("owner", instance["id"], issued_at=1, ttl_seconds=1)
    dynamo.instances_table().update_item(
        Key={"id": instance["id"]}, UpdateExpression="SET townToken = :token", ExpressionAttributeValues={":token": old}
    )
    dynamo.users_table().put_item(Item={"userId": "owner", "tokenRevokedAt": 2_000})
    monkeypatch.setattr(settings, "RUNTIME_BINDING_ENABLED", True)
    response = client.post("/api/v1/town/instance")
    assert response.status_code == 200
    fresh = unpack(response.json()["join_token"])["town_token"]
    assert fresh != old and verify_town_token(fresh)["iat"] * 1000 >= 2_000
    assert dynamo.instances_table().get_item(Key={"id": instance["id"]})["Item"]["townToken"] == fresh


def test_same_second_revocation_waits_for_safe_issue_time(dynamodb_tables, monkeypatch):
    import time

    app = FastAPI()
    app.include_router(router, prefix="/api/v1/town")
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="owner")
    TownService().create_instance("owner")
    dynamo.users_table().put_item(Item={"userId": "owner", "tokenRevokedAt": int(time.time()) * 1000 + 999})
    monkeypatch.setattr(settings, "RUNTIME_BINDING_ENABLED", True)
    response = TestClient(app).post("/api/v1/town/instance")
    assert response.status_code == 200
    from core.town_token import verify_town_token

    fresh = verify_town_token(unpack(response.json()["join_token"])["town_token"])
    revoked = dynamo.users_table().get_item(Key={"userId": "owner"})["Item"]["tokenRevokedAt"]
    assert fresh and fresh["iat"] * 1000 >= revoked


def test_repeated_onboarding_preserves_valid_town_token_and_rotates_grant(dynamodb_tables, monkeypatch):
    import time

    app = FastAPI()
    app.include_router(router, prefix="/api/v1/town")
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="owner")
    client = TestClient(app)
    monkeypatch.setattr(settings, "RUNTIME_BINDING_ENABLED", True)
    first = client.post("/api/v1/town/instance")
    assert first.status_code == 200
    clock = int(time.time())
    monkeypatch.setattr("time.time", lambda: clock + 2)
    second = client.post("/api/v1/town/instance")
    assert second.status_code == 200
    one, two = unpack(first.json()["join_token"]), unpack(second.json()["join_token"])
    assert one["town_token"] == two["town_token"]
    assert one["enrollment_token"] != two["enrollment_token"]
    assert TownService().get_instance_by_token(one["town_token"]) is not None


def test_stale_token_cannot_mint_grant(dynamodb_tables):
    instance = TownService().create_instance("owner")
    dynamo.instances_table().update_item(
        Key={"id": instance["id"]}, UpdateExpression="SET townToken = :new", ExpressionAttributeValues={":new": "new"}
    )
    with pytest.raises(EnrollmentStateChanged):
        mint_join_token(instance["id"], "owner", instance["townToken"])
    row = dynamo.instances_table().get_item(Key={"id": instance["id"]}, ConsistentRead=True)["Item"]
    assert row["townToken"] == "new" and "runtimeEnrollment" not in row


def test_refresh_loses_compare_and_swap_without_overwriting(dynamodb_tables, monkeypatch):
    from core.town_token import sign_town_token

    instance = TownService().create_instance("owner")
    stale = sign_town_token("owner", instance["id"], issued_at=1, ttl_seconds=1)
    dynamo.instances_table().update_item(
        Key={"id": instance["id"]}, UpdateExpression="SET townToken = :t", ExpressionAttributeValues={":t": stale}
    )
    table = dynamo.instances_table()
    client = dynamo.get_client().meta.client
    original = client.transact_write_items

    def race(**kwargs):
        if "TransactItems" in kwargs:
            table.update_item(
                Key={"id": instance["id"]},
                UpdateExpression="SET townToken = :new",
                ExpressionAttributeValues={":new": "winner"},
            )
        return original(**kwargs)

    monkeypatch.setattr("core.dynamo.instances_table", lambda: table)
    monkeypatch.setattr(client, "transact_write_items", race)
    monkeypatch.setattr(settings, "RUNTIME_BINDING_ENABLED", True)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/town")
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="owner")
    assert TestClient(app).post("/api/v1/town/instance").status_code == 503
    row = table.get_item(Key={"id": instance["id"]}, ConsistentRead=True)["Item"]
    assert row["townToken"] == "winner" and "runtimeEnrollment" not in row
