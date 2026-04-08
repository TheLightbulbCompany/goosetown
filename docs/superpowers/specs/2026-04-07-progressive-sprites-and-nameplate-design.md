# Progressive Sprites, Trimmed Animations, and Nameplate Polish — Design

**Status:** approved
**Date:** 2026-04-07
**Author:** Claude (brainstorming skill)

## Goal

Three bundled changes to the agent registration and presentation pipeline:

1. **Trim the auto-generated animation set** to only what the game currently uses and fix the template-id naming bug that has been leaving `spriteUrl=""` on all new agents.
2. **Progressive sprite readiness** — agents appear in the apartment as static standing characters the moment character creation is done, then "come to life" when the breathing-idle animation finishes uploading. Includes a daemon-side onboarding welcome during the static phase.
3. **Hover nameplate polish** — replace the current plain Label with a rounded white pill using the Upheaval Pro title font.

All three ship together because they share the same code paths (`routers/town.py` registration background, `sprite_storage.py`, `agent.gd`, `agent.tscn`).

## Architecture overview

```
registration → create_character (PixelLab) → wait creation job
  → build static spritesheet from 4 rotations → upload static.png
  → spriteReady=true, spriteUrl=static url, spriteAnimations={walk: static}
  → onboardingPhase="static"
  → connect agent to game server (visible standing)
  → phase 2 background: queue_signup_animations(walk, idle, talking)
      → each animation: upload → gs_client.update_agent_sprites()
      → game server broadcasts new spriteAnimations in world_state
      → agent_manager.gd loads new sheets, agent comes alive
      → when idle arrives: onboardingPhase="alive"
```

Only three animations ship at signup: **walk**, **idle** (breathing-idle), **talking**. The registry keeps its other entries (run, drink, crouch, pick_up, sitting, reading, etc.) available for lazy generation via existing pathways, but they are no longer auto-generated.

## Section 1 — Animation registry trim + template-id fix

### 1.1 `auto_generate` flag

Add a boolean `auto_generate` field to every `ANIMATION_REGISTRY` entry in `apps/backend/core/services/pixellab_service.py`. Only three entries get `auto_generate=True`:

- `walk` (template: `walk`)
- `idle` (template: `breathing-idle`)
- `talking` (custom V2)

All other entries get `auto_generate=False`. They stay functional via the existing lazy-trigger path — this change only affects what the signup background task queues.

### 1.2 `queue_signup_animations`

Rename `PixelLabService.queue_template_animations` → `queue_signup_animations`. The new method iterates all registry entries where `auto_generate=True`, dispatching by type:

- `type=="template"` → call existing `animate_character(character_id, animation=config["template_id"])` and wait for the job
- `type=="custom"` → call existing `queue_custom_animation(character_id, animation_name, config["prompt"])` and wait

Each iteration waits for its own jobs to complete before queuing the next (avoids PixelLab concurrent-job limits).

### 1.3 Template-id → friendly-name mapping

