"""Unit tests for ``_handle_agent_connect`` after migration to DaemonMailbox.

The test target is the seam-wiring proof — verify that the connect handler
delegates outbound WebSocket traffic (``connected`` event + kick-old
``close_connection``) to ``DaemonMailbox`` instead of constructing
``ManagementApiClient`` directly. The behaviour those calls produce is
already covered by ``test_daemon_mailbox.py``; here we validate that the
migration didn't accidentally bypass the mailbox.

Surfaced 2026-05-04 as architecture audit Candidate 5 (Phase 1 migration).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Patch targets — handler resolves these from its own module scope.
TOWN_SERVICE_PATH = "core.services.agent_connect_service.TownService"
GS_CLIENT_PATH = "core.services.agent_connect_service.get_game_server_client"
TOKEN_VERIFY_PATH = "core.town_token.verify_town_token"
MAILBOX_FACTORY_PATH = "core.services.daemon_mailbox.get_daemon_mailbox"


def _make_request(connection_id: str = "conn-new") -> SimpleNamespace:
    return SimpleNamespace(
        connectionId=connection_id,
        body={"token": "tok-X", "agent_name": "Lucky"},
    )


def _make_ws_manager_mock(existing_conn=None) -> MagicMock:
    mock = MagicMock()
    mock.get_by_agent_id.return_value = existing_conn
    return mock


def _agent_record() -> dict:
    return {
        "id": "agent-uuid",
        "displayName": "Lucky",
        "currentLocation": "plaza",
        "locationContext": "town",
        "spriteUrl": "",
        "spriteAnimations": {},
        "spriteReady": True,
        "mood": 5,
        "energy": 75,
        "onboardingPhase": "alive",
        "currentConversationId": "",
    }


@pytest.mark.asyncio
async def test_connect_routes_connected_event_through_mailbox():
    """The handler must call ``mailbox.notify_connection`` rather than
    ``ManagementApiClient.send_message`` directly. Closes the audit
    Candidate 5 deletion test for the connect path."""
    from core.services.agent_connect_service import _handle_agent_connect

    fake_mailbox = MagicMock()
    fake_mailbox.notify_connection.return_value = True

    svc_mock = MagicMock()
    svc_mock.get_agent_by_name.return_value = _agent_record()
    svc_mock.update_agent_state = MagicMock()
    svc_mock.update_agent_context = MagicMock()

    gs_mock = MagicMock()
    gs_mock.connect_agent = AsyncMock()

    with (
        patch(TOKEN_VERIFY_PATH, return_value={"uid": "u-1", "iid": "i-1"}),
        patch(TOWN_SERVICE_PATH, return_value=svc_mock),
        patch(GS_CLIENT_PATH, return_value=gs_mock),
        patch(MAILBOX_FACTORY_PATH, return_value=fake_mailbox),
    ):
        result = await _handle_agent_connect(_make_request(), _make_ws_manager_mock())

    assert result == {"statusCode": 200}

    # The mailbox saw a connected event addressed to the new connection id.
    assert fake_mailbox.notify_connection.called
    sent_conn_id, sent_event = fake_mailbox.notify_connection.call_args[0]
    assert sent_conn_id == "conn-new"
    assert sent_event["event"] == "connected"
    assert sent_event["agent"]["id"] == "agent-uuid"
    assert sent_event["agent"]["name"] == "Lucky"
    assert sent_event["agent"]["mood"] == 5
    assert sent_event["agent"]["energy"] == 75


@pytest.mark.asyncio
async def test_connect_kicks_old_connection_through_mailbox_disconnect():
    """When a stale daemon connection exists for the same agent, the
    handler must call ``mailbox.disconnect`` on the old connection id —
    not construct its own ``ManagementApiClient``. The kick is what
    prevents two daemons competing for messages on the same agent."""
    from core.services.agent_connect_service import _handle_agent_connect

    fake_mailbox = MagicMock()
    fake_mailbox.notify_connection.return_value = True

    svc_mock = MagicMock()
    svc_mock.get_agent_by_name.return_value = _agent_record()

    gs_mock = MagicMock()
    gs_mock.connect_agent = AsyncMock()

    # Stale connection for the same agent.
    existing_conn = SimpleNamespace(connection_id="conn-OLD")
    ws_mock = _make_ws_manager_mock(existing_conn=existing_conn)

    with (
        patch(TOKEN_VERIFY_PATH, return_value={"uid": "u-1", "iid": "i-1"}),
        patch(TOWN_SERVICE_PATH, return_value=svc_mock),
        patch(GS_CLIENT_PATH, return_value=gs_mock),
        patch(MAILBOX_FACTORY_PATH, return_value=fake_mailbox),
    ):
        await _handle_agent_connect(_make_request(connection_id="conn-NEW"), ws_mock)

    fake_mailbox.disconnect.assert_called_once_with("conn-OLD")


@pytest.mark.asyncio
async def test_connect_does_not_kick_when_connection_id_matches():
    """If the existing-connection lookup returns the SAME connection id as
    the incoming one (typical of an idempotent reconnect), don't kick —
    that would close our own socket."""
    from core.services.agent_connect_service import _handle_agent_connect

    fake_mailbox = MagicMock()
    fake_mailbox.notify_connection.return_value = True

    svc_mock = MagicMock()
    svc_mock.get_agent_by_name.return_value = _agent_record()

    gs_mock = MagicMock()
    gs_mock.connect_agent = AsyncMock()

    # Existing record with SAME connection id as the incoming request.
    same_conn = SimpleNamespace(connection_id="conn-NEW")
    ws_mock = _make_ws_manager_mock(existing_conn=same_conn)

    with (
        patch(TOKEN_VERIFY_PATH, return_value={"uid": "u-1", "iid": "i-1"}),
        patch(TOWN_SERVICE_PATH, return_value=svc_mock),
        patch(GS_CLIENT_PATH, return_value=gs_mock),
        patch(MAILBOX_FACTORY_PATH, return_value=fake_mailbox),
    ):
        await _handle_agent_connect(_make_request(connection_id="conn-NEW"), ws_mock)

    fake_mailbox.disconnect.assert_not_called()


@pytest.mark.asyncio
async def test_connect_returns_400_without_token():
    from core.services.agent_connect_service import _handle_agent_connect

    request = SimpleNamespace(
        connectionId="conn-x",
        body={"token": "", "agent_name": "Lucky"},
    )
    fake_mailbox = MagicMock()
    with patch(MAILBOX_FACTORY_PATH, return_value=fake_mailbox):
        result = await _handle_agent_connect(request, _make_ws_manager_mock())

    assert result["statusCode"] == 400
    assert result["error"] == "missing_token_or_agent_name"
    fake_mailbox.notify_connection.assert_not_called()


@pytest.mark.asyncio
async def test_connect_returns_401_on_invalid_token():
    from core.services.agent_connect_service import _handle_agent_connect

    fake_mailbox = MagicMock()
    with (
        patch(TOKEN_VERIFY_PATH, return_value=None),  # invalid
        patch(MAILBOX_FACTORY_PATH, return_value=fake_mailbox),
    ):
        result = await _handle_agent_connect(_make_request(), _make_ws_manager_mock())

    assert result["statusCode"] == 401
    assert result["error"] == "invalid_token"
    fake_mailbox.notify_connection.assert_not_called()


@pytest.mark.asyncio
async def test_connect_returns_404_when_agent_not_found():
    from core.services.agent_connect_service import _handle_agent_connect

    fake_mailbox = MagicMock()
    svc_mock = MagicMock()
    svc_mock.get_agent_by_name.return_value = None  # agent missing

    with (
        patch(TOKEN_VERIFY_PATH, return_value={"uid": "u-1", "iid": "i-1"}),
        patch(TOWN_SERVICE_PATH, return_value=svc_mock),
        patch(MAILBOX_FACTORY_PATH, return_value=fake_mailbox),
    ):
        result = await _handle_agent_connect(_make_request(), _make_ws_manager_mock())

    assert result["statusCode"] == 404
    assert result["error"] == "agent_not_found"
    fake_mailbox.notify_connection.assert_not_called()
