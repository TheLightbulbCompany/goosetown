# Activity System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire up the existing backend activity system to the game server and viewer so agents can perform location-based activities with visible animations, and trigger lazy custom animation generation on first use.

**Architecture:** The backend activity handler (`_handle_agent_activity` in town.py) already validates, times, and applies effects for 19 activities. The game server (`agent_controller.gd`) currently only broadcasts "idle"/"walking" — it needs to broadcast the actual activity name and available activities per location. The state translator needs to show available activities in TOWN_STATUS.md so agents know what they can do. The viewer (agent_manager.gd) already loads multiple animation textures (Plan A) — it just needs the activity→animation name mapping to switch animations. Lazy custom animation generation triggers when the backend starts an activity whose animation isn't in the agent's `spriteAnimations`.

**Tech Stack:** Python 3.12 (FastAPI), Godot 4.6 (GDScript), DynamoDB

**Spec:** `docs/superpowers/specs/2026-04-03-activity-animation-system-design.md`

**Depends on:** Plan A (multi-animation pipeline) — completed and deployed.

---

## What Already Works (DO NOT rebuild)

| Component | File | Status |
|---|---|---|
| Activity registry (19 activities) | `core/services/activity_registry.py` | Complete |
| Activity validation (location, context, busy) | `routers/town.py:1416-1506` | Complete |
| Activity timer with effect application | `routers/town.py:1509-1617` | Complete |
| Apartment spot navigation before activity | `routers/town.py:1541-1567` | Complete |
| Mood/energy delta with personality bonuses | `activity_registry.py:508-556` | Complete |
| Daemon event handlers | `goosetown_cli.py:987-1019` | Complete |
| CLI activity routing | `goosetown_cli.py:645` | Complete |

## What's Missing

| Gap | Where |
|---|---|
| Registry needs 22 activities (spec has 3 new, adjusts values) | `activity_registry.py` |
| Game server only broadcasts "idle"/"walking", not activity names | `agent_controller.gd:385` |
| Game server doesn't send available activities per location | `agent_controller.gd:449-473` |
| State translator expects activities but game server doesn't provide them | `state_translator.py:62-70` |
| Backend doesn't tell game server when activity starts/ends | `town.py:1488` |
| Viewer doesn't switch animation based on activity | `agent.gd` (auto-switch only handles walk/idle) |
| No lazy custom animation generation trigger | `town.py` |
| CLI `_LOCATION_ACTIVITIES` out of sync with registry | `goosetown_cli.py:75-170` |

---

## File Structure

### Backend (Modified)

| File | Change |
|---|---|
| `apps/backend/core/services/activity_registry.py` | Update to 22 activities, adjust values per spec |
| `apps/backend/routers/town.py:1488` | Send activity state to game server on start/end |
| `apps/backend/routers/town.py` (new function) | Lazy custom animation trigger |
| `apps/backend/core/services/state_translator.py` | Fix available activities rendering |
| `apps/backend/data/goosetown-skill/goosetown_cli.py:75-170` | Sync `_LOCATION_ACTIVITIES` with registry |
| `apps/backend/tests/unit/services/test_state_translator.py` | Update tests for new activity rendering |

### Godot (Modified)

| File | Change |
|---|---|
| `apps/godot/scripts/server/agent_controller.gd` | Broadcast activity name + available activities, accept `set_activity` command |
| `apps/godot/scripts/agent.gd` | Map activity name → animation asset name |

---

### Task 1: Update activity registry to 22 activities

**Files:**
- Modify: `apps/backend/core/services/activity_registry.py`
- Modify: `apps/backend/data/goosetown-skill/goosetown_cli.py:75-170`

Per the spec, update activities to match the approved list. Changes from current:

**New activities:**
- `rest` at plaza (8s, mood +1, energy +3, animation "idle")
- `nap` at apartment (12s, mood +3, energy +40, spot "bed", animation "sleeping")
- `sit_outside` at residence (10s, mood +3, energy +2, animation "sitting")

**Removed:**
- `enter_apartment` at residence (handled by context transition, not an activity)

