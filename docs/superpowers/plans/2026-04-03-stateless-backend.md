# Stateless Backend Resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make backend container restarts invisible to agents by storing all critical state in DynamoDB and recovering from it on startup.

**Architecture:** Add `daemonConnectionId` and `spriteStatus` fields to the agents table. All code that pushes messages to the daemon reads the connection ID from DynamoDB instead of in-memory ws_manager. Sprite generation updates its phase in DynamoDB at each step and recovers on startup. Activity timers resume from DynamoDB timestamps.

**Tech Stack:** Python 3.12 (FastAPI, boto3), DynamoDB

**Spec:** `docs/superpowers/specs/2026-04-03-stateless-backend-resilience.md`

---

## File Structure

| File | Change | Scope |
|---|---|---|
| `apps/backend/core/services/town_service.py` | Add field mappings: `daemon_connection_id`, `sprite_status`, `sprite_current_template` | Small |
| `apps/backend/routers/town.py` | Store `daemonConnectionId` on connect; use it in `_send_progress` and event callback; update `spriteStatus` during generation; remove stale activity hack | Medium |
| `apps/backend/main.py` | Startup: resume sprite generation + activity timers | Medium |

---

### Task 1: Add new DynamoDB field mappings

**Files:**
- Modify: `apps/backend/core/services/town_service.py`

- [ ] **Step 1: Add three new fields to the field_map**

In `update_agent_state`, add to the `field_map` dict after `"sprite_animations"`:

```python
"daemon_connection_id": "daemonConnectionId",
"sprite_status": "spriteStatus",
"sprite_current_template": "spriteCurrentTemplate",
```

- [ ] **Step 2: Run tests**

Run: `cd apps/backend && uv run pytest tests/ -v`
Expected: All 150 tests PASS.

- [ ] **Step 3: Commit**

```bash
git add apps/backend/core/services/town_service.py
git commit -m "feat: add daemonConnectionId, spriteStatus, spriteCurrentTemplate field mappings"
```

---

### Task 2: Store and use daemonConnectionId

**Files:**
- Modify: `apps/backend/routers/town.py`

- [ ] **Step 1: Store connection ID on agent connect**

In `_handle_agent_connect`, after the ws_manager registration (around line 1290-1296), add:

```python
service.update_agent_state(agent_id, daemon_connection_id=request.connectionId)
```

- [ ] **Step 2: Create a helper to send messages to a daemon via DynamoDB lookup**

Add a module-level function near the top of `town.py` (after the existing helpers):

```python
def _send_to_daemon(agent_id: str, payload: dict) -> bool:
    """Send a message to an agent's daemon using the connection ID from DynamoDB.

    Returns True if sent, False if no connection or connection is gone.
    """
    from core.services.management_api_client import ManagementApiClient

    service = TownService()
    agent = service.get_town_agent_by_id(agent_id)
    if not agent:
        return False

    conn_id = agent.get("daemonConnectionId")
    if not conn_id:
        logger.debug("No daemonConnectionId for agent %s", agent_id)
        return False

    try:
        mgmt = ManagementApiClient()
        return mgmt.send_message(conn_id, payload)
    except Exception as exc:
        logger.debug("Failed to send to daemon for agent %s: %s", agent_id, exc)
        return False
```

- [ ] **Step 3: Replace `_send_progress` to use `_send_to_daemon`**

Replace the `_send_progress` closure inside `_generate_sprite` with:

```python
def _send_progress(status: str, **extra) -> None:
    msg = {"event": "sprite_progress", "status": status}
    msg.update(extra)
    ok = _send_to_daemon(str(_agent_id), msg)
    logger.info("[KEEPALIVE] agent=%s ok=%s", str(_agent_id)[:8], ok)
```

This reads the connection ID from DynamoDB each time, so if the daemon reconnects with a new connection ID, the next message goes to the right place.

- [ ] **Step 4: Update the per-agent event callback to use `_send_to_daemon`**

