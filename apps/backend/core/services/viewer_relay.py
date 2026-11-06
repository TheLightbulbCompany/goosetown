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
        self._pending: Optional[dict] = None
        self._pending_event: Optional[asyncio.Event] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._seed_task: Optional[asyncio.Task] = None
        self._seed_removed: set[str] = set()

    def register_viewer(self, connection_id: str) -> None:
        """Add a browser viewer connection to the broadcast set."""
        self._viewer_connections.add(connection_id)
        if hasattr(self, "_seed_removed"):
            self._seed_removed.discard(connection_id)
        logger.info("Viewer registered: %s (%d total)", connection_id, len(self._viewer_connections))

    def unregister_viewer(self, connection_id: str) -> None:
        """Remove a browser viewer connection."""
        self._viewer_connections.discard(connection_id)
        if getattr(self, "_seed_task", None) is not None and not self._seed_task.done():
            self._seed_removed.add(connection_id)
        logger.info("Viewer unregistered: %s (%d remaining)", connection_id, len(self._viewer_connections))

    @staticmethod
    def _read_viewer_connections() -> set[str]:
        """Read a paginated snapshot without mutating event-loop-owned state.

        Paginated — walks the table until all viewer-type connections are loaded.
        """
        connections: set[str] = set()
        try:
            import core.dynamo as dynamo

            table = dynamo.connections_table()
            scan_kwargs: dict = {
                "FilterExpression": "connectionType = :t",
                "ExpressionAttributeValues": {":t": "viewer"},
            }
            while True:
                resp = table.scan(**scan_kwargs)
                for item in resp.get("Items", []):
                    conn_id = item.get("connectionId")
                    if conn_id:
                        connections.add(conn_id)
                last_key = resp.get("LastEvaluatedKey")
                if not last_key:
                    break
                scan_kwargs["ExclusiveStartKey"] = last_key
            return connections
        except Exception as e:
            logger.warning("Failed to seed viewer connections from DynamoDB: %s", e)
            return connections

    def seed_from_dynamodb(self) -> None:
        """Synchronous seed helper for callers outside the async startup path."""
        connections = self._read_viewer_connections()
        self._viewer_connections.update(connections)
        logger.info("Seeded %d viewer connections from DynamoDB", len(connections))

    async def _seed_viewers(self) -> None:
        try:
            connections = await asyncio.to_thread(self._read_viewer_connections)
            if self._running:
                # A disconnect observed while the snapshot was loading takes
                # precedence over its stale row; new registrations are retained.
                connections.difference_update(self._seed_removed)
                self._viewer_connections.update(connections)
                logger.info("Seeded %d viewer connections from DynamoDB", len(connections))
        finally:
            self._seed_removed.clear()

    async def start(self) -> None:
        """Start the relay: connect to game server and begin listening."""
        self._running = True
        self._pending_event = asyncio.Event()
        self._worker_task = asyncio.create_task(self._broadcast_worker())
        self._seed_removed.clear()
        self._seed_task = asyncio.create_task(self._seed_viewers())
        self._listen_task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Stop the relay and close connections."""
        self._running = False
        if self._seed_task:
            self._seed_task.cancel()
            try:
                await self._seed_task
            except asyncio.CancelledError:
                pass
            self._seed_task = None
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                # WS might already be closed by the time we shutdown — fine.
                logger.debug("ViewerRelay socket failed to close cleanly", exc_info=True)
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
                        self._pending = data
                        if self._pending_event is not None:
                            self._pending_event.set()
            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                logger.warning("ViewerRelay disconnected: %s — reconnecting in %.0fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _broadcast_worker(self) -> None:
        """Fan out the most recent world_state, dropping superseded frames.

        Latest-wins: while a fan-out is in flight, newer frames overwrite the
        pending slot, so the next iteration always sends the freshest one. This
        keeps viewers real-time and stops 10Hz inbound from piling up overlapping
        fan-outs.
        """
        assert self._pending_event is not None
        while self._running:
            await self._pending_event.wait()
            self._pending_event.clear()
            data = self._pending
            self._pending = None
            if data is not None:
                await self._broadcast(data)

    async def _fan_out(self, conns: list[str], data: dict) -> None:
        """Send `data` to each connection off the event loop; prune gone ones.
        Shared by _broadcast (world_state) and push_event (discrete events)."""
        if not conns:
            return
        from core.services.management_api_client import ManagementApiClient

        mgmt = ManagementApiClient()
        results = await asyncio.gather(
            *(asyncio.to_thread(mgmt.send_message, conn_id, data) for conn_id in conns),
            return_exceptions=True,
        )
        gone = [conn_id for conn_id, res in zip(conns, results) if res is False]
        if not gone:
            return
        for conn_id in gone:
            self._viewer_connections.discard(conn_id)
        import core.dynamo as dynamo

        await asyncio.gather(
            *(asyncio.to_thread(dynamo.delete_connection, conn_id) for conn_id in gone),
            return_exceptions=True,
        )
        logger.info("Pruned %d stale viewer connection(s)", len(gone))

    def push_event(self, payload: dict) -> None:
        """Best-effort discrete fan-out of an event (e.g. economy_event) to all
        viewer connections. Non-coalescing — every event lands. Fire-and-forget;
        silently no-ops when there is no running loop (tests/local)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._fan_out(list(self._viewer_connections), payload))

    async def _broadcast(self, data: dict) -> None:
        """Send world_state to all registered viewer connections."""
        conns = list(self._viewer_connections)
        if conns:
            logger.info(
                "Broadcasting world_state (%d agents) to %d viewers",
                len(data.get("agents", [])),
                len(conns),
            )
        await self._fan_out(conns, data)


_viewer_relay: Optional[ViewerRelay] = None


def get_viewer_relay() -> ViewerRelay:
    """Return the singleton ViewerRelay instance."""
    global _viewer_relay
    if _viewer_relay is None:
        from core.config import settings

        _viewer_relay = ViewerRelay(settings.GAME_SERVER_URL)
    return _viewer_relay
