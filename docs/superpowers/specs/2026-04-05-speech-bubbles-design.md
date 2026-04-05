# Speech Bubbles

**Date:** 2026-04-05
**Status:** Approved

## Problem

When agents have conversations via `chat`/`say`, the text is only delivered to the other agent's daemon. Viewers (browser Godot WebGL) see agents standing still with no visual indication of conversation. The SpeechBubble UI node exists in `agent.tscn` and the viewer code is already wired to display it — the gap is getting speech data into the `world_state` broadcast.

## Design

### Data Flow

```
_handle_say() in backend
  → gs_client.send_command(speaker_id, {"cmd": "set_speech", "text": "..."})
  → Game server stores speechText per agent, starts 5s timer
  → world_state broadcast includes speechText per agent
  → Viewer agent_manager reads speechText, calls agent.update_state()
  → SpeechBubble label shows text for 5 seconds
```

### Changes

**1. Backend (`routers/town.py`)**

In `_handle_say()`, after forwarding the message to the other daemon, send the speech text to the game server:

```python
try:
    gs_client = get_game_server_client()
    await gs_client.send_command(initiator_id, {"cmd": "set_speech", "text": message_text})
except Exception:
    pass  # Speech bubble is cosmetic, don't fail the conversation
```

Same in `_handle_chat()` for the opening message.

**2. Game Server — Town (`agent_controller.gd`)**

- Add `_agent_speech: Dictionary` — peer_id → text string
- Add `_speech_timers: Dictionary` — peer_id → seconds remaining (float)
- Handle `set_speech` command in `handle_command()`: store text, set timer to 5.0
- In `_process()`: tick timers by delta, clear speech when timer expires
- In `get_world_state_for_viewer()`: include `"speechText": _agent_speech.get(peer_id, "")` in agent data

**3. Game Server — Apartment (`apartment_agent_controller.gd`)**

Same pattern as town controller: `_agent_speech`, `_speech_timers`, `set_speech` handler, timer tick, include in world_state.

**4. Viewer (`agent_manager.gd`, `agent.gd`)**

No changes needed. Already wired:
- `agent_manager.gd` line 86-87: checks for `speechText`, calls `update_state()`
- `agent.gd` `update_state()`: sets `speech_bubble.text`, toggles `speech_bubble.visible`

### Requires

- Godot .pck re-export (game server GDScript changes)
- No DynamoDB changes
- No frontend React changes

### Files Changed

- `apps/backend/routers/town.py` — add `set_speech` command in `_handle_say` and `_handle_chat`
- `apps/godot/scripts/server/agent_controller.gd` — speech storage, timer, world_state field
- `apps/godot/scripts/server/apartment_agent_controller.gd` — same pattern
- `apps/frontend/public/godot/goosetown.pck` — re-exported with GDScript changes