Root cause of the current `spriteUrl=""` bug: PixelLab stores animations in the ZIP under their `template_id` (or for V2, under the request's animation name), so when `extract_all_spritesheets` reads `animations/<name>/` folders it gets things like `breathing-idle`, `running-6-frames`, and for V2 jobs `animating-<hash>`. The downstream code does `sprite_animations.get("walk", ...)` and finds nothing.

Fix in `apps/backend/core/services/sprite_storage.py::extract_all_spritesheets`:

1. Accept an optional `name_map: dict[str, str] | None` parameter mapping folder-name → friendly name.
2. After discovering animation folders, for each discovered folder name: if the name appears in `name_map`, use the mapped friendly name as the result key; otherwise fall through to the raw folder name (preserves backward compat for lazily-generated animations).

Caller (`routers/town.py`) builds the name_map before calling extract:

```python
name_map = {}
for friendly_name, cfg in ANIMATION_REGISTRY.items():
    if cfg["type"] == "template":
        name_map[cfg["template_id"]] = friendly_name
    # custom animations will be handled by the V2 path, not templates
```

For custom V2 animations, `queue_custom_animation` already composites direction frames in-memory and returns `(png_bytes, frame_count)` per animation; those don't go through the ZIP path and are already keyed by the friendly name we pass in. No changes needed for custom.

## Section 2 — Progressive sprite readiness

### 2.1 New helper: `build_static_spritesheet_from_rotations`

New function in `apps/backend/core/services/sprite_storage.py`:

```python
def build_static_spritesheet_from_rotations(
    rotation_bytes: dict[str, bytes]  # {"south": png, "west": png, "east": png, "north": png}
) -> bytes:
    """Build a 288x192 spritesheet from 4 direction rotations by duplicating each
    direction's single frame across all 6 columns of its row.

    Returns PNG bytes ready to upload to S3.
    """
```

Layout matches the existing walk sheet format that `agent.gd` already consumes:

- 288×192 canvas
- Row 0 = south (repeated 6 times)
- Row 1 = west (repeated 6 times)
- Row 2 = east (repeated 6 times)
- Row 3 = north (repeated 6 times)
- Each frame cropped + resized to 48×48 using existing `_crop_and_resize` helper

Because every column of a given row is identical, the agent's frame cycler runs harmlessly and the character appears still regardless of facing.

### 2.2 New PixelLabService helper: `fetch_all_rotations`

```python
async def fetch_all_rotations(character_id: str) -> dict[str, bytes]:
    """Download all 4 rotation PNGs for a character. Returns {direction: png_bytes}.
    Used by the static spritesheet builder."""
```

Implementation reuses `get_character()` to get `rotation_urls`, then `httpx.AsyncClient` to download each PNG. Returns an empty dict if any download fails.

### 2.3 Registration flow changes in `routers/town.py`

The `_sprite_pipeline_task` (or equivalent registration background task) is restructured into three phases:

**Phase A — Character creation (unchanged):**
- `pxl.create_character(description)`
- Poll creation job until complete

**Phase B — Static ready (new):**
1. `rotations = await pxl.fetch_all_rotations(character_id)`
2. `static_png = build_static_spritesheet_from_rotations(rotations)`
3. Upload to S3 as `sprites/<agent_id>/static.png` via existing `upload_sprite_to_s3`
4. Build initial `sprite_animations`:
   ```python
   sprite_animations = {
       "walk": {
           "url": static_cdn_url,
           "frameCount": 6,
           "fps": 8,
       }
   }
   ```
5. Write to DynamoDB:
   ```python
   service.update_agent_state(
       agent_id,
       sprite_ready=True,
       sprite_url=static_cdn_url,
       sprite_animations=sprite_animations,
       sprite_status="static_ready",
       onboarding_phase="static",
   )
   ```
6. Call `gs_client.connect_agent(agent_id, display_name, sprite_url=static_cdn_url, sprite_animations=sprite_animations, ...)` — the agent appears standing in the apartment immediately.

If Phase B fails for any reason (fetch, build, upload), fall through to Phase C's wait-for-full-pipeline flow as a safety net. The agent then takes longer to appear but is never lost.

**Phase C — Animation generation (trimmed):**
1. Call `pxl.queue_signup_animations(character_id, on_progress=...)` which queues walk, idle, talking per Section 1.
2. After all jobs complete: download ZIP, call `extract_all_spritesheets(zip_bytes, name_map=...)` for templates (walk, idle) and merge with the in-memory result from `queue_custom_animation` for talking.
3. Upload each to S3, build the full `sprite_animations` dict.
4. For each animation that finishes successfully, call `gs_client.update_agent_sprites(agent_id, sprite_animations)` to push the updated dict to the game server.
5. When the **idle** animation specifically lands, update DynamoDB: `onboarding_phase="alive"`.
6. Finally write the complete `sprite_animations` to DynamoDB and set `sprite_status="complete"`.

### 2.4 Game server `update_agent_sprites` command

New WS command handled by both the town controller and the apartment controller:

```gdscript
# apps/godot/scripts/server/agent_controller.gd (and apartment_agent_controller.gd)
func _handle_update_agent_sprites(agent_id: String, data: Dictionary) -> void:
    if not _agent_sprites.has(agent_id):
        return
    var animations: Dictionary = data.get("spriteAnimations", {})
    _agent_sprites[agent_id] = animations
    # The next world_state broadcast will include the updated dict.
```

Add a new `_agent_sprites: Dictionary` dict (agent_id → animations map). Populate on connect, update on `update_agent_sprites`, read when building `world_state`. The world_state broadcast already includes per-agent data; add a `spriteAnimations` field populated from this dict.

### 2.5 Backend `game_server_client.update_agent_sprites`

New method in `apps/backend/core/services/game_server_client.py`:

```python
async def update_agent_sprites(
    self, agent_id: str, sprite_animations: dict
) -> None:
    ws = self._agent_connections.get(agent_id)
    if ws is None:
        return  # Agent disconnected; DynamoDB has latest
    try:
        await ws.send(json.dumps({
            "command": "update_agent_sprites",
            "agent_id": agent_id,
            "spriteAnimations": sprite_animations,
        }))
    except Exception:
        logger.exception("Failed to push sprite update for %s", agent_id)
        # Swallow — DynamoDB is the source of truth and the next full reconnect
        # will load them fresh.
```

### 2.6 Godot client — `agent_manager.gd` sprite update handling

`agent_manager.gd::_on_world_state` currently loads `spriteAnimations` from world_state on agent spawn but does not re-load when the dict changes. Update the existing-agent branch:

```gdscript
if agent_id in agents:
    # ...existing position update...
    var animations: Dictionary = agent_data.get("spriteAnimations", {})
    if not animations.is_empty():
        var current: Dictionary = agents[agent_id].sprite_animations
        for anim_name in animations:
            var new_url: String = str(animations[anim_name].get("url", ""))
            var old_url: String = str(current.get(anim_name, {}).get("url", ""))
            if new_url != "" and new_url != old_url:
                _load_sprite_for_agent(agent_id, new_url, anim_name)
        agents[agent_id].sprite_animations = animations
        agents[agent_id].set_animation_configs(animations)
```

This replaces the static walk sheet with the real walk when it arrives, and adds idle/talking as they arrive. The existing `_physics_process` logic in `agent.gd` automatically switches animations based on `current_activity` and `_is_moving`, so when idle loads the agent "comes alive" without any explicit trigger.

### 2.7 Daemon onboarding message (no new events)

Extend the existing `agent_state` event payload with the `onboardingPhase` field (backend reads it from DynamoDB and includes it in every `agent_state` event).

In `goosetown_cli.py::TownDaemon._handle_agent_state` (the method that writes TOWN_STATUS.md), check the phase:

```python
phase = state.get("agent", {}).get("onboardingPhase", "alive")
if phase == "static":
    action_section = _render_welcome_message()
else:
    action_section = _render_think_prompt(...)  # existing logic
```

`_render_welcome_message` is a static string baked into the daemon:

```
## Waking up

The sprite artists of GooseTown are still at work on your movements.
You can feel your body settling into this world but you cannot yet move.
Look around and get your bearings — you will wake fully in a moment.

When you see "Your turn" above, you can use `town_act` to move, chat, and explore.
```

The agent's LLM on its next heartbeat reads this section instead of "Your turn", so it does not attempt to act while static. No backend act-gating is needed. When the phase flips to `alive`, the next `agent_state` write replaces the welcome with the normal action prompt.

## Section 3 — Hover nameplate polish

### 3.1 Font import

- Copy `apps/frontend/public/assets/fonts/upheaval_pro.ttf` → `apps/godot/assets/fonts/upheaval_pro.ttf`
- Let Godot generate the `.import` file on next open.

### 3.2 `agent.tscn` structure change

Remove the existing `NameLabel: Label` node. Add a new subtree:

```
NameplateContainer: Control
├── position = Vector2(0, -40)
├── size hugs child automatically
├── pivot_offset = size / 2  (centered on agent)
└── Pill: Panel
    ├── theme_override_styles/panel = StyleBoxFlat
    │   ├── bg_color = Color(1, 1, 1, 0.95)
    │   ├── corner_radius_top_left/top_right/bottom_left/bottom_right = 8
    │   ├── content_margin_left/right = 6
    │   ├── content_margin_top/bottom = 2
    │   ├── shadow_color = Color(0, 0, 0, 0.2)
    │   ├── shadow_size = 1
    │   └── shadow_offset = Vector2(0, 1)
    └── NameLabel: Label (child of Pill)
        ├── theme_override_fonts/font = FontFile(upheaval_pro.ttf)
        ├── theme_override_font_sizes/font_size = 10
        ├── theme_override_colors/font_color = Color(0, 0, 0)
        ├── horizontal_alignment = 1 (center)
        └── vertical_alignment = 1 (center)
```

Godot Control auto-layout: Panel sizes to Label content + content_margin. NameplateContainer sizes to Panel. Centered above the 48×48 sprite.

### 3.3 `agent.gd` wiring

Replace:

```gdscript
@onready var name_label: Label = $NameLabel
```

with:

```gdscript
@onready var nameplate: Control = $NameplateContainer
@onready var name_label: Label = $NameplateContainer/Pill/NameLabel
```

`setup()` still sets `name_label.text = display_name`. `_update_hover()` toggles `nameplate.visible` instead of `name_label.visible`. `update_state()` hides `nameplate` when speech text becomes non-empty (existing logic, just point at the new node).

## Testing

### Backend

- Unit test `build_static_spritesheet_from_rotations` with 4 stub PNGs. Assert output is 288×192 and that each row contains 6 identical frames (byte compare).
- Unit test `extract_all_spritesheets` with a mock ZIP containing `animations/walk/`, `animations/breathing-idle/` and a name_map `{"breathing-idle": "idle"}`. Assert result keys are `walk` and `idle`.
- Integration test (mocked PixelLab HTTP) for the registration task — assert `sprite_ready=True`, `spriteUrl!=""`, `onboarding_phase="static"` after Phase B and `onboarding_phase="alive"` after idle finishes in Phase C.
- Add a unit test for `_render_welcome_message` — assert static phase daemon output contains the expected welcome text and does not contain "Your turn".

### Godot

- Smoke test `agent.tscn` in the editor — confirm the pill renders at 10pt Upheaval Pro with rounded corners and correct font.
- Manual test with three agents — confirm hover shows the pill, speech still takes priority, long names do not clip.
- Manual end-to-end — register a fresh agent, confirm it appears standing within ~30s of `join`, confirm the welcome text in TOWN_STATUS.md, confirm the agent visibly "comes alive" (breathing) when idle loads.

## Non-goals

- No backend-side act gating during the static phase (daemon-side rendering is sufficient because heartbeats are the only entry point).
- No WebSocket tip stream (discussed and rejected — overwriting a file 7 times per heartbeat is pointless).
- No changes to the existing custom-animation lazy-load path.
- No changes to the sprite deduplication / re-registration flow (separate issue).

## Risks and mitigations

- **PixelLab rotation fetch fails for a direction** — `fetch_all_rotations` returns a partial dict; `build_static_spritesheet_from_rotations` treats missing directions as transparent rows. Character renders with some directions blank until phase C catches up. Acceptable.
- **`update_agent_sprites` arrives before agent is fully spawned on game server** — game server handler early-returns if `_agent_sprites` doesn't contain the ID. Next update will catch it.
- **Daemon reads `onboardingPhase` but field is missing** — default to `"alive"`. Old agents that predate this change are treated as already alive.
