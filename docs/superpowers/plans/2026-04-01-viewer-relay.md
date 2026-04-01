# Viewer Relay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Browser viewers see agent sprites on the map via a resilient world_state relay from the game server.

**Architecture:** New `ViewerRelay` class maintains a persistent auto-reconnecting WebSocket to the game server. On startup, seeds viewer connection IDs from DynamoDB once, then tracks in-memory. Broadcasts world_state to all viewers via Management API. Cleans up stale connections on GoneException.

**Tech Stack:** Python asyncio, websockets, boto3 (Management API), DynamoDB (connections table)

---

### File Structure

| File | Responsibility |
|------|---------------|
| `core/services/viewer_relay.py` | NEW — ViewerRelay class: game server connection, viewer tracking, broadcast |
| `main.py` | Replace inline relay with ViewerRelay.start()/stop() |
| `routers/town.py` | Register/unregister viewers with relay |
| `tests/unit/services/test_viewer_relay.py` | NEW — Unit tests for ViewerRelay |
| `hooks/useTownState.ts` | Remove debug console.logs |

---

### Task 1: Create ViewerRelay class

**Files:**
- Create: `apps/backend/core/services/viewer_relay.py`
- Test: `apps/backend/tests/unit/services/test_viewer_relay.py`

- [ ] **Step 1: Write the failing test for viewer registration**

```python
# tests/unit/services/test_viewer_relay.py
"""Tests for ViewerRelay."""

from core.services.viewer_relay import ViewerRelay


class TestViewerRegistration:
    def test_register_adds_connection(self):
        relay = ViewerRelay.__new__(ViewerRelay)
        relay._viewer_connections = set()
        relay.register_viewer("conn-abc")
        assert "conn-abc" in relay._viewer_connections

    def test_unregister_removes_connection(self):
        relay = ViewerRelay.__new__(ViewerRelay)
        relay._viewer_connections = {"conn-abc", "conn-def"}
        relay.unregister_viewer("conn-abc")
        assert "conn-abc" not in relay._viewer_connections
        assert "conn-def" in relay._viewer_connections

    def test_unregister_missing_connection_is_noop(self):
        relay = ViewerRelay.__new__(ViewerRelay)
        relay._viewer_connections = set()
        relay.unregister_viewer("conn-xyz")  # should not raise
        assert len(relay._viewer_connections) == 0

    def test_register_deduplicates(self):
        relay = ViewerRelay.__new__(ViewerRelay)
        relay._viewer_connections = set()
        relay.register_viewer("conn-abc")
        relay.register_viewer("conn-abc")
        assert len(relay._viewer_connections) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_viewer_relay.py -v`
