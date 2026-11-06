# ruff: noqa: F811
import base64
from unittest.mock import AsyncMock, MagicMock

from core import dynamo
from core.config import settings
from core.services.town_agent_ws import TownAgentWsManager
from tests.unit.test_runtime_join import begin, endpoint, enrollment, proof  # noqa: F401, F811


def setup_bound(fixture):
    c, key, body, headers, _ = fixture
    start = begin(fixture).json()
    result = c.post(endpoint(start) + "/complete", json=proof(start, key), headers=headers)
    assert result.status_code == 200, result.text
    return c, key, body, headers


def session(fixture):
    c, key, body, headers = setup_bound(fixture)
    path = "/api/v1/town/agents/actor-1/runtime-session"
    data = {"member_key": body["member_key"], "session_id": body["session_id"]}
    first = c.post(path + "/challenge", json=data, headers=headers)
    assert first.status_code == 200, first.text
    ch = first.json()
    raw = base64.urlsafe_b64decode(ch["signing_bytes_b64url"] + "=" * (-len(ch["signing_bytes_b64url"]) % 4))
    sig = base64.urlsafe_b64encode(key.sign(raw)).decode().rstrip("=")
    complete = {"challenge_id": ch["challenge_id"], "signature": sig}
    response = c.post(path + "/complete", json=complete, headers=headers)
    assert response.status_code == 200, response.text
    return c, body, headers, path, complete, response.json()["runtime_session_token"]


def test_proof_replay_token_domain_and_unlink(enrollment, monkeypatch):
    monkeypatch.setattr(settings, "RUNTIME_SESSION_ENFORCEMENT_ENABLED", True)
    c, body, headers, path, complete, token = session(enrollment)
    assert token.startswith("gtrs_")
    assert c.post(path + "/complete", json=complete, headers=headers).status_code == 409
    from core.town_token import verify_town_token
    from routers.runtime_session import verify

    assert verify(token) is not None
    assert verify_town_token(token) is None
    assert verify(headers["Authorization"][7:]) is None
    dynamo.agents_table().update_item(
        Key={"id": "actor-1"},
        UpdateExpression="SET bindingRevision = :r REMOVE runtimeBinding",
        ExpressionAttributeValues={":r": 2},
    )
    assert verify(token) is None


def test_bad_signature_wrong_key_wrong_owner_and_expiry(enrollment, monkeypatch):
    monkeypatch.setattr(settings, "RUNTIME_SESSION_ENFORCEMENT_ENABLED", True)
    c, key, body, headers = setup_bound(enrollment)
    path = "/api/v1/town/agents/actor-1/runtime-session"
    assert (
        c.post(
            path + "/challenge", json={"member_key": "a" * 52, "session_id": "a" * 12 + "/visitor"}, headers=headers
        ).status_code
        == 403
    )
    ch = c.post(
        path + "/challenge", json={"member_key": body["member_key"], "session_id": body["session_id"]}, headers=headers
    ).json()
    assert (
        c.post(
            path + "/complete", json={"challenge_id": ch["challenge_id"], "signature": "a" * 86}, headers=headers
        ).status_code
        == 403
    )
    monkeypatch.setattr("routers.runtime_binding._now", lambda: ch["expires_at"])
    assert (
        c.post(
            path + "/complete", json={"challenge_id": ch["challenge_id"], "signature": "a" * 86}, headers=headers
        ).status_code
        == 409
    )


def test_flag_off_challenge_unavailable(enrollment):
    c, _, body, headers = setup_bound(enrollment)
    path = "/api/v1/town/agents/actor-1/runtime-session/challenge"
    assert (
        c.post(
            path, json={"member_key": body["member_key"], "session_id": body["session_id"]}, headers=headers
        ).status_code
        == 404
    )