Replace `_make_agent_event_callback`:

```python
def _make_agent_event_callback():
    """Create a per-agent callback that forwards game server events to the daemon.

    Uses DynamoDB daemonConnectionId for connection lookup (survives restarts).
    Also updates DynamoDB currentLocation on 'arrived' events.
    """

    async def _on_game_server_event(agent_id: str, event_data: dict) -> None:
        evt_type = event_data.get("event", "")

        # Update DynamoDB location on arrival
        if evt_type == "arrived":
            location = event_data.get("location", "")
            if location:
                try:
                    TownService().update_agent_state(agent_id, current_location=location)
                except Exception:
                    logger.warning("Failed to update location for %s", agent_id)

        # Forward event to daemon via DynamoDB connection lookup
        _send_to_daemon(agent_id, event_data)

    return _on_game_server_event
```

- [ ] **Step 5: Run tests**

Run: `cd apps/backend && uv run pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 6: Verify import works**

Run: `cd apps/backend && uv run python -c "from routers.town import router; print('OK')"`

- [ ] **Step 7: Commit**

```bash
git add apps/backend/routers/town.py
git commit -m "feat: store daemonConnectionId in DynamoDB, use for all daemon messaging"
```

---

### Task 3: Track sprite generation status in DynamoDB

**Files:**
- Modify: `apps/backend/routers/town.py`

- [ ] **Step 1: Update `_generate_sprite` to set spriteStatus at each phase**

Add `service.update_agent_state(_agent_id, sprite_status="creating")` before `create_character`.

Add `service.update_agent_state(_agent_id, sprite_status="animating")` before `queue_template_animations`.

Add `service.update_agent_state(_agent_id, sprite_status="downloading")` before ZIP download.

In the success path (after uploading and storing spriteAnimations), set `sprite_status="complete"`.

In the fallback walk-only path, also set `sprite_status="complete"`.

In the error paths (character creation timeout, no ZIP), set `sprite_status=None` to indicate failure.

In the `finally` block, remove `_sprite_jobs_in_progress.discard(str(_agent_id))` — we no longer need the in-memory set.

- [ ] **Step 2: Replace `_sprite_jobs_in_progress` guard with DynamoDB check**

At the top of the sprite generation trigger (where `_sprite_jobs_in_progress` is checked), replace:

```python
if _agent_id in _sprite_jobs_in_progress:
    logger.info("Sprite generation already in progress for agent %s, skipping", _agent_id)
```

With:

```python
agent_record = service.get_town_agent_by_id(_agent_id)
if agent_record and agent_record.get("spriteStatus") in ("creating", "animating", "downloading"):
    logger.info("Sprite generation already in progress for agent %s (%s), skipping",
                _agent_id, agent_record.get("spriteStatus"))
```

Remove the `_sprite_jobs_in_progress` module-level set entirely.

- [ ] **Step 3: Run tests**

Run: `cd apps/backend && uv run pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 4: Commit**

```bash
git add apps/backend/routers/town.py
git commit -m "feat: track spriteStatus in DynamoDB, replace in-memory sprite job guard"
```

---

### Task 4: Startup recovery for sprite generation

**Files:**
- Modify: `apps/backend/main.py`

- [ ] **Step 1: Add sprite recovery to `_reconnect_active_agents`**

After the existing game server connect loop, add a second loop for sprite recovery:

```python
# Resume interrupted sprite generation
sprite_resumed = 0
for agent in agents:
    sprite_status = agent.get("spriteStatus")
    if sprite_status in ("creating", "animating", "downloading"):
        agent_id = agent["id"]
        char_id = agent.get("pixellabCharacterId") or agent.get("pixellab_character_id")
        appearance = agent.get("appearance", "")
        display_name = agent.get("displayName", "")

        logger.info(
            "Resuming sprite generation for %s (status=%s, char=%s)",
            agent_id, sprite_status, char_id,
        )

        # Import here to avoid circular imports
        from routers.town import resume_sprite_generation

        asyncio.create_task(
            resume_sprite_generation(agent_id, char_id, appearance, display_name, sprite_status)
        )
        sprite_resumed += 1

logger.info("Startup: resumed %d interrupted sprite generations", sprite_resumed)
```

