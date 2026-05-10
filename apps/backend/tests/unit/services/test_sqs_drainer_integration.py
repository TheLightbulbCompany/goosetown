"""Integration tests for sqs_drainer hooks in agent lifecycle.

Verifies that:
  - register_agent calls sqs_drainer.create_agent_queue (best-effort)
  - deactivate_agent calls sqs_drainer.delete_agent_queue (best-effort)
  - _handle_agent_connect schedules an asyncio drain task on connect

These tests target the wiring points, not the SQS behaviour itself (which is
covered in test_sqs_drainer.py).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_register_request(agent_name="test-agent", display_name="Test Agent", appearance="pixel art person"):
    """Minimal AgentRegisterRequest-like object for register_agent tests."""
    req = MagicMock()
    req.agent_name = agent_name
    req.display_name = display_name
    req.personality = "friendly"
    req.appearance = appearance
    return req


# ---------------------------------------------------------------------------
# register_agent → create_agent_queue
# ---------------------------------------------------------------------------


@patch("core.services.sqs_drainer.create_agent_queue")
@patch("core.services.sprite_queue.get_sprite_queue")
@patch("core.services.user_service.UserService")
@patch("routers.town.TownService")
def test_register_agent_calls_create_agent_queue(
    mock_town_service_cls,
    mock_user_service_cls,
    mock_get_sprite_queue,
    mock_create_queue,
):
    """register_agent should call sqs_drainer.create_agent_queue after opt_in."""
    from routers.town import register_agent

    agent_id = "agent-uuid-1234"
    service = MagicMock()
    service.get_active_instance.return_value = {"id": "instance-1"}
    service.get_agent_by_name.return_value = None  # not yet registered
    service.opt_in.return_value = {
        "id": agent_id,
        "agentName": "test-agent",
        "displayName": "Test Agent",
        "isActive": "true",
    }
    mock_town_service_cls.return_value = service

    # Sprite quota doesn't matter here
    user_service = MagicMock()
    mock_user_service_cls.return_value = user_service

    # No sprite generation (no api key)
    with patch("core.config.settings") as mock_settings:
        mock_settings.PIXELLAB_API_KEY = None
        mock_settings.SPRITE_S3_BUCKET = None
        mock_settings.SPRITE_CDN_URL = None

        token_info = ("user-123", "mock-token")
        result = asyncio.run(register_agent(request=_make_register_request(), token_info=token_info))

    mock_create_queue.assert_called_once_with(agent_id)
    assert result["agent_id"] == agent_id


@patch("core.services.sqs_drainer.create_agent_queue")
@patch("routers.town.TownService")
def test_register_agent_does_not_fail_if_create_queue_raises(
    mock_town_service_cls,
    mock_create_queue,
):
    """register_agent must succeed even if create_agent_queue raises."""
    from routers.town import register_agent

    mock_create_queue.side_effect = RuntimeError("SQS unavailable")

    agent_id = "agent-uuid-5678"
    service = MagicMock()
    service.get_active_instance.return_value = {"id": "instance-1"}
    service.get_agent_by_name.return_value = None
    service.opt_in.return_value = {
        "id": agent_id,
        "agentName": "test-agent",
        "displayName": "Test Agent",
        "isActive": "true",
    }
    mock_town_service_cls.return_value = service

    # Pass appearance="" to skip the sprite-quota branch which would hit DynamoDB
    token_info = ("user-123", "mock-token")
    result = asyncio.run(
        register_agent(
            request=_make_register_request(appearance=""),
            token_info=token_info,
        )
    )

    # Registration still succeeded
    assert result["agent_id"] == agent_id


# ---------------------------------------------------------------------------
# deactivate_agent → delete_agent_queue
# ---------------------------------------------------------------------------


@patch("core.services.sqs_drainer.delete_agent_queue")
@patch("core.services.game_server_client.get_game_server_client")
@patch("core.services.town_agent_ws.get_town_agent_ws_manager")
@patch("routers.town.TownService")
def test_deactivate_agent_calls_delete_agent_queue(
    mock_town_service_cls,
    mock_get_ws_manager,
    mock_get_gs_client,
    mock_delete_queue,
):
    """deactivate_agent should call sqs_drainer.delete_agent_queue after despawn."""
    agent_id = "agent-uuid-abcd"

    service = MagicMock()
    service.get_town_agent_by_id.return_value = {
        "id": agent_id,
        "userId": "user-123",
        "isActive": "true",
    }
    mock_town_service_cls.return_value = service

    ws_manager = MagicMock()
    mock_get_ws_manager.return_value = ws_manager

    gs_client = MagicMock()
    gs_client.disconnect_agent = AsyncMock()
    mock_get_gs_client.return_value = gs_client

    auth = MagicMock()
    auth.user_id = "user-123"

    from routers.town import deactivate_agent

    asyncio.run(deactivate_agent(agent_id=agent_id, auth=auth))

    mock_delete_queue.assert_called_once_with(agent_id)


@patch("core.services.sqs_drainer.delete_agent_queue")
@patch("core.services.game_server_client.get_game_server_client")
@patch("core.services.town_agent_ws.get_town_agent_ws_manager")
@patch("routers.town.TownService")
def test_deactivate_agent_does_not_fail_if_delete_queue_raises(
    mock_town_service_cls,
    mock_get_ws_manager,
    mock_get_gs_client,
    mock_delete_queue,
):
    """deactivate_agent must succeed even if delete_agent_queue raises."""
    mock_delete_queue.side_effect = RuntimeError("SQS unavailable")

    agent_id = "agent-uuid-efgh"
    service = MagicMock()
    service.get_town_agent_by_id.return_value = {
        "id": agent_id,
        "userId": "user-123",
        "isActive": "true",
    }
    mock_town_service_cls.return_value = service

    ws_manager = MagicMock()
    mock_get_ws_manager.return_value = ws_manager

    gs_client = MagicMock()
    gs_client.disconnect_agent = AsyncMock()
    mock_get_gs_client.return_value = gs_client

    auth = MagicMock()
    auth.user_id = "user-123"

    from routers.town import deactivate_agent

    # Should not raise
    result = asyncio.run(deactivate_agent(agent_id=agent_id, auth=auth))
    assert result["id"] == agent_id


# ---------------------------------------------------------------------------
# _handle_agent_connect → asyncio.create_task(drain_for_agent)
# ---------------------------------------------------------------------------


@patch("core.services.sqs_drainer.drain_for_agent")
@patch("core.services.agent_connect_service._cancel_sleep_grace")
@patch("core.services.agent_connect_service._make_agent_event_callback")
@patch("core.services.agent_connect_service.get_game_server_client")
@patch("core.services.daemon_mailbox.get_daemon_mailbox")
@patch("core.services.agent_connect_service.TownService")
@patch("core.town_token.verify_town_token")
def test_handle_agent_connect_schedules_drain_task(
    mock_verify_token,
    mock_town_service_cls,
    mock_get_mailbox,
    mock_get_gs_client,
    mock_make_callback,
    mock_cancel_grace,
    mock_drain,
):
    """_handle_agent_connect should schedule a drain task on successful connect."""
    from core.services.agent_connect_service import _handle_agent_connect
    from core.services.town_agent_ws import get_town_agent_ws_manager

    agent_id = "connect-agent-uuid"

    mock_verify_token.return_value = {"uid": "user-1", "iid": "instance-1"}

    service = MagicMock()
    service.get_agent_by_name.return_value = {
        "id": agent_id,
        "displayName": "Test",
        "spriteUrl": None,
        "spriteReady": False,
        "onboardingPhase": "alive",
        "currentConversationId": None,
        "currentLocation": "plaza",
        "locationContext": "town",
        "spriteAnimations": {},
        "mood": 0,
        "energy": 100,
    }
    mock_town_service_cls.return_value = service

    mailbox = MagicMock()
    mailbox.notify_connection.return_value = True
    mailbox.disconnect = MagicMock()
    mock_get_mailbox.return_value = mailbox

    gs_client = MagicMock()
    gs_client.connect_agent = AsyncMock()
    mock_get_gs_client.return_value = gs_client

    mock_make_callback.return_value = AsyncMock()
    mock_cancel_grace.return_value = False  # fresh connect, not within grace

    # drain_for_agent must behave as a coroutine function so asyncio.create_task
    # receives an awaitable. AsyncMock makes calls return an awaitable by default.
    mock_drain.__wrapped__ = None  # satisfy create_task's coroutine detection
    mock_drain.side_effect = AsyncMock(return_value=0)

    ws_manager = get_town_agent_ws_manager()

    request = MagicMock()
    request.connectionId = "conn-abc"
    request.body = {
        "type": "town_agent_connect",
        "token": "tok",  # nosec B105 - test fixture
        "agent_name": "test-agent",
    }

    with patch("core.services.phase_lattice.restore_phase_from_db"):
        asyncio.run(_handle_agent_connect(request, ws_manager))

    # drain_for_agent should have been called (the task was scheduled)
    mock_drain.assert_called_once_with(agent_id)
