# Viewer Relay — Resilient world_state Broadcasting

## Problem

Browser viewers never see agents on the map because:
1. The backend's single viewer connection to the game server drops on ECS redeploy
2. It never reconnects — `connect_viewer()` runs once at startup in `lifespan`
3. The relay scans DynamoDB for viewer connections on every 2s tick (doesn't scale)
4. If the connection drops, all browser viewers go dark permanently

## Design

### Architecture

```
Game Server (Godot headless, port 8080, private VPC)
    │
    │  WebSocket (persistent, auto-reconnect)
    │  Receives: world_state every 2s
    ▼
Backend (FastAPI, ECS Fargate)
    │  ViewerRelay class manages:
    │  - Persistent viewer WebSocket to game server
    │  - In-memory set of browser viewer connection IDs
    │  - Forwards world_state to all viewers via Management API
    │
    │  Management API (API Gateway post-to-connection)
    ▼
Browser Viewers (Godot WebGL via React WebSocket)
    Receives: world_state → AgentManager renders sprites
```

### Components

#### 1. ViewerRelay (new class in `core/services/viewer_relay.py`)

Single responsibility: maintain a persistent viewer connection to the game server and broadcast `world_state` to all registered browser viewers.

```python
class ViewerRelay:
    def __init__(self, game_server_url: str):
        self._gs_url = game_server_url
        self._ws = None
        self._viewer_connections: set[str] = set()  # connection IDs
        self._running = False
        self._reconnect_task = None

    async def start(self):
        """Start the relay. Connects to game server and begins listening."""
        self._running = True
        await self._connect()
        # Start background listener
        asyncio.create_task(self._listen_loop())

    async def stop(self):
        """Stop the relay and close connections."""
        self._running = False
        if self._ws:
            await self._ws.close()

    def register_viewer(self, connection_id: str):
        """Add a browser viewer connection to the broadcast set."""
        self._viewer_connections.add(connection_id)

    def unregister_viewer(self, connection_id: str):
        """Remove a browser viewer connection."""
        self._viewer_connections.discard(connection_id)

    async def _connect(self):
        """Connect to game server as a viewer."""
        ws = await websockets.connect(self._gs_url)
        await ws.send(json.dumps({"type": "viewer"}))
        self._ws = ws

    async def _listen_loop(self):
        """Listen for world_state and broadcast to all viewers.
        Auto-reconnects on disconnect with exponential backoff."""
        backoff = 1.0
        while self._running:
            try:
                async for raw in self._ws:
                    data = json.loads(raw)
                    if data.get("event") == "world_state":
                        await self._broadcast(data)
                    backoff = 1.0  # Reset on successful message
            except (ConnectionClosed, Exception):
                if not self._running:
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
                try:
                    await self._connect()
                except Exception:
                    pass  # Will retry on next loop

    async def _broadcast(self, data: dict):
        """Send world_state to all registered viewer connections."""
        if not self._viewer_connections:
            return
        mgmt = ManagementApiClient()
        stale = []
        for conn_id in list(self._viewer_connections):
            try:
                mgmt.send_message(conn_id, data)
            except GoneException:
                stale.append(conn_id)
            except Exception:
                pass
        for conn_id in stale:
            self._viewer_connections.discard(conn_id)
```

Key properties:
- **Auto-reconnect with exponential backoff** — if game server restarts or connection drops, relay reconnects automatically (1s → 2s → 4s → ... → 30s max)
- **In-memory viewer set** — no DynamoDB scan per tick. Viewers register/unregister via method calls.
- **Stale connection cleanup** — GoneException from Management API removes dead connections
- **Singleton** — one instance per backend process, accessed via `get_viewer_relay()`

#### 2. Viewer registration (ws-connect/ws-disconnect Lambdas → Backend)

When a viewer connects via API Gateway:
- `ws-connect` Lambda already writes to DynamoDB connections table with `type=viewer`
- Backend needs to know about it too — add a call to `viewer_relay.register_viewer(connection_id)` when the backend processes a viewer's first message

When a viewer disconnects:
- `ws-disconnect` Lambda already removes from DynamoDB
- Backend calls `viewer_relay.unregister_viewer(connection_id)`

Implementation: the `handle_ws_message` function in `routers/town.py` already receives all WebSocket messages. When it sees a connection that's in the connections table as `viewer`, register it with the relay.

#### 3. Startup (main.py lifespan)

```python
async def lifespan(app: FastAPI):
    relay = get_viewer_relay()
    await relay.start()
    yield
    await relay.stop()
```

Remove all the inline relay code currently in `main.py`.

#### 4. React frontend (useTownState.ts)

The React WebSocket (`useTownState`) is already connected and working. When it receives `world_state`, it forwards to Godot via `godotUpdateWorldState` bridge callback. No changes needed here — it already works if it receives the messages.

#### 5. Godot AgentManager (agent_manager.gd)

Already wired to receive world_state from JsBridge. No changes needed.

### Data Flow (end to end)

1. Game server broadcasts `world_state` every 2s on the viewer WebSocket
2. `ViewerRelay._listen_loop()` receives it
3. `ViewerRelay._broadcast()` sends to all registered viewer connection IDs via Management API
4. API Gateway pushes to each browser's WebSocket
5. React `useTownState.onmessage` receives `world_state`
6. React calls `window.godotUpdateWorldState(JSON.stringify(msg))`
7. Godot `JsBridge._on_update_world_state()` emits `world_state_received`
8. `AgentManager._on_world_state()` spawns/updates agent sprites

### Error Handling

| Scenario | Behavior |
|----------|----------|
| Game server unreachable at startup | Relay retries with backoff, logs warning |
| Game server connection drops | Auto-reconnect with exponential backoff (1s → 30s) |
| ECS backend redeploys | New container starts fresh relay in lifespan |
| Viewer connection goes stale | GoneException caught, connection removed from set |
| No viewers connected | Broadcast is a no-op (empty set check) |
| Game server restarts | Relay reconnects, game server re-broadcasts world_state |

### Files Changed

| File | Change |
|------|--------|
| `core/services/viewer_relay.py` | NEW — ViewerRelay class |
| `main.py` | Replace inline relay with ViewerRelay start/stop |
| `routers/town.py` | Register/unregister viewers with relay on connect/disconnect |
| Remove debug logs from `useTownState.ts` | Clean up console.log statements |

### Not Changed

- Game server (no changes)
- CDK infrastructure (no changes)
- Godot scripts (no changes — already wired via JsBridge)
- CLI / daemon (no changes)
- API Gateway Lambdas (no changes — already handle connect/disconnect)

### Success Criteria

- [ ] Browser viewer sees agent sprite on the map within 5 seconds of page load
- [ ] Agent moves on the map when `goosetown act move library` is called
- [ ] Viewer survives backend redeploy (reconnects automatically)
- [ ] Multiple browser tabs all see the same agent positions
- [ ] No DynamoDB scan per world_state tick
