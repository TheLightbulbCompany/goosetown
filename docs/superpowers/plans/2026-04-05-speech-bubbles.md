# Speech Bubbles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show speech bubble text above agents in the Godot viewer when they speak during conversations.

**Architecture:** Backend sends `set_speech` command to game server on each `say`/`chat`. Game server stores speech text per agent with a 5-second auto-clear timer. `world_state` broadcast includes `speechText` per agent. Viewer already has the UI wired — no viewer changes needed.

**Tech Stack:** Python (FastAPI backend), GDScript (Godot game server), pytest

---

### File Map

- **Modify:** `apps/backend/routers/town.py` — send `set_speech` in `_handle_say` and `_handle_chat`
- **Modify:** `apps/godot/scripts/server/agent_controller.gd` — speech storage, timer, `set_speech` command, world_state field
- **Modify:** `apps/godot/scripts/server/apartment_agent_controller.gd` — same pattern
- **Modify:** `apps/frontend/public/godot/goosetown.pck` — re-export after GDScript changes

---

### Task 1: Game Server — Speech Storage and Timer (Town Controller)

**Files:**
- Modify: `apps/godot/scripts/server/agent_controller.gd`

- [ ] **Step 1: Add speech state variables**

After `var _agent_activity: Dictionary = {}` (line 40), add:

```gdscript
## peer_id -> speech text (empty string = no speech)
var _agent_speech: Dictionary = {}
## peer_id -> seconds remaining before speech clears
var _speech_timers: Dictionary = {}
## How long speech bubbles stay visible
const SPEECH_DURATION: float = 5.0
```

- [ ] **Step 2: Add set_speech command handler**

In `handle_command()`, add a new match arm before the `_:` default:

```gdscript
		"set_speech":
			var text: String = str(data.get("text", ""))
			_agent_speech[peer_id] = text
			if text != "":
				_speech_timers[peer_id] = SPEECH_DURATION
			else:
				_speech_timers.erase(peer_id)
```

- [ ] **Step 3: Tick speech timers in _process**

In `_process()`, after `_check_proximity()` and before the state timer block, add:

```gdscript
	# Tick speech timers and clear expired speech
	var expired_peers: Array = []
	for peer_id in _speech_timers.keys():
		_speech_timers[peer_id] -= delta
		if _speech_timers[peer_id] <= 0.0:
			expired_peers.append(peer_id)
	for peer_id in expired_peers:
		_speech_timers.erase(peer_id)
		_agent_speech.erase(peer_id)
```

- [ ] **Step 4: Include speechText in world_state**

In `get_world_state_for_viewer()`, add to the agent dict after `"activity"`:

```gdscript
			"speechText": _agent_speech.get(peer_id, ""),
```

- [ ] **Step 5: Clean up speech on agent removal**

In `remove_agent()`, after `_agent_activity.erase(peer_id)`, add:

```gdscript
	_agent_speech.erase(peer_id)
	_speech_timers.erase(peer_id)
```

- [ ] **Step 6: Commit**

```bash
git add apps/godot/scripts/server/agent_controller.gd
git commit -m "feat: speech bubble support in town agent controller

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Game Server — Speech Storage and Timer (Apartment Controller)

**Files:**
- Modify: `apps/godot/scripts/server/apartment_agent_controller.gd`

- [ ] **Step 1: Add speech state variables**

After `var _agent_activity: Dictionary = {}` add:

```gdscript
var _agent_speech: Dictionary = {}
var _speech_timers: Dictionary = {}
const SPEECH_DURATION: float = 5.0
```

- [ ] **Step 2: Add set_speech command handler**

In `handle_command()`, add a new match arm before the `_:` default:

```gdscript
		"set_speech":
			var text: String = str(data.get("text", ""))
			_agent_speech[peer_id] = text
			if text != "":
				_speech_timers[peer_id] = SPEECH_DURATION
			else:
				_speech_timers.erase(peer_id)
```

- [ ] **Step 3: Add _process for speech timers**

The apartment controller may not have a `_process` method. If it doesn't, add one. If it does (for state broadcast), add the speech timer tick to it:

```gdscript
func _process(delta: float) -> void:
	if _agents.is_empty():
		return

	# Tick speech timers
	var expired_peers: Array = []
	for peer_id in _speech_timers.keys():
		_speech_timers[peer_id] -= delta
		if _speech_timers[peer_id] <= 0.0:
			expired_peers.append(peer_id)
	for peer_id in expired_peers:
		_speech_timers.erase(peer_id)
		_agent_speech.erase(peer_id)

	# Existing state broadcast timer logic (if any)
	_state_timer += delta
	if _state_timer >= STATE_INTERVAL:
		_state_timer -= STATE_INTERVAL
		_broadcast_agent_states()
```

- [ ] **Step 4: Include speechText in world_state**

In `get_world_state_for_viewer()`, add to the agent dict:

```gdscript
			"speechText": _agent_speech.get(peer_id, ""),
```

- [ ] **Step 5: Clean up speech on agent removal**

In `remove_agent()`, add:

```gdscript
	_agent_speech.erase(peer_id)
	_speech_timers.erase(peer_id)
```

- [ ] **Step 6: Commit**

```bash
git add apps/godot/scripts/server/apartment_agent_controller.gd
git commit -m "feat: speech bubble support in apartment agent controller

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Backend — Send set_speech on Chat and Say

**Files:**
- Modify: `apps/backend/routers/town.py`

- [ ] **Step 1: Add set_speech to _handle_say**

In `_handle_say()`, after the message forwarding block (after line ~1305 where we log the conversation turn), add:

```python
    # Send speech bubble to game server for viewer display
    try:
        gs_client = get_game_server_client()
        await gs_client.send_command(speaker_id, {"cmd": "set_speech", "text": message})
    except Exception:
        pass  # Speech bubble is cosmetic, don't fail the conversation
```

- [ ] **Step 2: Add set_speech to _handle_chat**

In `_handle_chat()`, after the conversation invite is delivered and the act_ok is sent (after line ~1216), add:

```python
    # Send speech bubble to game server for viewer display
    try:
        gs_client = get_game_server_client()
        await gs_client.send_command(initiator_id, {"cmd": "set_speech", "text": opening_message})
    except Exception:
        pass  # Speech bubble is cosmetic, don't fail the conversation
```

- [ ] **Step 3: Run tests**

Run: `cd apps/backend && uv run pytest tests/ -q`

Expected: All tests pass (no new tests needed — this is fire-and-forget cosmetic behavior)

- [ ] **Step 4: Commit**

```bash
git add apps/backend/routers/town.py
git commit -m "feat: send set_speech to game server on chat/say for speech bubbles

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Export and Push

- [ ] **Step 1: Export Godot .pck**

Open Godot, export Web (Runnable) to `apps/frontend/public/godot/`. Exclude `.godot/*` and `exported/*` in Resources tab.

- [ ] **Step 2: Commit .pck with GDScript changes**

```bash
git add apps/frontend/public/godot/goosetown.pck
git commit --amend --no-edit
```

- [ ] **Step 3: Push**

```bash
git push origin main
```

- [ ] **Step 4: Test after deploy**

1. Connect both agents: captain-owl and test-fox
2. Move both to plaza
3. Start a conversation: `python3 goosetown_cli.py act chat test-fox "Hello fox!"`
4. Reply: `cd test-workspace-2 && python3 goosetown_cli.py act say <conv_id> "Hi owl!"`
5. Verify speech bubbles appear above agents in browser (hard refresh first)
6. Verify bubbles disappear after ~5 seconds
