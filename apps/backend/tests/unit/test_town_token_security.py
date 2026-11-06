import base64
import hashlib
import hmac
import importlib.util
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.town_token import _get_town_token_secret, verify_town_token


def token(claims):
    data = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    sig = hmac.new(_get_town_token_secret().encode(), data.encode(), hashlib.sha256).digest()
    return data + "." + base64.urlsafe_b64encode(sig).rstrip(b"=").decode()


@pytest.fixture
def ws(monkeypatch):
    path = Path(__file__).resolve().parents[3] / "infra/lambda/ws-authorizer/index.py"
    spec = importlib.util.spec_from_file_location("town_auth_security_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "changes,valid",
    [
        ({}, True),
        ({"iat": None}, False),
        ({"exp": None}, False),
        ({"iat": True}, False),
        ({"exp": "9999999999"}, False),
        ({"iat": 4102444800}, False),
        ({"exp": 1}, False),
    ],
)
def test_town_claims_parity(ws, changes, valid):
    now = int(time.time())
    claims = {"uid": "user", "iid": "instance", "iat": now - 1, "exp": now + 60}
    claims.update(changes)
    t = token(claims)
    with patch("core.services.user_service.UserService.is_token_revoked", return_value=False):
        assert (verify_town_token(t) is not None) is valid
    assert (ws._verify_town_token(t, _get_town_token_secret()) is not None) is valid


def test_backend_fails_closed_on_store_outage():
    now = int(time.time())
    t = token({"uid": "user", "iid": "instance", "iat": now, "exp": now + 60})
    with patch("core.services.user_service.UserService.is_token_revoked", side_effect=RuntimeError("outage")):
        assert verify_town_token(t) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("iid", ["viewer", "anonymous-viewer"])
async def test_viewer_token_rejected_by_actual_connect_router(iid):
    from routers.town import WsMessageRequest, handle_ws_message

    now = int(time.time())
    t = token({"uid": "user", "iid": iid, "iat": now, "exp": now + 60})
    req = WsMessageRequest(
        connectionId="conn", principalId="user", body={"type": "town_agent_connect", "token": t, "agent_name": "Goose"}
    )
    with (
        patch("core.services.user_service.UserService.is_token_revoked", return_value=False),
        patch("routers.town.get_town_agent_ws_manager", return_value=MagicMock()),
        patch("core.services.daemon_mailbox.get_daemon_mailbox"),
    ):
        assert (await handle_ws_message(req, MagicMock()))["statusCode"] == 401


@pytest.mark.parametrize("iid,uid", [("instance", "user"), ("viewer", "user"), ("anonymous-viewer", "anonymous")])
def test_ws_authorizer_accepts_valid_agent_and_viewers(ws, monkeypatch, iid, uid):
    now = int(time.time())
    t = token({"uid": uid, "iid": iid, "iat": now - 1, "exp": now + 60})
    monkeypatch.setenv("TOWN_TOKEN_SECRET", "secret-name")
    monkeypatch.setattr(ws, "_get_secret", lambda _: _get_town_token_secret())
    monkeypatch.setattr(ws, "_town_token_not_revoked", lambda _: True)
    event = {
        "methodArn": "arn:aws:execute-api:us-east-1:123:api/stage/$connect",
        "headers": {"Authorization": "Bearer " + t},
    }
    assert ws.handler(event, None)["policyDocument"]["Statement"][0]["Effect"] == "Allow"


@pytest.mark.parametrize("revocation", [True, RuntimeError("store outage")])
def test_ws_authorizer_denies_revocation_and_store_outage(ws, monkeypatch, revocation):
    now = int(time.time())
    t = token({"uid": "user", "iid": "instance", "iat": now - 1, "exp": now + 60})
    monkeypatch.setenv("TOWN_TOKEN_SECRET", "secret-name")
    monkeypatch.setattr(ws, "_get_secret", lambda _: _get_town_token_secret())

    def check(_):
        if isinstance(revocation, Exception):
            raise revocation
        return False

    monkeypatch.setattr(ws, "_town_token_not_revoked", check)
    event = {
        "methodArn": "arn:aws:execute-api:us-east-1:123:api/stage/$connect",
        "headers": {"Authorization": "Bearer " + t},
    }
    assert ws.handler(event, None)["policyDocument"]["Statement"][0]["Effect"] == "Deny"


def test_ws_revocation_lookup_uses_consistent_read(ws, monkeypatch):
    client = MagicMock()
    monkeypatch.setenv("USERS_TABLE", "users")
    monkeypatch.setattr(ws.boto3, "client", lambda _: client)
    payload = {"uid": "user", "iid": "instance", "iat": 100}
    client.get_item.return_value = {"Item": {"tokenRevokedAt": {"N": "100001"}}}
    assert ws._town_token_not_revoked(payload) is False
    assert client.get_item.call_args.kwargs["ConsistentRead"] is True
    client.get_item.return_value = {"Item": {"tokenRevokedAt": {"N": "99000"}}}
    assert ws._town_token_not_revoked(payload) is True


@pytest.mark.asyncio
async def test_viewer_can_subscribe_through_router():
    from routers.town import WsMessageRequest, handle_ws_message

    req = WsMessageRequest(connectionId="conn", principalId="user", body={"type": "town_subscribe"})
    relay = MagicMock()
    with (
        patch("routers.town.get_town_agent_ws_manager", return_value=MagicMock()),
        patch("core.services.viewer_relay.get_viewer_relay", return_value=relay),
    ):
        assert (await handle_ws_message(req, MagicMock()))["statusCode"] == 200
    relay.register_viewer.assert_called_once_with("conn")


@pytest.mark.asyncio
async def test_valid_agent_reaches_actual_connect_router():
    from routers.town import WsMessageRequest, handle_ws_message

    now = int(time.time())
    t = token({"uid": "user", "iid": "instance", "iat": now - 1, "exp": now + 60})
    req = WsMessageRequest(
        connectionId="conn", principalId="user", body={"type": "town_agent_connect", "token": t, "agent_name": "Goose"}
    )
    service = MagicMock()
    service.get_agent_by_name.return_value = {
        "id": "agent-1",
        "displayName": "Goose",
        "spriteReady": True,
        "onboardingPhase": "alive",
    }
    manager = MagicMock()
    manager.get_by_agent_id.return_value = None
    game = MagicMock()
    game.connect_agent = AsyncMock()
    with (
        patch("core.services.user_service.UserService.is_token_revoked", return_value=False),
        patch("routers.town.get_town_agent_ws_manager", return_value=manager),
        patch("core.services.daemon_mailbox.get_daemon_mailbox"),
        patch("core.services.agent_connect_service.TownService", return_value=service),
        patch("core.services.agent_connect_service.get_game_server_client", return_value=game),
    ):
        result = await handle_ws_message(req, MagicMock())
    assert result == {"statusCode": 200}
    manager.register.assert_called_once()
    service.get_agent_by_name.assert_called_once_with("user", "Goose")