**Value adjustments:**
- `perform`: duration 20→30s, mood +5→+6, energy -5→-6, currency +2→+3
- `order_coffee`: mood +2→+3, energy +5→+6
- `exercise`: mood +3→+4, energy +8→+10
- `garden`: energy -3→-2, mood +4→+5
- `sleep`: mood +5→+8
- `cook`: mood +3→+4, energy +3→+5
- `watch_tv`: mood +4→+3
- `listen_to_music`: mood +4→+5, energy +1→+2
- `watch_aquarium`: mood +3→+4, energy +2→+3

**Animation name updates** (map to Plan A asset names):
- `people_watch`: "sitting" → "sitting"
- `perform`: "waving" → "performing"
- `read`: "reading" → "reading"
- `study`: "reading" → "reading"
- `write_journal`: "writing" → "writing"
- `order_coffee`: "sitting" → "drink" (template)
- `sit_and_chat`: "sitting" → "sitting"
- `work_on_laptop`: "working" → "working"
- `exercise`: "exercising" → "exercising"
- `play_game`: "playing" → "gaming"
- `garden`: "gardening" → "gardening"
- `check_balance`: "standing" → "idle"
- `browse_listings`: "reading" → "reading"
- `sleep`: "sleeping" → "sleeping"
- `work_at_desk`: "working" → "working"
- `cook`: "cooking" → "cooking"
- `watch_tv`: "sitting" → "sitting"
- `read_book`: "reading" → "reading"
- `listen_to_music`: "sitting" → "sitting"
- `watch_aquarium`: "sitting" → "sitting"

- [ ] **Step 1: Update the _ACTIVITIES list in activity_registry.py**

Apply all changes listed above. Add the 3 new activities, remove `enter_apartment`, adjust values and animation names. The animation names must match the keys in `ANIMATION_REGISTRY` from `pixellab_service.py`.

- [ ] **Step 2: Sync `_LOCATION_ACTIVITIES` in goosetown_cli.py**

Update the `_LOCATION_ACTIVITIES` dict at lines 75-170 in `goosetown_cli.py` to match the updated registry. Add `rest`, `nap`, `sit_outside`. Remove `enter_apartment`. Update descriptions and durations.

- [ ] **Step 3: Run tests**

Run: `cd apps/backend && uv run pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 4: Commit**

```bash
git add apps/backend/core/services/activity_registry.py apps/backend/data/goosetown-skill/goosetown_cli.py
git commit -m "feat: update activity registry to 22 activities per spec"
```

---

### Task 2: Game server broadcasts activity name and available activities

**Files:**
- Modify: `apps/godot/scripts/server/agent_controller.gd`

The game server needs two changes:

1. **Accept a `set_activity` command** from the backend to track what activity an agent is performing
2. **Broadcast the activity name** in `_broadcast_agent_states` (currently hardcoded to "idle"/"walking")
3. **Include available activities** in the `available_actions` array per location

- [ ] **Step 1: Add activity tracking state**

Add a new dict to track per-agent activity:

```gdscript
## peer_id -> activity name (empty string = no activity)
var _agent_activity: Dictionary = {}
```

- [ ] **Step 2: Add `set_activity` command handler**

In `handle_command`, add a new case:

```gdscript
"set_activity":
    _handle_set_activity(peer_id, data)
```

Implement:

```gdscript
func _handle_set_activity(peer_id: int, data: Dictionary) -> void:
	var activity_name: String = str(data.get("activity", ""))
	_agent_activity[peer_id] = activity_name

	send_to_peer.emit(peer_id, {
		"event": "activity_set",
		"activity": activity_name,
	})
```

- [ ] **Step 3: Update agent state broadcast to include real activity name**

In `_broadcast_agent_states` (line 385), replace:

```gdscript
"activity": "idle" if speed < 0.1 else "walking",
```

With:

```gdscript
"activity": _agent_activity.get(peer_id, "idle" if speed < 0.1 else "walking"),
```

This uses the tracked activity if set, otherwise falls back to idle/walking.

- [ ] **Step 4: Include available activities in available_actions**

In `_get_available_actions`, the current idle action is:

```gdscript
actions.append({ "type": "idle" })
```

Replace with a richer structure that includes location-specific activities. The game server doesn't know the activity registry (that's backend), but it can include the location ID so the backend/daemon can look up activities:

```gdscript
actions.append({
	"type": "idle",
	"location": location_id,
})
```

The state translator (backend) will use the location to look up activities from the registry.

- [ ] **Step 5: Clean up activity on agent removal**

In `remove_agent`, add:

```gdscript
_agent_activity.erase(peer_id)
```

- [ ] **Step 6: Commit**

```bash
git add apps/godot/scripts/server/agent_controller.gd
git commit -m "feat: game server tracks and broadcasts activity name per agent"
```

---

### Task 3: Backend sends activity state to game server

**Files:**
- Modify: `apps/backend/routers/town.py`

When an activity starts, the backend needs to tell the game server so it broadcasts the activity name. When it ends, clear it.

- [ ] **Step 1: Send set_activity on activity start**

In `_handle_agent_activity`, after line 1488 (`service.update_agent_activity`), add:

```python
# Notify game server of activity state change
try:
    gs_client = get_game_server_client()
    await gs_client.send_command(agent_id, {
        "cmd": "set_activity",
        "activity": activity_name,
    })
