# Removed Code Restoration Plan

Commit `faad1be` removed ~620 lines of WebSocket push and agent connection code.
This document tracks what was removed, what must be restored, and what improvements
to make during restoration.

## Context

The code was removed because it was "dead" — nothing called it at the time. But it
was scaffolding for the WebSocket push architecture that agents need to function
autonomously. Without it, the simulation is a passive state machine that agents must
poll rather than a live system that pushes events to them.

## Critical Bug Introduced

**Inactive detection has no connection guard.** Line 268 of `town_simulation.py`
now sends ALL active agents home after 5 minutes regardless of whether they're
connected. Previously it checked `ws_manager.is_agent_connected(agent_name)` first.
This must be fixed when restoring connection tracking.

---

## Files to Restore/Create

### 1. `ws_connection_service.py` (NEW — replaces `town_agent_ws.py`)

**What was removed:** `TownAgentWsManager` — in-memory dict mapping connectionId to
agent info. Singleton pattern. Methods: `register()`, `unregister()`,
`get_by_connection()`, `get_agent_connection_id()`, `is_agent_connected()`,
`connected_agents()`.

**Why in-memory was wrong:** Fargate tasks can scale horizontally. In-memory state
is lost on redeploy and not shared across tasks.

**What to build:** DynamoDB-backed connection service with the same interface.
- Table: `goosetown-connections` with `connection_id` as PK
- GSI: `agent_name-index` for `is_agent_connected()` and `get_agent_connection_id()`
- TTL column for automatic cleanup of stale connections
- Same methods as before but backed by DynamoDB

### 2. `management_api_client.py` (RESTORE — with improvements)

**What was removed:** `ManagementApiClient` class wrapping boto3
`apigatewaymanagementapi`. Methods: `send_message(connection_id, payload)`,
`close_connection(connection_id)`.

**Improvements:**
- Use `asyncio.to_thread()` for non-blocking boto3 calls
- Construct endpoint URL from `WS_API_ID` + `AWS_REGION` env vars instead of
  requiring full URL
- Add batch send support for world_update broadcasts

### 3. `config.py` — Add `WS_API_ID`

**What was removed:** `WS_MANAGEMENT_API_URL` env var.

**Replacement:** `WS_API_ID` — just the API Gateway ID. URL constructed at runtime:
`https://{api_id}.execute-api.{region}.amazonaws.com/{stage}`

### 4. `dynamo.py` — Restore `connections_table()`

**What was removed:** `connections_table()` function returning
`get_client().Table(table_name("connections"))`.

**Restore as-is.**

### 5. `town_simulation.py` — Restore push functionality

**What was removed (~390 lines):**

#### Constants
- `ARRIVAL_THRESHOLD = 0.5`
- `DECISION_COOLDOWN = 10.0`
- `CONVERSATION_COOLDOWN = 120.0`
- `WORLD_UPDATE_INTERVAL = 5`
- `THINK_INTERVAL = 15.0`

#### `__init__` state
- `self._mgmt_client` / `self._mgmt_client_failed` — lazy management API client
- `self._tick_count` — counter for periodic world updates
- `self._last_think_at` — per-agent think ping timestamps
- `self._notify_fn` — viewer broadcast callback

#### Methods to restore
- `_get_mgmt_client()` — lazy init of ManagementApiClient
- `_get_ws_manager()` → change to `_get_connection_service()` using new DynamoDB service
- `set_notify_fn(fn)` — set viewer broadcast callback
- `_push_agent_events(ws_manager, events)` — send events to connected agents
- `_push_world_updates(ws_manager, states, relationships_cache)` — periodic rich push
- `_build_context_summary(agent_state, nearby, pending_messages)` — markdown for TOWN_STATUS.md
- `_pick_random_location(exclude)` — random destination picker
- `_conversation_probability(affinity)` — relationship-influenced chat probability

#### Tick logic to restore
- **Wake alarm detection** — check `wake_at` for sleeping connected agents, transition
  to active, push `town_event: wake`
- **"Arrived" event** — push `town_event: arrived` with location and position when
  agent reaches target
- **"Nearby" event** — push `town_event: nearby` to both agents when new proximity
  pair detected
- **World update broadcast** — every `WORLD_UPDATE_INTERVAL` ticks, push rich state
  to each connected agent including:
  - Agent's own position, location, mood, energy, activity
  - Nearby agents with personality, relationship data, distance
  - All locations with occupancy
  - Think flag when idle and enough time has elapsed
  - Context summary markdown
- **Viewer broadcast** — call `notify_fn()` after state changes
- **Inactive guard** — only send agents home if they're NOT connected (currently broken)

---

## CDK Infrastructure Needed

The `connections` DynamoDB table needs to be created in CDK:
- Table name: `goosetown-connections`
- Partition key: `connection_id` (S)
- GSI `agent_name-index`: partition key `agent_name` (S)
- TTL attribute: `ttl`
- Billing: PAY_PER_REQUEST

The WebSocket API Gateway stack also needs the management API endpoint
configuration (VPC Link, NLB, etc.) — tracked separately.

---

## Order of Restoration

1. `dynamo.py` — restore `connections_table()`
2. `config.py` — add `WS_API_ID` and `WS_STAGE`
3. `ws_connection_service.py` — new DynamoDB-backed connection tracking
4. `management_api_client.py` — restore with async + URL construction
5. `town_simulation.py` — restore all push logic using new services
6. CDK — add connections table to DatabaseStack
7. Wire up WebSocket connect/disconnect Lambda handlers
