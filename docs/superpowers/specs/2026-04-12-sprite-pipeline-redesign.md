# Sprite Pipeline Redesign — API-based Animation Resolution

## Problem

PixelLab's character ZIP uses opaque internal folder names (`walking-5a721b84`, `animating-abe2dc22`) for animation data. Each folder contains frames for one direction of one animation. These names don't map to our animation names (`walk`, `idle`) or PixelLab's own template IDs (`walk`, `breathing-idle`). The current code tries to match via `name_map` but fails because the keys never match.

Result: `spriteUrl=""` in DynamoDB, sprites don't render, and S3 is littered with files keyed by opaque PixelLab hashes instead of clean names.

Additionally, source frames (136×136) are pre-resized to 48×48 before compositing, throwing away quality. Godot renders at 48×48 — no downscaling benefit.

## Solution

**ZIP download + API cross-reference.** Keep the efficient single-ZIP download but resolve animation names using the structured character API. Store spritesheets at 96×96 per frame (2×) so Godot downscales for quality.

## Section 1: PixelLabService — `fetch_animation_spritesheets`

New method in `apps/backend/core/services/pixellab_service.py`:

```python
async def fetch_animation_spritesheets(
    self, character_id: str
) -> dict[str, dict[str, list[bytes]]]:
```

**Flow:**
1. Call `get_character(char_id)` → get `animations` array with clean names (`walk`, `breathing-idle`), each entry having `name`, `direction`, `frame_count`.
2. Download ZIP via existing `download_character_zip(char_id)`.
3. Parse `metadata.json` from ZIP → get `frames.animations` dict mapping opaque folder names → `{direction: [frame_paths]}`.
4. Build lookup from API: for each animation entry, record `(name, direction, frame_count)`.
5. Match ZIP folders to API animations: for each ZIP folder, check its direction and frame count against the API animation list. Assign to the matching animation name.
6. Extract frame PNGs from ZIP for matched folders.
7. Return `{animation_name: {direction: [frame_png_bytes]}}` keyed by PixelLab's clean API name.

**Matching logic:** Each ZIP folder has exactly one direction and N frames. The API tells us which animation names exist and how many frames each has. For each ZIP folder, find the API animation with matching `(direction, frame_count)`. If multiple API animations share the same frame count, use the most recent (latest folder in ZIP). For current signup animations (walk=6 frames, idle=4 frames), there's no ambiguity.

**Duplicate handling:** PixelLab creates duplicate folders when animations are re-queued (e.g., after backend restart). The matching picks the LAST folder per (animation, direction) pair — last wins, which is the most recent generation.

**What stays:** `download_character_zip` remains as a utility. `get_character`, `get_existing_animation_names` unchanged.

## Section 2: sprite_storage — `composite_animation_spritesheet` + 96px frames

### New function

Replace `extract_all_spritesheets` and `extract_animation_spritesheet` with:

```python
def composite_animation_spritesheet(
    direction_frames: dict[str, list[bytes]],
    frame_size: int = 96,
) -> tuple[bytes, int] | None:
```

Takes pre-grouped `{direction: [frame_png_bytes]}` and composites into a single spritesheet. Layout: `frame_count` columns × 4 rows (south/west/east/north), `frame_size` px per cell.

Uses existing `_crop_and_resize` but targets 96×96 instead of 48×48.

### Frame size change

| Constant | Old | New |
|----------|-----|-----|
| `FRAME_SIZE` | 48 | 96 |
| `SHEET_WIDTH` | 288 (6×48) | 576 (6×96) |
| `SHEET_HEIGHT` | 192 (4×48) | 384 (4×96) |

`build_static_spritesheet_from_rotations` also updates to use 96×96 frames.

### `upload_sprite_to_s3` filename parameter

Add optional `filename` parameter (default `"walk.png"`) so callers can write `idle.png`, `talking.png`:

```python
def upload_sprite_to_s3(
    png_bytes: bytes, agent_id: str, bucket: str | None = None, filename: str = "walk.png"
) -> str:
    key = f"sprites/{agent_id}/{filename}"
```

Backward compatible — existing callers without `filename` still write `walk.png`.

### Deletions

- `extract_all_spritesheets` — replaced by `composite_animation_spritesheet` + PixelLabService grouping
- `extract_animation_spritesheet` — same
- `_resolve_name` reference (broken, never defined)

### Retained

- `build_static_spritesheet_from_rotations` — Phase B static sprite
- `upload_sprite_to_s3` / `upload_sprites_to_s3` — S3 upload
- `composite_v2_animation` — custom/talking animations (V2 API)
- `_crop_and_resize` — shared utility (updated target size)

## Section 3: Pipeline (town.py) — Phase C rewrite

### New Phase C flow

```
queue_signup_animations(char_id)
    ↓
fetch_animation_spritesheets(char_id)   ← replaces download_zip + extract_all
    ↓
for each animation:
    composite_animation_spritesheet(direction_frames)
    upload_sprite_to_s3(png, agent_id, filename=f"{friendly_name}.png")
    ↓
update DynamoDB with clean sprite_animations dict
push to game server via update_agent_sprites
flip onboarding_phase to "alive"
```

### Name mapping

```python
template_id_to_friendly = {
    cfg["template_id"]: name
    for name, cfg in ANIMATION_REGISTRY.items()
    if cfg.get("template_id")
}
# Result: {"walk": "walk", "breathing-idle": "idle", "running-6-frames": "run", ...}
```

Applied once after `fetch_animation_spritesheets` returns. All downstream code uses friendly names.

### S3 result

```
sprites/{agent_id}/walk.png       ← 576×384, 4 directions, 6 frames
sprites/{agent_id}/idle.png       ← 576×384, 4 directions, 4 frames
sprites/{agent_id}/talking.png    ← from V2 custom path (unchanged)
```

### Custom animations (talking)

The V2 API path (`queue_custom_animation` → `get_v2_animation_frames` → `composite_v2_animation`) is already keyed by friendly name and doesn't use the ZIP. The only change: `composite_v2_animation` should also target 96×96 frames for consistency with the template animation sheets.

### Imports

Remove: `extract_all_spritesheets`, `extract_animation_spritesheet`
Add: `composite_animation_spritesheet`

### Deleted plumbing

- `template_name_map` / `name_map` construction in Phase C
- `template_name_map` in `resume_sprite_generation`
- All `name_map=` parameters in calls

## Section 4: `resume_sprite_generation` — same new flow

Same changes as Phase C. Key additions:

1. Read `existing_sprite_url` from DynamoDB at top of function (fallback for `walk_url` if frame-count lookup fails).
2. Replace `download_character_zip` + `extract_all_spritesheets` with `fetch_animation_spritesheets` + `composite_animation_spritesheet`.
3. Set `onboarding_phase="alive"` on completion.

## Section 5: Godot — 96px frame constants

### `agent.gd`

```gdscript
const FRAME_SIZE: int = 96    # was 48
const SHEET_WIDTH: int = 576  # 6 × 96
const SHEET_HEIGHT: int = 384 # 4 × 96
```

AtlasTexture regions in `_update_sprite_frame` use these constants — they adjust automatically. Sprite2D renders at the same visual size on the map (Godot handles the downscale).

### `agent_manager.gd`

No changes — it passes URLs and configs, doesn't know about pixel sizes.

### Re-export `.pck`

Required after changing constants in `agent.gd`.

## Testing

- Unit test `composite_animation_spritesheet` with mock frame PNGs (4 directions × N frames → verify sheet dimensions)
- Unit test `fetch_animation_spritesheets` with mock API response + mock ZIP
- Integration test: register fresh agent → verify `spriteUrl` is non-empty, ends with `/walk.png`, S3 object exists at clean path
- Manual: verify sprites render in Godot viewer at correct size and quality