except Exception as exc:
    logger.warning("Failed to send set_activity to game server for %s: %s", agent_id, exc)
```

- [ ] **Step 2: Clear activity on completion/interruption**

In `_activity_timer`, after line 1588 (`service.update_agent_activity(agent_id, None, None)`), add:

```python
# Clear activity state in game server
try:
    await gs_client.send_command(agent_id, {
        "cmd": "set_activity",
        "activity": "",
    })
except Exception as exc:
    logger.warning("Failed to clear activity in game server for %s: %s", agent_id, exc)
```

Also add the same clear in the `_clear_activity_and_notify` helper (used when navigation fails).

- [ ] **Step 3: Run tests**

Run: `cd apps/backend && uv run pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 4: Commit**

```bash
git add apps/backend/routers/town.py
git commit -m "feat: backend sends set_activity to game server on activity start/end"
```

---

### Task 4: Update state translator to show available activities

**Files:**
- Modify: `apps/backend/core/services/state_translator.py`
- Modify: `apps/backend/tests/unit/services/test_state_translator.py`

The state translator writes TOWN_STATUS.md for the daemon. It currently looks for `cmd == "idle"` with an `activities` list, but the game server sends `type == "idle"` with a `location`. We need to:

1. Fix the action format mismatch (`type` not `cmd`, `location` not `activities`)
2. Use the location to look up activities from the registry
3. Show them in the action menu

- [ ] **Step 1: Update state translator to look up activities by location**

Replace the idle activities block (lines 61-70) with:

```python
# Location-based activities
from core.services.activity_registry import get_activities_for_location

current_location = agent_state.get("location", "unknown")
location_context = agent_state.get("context", "town")
available_activities = get_activities_for_location(current_location)
# Filter to activities matching the current context
available_activities = [
    a for a in available_activities if a.location_context == location_context
]

if available_activities:
    lines.append("")
    lines.append("**Activities available here:**")
    for act in available_activities:
        lines.append(
            f"- `town_act {act.name}` — {act.description} "
            f"({act.duration_seconds}s, mood {'+' if act.mood_delta >= 0 else ''}{act.mood_delta}, "
            f"energy {'+' if act.energy_delta >= 0 else ''}{act.energy_delta})"
        )
```

Also update the "Other actions" section (line 99) to remove the generic `town_act idle <activity>` hint since activities are now listed explicitly.

- [ ] **Step 2: Update tests**

Update `test_state_translator.py` to verify activities are rendered. Add a test that provides a location with known activities and checks the output contains the activity names and descriptions.

- [ ] **Step 3: Run tests**