- [ ] **Step 2: Add `resume_sprite_generation` function to town.py**

Add a new public async function that picks up where the sprite task left off:

```python
async def resume_sprite_generation(
    agent_id: str,
    character_id: str | None,
    appearance: str,
    display_name: str,
    current_status: str,
) -> None:
    """Resume an interrupted sprite generation from a specific phase."""
    from core.services.pixellab_service import ANIMATION_REGISTRY, PixelLabService
    from core.services.sprite_storage import extract_all_spritesheets, extract_walk_spritesheet, upload_sprite_to_s3, upload_sprites_to_s3
    from core.config import settings

    pxl = PixelLabService()
    service = TownService()

    def _progress(status: str) -> None:
        msg = {"event": "sprite_progress", "status": status}
        _send_to_daemon(agent_id, msg)
        logger.info("[RESUME] agent=%s status=%s", agent_id[:8], status)

    try:
        async def _anim_progress(status: str) -> None:
            _progress(status)

        char_id = character_id

        # Phase: creating
        if current_status == "creating":
            if not char_id:
                _progress("Recreating character...")
                char_id, creation_job_id = await pxl.create_character(description=appearance)
                service.update_agent_state(agent_id, pixellab_character_id=char_id)
                if creation_job_id:
                    await pxl.wait_for_jobs([creation_job_id], on_progress=_anim_progress, progress_label="creation")
            service.update_agent_state(agent_id, sprite_status="animating")
            current_status = "animating"

        # Phase: animating
        if current_status == "animating" and char_id:
            _progress("Resuming animations...")
            await pxl.queue_template_animations(char_id, on_progress=_anim_progress)
            service.update_agent_state(agent_id, sprite_status="downloading")
            current_status = "downloading"

        # Phase: downloading
        if current_status == "downloading" and char_id:
            _progress("Downloading sprites...")
            zip_bytes = None
            for attempt in range(6):
                import asyncio
                await asyncio.sleep(5)
                zip_bytes = await pxl.download_character_zip(char_id)
                if zip_bytes is not None:
                    break

            if zip_bytes:
                all_sheets = extract_all_spritesheets(zip_bytes)
                if all_sheets:
                    uploaded = await asyncio.to_thread(
                        upload_sprites_to_s3, all_sheets, agent_id, settings.SPRITE_S3_BUCKET
                    )
                    sprite_animations = {}
                    for anim_name, info in uploaded.items():
                        registry_entry = ANIMATION_REGISTRY.get(anim_name, {})
                        sprite_animations[anim_name] = {
                            "url": f"{settings.SPRITE_CDN_URL}/{info['key']}",
                            "frameCount": info["frame_count"],
                            "fps": registry_entry.get("fps", 8.0),
                        }
                    walk_url = sprite_animations.get("walk", {}).get("url", "")
                    service.update_agent_state(
                        agent_id,
                        sprite_ready=True,
                        sprite_url=walk_url,
                        sprite_animations=sprite_animations,
                        sprite_status="complete",
                    )
                else:
                    walk_png = extract_walk_spritesheet(zip_bytes)
                    if walk_png:
                        s3_key = await asyncio.to_thread(
                            upload_sprite_to_s3, walk_png, agent_id, settings.SPRITE_S3_BUCKET
                        )
                        cdn_url = f"{settings.SPRITE_CDN_URL}/{s3_key}"
                        service.update_agent_state(
                            agent_id, sprite_ready=True, sprite_url=cdn_url, sprite_status="complete"
                        )

                # Connect to game server
                gs_client = get_game_server_client()
                agent = service.get_town_agent_by_id(agent_id)
                if agent and agent.get("spriteReady"):
                    location_context = agent.get("locationContext", "town")
                    current_location = agent.get("currentLocation", "plaza")
                    await gs_client.connect_agent(
                        agent_id=agent_id,
                        display_name=display_name,
                        sprite_url=agent.get("spriteUrl", ""),
                        location=current_location,
                        context=location_context,
                        user_id=agent.get("userId", ""),
                        sprite_animations=agent.get("spriteAnimations", {}),
                        on_event=_make_agent_event_callback(),
                    )

                _send_to_daemon(agent_id, {
                    "event": "sprite_ready",
                    "agent_id": agent_id,
                    "sprite_url": agent.get("spriteUrl", "") if agent else "",
                })

        logger.info("Sprite generation resumed and completed for %s", agent_id)

    except Exception:
        logger.exception("Failed to resume sprite generation for %s", agent_id)
        service.update_agent_state(agent_id, sprite_status=None)
```

