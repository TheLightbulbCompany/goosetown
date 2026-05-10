"""Tests for set_goal / complete_goal action dispatch in town_agent_act handler.

Uses the same WsMessageRequest + handle_ws_message invocation pattern as
test_energy_and_sprite_url.py::TestEnergySoftGate — call the async handler
directly with mocked ws_manager and goals_service, no HTTP round-trip.
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
    - mock_goals: patched routers.town.goals_service module
    """
    agent_conn = _make_conn()
    ws_manager = MagicMock()
    ws_manager.get_by_connection.return_value = agent_conn

    # goals_service is a module; mock its functions individually.
    mock_goals = MagicMock()

    with (
        patch("routers.town.get_town_agent_ws_manager", return_value=ws_manager),
        # Energy gate calls TownService; return an agent with energy > 0
        # so it doesn't block the path before the action dispatcher.
        patch(
            "routers.town.TownService",
            return_value=MagicMock(
                **{"get_town_agent_by_id.return_value": {"energy": 80, "mood": 5}},
            ),
        ),
        patch("routers.town.goals_service", mock_goals),
        # game server client is looked up before the elif chain — give it a no-op.
        patch("routers.town.get_game_server_client", return_value=MagicMock()),
    ):
        yield SimpleNamespace(
            ws_manager=ws_manager,
            agent_conn=agent_conn,
            mock_goals=mock_goals,
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_goal_dispatches_to_service(handler_setup):
    """The town_agent_act handler routes set_goal to goals_service.set_goal."""
    from fastapi import Request

    from routers.town import WsMessageRequest, handle_ws_message

    handler_setup.mock_goals.set_goal.return_value = {"status": "ok", "currentGoal": "test goal"}

    request = WsMessageRequest(
        connectionId="conn-abc",
        principalId="user-1",
        body={"type": "town_agent_act", "action": "set_goal", "text": "test goal"},
    )
    raw = MagicMock(spec=Request)
    raw.headers = {}

    result = await handle_ws_message(request, raw)

    assert result["statusCode"] == 200
    handler_setup.mock_goals.set_goal.assert_called_once_with(agent_id="alice-123", text="test goal")


@pytest.mark.asyncio
async def test_complete_goal_dispatches_to_service(handler_setup):
    """The town_agent_act handler routes complete_goal to goals_service.complete_goal."""
    from fastapi import Request

    from routers.town import WsMessageRequest, handle_ws_message

    handler_setup.mock_goals.complete_goal.return_value = {
        "status": "ok",
        "completed_goal_id": "g1",
        "goal_text": "test goal",
    }

    request = WsMessageRequest(
        connectionId="conn-abc",
        principalId="user-1",
        body={"type": "town_agent_act", "action": "complete_goal", "reflection": "did it"},
    )
    raw = MagicMock(spec=Request)
    raw.headers = {}

    result = await handle_ws_message(request, raw)

    assert result["statusCode"] == 200
    handler_setup.mock_goals.complete_goal.assert_called_once_with(agent_id="alice-123", reflection="did it")


@pytest.mark.asyncio
async def test_set_goal_returns_service_result(handler_setup):
    """The handler's response body comes from goals_service.set_goal's return value."""
    from fastapi import Request

    from routers.town import WsMessageRequest, handle_ws_message

    service_result = {"status": "ok", "currentGoal": "become a librarian"}
    handler_setup.mock_goals.set_goal.return_value = service_result

    request = WsMessageRequest(
        connectionId="conn-abc",
        principalId="user-1",
        body={"type": "town_agent_act", "action": "set_goal", "text": "become a librarian"},
    )
    raw = MagicMock(spec=Request)
    raw.headers = {}

    result = await handle_ws_message(request, raw)

    assert result == {"statusCode": 200, "body": service_result}


@pytest.mark.asyncio
async def test_complete_goal_returns_service_result(handler_setup):
    """The handler's response body comes from goals_service.complete_goal's return value."""
    from fastapi import Request

    from routers.town import WsMessageRequest, handle_ws_message

    service_result = {
        "status": "ok",
        "completed_goal_id": "goal-uuid-xyz",
        "goal_text": "become a librarian",
    }
    handler_setup.mock_goals.complete_goal.return_value = service_result

    request = WsMessageRequest(
        connectionId="conn-abc",
        principalId="user-1",
        body={"type": "town_agent_act", "action": "complete_goal", "reflection": "done"},
    )
    raw = MagicMock(spec=Request)
    raw.headers = {}

    result = await handle_ws_message(request, raw)

    assert result == {"statusCode": 200, "body": service_result}


@pytest.mark.asyncio
async def test_set_goal_reflection_not_required_for_set(handler_setup):
    """set_goal only reads 'text' from the body — extra fields (like reflection)
    are silently ignored and do not reach goals_service.set_goal."""
    from fastapi import Request

    from routers.town import WsMessageRequest, handle_ws_message

    handler_setup.mock_goals.set_goal.return_value = {"status": "ok", "currentGoal": "test"}

    request = WsMessageRequest(
        connectionId="conn-abc",
        principalId="user-1",
        body={
            "type": "town_agent_act",
            "action": "set_goal",
            "text": "test",
            "reflection": "should be ignored",
        },
    )
    raw = MagicMock(spec=Request)
    raw.headers = {}

    await handle_ws_message(request, raw)

    # Only text was forwarded — reflection must NOT appear in the call.
    handler_setup.mock_goals.set_goal.assert_called_once_with(agent_id="alice-123", text="test")


_ = asyncio  # referenced by pytest.mark.asyncio markers indirectly