def test_connected_action_rechecks_binding_and_storage_failure(enrollment, monkeypatch):
    monkeypatch.setattr(settings, "RUNTIME_SESSION_ENFORCEMENT_ENABLED", True)
    c, body, headers, _, _, token = session(enrollment)
    manager = TownAgentWsManager()
    mailbox = MagicMock()
    mailbox.notify_connection.return_value = True
    monkeypatch.setattr("routers.town.get_town_agent_ws_manager", lambda: manager)
    monkeypatch.setattr("core.services.daemon_mailbox._singleton", mailbox)
    gs = MagicMock()
    gs.connect_agent = AsyncMock()
    gs.send_command = AsyncMock()
    monkeypatch.setattr("core.services.agent_connect_service.get_game_server_client", lambda: gs)
    town_token = headers["Authorization"][7:]

    def msg(connection, body):
        return c.post(
            "/api/v1/town/ws-message", json={"connectionId": connection, "principalId": "owner", "body": body}
        )

    invalid = msg(
        "bad",
        {
            "type": "town_agent_connect",
            "agent_name": "visitor",
            "token": town_token,
            "runtime_session_token": "bad",  # nosec B105: deliberately malformed credential for denial test
        },
    )
    assert invalid.json()["statusCode"] == 401
    result = msg(
        "good",
        {"type": "town_agent_connect", "agent_name": "visitor", "token": town_token, "runtime_session_token": token},
    )
    assert result.json()["statusCode"] == 200, result.text
    accepted = msg("good", {"type": "town_agent_act", "action": "idle", "request_id": "req0"})
    assert accepted.json()["statusCode"] == 200
    assert any(
        call.args == ("good", {"event": "action_result", "request_id": "req0", "status": "accepted"})
        for call in mailbox.notify_connection.call_args_list
    )
    conn = manager.get_by_connection("good")
    conn.location_context = "transitioning"
    busy = msg("good", {"type": "town_agent_act", "action": "idle", "request_id": "busy1"})
    assert busy.json()["rejected"] == "transitioning"
    assert any(
        call.args
        == ("good", {"event": "action_result", "request_id": "busy1", "status": "rejected", "error": "action_rejected"})
        for call in mailbox.notify_connection.call_args_list
    )
    conn.location_context = "town"
    monkeypatch.setattr("routers.town._handle_context_transition", AsyncMock(return_value=False))
    failed = msg("good", {"type": "town_agent_act", "action": "move", "destination": "library", "request_id": "move1"})
    assert failed.json()["rejected"] == "transition_failed"
    assert any(
        call.args
        == ("good", {"event": "action_result", "request_id": "move1", "status": "rejected", "error": "action_rejected"})
        for call in mailbox.notify_connection.call_args_list
    )
    # A downstream transport error may be raised after delivery. It must not
    # be acknowledged as accepted or as a definite rejection without effect.
    gs.send_command.side_effect = ConnectionError("delivery outcome unknown")
    monkeypatch.setattr("routers.town.get_game_server_client", lambda: gs)
    uncertain = msg("good", {"type": "town_agent_act", "action": "move", "destination": "cafe", "request_id": "move2"})
    assert uncertain.json()["uncertain"] == "move_dispatch"
    assert any(
        call.args
        == (
            "good",
            {
                "event": "action_result",
                "request_id": "move2",
                "status": "uncertain",
                "error": "action_delivery_uncertain",
            },
        )
        for call in mailbox.notify_connection.call_args_list
    )
    gs.send_command.reset_mock()
    dynamo.agents_table().update_item(
        Key={"id": "actor-1"},
        UpdateExpression="SET bindingRevision = :r REMOVE runtimeBinding",
        ExpressionAttributeValues={":r": 2},
    )
    denied = msg("good", {"type": "town_agent_act", "action": "move", "destination": "cafe", "request_id": "req1"})
    assert denied.json()["statusCode"] == 401
    mailbox.disconnect.assert_called_with("good")
    assert any(
        call.args
        == (
            "good",
            {"event": "action_result", "request_id": "req1", "status": "rejected", "error": "invalid_runtime_session"},
        )
        for call in mailbox.notify_connection.call_args_list
    )
    assert manager.get_by_connection("good") is not None
    monkeypatch.setattr(settings, "WS_STRICT_VALIDATION", False)
    raw = msg("good", {"cmd": "move", "destination": "cafe"})
    assert raw.json()["statusCode"] == 401
    gs.send_command.assert_not_awaited()


def test_revocation_expiry_origin_and_database_failure(enrollment, monkeypatch):
    monkeypatch.setattr(settings, "RUNTIME_SESSION_ENFORCEMENT_ENABLED", True)
    c, body, headers, path, complete, token = session(enrollment)
    from routers.runtime_session import verify

    assert verify(token)
    monkeypatch.setattr(settings, "RUNTIME_BINDING_ORIGIN", "https://other.example")
    assert verify(token) is None
    monkeypatch.setattr(settings, "RUNTIME_BINDING_ORIGIN", "https://api-prod.goosetown.isol8.co")
    dynamo.users_table().update_item(
        Key={"userId": "owner"},
        UpdateExpression="SET tokenRevokedAt=:r",
        ExpressionAttributeValues={":r": 9999999999999},
    )
    assert verify(token) is None
    dynamo.users_table().update_item(Key={"userId": "owner"}, UpdateExpression="REMOVE tokenRevokedAt")

    class Broken:
        def get_item(self, **kwargs):
            raise RuntimeError("offline")

    monkeypatch.setattr(dynamo, "instances_table", lambda: Broken())
    assert verify(token) is None


def test_wrong_owner_and_superseded_challenge(enrollment, monkeypatch):
    monkeypatch.setattr(settings, "RUNTIME_SESSION_ENFORCEMENT_ENABLED", True)
    c, key, body, headers = setup_bound(enrollment)
    path = "/api/v1/town/agents/actor-1/runtime-session"
    data = {"member_key": body["member_key"], "session_id": body["session_id"]}
    assert c.post(path + "/challenge", json=data, headers={"Authorization": "Bearer invalid"}).status_code == 401
    old = c.post(path + "/challenge", json=data, headers=headers).json()
    new = c.post(path + "/challenge", json=data, headers=headers).json()

    def signed(ch):
        raw = base64.urlsafe_b64decode(ch["signing_bytes_b64url"] + "=" * (-len(ch["signing_bytes_b64url"]) % 4))
        return {
            "challenge_id": ch["challenge_id"],
            "signature": base64.urlsafe_b64encode(key.sign(raw)).decode().rstrip("="),
        }

    assert c.post(path + "/complete", json=signed(old), headers=headers).status_code == 409
    assert c.post(path + "/complete", json=signed(new), headers=headers).status_code == 200


def test_session_expires_and_wrong_owner_cannot_mint(enrollment, monkeypatch):
    import time

    from core.services.town_service import TownService
    from routers.runtime_session import verify

    monkeypatch.setattr(settings, "RUNTIME_SESSION_ENFORCEMENT_ENABLED", True)
    c, body, headers, path, _, token = session(enrollment)
    other = TownService().create_instance("other")
    other_headers = {"Authorization": "Bearer " + other["townToken"]}
    request = {"member_key": body["member_key"], "session_id": body["session_id"]}
    assert c.post(path + "/challenge", json=request, headers=other_headers).status_code == 404
    monkeypatch.setattr("routers.runtime_binding._now", lambda: int(time.time()) + 301)
    assert verify(token) is None
