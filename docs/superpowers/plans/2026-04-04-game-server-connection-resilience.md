# Game Server Connection Resilience — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make backend→game server WebSocket connections resilient to game server restarts by fixing connection invalidation, reconnection, and error handling.

**Architecture:** Three fixes in `GameServerClient`: (1) `_listen_agent` clears stale connections before reconnecting, (2) `connect_agent` always creates a fresh connection instead of trusting `ws.state`, (3) `send_command`/`transition_agent` raise on failure and invalidate dead connections.

**Tech Stack:** Python asyncio, websockets library, pytest + AsyncMock

---

### File Map

- **Modify:** `apps/backend/core/services/game_server_client.py` — all three fixes
- **Modify:** `apps/backend/tests/unit/services/test_game_server_client.py` — new tests for each fix

---

### Task 1: _listen_agent Invalidates Connection on Disconnect

**Files:**
- Modify: `apps/backend/tests/unit/services/test_game_server_client.py`
- Modify: `apps/backend/core/services/game_server_client.py:220-277`

- [ ] **Step 1: Write failing test — connection cleared on disconnect**

Add to `tests/unit/services/test_game_server_client.py`:

```python
@pytest.mark.asyncio
async def test_listen_agent_clears_connection_on_disconnect():
    """When the game server connection closes, _listen_agent must remove
    the connection from _agent_connections immediately so no other code
    can use the dead socket."""
    import websockets

    client = GameServerClient(base_url="ws://test:8080")

    mock_ws = AsyncMock()
    # Simulate ConnectionClosed after one iteration
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

    # Patch _reconnect_agent to prevent actual reconnect attempts
    client._reconnect_agent = AsyncMock()

    await client._listen_agent("agent-1", mock_ws)

    # Connection must be cleared BEFORE reconnect is called
    assert "agent-1" not in client._agent_connections
    client._reconnect_agent.assert_called_once_with("agent-1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_game_server_client.py::test_listen_agent_clears_connection_on_disconnect -v`

Expected: FAIL — current code calls `_reconnect_agent` without clearing `_agent_connections` first.

- [ ] **Step 3: Fix _listen_agent to clear connection before reconnecting**

In `apps/backend/core/services/game_server_client.py`, replace the `ConnectionClosed` handler (lines 268-271):

```python
        except websockets.ConnectionClosed:
            logger.warning("Game server connection closed for agent %s, reconnecting...", agent_id)
            # Immediately invalidate so no other code uses the dead socket
            self._agent_connections.pop(agent_id, None)
            self._listen_tasks.pop(agent_id, None)
            await self._reconnect_agent(agent_id)
            return
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_game_server_client.py::test_listen_agent_clears_connection_on_disconnect -v`

Expected: PASS

- [ ] **Step 5: Run all existing tests to verify no regressions**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_game_server_client.py -v`

Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/game_server_client.py apps/backend/tests/unit/services/test_game_server_client.py
git commit -m "fix: _listen_agent clears connection before reconnecting"
```

---

### Task 2: connect_agent Always Creates Fresh Connection

**Files:**
- Modify: `apps/backend/tests/unit/services/test_game_server_client.py`
- Modify: `apps/backend/core/services/game_server_client.py:49-113`

- [ ] **Step 1: Write failing test — existing connection is replaced**

Add to `tests/unit/services/test_game_server_client.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_game_server_client.py::test_connect_agent_replaces_existing_connection -v`

Expected: FAIL — current code returns early with "already connected" when `ws.state == 1`.

- [ ] **Step 3: Rewrite connect_agent to always reconnect**

In `apps/backend/core/services/game_server_client.py`, replace `connect_agent` method (lines 49-113):