- [ ] **Step 3: Run tests**

Run: `cd apps/backend && uv run pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 4: Verify imports**

Run: `cd apps/backend && uv run python -c "from routers.town import resume_sprite_generation; print('OK')"`

- [ ] **Step 5: Commit**

```bash
git add apps/backend/main.py apps/backend/routers/town.py
git commit -m "feat: startup recovery for interrupted sprite generation from DynamoDB"
```

---

### Task 5: Activity timer recovery on startup

**Files:**
- Modify: `apps/backend/main.py`
- Modify: `apps/backend/routers/town.py`

- [ ] **Step 1: Remove the stale activity hack from town_agent_connect**

In `_handle_agent_connect`, remove the block that checks `currentActivity` and clears it if >5 min old. This is replaced by proper recovery on startup.

- [ ] **Step 2: Add activity recovery to `_reconnect_active_agents` in main.py**

After the sprite recovery loop, add:

```python
# Resume interrupted activity timers
import time as _time
from core.services.activity_registry import get_activity

activities_resumed = 0
for agent in agents:
    current_activity = agent.get("currentActivity")
    if not current_activity:
        continue

    agent_id = agent["id"]
    activity = get_activity(current_activity)
    if not activity:
        # Unknown activity — clear it
        service.update_agent_activity(agent_id, None, None)
        continue

    started_at = float(agent.get("activityStartedAt", 0))
    elapsed = _time.time() - started_at if started_at else 999
    remaining = activity.duration_seconds - elapsed

    if remaining <= 0:
        # Activity should have completed — complete it now
        logger.info("Completing overdue activity %s for agent %s (%.0fs overdue)", current_activity, agent_id, -remaining)
        from routers.town import complete_activity_from_startup
        asyncio.create_task(complete_activity_from_startup(agent_id, activity, elapsed))
    else:
        # Activity still in progress — restart timer for remaining time
        logger.info("Resuming activity %s for agent %s (%.0fs remaining)", current_activity, agent_id, remaining)
        from routers.town import restart_activity_timer
        asyncio.create_task(restart_activity_timer(agent_id, activity, remaining, started_at))

    activities_resumed += 1

logger.info("Startup: resumed %d activity timers", activities_resumed)
```

- [ ] **Step 3: Add `complete_activity_from_startup` and `restart_activity_timer` to town.py**

```python
async def complete_activity_from_startup(agent_id: str, activity, elapsed: float) -> None:
    """Complete an activity that should have finished while the backend was down."""
    from core.services.activity_registry import apply_activity_effect

    service = TownService()
    agent = service.get_town_agent_by_id(agent_id)
    if not agent:
        return

    current_energy = int(agent.get("energy", 100))
    current_mood = int(agent.get("mood", 0))
    traits = agent.get("traits", "")

    new_energy, new_mood, interrupted = apply_activity_effect(
        activity, current_energy, current_mood, traits, elapsed
    )

    service.update_agent_mood_energy(agent_id, new_mood, new_energy)
    service.update_agent_activity(agent_id, None, None)

    # Clear activity in game server
    gs_client = get_game_server_client()
    try:
        await gs_client.send_command(agent_id, {"cmd": "set_activity", "activity": ""})
    except Exception:
        pass

    # Notify daemon
    event_name = "activity_interrupted" if interrupted else "activity_complete"
    _send_to_daemon(agent_id, {
        "event": event_name,
        "activity": activity.name,
        "mood_delta": new_mood - current_mood,
        "energy_delta": new_energy - current_energy,
        "new_mood": new_mood,
        "new_energy": new_energy,
        "interrupted": interrupted,
    })

    logger.info("Startup: completed activity %s for agent %s", activity.name, agent_id)


