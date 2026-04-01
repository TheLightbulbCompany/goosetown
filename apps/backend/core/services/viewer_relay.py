"""Resilient world_state relay from game server to browser viewers.

Maintains a persistent auto-reconnecting WebSocket to the game server.
Broadcasts world_state to all registered browser viewer connections
via the API Gateway Management API.

Viewer connection IDs are seeded from DynamoDB on startup, then tracked
in-memory. Stale connections are cleaned up on GoneException.
"""

import asyncio
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class ViewerRelay:
    """Relay world_state from the game server to browser viewers."""

    def __init__(self, game_server_url: str):
        self._gs_url = game_server_url
        self._ws = None
        self._viewer_connections: set[str] = set()
        self._running = False
        self._listen_task: Optional[asyncio.Task] = None

    def register_viewer(self, connection_id: str) -> None:
        """Add a browser viewer connection to the broadcast set."""
        self._viewer_connections.add(connection_id)
        logger.info("Viewer registered: %s (%d total)", connection_id, len(self._viewer_connections))

    def unregister_viewer(self, connection_id: str) -> None:
        """Remove a browser viewer connection."""
        self._viewer_connections.discard(connection_id)
        logger.info("Viewer unregistered: %s (%d remaining)", connection_id, len(self._viewer_connections))

    def seed_from_dynamodb(self) -> None:
        """Read all viewer connections from DynamoDB to seed the in-memory set.

        Called once on startup to recover viewer connections that existed
        before this container started (e.g., after a redeploy).
        """
        try:
            import core.dynamo as dynamo

            table = dynamo.connections_table()
            resp = table.scan(
                FilterExpression="connectionType = :t",
                ExpressionAttributeValues={":t": "viewer"},
            )
            for item in resp.get("Items", []):
                conn_id = item.get("connectionId")
                if conn_id:
                    self._viewer_connections.add(conn_id)
            logger.info("Seeded %d viewer connections from DynamoDB", len(self._viewer_connections))
        except Exception as e:
            logger.warning("Failed to seed viewer connections from DynamoDB: %s", e)

    async def start(self) -> None:
        """Start the relay: connect to game server and begin listening."""
        self._running = True
        self.seed_from_dynamodb()
        self._listen_task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Stop the relay and close connections."""
        self._running = False
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        logger.info("ViewerRelay stopped")

    async def _connect(self) -> None:
        """Connect to game server as a viewer."""
        import websockets

        ws = await websockets.connect(self._gs_url, ping_interval=30, ping_timeout=10)
        await ws.send(json.dumps({"type": "viewer"}))
        self._ws = ws
        logger.info("ViewerRelay connected to game server at %s", self._gs_url)

    async def _run_loop(self) -> None:
        """Main loop: connect, listen, auto-reconnect on failure."""
        backoff = 1.0
        while self._running:
            try:
                await self._connect()
                backoff = 1.0
                async for raw in self._ws:
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if data.get("event") == "world_state":
                        await self._broadcast(data)
            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                logger.warning("ViewerRelay disconnected: %s — reconnecting in %.0fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _broadcast(self, data: dict) -> None:
        """Send world_state to all registered viewer connections."""
        if not self._viewer_connections:
            return

        from core.services.management_api_client import ManagementApiClient

        agent_count = len(data.get("agents", []))
        logger.info(
            "Broadcasting world_state (%d agents) to %d viewers", agent_count, len(self._viewer_connections)
        )

        mgmt = ManagementApiClient()
        stale: list[str] = []
        for conn_id in list(self._viewer_connections):
            try:
                mgmt.send_message(conn_id, data)
            except Exception as e:
                err_str = str(e)
                if "GoneException" in err_str or "410" in err_str:
                    stale.append(conn_id)
        for conn_id in stale:
            self._viewer_connections.discard(conn_id)
            logger.debug("Removed stale viewer connection: %s", conn_id)


_viewer_relay: Optional[ViewerRelay] = None


def get_viewer_relay() -> ViewerRelay:
    """Return the singleton ViewerRelay instance."""
    global _viewer_relay
    if _viewer_relay is None:
        from core.config import settings

        _viewer_relay = ViewerRelay(settings.GAME_SERVER_URL)
    return _viewer_relay
