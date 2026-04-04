# Game Server Connection Resilience

**Date:** 2026-04-04  
**Status:** Approved  
**Scope:** `apps/backend/core/services/game_server_client.py`

## Problem

The backend maintains per-agent WebSocket connections to the Godot headless game server. When the game server restarts (ECS deploy), these connections die but the backend doesn't detect it:

1. `connect_agent` trusts `ws.state == 1` (TCP OPEN) — but the game server has restarted and has no record of the agent
2. `send_command` silently fails on dead connections — agents stop moving, no error reaches the daemon
3. `_listen_agent` catches `ConnectionClosed` but doesn't clear `_agent_connections` before reconnecting — other code can still find and use the dead socket

The daemon (client) should never discover our infrastructure is broken. The backend must detect and recover from dead connections internally.

## Approach

Three targeted fixes in `game_server_client.py`. No game server changes. No new infrastructure.

## Fix 1: Connection Invalidation on Disconnect

**File:** `_listen_agent()` and `_reconnect_agent()`

When `_listen_agent` catches `ConnectionClosed`:
1. Immediately remove the connection from `_agent_connections`
2. Remove the listen task from `_listen_tasks`
3. Then call `_reconnect_agent()`

This ensures that between disconnect and reconnect, no other code can use the dead socket. `send_command` will see no connection (and raise), `connect_agent` will see no connection (and create a fresh one).

`_reconnect_agent` already sends the full connect params to the game server on reconnect, which re-registers the agent.

## Fix 2: connect_agent Always Reconnects

**File:** `connect_agent()`

Remove the `ws.state == 1` shortcut entirely. On every `connect_agent` call:
1. If an existing connection exists, close it and cancel its listen task
2. Create a fresh WebSocket connection
3. Send the full connect params (agent_id, name, sprite_url, location, context, etc.)
4. Start a new listen task

This is safe because:
- The game server's `spawn_agent` deduplicates by agent_id (removes old peer before spawning new one)
- `connect_agent` is called infrequently: once per daemon session, once per backend startup recovery
- The cost is ~50ms for a new WebSocket vs agents stuck forever on a stale connection

The callback update (`_agent_callbacks[agent_id] = on_event`) happens before the connection logic so the latest callback is always used regardless.

## Fix 3: send_command Error Handling

**File:** `send_command()` and `transition_agent()`

When `ws.send()` fails:
1. Log the error with agent ID
2. Invalidate the connection: remove from `_agent_connections`, cancel listen task
3. Raise the exception to the caller

When no connection exists:
1. Raise `RuntimeError` instead of silently returning

Callers in `town.py` already handle game server errors — they send `move_rejected`, `activity_rejected`, etc. back to the daemon. Raising gives the daemon a clear signal instead of silent failure.

Same pattern applied to `transition_agent` which also does direct `ws.send()`.

## Files Changed

- `apps/backend/core/services/game_server_client.py` — all three fixes

## What This Does NOT Change

- Game server code (no GDScript changes, no .pck re-export)
- DynamoDB schema
- Daemon/CLI code
- API Gateway or Lambda code
- Frontend code

## Testing

1. Deploy backend with fix
2. Force restart game server (`aws ecs update-service --force-new-deployment`)
3. Verify agents reconnect and can move without daemon intervention
4. Verify `send_command` failures produce clear errors in logs
5. Verify daemon receives `move_rejected` on connection failure (not silent timeout)
