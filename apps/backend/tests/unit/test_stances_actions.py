"""Tests for describe_stance action dispatch in town_agent_act handler.

Uses the same WsMessageRequest + handle_ws_message invocation pattern as
test_goals_actions.py — call the async handler directly with mocked
ws_manager and stances_service, no HTTP round-trip.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_conn(agent_id: str = "alice-123", user_id: str = "user-1") -> SimpleNamespace:
    """Minimal AgentConnection-like object with fields the handler reads."""
    return SimpleNamespace(
        agent_id=agent_id,
        runtime_session_claims=None,
        user_id=user_id,
        agent_name="alice",
        location_context="town",
        exhausted_rejections=0,
        leaving_cleanly=False,
    )


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def handler_setup():
    """Patch the collaborators that handle_ws_message resolves at dispatch time.

    Yields a SimpleNamespace with:
    - ws_manager: mocked ws manager whose get_by_connection returns _make_conn()
    - agent_conn: the AgentConnection stub
    - mock_stances: patched routers.town.stances_service module
    """
    agent_conn = _make_conn()
    ws_manager = MagicMock()
    ws_manager.get_by_connection.return_value = agent_conn

    mock_stances = MagicMock()

    with (
        patch("routers.town.get_town_agent_ws_manager", return_value=ws_manager),
        patch(
            "routers.town.TownService",
            return_value=MagicMock(
                **{"get_town_agent_by_id.return_value": {"energy": 80, "mood": 5}},
            ),
        ),
        patch("routers.town.stances_service", mock_stances),
        patch("routers.town.get_game_server_client", return_value=MagicMock()),
    ):
        yield SimpleNamespace(
            ws_manager=ws_manager,
            agent_conn=agent_conn,
            mock_stances=mock_stances,
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_describe_stance_dispatches_to_service(handler_setup):
    """The town_agent_act handler routes describe_stance to stances_service.describe_stance."""
    from fastapi import Request

    from routers.town import WsMessageRequest, handle_ws_message

    handler_setup.mock_stances.describe_stance.return_value = {"status": "ok", "stance": "kind but distant"}

    request = WsMessageRequest(
        connectionId="conn-abc",
        principalId="user-1",
        body={
            "type": "town_agent_act",
            "action": "describe_stance",
            "target_agent_id": "bob-456",
            "text": "kind but distant",
        },
    )
    raw = MagicMock(spec=Request)
    raw.headers = {}

    result = await handle_ws_message(request, raw)

    assert result["statusCode"] == 200
    handler_setup.mock_stances.describe_stance.assert_called_once_with(
        agent_id="alice-123",
        target_agent_id="bob-456",
        text="kind but distant",
    )


@pytest.mark.asyncio
async def test_describe_stance_returns_service_result(handler_setup):
    """The handler's response body comes from stances_service.describe_stance's return value."""
    from fastapi import Request

    from routers.town import WsMessageRequest, handle_ws_message

    service_result = {"status": "ok", "stance": "curious and warm, always willing to help"}
    handler_setup.mock_stances.describe_stance.return_value = service_result

    request = WsMessageRequest(
        connectionId="conn-abc",
        principalId="user-1",
        body={
            "type": "town_agent_act",
            "action": "describe_stance",
            "target_agent_id": "bob-456",
            "text": "curious and warm, always willing to help",
        },
    )
    raw = MagicMock(spec=Request)
    raw.headers = {}

    result = await handle_ws_message(request, raw)

    assert result == {"statusCode": 200, "body": service_result}


_ = asyncio  # referenced by pytest.mark.asyncio markers indirectly