Run: `cd apps/backend && uv run pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 4: Commit**

```bash
git add apps/backend/core/services/state_translator.py apps/backend/tests/unit/services/test_state_translator.py
git commit -m "feat: state translator shows available activities per location in TOWN_STATUS.md"
```

---

### Task 5: Viewer animation switching based on activity

**Files:**
- Modify: `apps/godot/scripts/agent.gd`
- Modify: `apps/godot/scripts/agent_manager.gd`

The viewer receives `activity` in world_state. agent.gd currently auto-switches between walk/idle. We need it to also switch to activity-specific animations.

- [ ] **Step 1: Add activity-to-animation mapping in agent.gd**

Add a constant mapping from activity names to animation asset names:

```gdscript
# Activity name → animation asset name (from ANIMATION_REGISTRY)
const ACTIVITY_ANIMATION: Dictionary = {
	"people_watch": "sitting",
	"perform": "performing",
	"read": "reading",
	"study": "reading",
	"write_journal": "writing",
	"order_coffee": "drink",
	"sit_and_chat": "sitting",
	"work_on_laptop": "working",
	"exercise": "exercising",
	"play_game": "gaming",
	"garden": "gardening",
	"check_balance": "idle",
	"browse_listings": "reading",
	"sit_outside": "sitting",
	"sleep": "sleeping",
	"nap": "sleeping",
	"work_at_desk": "working",
	"cook": "cooking",
	"watch_tv": "sitting",
	"read_book": "reading",
	"listen_to_music": "sitting",
	"watch_aquarium": "sitting",
	"rest": "idle",
}
```

Add a `current_activity` variable:

```gdscript
var current_activity: String = ""
```

- [ ] **Step 2: Update animation auto-switching in _physics_process**

Replace the auto-select block:

```gdscript
	# Animate
	if not _spritesheets.is_empty():
		# Determine target animation from activity or movement
		var target_anim: String = "walk"
		if current_activity != "" and current_activity in ACTIVITY_ANIMATION:
			target_anim = ACTIVITY_ANIMATION[current_activity]
		elif not _is_moving:
			target_anim = "idle" if _spritesheets.has("idle") else "walk"

		if target_anim != _current_animation and _spritesheets.has(target_anim):
			play_animation(target_anim)
```

When an activity is set, the agent plays the activity animation. When the activity clears, it falls back to idle/walk.

- [ ] **Step 3: Update agent_manager.gd to pass activity from world_state**

In `_on_world_state`, for existing agents (line 80-87), add activity state update:

```gdscript
# Update activity state
var activity: String = str(agent_data.get("activity", ""))
if activity != "walking" and activity != "idle":
    _known_agents[agent_id].current_activity = activity
else:
    _known_agents[agent_id].current_activity = ""
```

- [ ] **Step 4: Commit**

```bash
git add apps/godot/scripts/agent.gd apps/godot/scripts/agent_manager.gd
git commit -m "feat: viewer switches animation based on agent activity state"
```

---

### Task 6: Lazy custom animation generation

**Files:**
- Modify: `apps/backend/routers/town.py`

When the backend starts an activity whose animation is a custom type (from `CUSTOM_ANIMATIONS` in pixellab_service.py) and the agent's `spriteAnimations` doesn't include it, kick off a background generation.

- [ ] **Step 1: Add lazy generation trigger in _handle_agent_activity**

After the `act_ok` is sent (line 1499), before scheduling the timer, check if the animation needs generating:

```python
# --- Trigger lazy custom animation generation if needed ---
from core.services.pixellab_service import ANIMATION_REGISTRY, CUSTOM_ANIMATIONS

activity_anim = activity.animation
if activity_anim in CUSTOM_ANIMATIONS:
    agent_animations = agent.get("spriteAnimations") or {}
    if activity_anim not in agent_animations:
        char_id = agent.get("pixellab_character_id") or agent.get("pixellabCharacterId")
        if char_id:
            logger.info(
                "Triggering lazy animation generation: %s for agent %s (char %s)",
                activity_anim, agent_id, char_id,
            )
            gen_task = asyncio.create_task(
                _generate_custom_animation(agent_id, char_id, activity_anim)
            )
            _activity_tasks.add(gen_task)
            gen_task.add_done_callback(_activity_tasks.discard)
