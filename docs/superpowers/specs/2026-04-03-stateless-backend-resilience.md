# Stateless Backend Resilience Design

**Date:** 2026-04-03
**Status:** Approved

## Goal

Make the backend stateless — all persistent state lives in DynamoDB, compute reconstructs from DB on startup. Container restarts become invisible to agents.

## Problem

The backend stores critical state in memory that dies on restart:
- `ws_manager` maps agent_id → API Gateway connection_id (stale after restart)
- Sprite generation background tasks (lost on restart, agent stuck at spriteReady=false)
- Activity timer tasks (lost on restart, agent stuck in currentActivity)

## Changes

### 1. daemonConnectionId on Agent Record

Add `daemonConnectionId` field to the agents DynamoDB table. Updated on every `town_agent_connect`. All code that needs to send messages to a daemon reads this field from the agent record instead of the in-memory `ws_manager`.

**Write path:** `town_agent_connect` handler stores `request.connectionId` as `daemonConnectionId` via `update_agent_state`.

**Read path:** `_send_progress`, the per-agent event callback, and any other code that pushes to the daemon reads `daemonConnectionId` from the agent record (or a cached copy passed through the call chain).

**Cleanup:** On WebSocket disconnect (`$disconnect` Lambda route), clear `daemonConnectionId` on the agent record.

### 2. Sprite Generation Status in DynamoDB

Add `spriteStatus` field to the agents table. Values: `"creating"`, `"animating"`, `"downloading"`, `"complete"`, or absent/null (no generation in progress).

Add `spriteCurrentTemplate` field — tracks which template animation is currently being generated (e.g. `"walk"`, `"idle"`).

**Phase transitions in `_generate_sprite`:**
- Before `create_character` → set `spriteStatus="creating"`
- Before `queue_template_animations` → set `spriteStatus="animating"`
- Before ZIP download → set `spriteStatus="downloading"`
- After upload + DynamoDB update → set `spriteStatus="complete"`

**Startup recovery in `_reconnect_active_agents`:**
- Query agents where `spriteStatus` is not null and not `"complete"`
- For each, resume the sprite generation from the current phase:
  - `"creating"`: check if `pixellabCharacterId` exists → skip to animating if so
  - `"animating"`: use `get_existing_animation_names` to find what's done, queue remaining templates
  - `"downloading"`: re-download ZIP and continue pipeline

**`_send_progress` fix:** Uses `daemonConnectionId` from the agent record (Change 1) instead of ws_manager. The sprite task passes a `send_to_daemon` function that reads the connection ID from DynamoDB each time, so reconnected daemons get messages on their new connection.

### 3. Activity Timer Recovery

`currentActivity` and `activityStartedAt` are already in DynamoDB. On startup:

1. Query agents with non-null `currentActivity`
2. Look up the activity's `duration_seconds` from the activity registry
3. Calculate `elapsed = now - activityStartedAt`
4. If `elapsed >= duration_seconds` → complete immediately (apply effects, clear activity)
5. If `elapsed < duration_seconds` → restart timer for `duration_seconds - elapsed`

The existing stale activity cleanup (>5 min hardcoded timeout) is replaced by this proper recovery logic.

## What Gets Removed

- `ws_manager.get_by_agent_id()` usage in `_send_progress` and event callbacks — replaced by DynamoDB read
- The >5 min stale activity cleanup hack in `town_agent_connect`
- The `_sprite_jobs_in_progress` in-memory set — replaced by `spriteStatus` in DynamoDB

## What Stays

- `ws_manager` itself stays for now (used by other code paths) but is no longer the source of truth for daemon connection lookup during sprite generation
- Game server WebSocket connections stay in memory (unavoidable — live TCP sockets)
- Per-agent event callbacks stay in memory (the callback is set on `connect_agent` and invoked by `_listen_agent`)