Expected: FAIL with ImportError (module doesn't exist)

- [ ] **Step 3: Write ViewerRelay implementation**

```python
# core/services/viewer_relay.py
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
                    # Cache locations if present
                    if "locations" in data:
                        pass  # Could cache for future use
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

        mgmt = ManagementApiClient()
        stale: list[str] = []
        for conn_id in list(self._viewer_connections):
            try:
                mgmt.send_message(conn_id, data)
            except Exception as e:
                err_str = str(e)
                if "GoneException" in err_str or "410" in err_str:
                    stale.append(conn_id)
                # Other errors: skip silently (transient)
        for conn_id in stale:
            self._viewer_connections.discard(conn_id)
            logger.debug("Removed stale viewer connection: %s", conn_id)


# Singleton
_viewer_relay: Optional[ViewerRelay] = None


def get_viewer_relay() -> ViewerRelay:
    """Return the singleton ViewerRelay instance."""
    global _viewer_relay
    if _viewer_relay is None:
        from core.config import settings

        _viewer_relay = ViewerRelay(settings.GAME_SERVER_URL)
    return _viewer_relay
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_viewer_relay.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Lint**

Run: `cd apps/backend && uv run ruff check core/services/viewer_relay.py`
Expected: All checks passed

- [ ] **Step 6: Commit**

```bash
git add core/services/viewer_relay.py tests/unit/services/test_viewer_relay.py
git commit -m "feat: ViewerRelay class with auto-reconnect and in-memory viewer tracking"
```

---

### Task 2: Wire ViewerRelay into backend startup

**Files:**
- Modify: `apps/backend/main.py`

- [ ] **Step 1: Replace inline relay with ViewerRelay**

Replace the entire lifespan function in `main.py`:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("GooseTown backend starting")

    settings.validate()

    # Start viewer relay (game server → browser viewers)
    from core.services.viewer_relay import get_viewer_relay

    relay = get_viewer_relay()
    await relay.start()

    yield

    # Cleanup
    await relay.stop()
    gs_client = get_game_server_client()
    for agent_id in list(gs_client._agent_connections.keys()):
        await gs_client.disconnect_agent(agent_id)
    logger.info("GooseTown backend stopping")
```

- [ ] **Step 2: Run all tests**

Run: `cd apps/backend && uv run pytest tests/ -v`
Expected: All tests pass (131+)

- [ ] **Step 3: Lint**

Run: `cd apps/backend && uv run ruff check main.py`

- [ ] **Step 4: Commit**

```bash
git add main.py
git commit -m "feat: wire ViewerRelay into backend lifespan"
```

---

### Task 3: Register/unregister viewers in the relay

**Files:**
- Modify: `apps/backend/routers/town.py`

- [ ] **Step 1: Register viewer on ws-message**

In the `handle_ws_message` function, when a message arrives from a connection that's a viewer (check connections table or the ws-connect Lambda already set the type), register it with the relay.

Find the section where `town_agent_connect` is handled. Before that, add viewer registration:

```python
# At the top of handle_ws_message, after getting ws_manager:
# Check if this is a viewer connection and register with relay
if body.get("type") == "town_subscribe":
    from core.services.viewer_relay import get_viewer_relay
    relay = get_viewer_relay()
    relay.register_viewer(request.connectionId)
    return {"statusCode": 200}
```

- [ ] **Step 2: Unregister viewer on disconnect**

The ws-disconnect Lambda already deletes from DynamoDB. But we also need to remove from the in-memory set. Add to the `handle_ws_message` for disconnect events, OR handle it passively via GoneException cleanup (already implemented in ViewerRelay._broadcast).

The passive approach is simpler — stale connections get cleaned up on the next broadcast. No code change needed for unregister.

- [ ] **Step 3: Run all tests**

Run: `cd apps/backend && uv run pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 4: Lint**

Run: `cd apps/backend && uv run ruff check routers/town.py`

- [ ] **Step 5: Commit**

```bash
git add routers/town.py
git commit -m "feat: register viewers with relay on town_subscribe message"
```

---

### Task 4: Clean up debug logs

**Files:**
- Modify: `apps/frontend/src/hooks/useTownState.ts`

- [ ] **Step 1: Remove debug console.logs**

Remove all the `console.log('[useTownState]...` lines added during debugging. Keep the `godotUpdateWorldState` forwarding code.

- [ ] **Step 2: Typecheck**

Run: `cd apps/frontend && npx tsc --noEmit`

- [ ] **Step 3: Commit**

```bash
git add apps/frontend/src/hooks/useTownState.ts
git commit -m "chore: remove debug console.logs from useTownState"
```

---

### Task 5: Integration test and push

- [ ] **Step 1: Run full test suite**

Run: `cd apps/backend && uv run pytest tests/ -v && uv run ruff check .`
Expected: All tests pass, lint clean

- [ ] **Step 2: Push all commits**

```bash
git push
```

- [ ] **Step 3: Wait for deploy**

Watch: `gh run list --limit 1` then check compute + Vercel deploy

- [ ] **Step 4: Reconnect agent and verify**

```bash
cd apps/backend/data/goosetown-skill
python3 goosetown_cli.py join "<token>" --name "claude-opus" --appearance "owl"
```

- [ ] **Step 5: Verify browser receives world_state**

Open browser console on dev.goosetown.isol8.co. Should see `[useTownState] world_state received` (if debug logs still present) or agent sprite on map.

- [ ] **Step 6: Verify sprite renders on map**

Agent sprite should be visible on the town map at its current position.