```python
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

        Always creates a fresh connection — closes any existing one first.
        The game server deduplicates by agent_id on spawn, so re-registering
        is safe and handles game server restarts transparently.
        """
        # Update callback (daemon reconnect updates the callback)
        if on_event is not None:
            self._agent_callbacks[agent_id] = on_event

        # Close existing connection if any — don't trust ws.state
        existing_ws = self._agent_connections.pop(agent_id, None)
        if existing_ws is not None:
            task = self._listen_tasks.pop(agent_id, None)
            if task:
                task.cancel()
            try:
                await existing_ws.close()
            except Exception:
                pass
            logger.info("Closed existing game server connection for agent %s", agent_id)

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
```

- [ ] **Step 4: Also update has_agent_connection to not trust ws.state**

Replace `has_agent_connection` (lines 210-218):

```python
    def has_agent_connection(self, agent_id: str) -> bool:
        """Check if an agent has a game server connection."""
        return agent_id in self._agent_connections
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_game_server_client.py::test_connect_agent_replaces_existing_connection -v`

Expected: PASS

- [ ] **Step 6: Run all tests**

Run: `cd apps/backend && uv run pytest tests/ -v --tb=short`

Expected: All 150+ tests pass

- [ ] **Step 7: Commit**

```bash
git add apps/backend/core/services/game_server_client.py apps/backend/tests/unit/services/test_game_server_client.py
git commit -m "fix: connect_agent always creates fresh connection, no stale ws.state check"
```

---

### Task 3: send_command and transition_agent Error Handling

**Files:**
- Modify: `apps/backend/tests/unit/services/test_game_server_client.py`
- Modify: `apps/backend/core/services/game_server_client.py:133-177`

- [ ] **Step 1: Write failing test — send_command raises on no connection**

Add to `tests/unit/services/test_game_server_client.py`:

```python
@pytest.mark.asyncio
async def test_send_command_raises_on_no_connection():
    """send_command must raise RuntimeError when no connection exists,
    not silently return."""
    client = GameServerClient(base_url="ws://test:8080")
    with pytest.raises(RuntimeError, match="No game server connection"):
        await client.send_command("unknown-agent", {"cmd": "move"})
```

- [ ] **Step 2: Write failing test — send_command invalidates on send failure**

```python
@pytest.mark.asyncio
async def test_send_command_invalidates_on_send_failure():
    """When ws.send() fails, send_command must remove the dead connection
    from _agent_connections and re-raise the exception."""
    client = GameServerClient(base_url="ws://test:8080")

    mock_ws = AsyncMock()
    mock_ws.send = AsyncMock(side_effect=ConnectionError("broken pipe"))
    client._agent_connections["agent-1"] = mock_ws
    client._listen_tasks["agent-1"] = AsyncMock()

    with pytest.raises(ConnectionError, match="broken pipe"):
        await client.send_command("agent-1", {"cmd": "move"})

    # Connection must be invalidated
    assert "agent-1" not in client._agent_connections
    assert "agent-1" not in client._listen_tasks
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_game_server_client.py::test_send_command_raises_on_no_connection tests/unit/services/test_game_server_client.py::test_send_command_invalidates_on_send_failure -v`

Expected: Both FAIL — current code silently returns on no connection, and doesn't catch send failures.

- [ ] **Step 4: Rewrite send_command with error handling**

In `apps/backend/core/services/game_server_client.py`, replace `send_command` (lines 133-141):

```python
    async def send_command(self, agent_id: str, command: dict[str, Any]) -> None:
        """Send a JSON command on an agent's WebSocket connection.

        Raises RuntimeError if no connection exists.
        On send failure, invalidates the connection and re-raises.
        """
        ws = self._agent_connections.get(agent_id)
        if ws is None:
            raise RuntimeError(f"No game server connection for agent {agent_id}")

        try:
            await ws.send(json.dumps(command))
            logger.debug("Sent command to agent %s: %s", agent_id, command.get("cmd", "unknown"))
        except Exception as exc:
            logger.warning(
                "[agent:%s] Game server send failed (%s), invalidating connection",
                agent_id[:8], exc,
            )
            self._agent_connections.pop(agent_id, None)
            task = self._listen_tasks.pop(agent_id, None)
            if task:
                task.cancel()
            raise
```