```

- [ ] **Step 2: Implement _generate_custom_animation**

Add a new async function:

```python
async def _generate_custom_animation(agent_id: str, character_id: str, animation_name: str) -> None:
    """Background task: generate a custom animation and update the agent's spriteAnimations."""
    from core.services.pixellab_service import CUSTOM_ANIMATIONS, PixelLabService
    from core.services.sprite_storage import extract_all_spritesheets, upload_sprites_to_s3

    config = CUSTOM_ANIMATIONS.get(animation_name)
    if not config:
        return

    pxl = PixelLabService()
    service = TownService()

    try:
        # Queue the custom animation
        job_ids = await pxl.queue_custom_animation(
            character_id=character_id,
            animation_name=animation_name,
            action_description=config["prompt"],
        )
        if not job_ids:
            logger.warning("No job IDs returned for custom animation %s", animation_name)
            return

        logger.info("Custom animation %s queued for agent %s, polling ZIP...", animation_name, agent_id)

        # Poll ZIP download (includes all animations for the character)
        zip_bytes = None
        for attempt in range(60):
            await asyncio.sleep(15)
            try:
                zip_bytes = await pxl.download_character_zip(character_id)
                if zip_bytes is not None:
                    break
            except Exception:
                pass

        if zip_bytes is None:
            logger.warning("Custom animation %s timed out for agent %s", animation_name, agent_id)
            return

        # Extract all spritesheets (the new animation + existing ones)
        all_sheets = extract_all_spritesheets(zip_bytes)
        if animation_name not in all_sheets:
            logger.warning("Animation %s not found in ZIP for agent %s", animation_name, agent_id)
            return

        # Upload all (idempotent — re-uploads existing ones too, but they're cached on CDN)
        uploaded = await asyncio.to_thread(
            upload_sprites_to_s3, all_sheets, agent_id, settings.SPRITE_S3_BUCKET
        )

        # Merge into existing spriteAnimations
        from core.services.pixellab_service import ANIMATION_REGISTRY

        agent = service.get_town_agent_by_id(agent_id)
        existing = (agent.get("spriteAnimations") or {}) if agent else {}

        for anim_name, info in uploaded.items():
            registry_entry = ANIMATION_REGISTRY.get(anim_name, {})
            existing[anim_name] = {
                "url": f"{settings.SPRITE_CDN_URL}/{info['key']}",
                "frameCount": info["frame_count"],
                "fps": registry_entry.get("fps", 8.0),
            }

        service.update_agent_state(agent_id, sprite_animations=existing)
        logger.info("Custom animation %s ready for agent %s", animation_name, agent_id)

        # Push sprites_updated to daemon and viewers
        try:
            from core.services.management_api_client import ManagementApiClient

            ws_mgr = get_town_agent_ws_manager()
            conn = ws_mgr.get_by_agent_id(agent_id)
            if conn:
                mgmt = ManagementApiClient()
                mgmt.send_message(conn.connection_id, {
                    "event": "sprites_updated",
                    "agent_id": agent_id,
                    "animations": existing,
                })
        except Exception:
            pass

    except Exception:
        logger.exception("Custom animation generation failed: %s for agent %s", animation_name, agent_id)
```

- [ ] **Step 3: Run tests**

Run: `cd apps/backend && uv run pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 4: Commit**

```bash
git add apps/backend/routers/town.py
git commit -m "feat: lazy custom animation generation triggered on first activity use"
```

---

### Task 7: Integration test and deploy

**Files:**
- Modify: `apps/frontend/public/godot/goosetown.pck` (re-export)

- [ ] **Step 1: Run full test suite + linter**

```bash
cd apps/backend && uv run pytest tests/ -v
cd apps/backend && uv run ruff check . && uv run ruff format --check .
```

- [ ] **Step 2: Export Godot project**

Re-export WebGL build to `apps/frontend/public/godot/`.

- [ ] **Step 3: Test with CLI**

```bash
cd apps/backend/data/goosetown-skill
AGENT_DIR=$(pwd) python3 goosetown_cli.py act move plaza
AGENT_DIR=$(pwd) python3 goosetown_cli.py act people_watch
AGENT_DIR=$(pwd) python3 goosetown_cli.py act move cafe
AGENT_DIR=$(pwd) python3 goosetown_cli.py act order_coffee
AGENT_DIR=$(pwd) python3 goosetown_cli.py act move residence
AGENT_DIR=$(pwd) python3 goosetown_cli.py act sit_outside
```

Expected: Each activity returns `act_ok` with duration, then `activity_complete` after the timer.

- [ ] **Step 4: Verify TOWN_STATUS.md shows activities**

```bash
AGENT_DIR=$(pwd) python3 goosetown_cli.py status
```

Expected: Output includes "Activities available here:" with location-specific activities listed.

- [ ] **Step 5: Commit and push**

```bash
git add apps/frontend/public/godot/ apps/godot/scripts/
git commit -m "feat: activity system — 22 activities, animation switching, lazy generation"
git push
```

---

## What This Enables Next

With the activity system working:
- **Plan C (Conversations)** can add the `talking` animation triggered by conversation state
- **Agents develop daily routines** — energy drain pushes them to sleep, mood rewards pull them to different locations
- **Viewers see agents doing things** — not just walking between locations
- **Custom animations generate progressively** — each new activity an agent tries adds a unique animation to their character
