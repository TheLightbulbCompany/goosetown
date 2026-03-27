"""Game server WebSocket client — per-agent connection manager.

Manages WebSocket connections to the Godot headless game server, with one
connection per agent and an optional viewer connection for world state updates.

Usage:
    client = get_game_server_client()
    await client.connect_agent("agent-123", "Goose", "https://cdn/sprite.png", "plaza")
    await client.send_command("agent-123", {"type": "move", "x": 10, "y": 5})
    await client.disconnect_agent("agent-123")
"""

import asyncio
import json
import logging
from collections.abc import Callable, Coroutine
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from core.config import settings

logger = logging.getLogger(__name__)


class GameServerClient:
    """Per-agent WebSocket connection manager for the Godot game server."""

    def __init__(self, base_url: str | None = None):
        self._base_url = base_url or settings.GAME_SERVER_URL
        self._agent_connections: dict[str, ClientConnection] = {}
        self._agent_connect_params: dict[str, dict[str, str]] = {}
        self._viewer_connection: ClientConnection | None = None
        self._event_handlers: dict[str, list[Callable[..., Coroutine[Any, Any, Any]]]] = {}
        self._listen_tasks: dict[str, asyncio.Task] = {}
        self._locations: list[dict[str, Any]] = []

    async def connect_agent(
        self,
        agent_id: str,
        display_name: str,
        sprite_url: str,
        location: str,
    ) -> None:
        """Open a WebSocket connection for an agent and start listening."""
        if agent_id in self._agent_connections:
            logger.warning("Agent %s already connected, skipping", agent_id)
            return

        ws = await websockets.connect(self._base_url)

        connect_params = {
            "type": "agent",
            "agent_id": agent_id,
            "name": display_name,
            "sprite_url": sprite_url,
            "location": location,
        }
        await ws.send(json.dumps(connect_params))

        self._agent_connections[agent_id] = ws
        self._agent_connect_params[agent_id] = connect_params
        task = asyncio.create_task(self._listen_agent(agent_id, ws))
        self._listen_tasks[agent_id] = task

        logger.info("Agent %s connected to game server", agent_id)

    async def disconnect_agent(self, agent_id: str) -> None:
        """Close an agent's WebSocket connection and clean up."""
        task = self._listen_tasks.pop(agent_id, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        ws = self._agent_connections.pop(agent_id, None)
        self._agent_connect_params.pop(agent_id, None)
        if ws is not None:
            await ws.close()
            logger.info("Agent %s disconnected from game server", agent_id)

    async def send_command(self, agent_id: str, command: dict[str, Any]) -> None:
        """Send a JSON command on an agent's WebSocket connection."""
        ws = self._agent_connections.get(agent_id)
        if ws is None:
            logger.warning("No connection for agent %s, cannot send command", agent_id)
            return

        await ws.send(json.dumps(command))
        logger.debug("Sent command to agent %s: %s", agent_id, command.get("type", "unknown"))

    async def connect_viewer(self) -> None:
        """Open a viewer WebSocket connection for world state updates."""
        ws = await websockets.connect(self._base_url)
        await ws.send(json.dumps({"type": "viewer"}))

        self._viewer_connection = ws
        task = asyncio.create_task(self._listen_viewer())
        self._listen_tasks["__viewer__"] = task

        logger.info("Viewer connected to game server")

    def on_event(self, event_name: str, handler: Callable[..., Coroutine[Any, Any, Any]]) -> None:
        """Register an async handler for a game server event."""
        self._event_handlers.setdefault(event_name, []).append(handler)

    def get_cached_locations(self) -> list[dict[str, Any]]:
        """Return the most recently cached locations list."""
        return list(self._locations)

    async def _listen_agent(self, agent_id: str, ws: ClientConnection) -> None:
        """Listen loop for an agent's WebSocket connection."""
        try:
            async for raw_message in ws:
                try:
                    message = json.loads(raw_message)
                except json.JSONDecodeError:
                    logger.warning("Non-JSON message from game server for agent %s", agent_id)
                    continue

                msg_type = message.get("event", "")

                # Cache locations if present
                if "locations" in message:
                    self._locations = message["locations"]

                # Dispatch to registered handlers
                handlers = self._event_handlers.get(msg_type, [])
                for handler in handlers:
                    try:
                        await handler(agent_id, message)
                    except Exception:
                        logger.exception(
                            "Error in handler for event %s (agent %s)", msg_type, agent_id
                        )

        except websockets.ConnectionClosed:
            logger.warning("Game server connection closed for agent %s, reconnecting...", agent_id)
            await self._reconnect_agent(agent_id)
            return
        except asyncio.CancelledError:
            logger.debug("Listener cancelled for agent %s", agent_id)
            raise

        # Clean up on unexpected disconnect
        self._agent_connections.pop(agent_id, None)
        self._listen_tasks.pop(agent_id, None)

    async def _reconnect_agent(self, agent_id: str, max_retries: int = 5) -> None:
        """Reconnect an agent with exponential backoff, giving up after max_retries."""
        params = self._agent_connect_params.get(agent_id)
        if params is None:
            logger.warning("No connect params for agent %s, cannot reconnect", agent_id)
            self._agent_connections.pop(agent_id, None)
            self._listen_tasks.pop(agent_id, None)
            return

        delay = 1.0
        for attempt in range(1, max_retries + 1):
            if agent_id not in self._agent_connect_params:
                break
            try:
                await asyncio.sleep(delay)
                ws = await websockets.connect(self._base_url)
                await ws.send(json.dumps(params))
                self._agent_connections[agent_id] = ws
                logger.info("Reconnected agent %s to game server", agent_id)
                # Restart listening on the new connection
                task = asyncio.create_task(self._listen_agent(agent_id, ws))
                self._listen_tasks[agent_id] = task
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                delay = min(delay * 2, 10.0)
                logger.warning(
                    "Reconnect failed for agent %s (attempt %d/%d): %s, retrying in %ss",
                    agent_id, attempt, max_retries, e, delay,
                )

        # Exhausted all retries — clean up
        logger.error(
            "Failed to reconnect agent %s after %d attempts, giving up", agent_id, max_retries
        )
        self._agent_connections.pop(agent_id, None)
        self._agent_connect_params.pop(agent_id, None)
        self._listen_tasks.pop(agent_id, None)

    async def _listen_viewer(self) -> None:
        """Listen loop for the viewer WebSocket connection."""
        ws = self._viewer_connection
        if ws is None:
            return

        try:
            async for raw_message in ws:
                try:
                    message = json.loads(raw_message)
                except json.JSONDecodeError:
                    logger.warning("Non-JSON message from game server on viewer connection")
                    continue

                msg_type = message.get("event", "")

                if "locations" in message:
                    self._locations = message["locations"]

                handlers = self._event_handlers.get(msg_type, [])
                for handler in handlers:
                    try:
                        await handler(None, message)
                    except Exception:
                        logger.exception("Error in viewer handler for event %s", msg_type)

        except websockets.ConnectionClosed:
            logger.warning("Game server viewer connection closed")
        except asyncio.CancelledError:
            logger.debug("Viewer listener cancelled")
            raise

        self._viewer_connection = None
        self._listen_tasks.pop("__viewer__", None)


# Module-level singleton
_game_server_client: GameServerClient | None = None


def get_game_server_client() -> GameServerClient:
    global _game_server_client
    if _game_server_client is None:
        _game_server_client = GameServerClient()
    return _game_server_client
