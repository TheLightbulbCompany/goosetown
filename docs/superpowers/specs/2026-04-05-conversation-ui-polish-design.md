# Conversation UI Polish

**Date:** 2026-04-05
**Status:** Approved

## Problem

Speech bubbles work but the UI is poor: raw text overlaps the agent name, no background, agents don't face each other or animate during conversation. The experience should feel like a real town simulation.

## Changes

### 1. Comic-Style Speech Bubble

Replace the bare SpeechBubble Label with a NinePatchRect + Label child. The NinePatchRect uses a 9-patch pixel art white bubble texture with a tail pointing down toward the agent. Word wrap enabled, max width ~120px, auto-height. Centered above the agent. NameLabel hides while speech is visible to avoid overlap.

Requires a new asset: `apps/godot/assets/speech_bubble.png` — a small pixel art bubble texture suitable for NinePatchRect (white with border, tail at bottom center).

### 2. Name Label on Hover Only

Add an Area2D with a CollisionShape2D (rectangle covering the sprite area) to agent.tscn. Connect `mouse_entered` / `mouse_exited` signals to show/hide the NameLabel. NameLabel starts hidden. When speech bubble is active, name stays hidden regardless of hover.

### 3. Agents Face Each Other During Conversation

**Backend:** In `_handle_chat`, after creating the conversation, calculate the facing direction for each agent based on their relative positions from the game server's cached agent states. Send `{"cmd": "set_facing", "direction": "<dir>"}` to both agents.

**Game server:** Add `set_facing` command to `agent_controller.gd` and `apartment_agent_controller.gd`. Updates the agent node's `facing` property which is already included in world_state broadcast.

On conversation end, no facing reset needed — agents face naturally on their next move.

### 4. Talking Animation During Conversation

**Backend:** In `_handle_chat` and `_handle_say`, send `{"cmd": "set_activity", "activity": "talking"}` to the speaking agent. In `_handle_end_conversation`, send `{"cmd": "set_activity", "activity": ""}` to both agents to clear the talking state.

**Viewer:** Add `"talking": "talking"` to the `ACTIVITY_ANIMATION` dictionary in `agent.gd`. The talking custom animation is already defined in `CUSTOM_ANIMATIONS` registry and will generate lazily via PixelLab V2 pro on first use.

## Files Changed

- `apps/godot/scenes/agent.tscn` — add Area2D for hover, restructure speech bubble to NinePatchRect + Label
- `apps/godot/scripts/agent.gd` — hover show/hide, bubble management, add "talking" to ACTIVITY_ANIMATION
- `apps/godot/assets/speech_bubble.png` — new 9-patch bubble texture
- `apps/backend/routers/town.py` — set_facing on chat start, set_activity talking on chat/say, clear on end
- `apps/godot/scripts/server/agent_controller.gd` — set_facing command handler
- `apps/godot/scripts/server/apartment_agent_controller.gd` — set_facing command handler
- `apps/frontend/public/godot/goosetown.pck` — re-export

## What This Does NOT Change

- DynamoDB schema
- Daemon/CLI code
- Frontend React code
- Conversation logic (chat/say/end already work)
