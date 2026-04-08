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
            "sprite_animations": {},
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
async def test_send_command_raises_for_unknown_agent():
    client = GameServerClient(base_url="ws://test:8080")
    with pytest.raises(RuntimeError, match="No game server connection"):
        await client.send_command("unknown-agent", {"type": "move"})


@pytest.mark.asyncio
async def test_send_command_raises_on_no_connection():
    """send_command must raise RuntimeError when no connection exists."""
    client = GameServerClient(base_url="ws://test:8080")
    with pytest.raises(RuntimeError, match="No game server connection"):
        await client.send_command("unknown-agent", {"cmd": "move"})


@pytest.mark.asyncio
async def test_send_command_invalidates_on_send_failure():
    """When ws.send() fails, send_command must remove the dead connection
    and re-raise the exception."""
    client = GameServerClient(base_url="ws://test:8080")

    mock_ws = AsyncMock()
    mock_ws.send = AsyncMock(side_effect=ConnectionError("broken pipe"))
    client._agent_connections["agent-1"] = mock_ws
    client._listen_tasks["agent-1"] = AsyncMock()

    with pytest.raises(ConnectionError, match="broken pipe"):
        await client.send_command("agent-1", {"cmd": "move"})

    assert "agent-1" not in client._agent_connections
    assert "agent-1" not in client._listen_tasks


@pytest.mark.asyncio
async def test_connect_agent_stores_callback():
    """Verify that on_event callback is stored per-agent."""
    client = GameServerClient(base_url="ws://test:8080")
    callback = AsyncMock()
    # Directly set callback (connect_agent requires a real WS server)
    client._agent_callbacks["agent-1"] = callback

    assert "agent-1" in client._agent_callbacks
    assert client._agent_callbacks["agent-1"] is callback


@pytest.mark.asyncio
async def test_get_cached_locations_returns_copy():
    client = GameServerClient(base_url="ws://test:8080")
    client._locations = [{"name": "plaza", "x": 18, "y": 12}]

    locations = client.get_cached_locations()
    assert locations == [{"name": "plaza", "x": 18, "y": 12}]
    # Verify it's a copy
    locations.append({"name": "fake"})
    assert len(client._locations) == 1


@pytest.mark.asyncio
async def test_listen_agent_clears_connection_on_disconnect():
    """When the game server connection closes, _listen_agent must remove
    the connection from _agent_connections immediately so no other code
    can use the dead socket."""
    import websockets

    client = GameServerClient(base_url="ws://test:8080")

    mock_ws = AsyncMock()
    mock_ws.__aiter__ = MagicMock(
        return_value=AsyncMock(
            __anext__=AsyncMock(side_effect=websockets.ConnectionClosed(None, None))
        )
    )

    client._agent_connections["agent-1"] = mock_ws
    client._agent_connect_params["agent-1"] = {
        "type": "agent", "agent_id": "agent-1", "name": "Test",
        "sprite_url": "", "location": "plaza", "context": "town",
        "user_id": "", "sprite_animations": {},
    }

    client._reconnect_agent = AsyncMock()

    await client._listen_agent("agent-1", mock_ws)

    assert "agent-1" not in client._agent_connections
    client._reconnect_agent.assert_called_once_with("agent-1")


@pytest.mark.asyncio
async def test_connect_agent_replaces_existing_connection():
    """connect_agent must always create a fresh connection, even if
    an existing one appears alive. This handles game server restarts
    where TCP stays open but the server has no record of the agent."""
    mock_ws_old = AsyncMock()
    mock_ws_old.state = 1  # Appears OPEN
    mock_ws_old.close = AsyncMock()
    mock_ws_old.__aiter__ = MagicMock(
        return_value=AsyncMock(__anext__=AsyncMock(side_effect=StopAsyncIteration))
    )

    mock_ws_new = AsyncMock()
    mock_ws_new.send = AsyncMock()
    mock_ws_new.__aiter__ = MagicMock(
        return_value=AsyncMock(__anext__=AsyncMock(side_effect=StopAsyncIteration))
    )

    client = GameServerClient(base_url="ws://test:8080")
    client._agent_connections["agent-1"] = mock_ws_old
    client._listen_tasks["agent-1"] = AsyncMock()  # fake task

    with patch("core.services.game_server_client.websockets.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_ws_new

        await client.connect_agent("agent-1", "Goose", "https://cdn/sprite.png", "plaza")

        # Old connection must be closed
        mock_ws_old.close.assert_called_once()
        # New connection must be created
        mock_connect.assert_called_once_with("ws://test:8080")
        # _agent_connections must point to the new WebSocket
        assert client._agent_connections["agent-1"] is mock_ws_new

    await client.disconnect_agent("agent-1")


@pytest.mark.asyncio
async def test_update_agent_sprites_sends_command_on_open_connection():
    client = GameServerClient(base_url="ws://stub")
    fake_ws = AsyncMock()
    client._agent_connections["agent-1"] = fake_ws

    await client.update_agent_sprites("agent-1", {
        "walk": {"url": "https://cdn.example.com/walk.png", "frameCount": 6, "fps": 8},
    })

    fake_ws.send.assert_awaited_once()
    sent = json.loads(fake_ws.send.call_args[0][0])
    assert sent["cmd"] == "update_agent_sprites"
    assert sent["agent_id"] == "agent-1"
    assert sent["sprite_animations"]["walk"]["url"] == "https://cdn.example.com/walk.png"


@pytest.mark.asyncio
async def test_update_agent_sprites_silently_skips_when_no_connection():
    client = GameServerClient(base_url="ws://stub")
    await client.update_agent_sprites("agent-2", {"walk": {"url": "x"}})
