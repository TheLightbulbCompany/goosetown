"""Game server WebSocket client — per-agent connection manager.

Manages WebSocket connections to the Godot headless game server, with one
connection per agent and an optional viewer connection for world state updates.

Each agent connection has a callback that fires on every game server event,
forwarding state directly to the daemon. No global handler registry.

Usage:
    client = get_game_server_client()
    await client.connect_agent("agent-123", "Goose", "https://cdn/sprite.png", "plaza",
                               on_event=my_callback)
    await client.send_command("agent-123", {"cmd": "move", "destination": "plaza"})
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

# Type for the per-agent event callback: (agent_id, event_data) -> None
AgentEventCallback = Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]]


class GameServerClient:
    """Per-agent WebSocket connection manager for the Godot game server."""

    def __init__(self, base_url: str | None = None):
        self._base_url = base_url or settings.GAME_SERVER_URL
        self._agent_connections: dict[str, ClientConnection] = {}
        self._agent_connect_params: dict[str, dict[str, str]] = {}
        self._agent_callbacks: dict[str, AgentEventCallback] = {}
        self._viewer_connection: ClientConnection | None = None
        self._listen_tasks: dict[str, asyncio.Task] = {}
        self._locations: list[dict[str, Any]] = []
        self._arrival_events: dict[str, asyncio.Event] = {}
        self._agent_state_cache: dict[str, dict[str, Any]] = {}

    async def connect_agent(
        self,
        agent_id: str,
        display_name: str,
        sprite_url: str,
        location: str,
        context: str = "town",
        user_id: str = "",
        sprite_animations: dict | None = None,
        on_event: AgentEventCallback | None = None,
    ) -> None:
        """Open a WebSocket connection for an agent and start listening.

        Args:
            agent_id: Unique agent identifier.
            display_name: Human-readable agent name.
            sprite_url: CDN URL for the agent's spritesheet.
            location: Starting location marker name.
            context: Location context ("town" or "apartment").
            user_id: Clerk user ID owning this agent.
            sprite_animations: Animation metadata keyed by animation name.
            on_event: Async callback fired on every game server event for this agent.
                      Signature: (agent_id: str, event_data: dict) -> None
        """
        # Update callback even if already connected (daemon reconnect updates the callback)
        if on_event is not None:
            self._agent_callbacks[agent_id] = on_event

        existing_ws = self._agent_connections.get(agent_id)
        if existing_ws is not None:
            try:
                is_alive = existing_ws.state == 1  # State.OPEN
            except Exception:
                is_alive = False
            if is_alive:
                logger.info("Agent %s already connected to game server, updated callback", agent_id)
                return
            # Stale connection — clean up and reconnect
            logger.info("Agent %s has stale connection, reconnecting", agent_id)
            self._agent_connections.pop(agent_id, None)
            self._agent_connect_params.pop(agent_id, None)
            task = self._listen_tasks.pop(agent_id, None)
            if task:
                task.cancel()

        ws = await websockets.connect(self._base_url)

        connect_params = {
            "type": "agent",
            "agent_id": agent_id,
            "name": display_name,
            "sprite_url": sprite_url,
            "location": location,
            "context": context,
            "user_id": user_id,
            "sprite_animations": sprite_animations or {},
        }
        await ws.send(json.dumps(connect_params))

        self._agent_connections[agent_id] = ws
        self._agent_connect_params[agent_id] = connect_params
        task = asyncio.create_task(self._listen_agent(agent_id, ws))
        self._listen_tasks[agent_id] = task

        logger.info("Agent %s connected to game server (context=%s)", agent_id, context)

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
        self._agent_callbacks.pop(agent_id, None)
        self._agent_state_cache.pop(agent_id, None)
        if ws is not None:
            await ws.close()
            logger.info("Agent %s disconnected from game server", agent_id)

    async def send_command(self, agent_id: str, command: dict[str, Any]) -> None:
        """Send a JSON command on an agent's WebSocket connection."""
        ws = self._agent_connections.get(agent_id)
        if ws is None:
            logger.warning("No game server connection for agent %s, cannot send command", agent_id)
            return

        await ws.send(json.dumps(command))
        logger.debug("Sent command to agent %s: %s", agent_id, command.get("cmd", "unknown"))

    async def transition_agent(
        self, agent_id: str, to_context: str, user_id: str, agent_data: dict
    ) -> None:
        """Send a transition command to the game server."""
        ws = self._agent_connections.get(agent_id)
        if not ws:
            raise RuntimeError(f"No connection for agent {agent_id}")
        await ws.send(json.dumps({
            "cmd": "transition",
            "to": to_context,
            "user_id": user_id,
            "agent_data": agent_data,
        }))
        params = self._agent_connect_params.get(agent_id)
        if params:
            params["context"] = to_context
            if agent_data.get("location"):
                params["location"] = agent_data["location"]
        logger.info("Sent transition command for agent %s to %s", agent_id, to_context)

    def expect_arrival(self, agent_id: str) -> asyncio.Event:
        """Register an arrival event BEFORE sending a move command."""
        event = asyncio.Event()
        self._arrival_events[agent_id] = event
        return event

    async def wait_for_arrival(self, agent_id: str, event: asyncio.Event | None = None) -> None:
        """Await an arrival confirmation from the game server."""
        if event is None:
            event = asyncio.Event()
            self._arrival_events[agent_id] = event
        await event.wait()
        self._arrival_events.pop(agent_id, None)

    async def connect_viewer(self) -> None:
        """Open a viewer WebSocket connection for world state updates."""
        ws = await websockets.connect(self._base_url)
        await ws.send(json.dumps({"type": "viewer"}))
        self._viewer_connection = ws
        task = asyncio.create_task(self._listen_viewer())
        self._listen_tasks["__viewer__"] = task
        logger.info("Viewer connected to game server")

    def get_cached_locations(self) -> list[dict[str, Any]]:
        """Return the most recently cached locations list."""
        return list(self._locations)

    def get_cached_agent_state(self, agent_id: str) -> dict[str, Any] | None:
        """Return the latest cached agent_state for an agent."""
        return self._agent_state_cache.get(agent_id)

    def has_agent_connection(self, agent_id: str) -> bool:
        """Check if an agent has a live game server connection."""
        ws = self._agent_connections.get(agent_id)
        if ws is None:
            return False
        try:
            return ws.state == 1
        except Exception:
            return False

    async def _listen_agent(self, agent_id: str, ws: ClientConnection) -> None:
        """Listen loop for an agent's WebSocket connection.

        Caches agent_state, signals arrival waiters, and invokes the
        per-agent callback for every event.
        """
        logger.info("_listen_agent started for %s", agent_id)
        try:
            async for raw_message in ws:
                try:
                    message = json.loads(raw_message)
                except json.JSONDecodeError:
                    continue

                msg_type = message.get("event", "")

                # Cache locations
                if msg_type == "locations":
                    locs = message.get("locations", [])
                    if locs:
                        self._locations = locs

                # Cache latest agent_state
                if msg_type == "agent_state":
                    self._agent_state_cache[agent_id] = message

                # Signal arrival waiters
                if msg_type == "arrived":
                    arrival_event = self._arrival_events.get(agent_id)
                    if arrival_event is not None:
                        arrival_event.set()

                # Invoke per-agent callback
                callback = self._agent_callbacks.get(agent_id)
                if callback is not None:
                    try:
                        await callback(agent_id, message)
                    except Exception:
                        logger.exception("Error in callback for %s event (agent %s)", msg_type, agent_id)

        except websockets.ConnectionClosed:
            logger.warning("Game server connection closed for agent %s, reconnecting...", agent_id)
            await self._reconnect_agent(agent_id)
            return
        except asyncio.CancelledError:
            logger.debug("Listener cancelled for agent %s", agent_id)
            raise

        self._agent_connections.pop(agent_id, None)
        self._listen_tasks.pop(agent_id, None)

    async def _reconnect_agent(self, agent_id: str, max_retries: int = 5) -> None:
        """Reconnect an agent with exponential backoff."""
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
                logger.info("Reconnected agent %s to game server (attempt %d)", agent_id, attempt)
                task = asyncio.create_task(self._listen_agent(agent_id, ws))
                self._listen_tasks[agent_id] = task
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                delay = min(delay * 2, 10.0)
                logger.warning(
                    "Reconnect failed for agent %s (attempt %d/%d): %s",
                    agent_id, attempt, max_retries, e,
                )

        logger.error("Failed to reconnect agent %s after %d attempts", agent_id, max_retries)
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
                    continue
                if message.get("event") == "locations":
                    locs = message.get("locations", [])
                    if locs:
                        self._locations = locs
        except websockets.ConnectionClosed:
            logger.warning("Game server viewer connection closed")
        except asyncio.CancelledError:
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