- [ ] **Step 5: Apply same pattern to transition_agent's ws.send**

In `transition_agent`, wrap the `ws.send` in the same try/except (lines 147-160):

```python
    async def transition_agent(
        self, agent_id: str, to_context: str, user_id: str, agent_data: dict
    ) -> None:
        """Send a transition command and wait for game server confirmation."""
        ws = self._agent_connections.get(agent_id)
        if not ws:
            raise RuntimeError(f"No connection for agent {agent_id}")

        # Register transition event BEFORE sending command
        event = asyncio.Event()
        self._transition_events[agent_id] = event

        try:
            await ws.send(json.dumps({
                "cmd": "transition",
                "to": to_context,
                "user_id": user_id,
                "agent_data": agent_data,
            }))
        except Exception as exc:
            logger.warning(
                "[agent:%s] Transition send failed (%s), invalidating connection",
                agent_id[:8], exc,
            )
            self._agent_connections.pop(agent_id, None)
            task = self._listen_tasks.pop(agent_id, None)
            if task:
                task.cancel()
            self._transition_events.pop(agent_id, None)
            raise

        logger.info("Sent transition command for agent %s to %s, waiting for confirmation", agent_id, to_context)

        # Wait for game server to confirm transition
        try:
            await asyncio.wait_for(event.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("Transition confirmation timed out for agent %s", agent_id)
            raise
        finally:
            self._transition_events.pop(agent_id, None)

        params = self._agent_connect_params.get(agent_id)
        if params:
            params["context"] = to_context
            if agent_data.get("location"):
                params["location"] = agent_data["location"]
        logger.info("Transition confirmed for agent %s to %s", agent_id, to_context)
```

- [ ] **Step 6: Update existing test that expects silent return**

The existing test `test_send_command_warns_for_unknown_agent` expects no exception. Update it:

```python
@pytest.mark.asyncio
async def test_send_command_raises_for_unknown_agent():
    client = GameServerClient(base_url="ws://test:8080")
    with pytest.raises(RuntimeError, match="No game server connection"):
        await client.send_command("unknown-agent", {"type": "move"})
```

- [ ] **Step 7: Run all tests**

Run: `cd apps/backend && uv run pytest tests/ -v --tb=short`

Expected: All tests pass. Check that callers of `send_command` in `town.py` already have try/except blocks.

- [ ] **Step 8: Verify callers handle the new exception**

Check that these callers in `routers/town.py` already have exception handling:
- Line ~1019: `await gs_client.send_command(...)` for moves — wrapped in the ws-message handler which returns `{"statusCode": 200}` 
- Line ~1637: `await gs_client.send_command(...)` for set_activity — wrapped in try/except
- Line ~1886: `await gs_client.send_command(...)` for clearing activity — wrapped in try/except

If any caller doesn't handle RuntimeError, add a try/except that logs a warning and continues.

- [ ] **Step 9: Commit**

```bash
git add apps/backend/core/services/game_server_client.py apps/backend/tests/unit/services/test_game_server_client.py
git commit -m "fix: send_command raises on failure and invalidates dead connections"
```

---

### Task 4: Integration Verification

- [ ] **Step 1: Run full test suite**

Run: `cd apps/backend && uv run pytest tests/ -v --tb=short`

Expected: All tests pass, no regressions.

- [ ] **Step 2: Run linter**

Run: `cd apps/backend && uv run ruff check core/services/game_server_client.py`

Expected: Clean

- [ ] **Step 3: Final commit and push**

```bash
git push origin main
```

- [ ] **Step 4: After deploy, verify with manual test**

1. Force restart game server: `aws ecs update-service --cluster <cluster> --service <service> --force-new-deployment`
2. Wait 2 min for new task
3. Reconnect daemon: `python3 goosetown_cli.py act move plaza`
4. Verify agent moves on screen
5. Check backend logs for `"Closed existing game server connection"` (confirms old connection was replaced)
