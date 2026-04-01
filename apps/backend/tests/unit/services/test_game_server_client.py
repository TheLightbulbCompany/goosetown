"""Tests for GameServerClient."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services.game_server_client import GameServerClient


@pytest.mark.asyncio
async def test_connect_agent_sends_connect_message():
    mock_ws = AsyncMock()
    mock_ws.send = AsyncMock()
    mock_ws.__aiter__ = MagicMock(return_value=AsyncMock(__anext__=AsyncMock(side_effect=StopAsyncIteration)))

    with patch("core.services.game_server_client.websockets.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_ws

        client = GameServerClient(base_url="ws://test:8080")
        await client.connect_agent("agent-1", "Goose", "https://cdn/sprite.png", "plaza")

        mock_connect.assert_called_once_with("ws://test:8080")
        sent_data = json.loads(mock_ws.send.call_args_list[0][0][0])
        assert sent_data == {
            "type": "agent",
            "agent_id": "agent-1",
            "name": "Goose",
            "sprite_url": "https://cdn/sprite.png",
            "location": "plaza",
            "context": "town",
            "user_id": "",
        }

        await client.disconnect_agent("agent-1")


@pytest.mark.asyncio
async def test_disconnect_agent_closes_ws():
    mock_ws = AsyncMock()
    mock_ws.send = AsyncMock()
    mock_ws.close = AsyncMock()
    mock_ws.__aiter__ = MagicMock(return_value=AsyncMock(__anext__=AsyncMock(side_effect=StopAsyncIteration)))

    with patch("core.services.game_server_client.websockets.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_ws

        client = GameServerClient(base_url="ws://test:8080")
        await client.connect_agent("agent-1", "Goose", "https://cdn/sprite.png", "plaza")
        await client.disconnect_agent("agent-1")

        mock_ws.close.assert_called_once()
        assert "agent-1" not in client._agent_connections


@pytest.mark.asyncio
async def test_send_command_forwards_to_agent_ws():
    mock_ws = AsyncMock()
    mock_ws.send = AsyncMock()
    mock_ws.__aiter__ = MagicMock(return_value=AsyncMock(__anext__=AsyncMock(side_effect=StopAsyncIteration)))

    with patch("core.services.game_server_client.websockets.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_ws

        client = GameServerClient(base_url="ws://test:8080")
        await client.connect_agent("agent-1", "Goose", "https://cdn/sprite.png", "plaza")

        command = {"type": "move", "x": 10, "y": 5}
        await client.send_command("agent-1", command)

        # First call is the connect message, second is the command
        assert mock_ws.send.call_count == 2
        sent_command = json.loads(mock_ws.send.call_args_list[1][0][0])
        assert sent_command == command

        await client.disconnect_agent("agent-1")


@pytest.mark.asyncio
async def test_send_command_warns_for_unknown_agent():
    client = GameServerClient(base_url="ws://test:8080")
    # Should not raise, just log a warning
    await client.send_command("unknown-agent", {"type": "move"})


@pytest.mark.asyncio
async def test_on_event_registers_handler():
    client = GameServerClient(base_url="ws://test:8080")
    handler = AsyncMock()
    client.on_event("world_state", handler)

    assert "world_state" in client._event_handlers
    assert handler in client._event_handlers["world_state"]


@pytest.mark.asyncio
async def test_get_cached_locations_returns_copy():
    client = GameServerClient(base_url="ws://test:8080")
    client._locations = [{"name": "plaza", "x": 18, "y": 12}]

    locations = client.get_cached_locations()
    assert locations == [{"name": "plaza", "x": 18, "y": 12}]
    # Verify it's a copy
    locations.append({"name": "fake"})
    assert len(client._locations) == 1