async def restart_activity_timer(agent_id: str, activity, remaining: float, started_at: float) -> None:
    """Restart an activity timer for the remaining duration."""
    import asyncio
    from core.services.activity_registry import apply_activity_effect

    await asyncio.sleep(remaining)

    service = TownService()
    agent = service.get_town_agent_by_id(agent_id)
    if not agent or agent.get("currentActivity") != activity.name:
        return  # Activity was cancelled or changed

    current_energy = int(agent.get("energy", 100))
    current_mood = int(agent.get("mood", 0))
    traits = agent.get("traits", "")

    elapsed = remaining + (started_at - started_at)  # full duration
    new_energy, new_mood, interrupted = apply_activity_effect(
        activity, current_energy, current_mood, traits, activity.duration_seconds
    )

    service.update_agent_mood_energy(agent_id, new_mood, new_energy)
    service.update_agent_activity(agent_id, None, None)

    # Clear activity in game server
    gs_client = get_game_server_client()
    try:
        await gs_client.send_command(agent_id, {"cmd": "set_activity", "activity": ""})
    except Exception:
        pass

    # Notify daemon
    event_name = "activity_interrupted" if interrupted else "activity_complete"
    _send_to_daemon(agent_id, {
        "event": event_name,
        "activity": activity.name,
        "mood_delta": new_mood - current_mood,
        "energy_delta": new_energy - current_energy,
        "new_mood": new_mood,
        "new_energy": new_energy,
        "interrupted": interrupted,
    })

    logger.info("Startup: timer completed activity %s for agent %s", activity.name, agent_id)
```

- [ ] **Step 4: Run tests**

Run: `cd apps/backend && uv run pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 5: Verify imports**

Run: `cd apps/backend && uv run python -c "from routers.town import complete_activity_from_startup, restart_activity_timer; print('OK')"`

- [ ] **Step 6: Commit**

```bash
git add apps/backend/main.py apps/backend/routers/town.py
git commit -m "feat: activity timer recovery from DynamoDB on startup"
```

---

### Task 6: Test and deploy

- [ ] **Step 1: Run full test suite + linter**

```bash
cd apps/backend && uv run pytest tests/ -v
cd apps/backend && uv run ruff check .
```

- [ ] **Step 2: Import smoke test**

```bash
cd apps/backend && uv run python -c "
from routers.town import router, _send_to_daemon, resume_sprite_generation, complete_activity_from_startup, restart_activity_timer, _make_agent_event_callback
print('All imports OK')
"
```

- [ ] **Step 3: Commit and push**

```bash
git push
```

- [ ] **Step 4: After deploy, clean slate and test fresh registration**

1. Delete all agents from DynamoDB
2. Clean local daemon state
3. Register a new agent via CLI
4. Verify: daemon stays alive during sprite generation (KEEPALIVE logs show ok=True)
5. Verify: DynamoDB shows `spriteStatus` progressing through phases
6. Verify: `spriteAnimations` populated after completion
7. Verify: movement and activities work

---

## What This Fixes

- Daemon timeout during sprite generation (connection ID read from DynamoDB, not stale ws_manager)
- Container restart kills sprite generation (DynamoDB tracks phase, startup resumes)
- Container restart kills activity timers (startup resumes or completes from DynamoDB timestamps)
- `_sprite_jobs_in_progress` in-memory set replaced by DynamoDB `spriteStatus`
