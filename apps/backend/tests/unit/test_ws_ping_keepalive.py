"""The ws-message ``ping`` handler answers end-to-end so the daemon can detect a
half-open connection (API Gateway alive but backend unregistered).

- registered connection  → push {"event":"pong"}
- unregistered connection → push {"event":"reconnect"}

This is what lets an idle agent recover after a backend redeploy: its WS to API
Gateway stays open, but the new backend task has no registration for it, so the
ping gets a ``reconnect`` and the daemon re-sends town_agent_connect.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _request():
    from routers.town import WsMessageRequest

    return WsMessageRequest(
        connectionId="conn-xyz",
        principalId="user-1",
        body={"type": "ping"},
    )


@pytest.fixture
def mailbox():
    mb = MagicMock()
    mb.notify_connection.return_value = True
    with patch("core.services.daemon_mailbox.get_daemon_mailbox", return_value=mb):
        yield mb


def _raw():
    from fastapi import Request

    r = MagicMock(spec=Request)
    r.headers = {}
    return r


@pytest.mark.asyncio
async def test_ping_registered_connection_pongs(mailbox):
    from routers.town import handle_ws_message

    ws_manager = MagicMock()
    ws_manager.get_by_connection.return_value = SimpleNamespace(agent_id="a1")  # registered

    with patch("routers.town.get_town_agent_ws_manager", return_value=ws_manager):
        result = await handle_ws_message(_request(), _raw())

    assert result == {"statusCode": 200}
    mailbox.notify_connection.assert_called_once_with("conn-xyz", {"event": "pong"})


@pytest.mark.asyncio
async def test_ping_unregistered_connection_asks_reconnect(mailbox):
    from routers.town import handle_ws_message

    ws_manager = MagicMock()
    ws_manager.get_by_connection.return_value = None  # NOT registered

    with patch("routers.town.get_town_agent_ws_manager", return_value=ws_manager):
        result = await handle_ws_message(_request(), _raw())

    assert result == {"statusCode": 200}
    mailbox.notify_connection.assert_called_once_with("conn-xyz", {"event": "reconnect"})
