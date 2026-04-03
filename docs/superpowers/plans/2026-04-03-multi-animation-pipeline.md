# Multi-Animation Sprite Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the walk-only sprite pipeline with a two-phase system: template animations at registration (agent playable in ~5 min), and lazy custom animations generated on first activity use (~8 min each, cached forever).

**Architecture:** Backend generates 6 template animations at registration via PixelLab, polls the ZIP download endpoint (423 while pending, bytes when done), extracts all spritesheets, uploads to S3, stores a `spriteAnimations` map in DynamoDB. Custom animations are queued lazily when an agent first performs an activity that needs one. Godot agent.gd holds a dict of textures and switches animation based on activity/movement state. Fallback chain: activity animation → idle → walk frame 0.

**Tech Stack:** Python 3.12 (FastAPI, Pillow, boto3, httpx), Godot 4.6 (GDScript), PixelLab API v2, DynamoDB, S3 + CloudFront CDN.

**Spec:** `docs/superpowers/specs/2026-04-03-activity-animation-system-design.md`

---

## File Structure

### Backend (Modified)

| File | Change | Responsibility |
|---|---|---|
| `apps/backend/core/services/sprite_storage.py` | Major rewrite | Generic animation extraction, batch extraction from ZIP, batch S3 upload |
| `apps/backend/core/services/pixellab_service.py` | Add methods | Animation registry config, batch template queuing, custom animation queuing with cost confirmation |
| `apps/backend/core/services/town_service.py:209` | Add field | `sprite_animations` → `spriteAnimations` field mapping |
| `apps/backend/routers/town.py:378-579` | Major rewrite | Two-phase sprite generation background task |
| `apps/backend/core/services/game_server_client.py:81-89` | Add field | Pass `sprite_animations` in connect params |

### Backend (New)

| File | Responsibility |
|---|---|
| `apps/backend/tests/unit/services/test_sprite_storage.py` | Tests for animation extraction and batch extraction |

### Godot (Modified)

| File | Change | Responsibility |
|---|---|---|
| `apps/godot/scripts/agent.gd` | Major rewrite of animation system | Multi-texture dict, per-animation config, play_animation(), auto-switching |
| `apps/godot/scripts/agent_manager.gd` | Add multi-sprite loading | Load animation URLs from world_state, handle sprites_updated, route textures by animation name |
| `apps/godot/scripts/server/agent_controller.gd:145-163` | Add field to broadcast | Include `spriteAnimations` in world_state |

---

## PixelLab API Reference (verified from MCP docs)

**Template animations:** Pass `template_animation_id`. Auto-generates all stored directions (4). Costs 1 gen/direction. No `directions` param needed.

**Custom animations:** Omit `template_animation_id`, pass `action_description`. **Defaults to south only** — MUST pass `directions` explicitly. Requires two-call flow: first without `confirm_cost` to see price, then with `confirm_cost=true`.

**Cost discrepancy:** MCP docs say 20-40 gen/direction for custom. UI shows Custom V3 at 5 gen/direction. The REST API (`/characters/animations`) may have different pricing. Task 3 includes a verification step.

**ZIP download:** Returns HTTP 423 while ANY animation is pending. Returns bytes when ALL are done. This is our completion signal — no need to poll individual job IDs.

---

### Task 1: Generalize sprite_storage.py

**Files:**
- Modify: `apps/backend/core/services/sprite_storage.py`
- Create: `apps/backend/tests/unit/services/test_sprite_storage.py`

- [ ] **Step 1: Write tests for generic animation extraction**

Create `apps/backend/tests/unit/services/test_sprite_storage.py`:

```python
"""Tests for sprite_storage extraction logic."""

import io
import zipfile

from PIL import Image

from core.services.sprite_storage import (
    DIRECTION_ROWS,
    FRAME_SIZE,
    extract_animation_spritesheet,
    extract_all_spritesheets,
    extract_walk_spritesheet,
)


def _make_test_zip(animations: dict[str, dict[str, int]]) -> bytes:
    """Build a minimal ZIP with colored frames.

    Args:
        animations: {animation_name: {direction: frame_count}}
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for anim_name, directions in animations.items():
            for direction, frame_count in directions.items():
                for i in range(frame_count):
                    img = Image.new("RGBA", (96, 96), (255, 0, 0, 255))
                    frame_buf = io.BytesIO()
                    img.save(frame_buf, format="PNG")
                    zf.writestr(
                        f"animations/{anim_name}/{direction}/frame_{i:04d}.png",
                        frame_buf.getvalue(),
                    )
    return buf.getvalue()


def _make_rotation_zip() -> bytes:
    """Build a ZIP with only static rotation PNGs (no animations)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for direction in DIRECTION_ROWS:
            img = Image.new("RGBA", (96, 96), (0, 255, 0, 255))
            frame_buf = io.BytesIO()
            img.save(frame_buf, format="PNG")
            zf.writestr(f"rotations/{direction}.png", frame_buf.getvalue())
    return buf.getvalue()


class TestExtractAnimationSpritesheet:
    def test_all_four_directions(self):
        zip_bytes = _make_test_zip({
            "walk": {"south": 6, "west": 6, "east": 6, "north": 6},
        })
        result = extract_animation_spritesheet(zip_bytes, "walk")
        assert result is not None
        png_bytes, frame_count = result
        assert frame_count == 6
        img = Image.open(io.BytesIO(png_bytes))
        assert img.width == 6 * FRAME_SIZE
        assert img.height == 4 * FRAME_SIZE

    def test_partial_directions(self):
        zip_bytes = _make_test_zip({"walk": {"south": 6, "east": 6}})
        result = extract_animation_spritesheet(zip_bytes, "walk")
        assert result is not None
        _, frame_count = result
        assert frame_count == 6

    def test_animation_not_found(self):
        zip_bytes = _make_test_zip({"walk": {"south": 6}})
        assert extract_animation_spritesheet(zip_bytes, "run") is None

    def test_variable_frame_count(self):
        zip_bytes = _make_test_zip({
            "breathing-idle": {"south": 4, "west": 4, "east": 4, "north": 4},
        })
        result = extract_animation_spritesheet(zip_bytes, "breathing-idle")
        assert result is not None
        png_bytes, frame_count = result
        assert frame_count == 4
        img = Image.open(io.BytesIO(png_bytes))
        assert img.width == 4 * FRAME_SIZE

    def test_bad_zip(self):
        assert extract_animation_spritesheet(b"not a zip", "walk") is None


class TestExtractAllSpritesheets:
    def test_multiple_animations(self):
        zip_bytes = _make_test_zip({
            "walk": {"south": 6, "west": 6, "east": 6, "north": 6},
            "breathing-idle": {"south": 4, "west": 4, "east": 4, "north": 4},
        })
        results = extract_all_spritesheets(zip_bytes)
        assert "walk" in results
        assert "breathing-idle" in results
        assert results["walk"][1] == 6
        assert results["breathing-idle"][1] == 4

    def test_empty_zip(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w"):
            pass
        assert extract_all_spritesheets(buf.getvalue()) == {}

    def test_bad_zip(self):
        assert extract_all_spritesheets(b"not a zip") == {}


class TestExtractWalkSpritesheetBackwardCompat:
    def test_returns_png_bytes_from_walk_animation(self):
        zip_bytes = _make_test_zip({
            "walk": {"south": 6, "west": 6, "east": 6, "north": 6},
        })
        result = extract_walk_spritesheet(zip_bytes)
        assert result is not None
        img = Image.open(io.BytesIO(result))
        assert img.width == 6 * FRAME_SIZE

    def test_falls_back_to_rotations(self):
        zip_bytes = _make_rotation_zip()
        result = extract_walk_spritesheet(zip_bytes)
        assert result is not None
        img = Image.open(io.BytesIO(result))
        assert img.width == 6 * FRAME_SIZE  # rotations repeated across 6 columns
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_sprite_storage.py -v`
Expected: FAIL — `extract_animation_spritesheet` and `extract_all_spritesheets` not defined.

- [ ] **Step 3: Implement generalized extraction**

Rewrite `apps/backend/core/services/sprite_storage.py`. Keep `_crop_and_resize` unchanged. Replace `extract_walk_spritesheet` body and add new functions:

```python
def extract_animation_spritesheet(
    zip_bytes: bytes, animation_name: str
) -> tuple[bytes, int] | None:
    """Extract a single animation from a PixelLab ZIP into a spritesheet.

    Looks for: animations/<animation_name>/<direction>/<frame>.png
    Detects frame count dynamically from ZIP contents.

    Returns (png_bytes, frame_count) or None if animation not found.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        logger.error("Invalid ZIP file from PixelLab")
        return None

    all_files = zf.namelist()
    prefix = f"animations/{animation_name}/"
    anim_files = [f for f in all_files if f.startswith(prefix) and f.endswith(".png")]

    if not anim_files:
        return None

    # Detect frame count from the direction with the most frames
    max_frames = 0
    for direction_name in DIRECTION_ROWS:
        dir_frames = [f for f in anim_files if f"/{direction_name}/" in f]
        max_frames = max(max_frames, len(dir_frames))

    if max_frames == 0:
        return None

    frame_count = max_frames
    sheet_w = frame_count * FRAME_SIZE
    sheet_h = len(DIRECTION_ROWS) * FRAME_SIZE
    sheet = Image.new("RGBA", (sheet_w, sheet_h), (0, 0, 0, 0))
    directions_found = 0

    for direction_name, row_index in DIRECTION_ROWS.items():
        dir_frames = sorted([f for f in anim_files if f"/{direction_name}/" in f])
        if not dir_frames:
            continue
        directions_found += 1
        for frame_idx, frame_path in enumerate(dir_frames[:frame_count]):
            try:
                with zf.open(frame_path) as fp:
                    frame_img = Image.open(fp).convert("RGBA")
                    frame_img = _crop_and_resize(frame_img, FRAME_SIZE)
                    sheet.paste(frame_img, (frame_idx * FRAME_SIZE, row_index * FRAME_SIZE))
            except Exception as e:
                logger.warning("Failed to process frame %s: %s", frame_path, e)

    if directions_found == 0:
        return None

    logger.info(
        "Composited %s spritesheet: %d directions, %d frames",
        animation_name, directions_found, frame_count,
    )
    buf = io.BytesIO()
    sheet.save(buf, format="PNG")
    return buf.getvalue(), frame_count


def extract_all_spritesheets(
    zip_bytes: bytes,
) -> dict[str, tuple[bytes, int]]:
    """Extract all animations from a PixelLab ZIP.

    Returns dict: animation_name -> (png_bytes, frame_count).
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        logger.error("Invalid ZIP file from PixelLab")
        return {}

    animation_names: set[str] = set()
    for f in zf.namelist():
        if f.startswith("animations/") and f.endswith(".png"):
            parts = f.split("/")
            if len(parts) >= 3:
                animation_names.add(parts[1])

    results: dict[str, tuple[bytes, int]] = {}
    for anim_name in sorted(animation_names):
        result = extract_animation_spritesheet(zip_bytes, anim_name)
        if result is not None:
            results[anim_name] = result

    logger.info("Extracted %d animations from ZIP: %s", len(results), list(results.keys()))
    return results


def _extract_rotation_fallback(zip_bytes: bytes) -> bytes | None:
    """Fallback: composite static rotation PNGs into a walk-like spritesheet."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        return None

    all_files = zf.namelist()
    rotation_files = [f for f in all_files if f.startswith("rotations/") and f.endswith(".png")]
    if not rotation_files:
        return None

    sheet = Image.new("RGBA", (SHEET_WIDTH, SHEET_HEIGHT), (0, 0, 0, 0))
    directions_found = 0

    for direction_name, row_index in DIRECTION_ROWS.items():
        rot_path = f"rotations/{direction_name}.png"
        if rot_path not in all_files:
            continue
        directions_found += 1
        try:
            with zf.open(rot_path) as fp:
                frame_img = Image.open(fp).convert("RGBA")
                frame_img = _crop_and_resize(frame_img, FRAME_SIZE)
                for frame_idx in range(FRAME_COUNT):
                    sheet.paste(frame_img, (frame_idx * FRAME_SIZE, row_index * FRAME_SIZE))
        except Exception as e:
            logger.warning("Failed to process rotation %s: %s", rot_path, e)

    if directions_found == 0:
        return None

    logger.info("Composited rotation fallback spritesheet: %d directions", directions_found)
    buf = io.BytesIO()
    sheet.save(buf, format="PNG")
    return buf.getvalue()


def extract_walk_spritesheet(zip_bytes: bytes) -> bytes | None:
    """Legacy wrapper — extracts walk animation only. Returns PNG bytes or None."""
    result = extract_animation_spritesheet(zip_bytes, "walk")
    if result is not None:
        return result[0]
    return _extract_rotation_fallback(zip_bytes)


def upload_sprites_to_s3(
    animations: dict[str, tuple[bytes, int]],
    agent_id: str,
    bucket: str | None = None,
) -> dict[str, dict]:
    """Upload all animation spritesheets to S3.

    Returns dict: animation_name -> {"key": s3_key, "frame_count": N}
    """
    bucket = bucket or settings.SPRITE_S3_BUCKET
    s3 = boto3.client("s3")
    results: dict[str, dict] = {}

    for anim_name, (png_bytes, frame_count) in animations.items():
        key = f"sprites/{agent_id}/{anim_name}.png"
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=png_bytes,
            ContentType="image/png",
            CacheControl="public, max-age=31536000",
        )
        results[anim_name] = {"key": key, "frame_count": frame_count}
        logger.info("Uploaded %s sprite to s3://%s/%s (%d frames)", anim_name, bucket, key, frame_count)

    return results
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_sprite_storage.py -v`
Expected: All 10 tests PASS.

- [ ] **Step 5: Run full test suite for regressions**

Run: `cd apps/backend && uv run pytest tests/ -v`
Expected: All 138+ tests PASS. `extract_walk_spritesheet` backward compat preserved.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/sprite_storage.py apps/backend/tests/unit/services/test_sprite_storage.py
git commit -m "feat: generalize sprite extraction to support any animation from PixelLab ZIP"
```

---

### Task 2: Add animation registry and batch methods to pixellab_service.py

**Files:**
- Modify: `apps/backend/core/services/pixellab_service.py`

- [ ] **Step 1: Add animation registry config**

Add at module level, after the `PIXELLAB_API_URL` constant:

```python
# Animation registry — single source of truth for all animation assets.
# Template animations are generated at registration.
# Custom animations are generated lazily on first activity use.
ANIMATION_REGISTRY: dict[str, dict] = {
    # Templates (1 gen/direction, auto all directions)
    "walk":       {"type": "template", "template_id": "walk", "fps": 8.0},
    "idle":       {"type": "template", "template_id": "breathing-idle", "fps": 4.0},
    "run":        {"type": "template", "template_id": "running-6-frames", "fps": 12.0},
    "drink":      {"type": "template", "template_id": "drinking", "fps": 6.0},
    "crouch":     {"type": "template", "template_id": "crouching", "fps": 6.0},
    "pick_up":    {"type": "template", "template_id": "picking-up", "fps": 6.0},
    # Custom V3 (generated lazily on first use)
    "sitting":    {"type": "custom", "prompt": "sitting relaxed, gently swaying", "frames": 8, "fps": 4.0},
    "reading":    {"type": "custom", "prompt": "sitting cross-legged reading a book, turning pages", "frames": 8, "fps": 4.0},
    "writing":    {"type": "custom", "prompt": "sitting at a table writing in a notebook", "frames": 8, "fps": 6.0},
    "working":    {"type": "custom", "prompt": "sitting hunched over a laptop typing", "frames": 8, "fps": 6.0},
    "performing":  {"type": "custom", "prompt": "standing arms raised performing expressively", "frames": 8, "fps": 6.0},
    "exercising": {"type": "custom", "prompt": "doing jumping jacks energetically", "frames": 8, "fps": 8.0},
    "gaming":     {"type": "custom", "prompt": "leaning forward excited, hands over a game board", "frames": 8, "fps": 6.0},
    "gardening":  {"type": "custom", "prompt": "crouching tending plants with hands", "frames": 8, "fps": 4.0},
    "cooking":    {"type": "custom", "prompt": "standing at counter stirring a pot", "frames": 8, "fps": 6.0},
    "sleeping":   {"type": "custom", "prompt": "lying down, chest rising and falling slowly", "frames": 8, "fps": 3.0},
    "talking":    {"type": "custom", "prompt": "standing gesturing while speaking to someone", "frames": 8, "fps": 6.0},
}

TEMPLATE_ANIMATIONS = {k: v for k, v in ANIMATION_REGISTRY.items() if v["type"] == "template"}
CUSTOM_ANIMATIONS = {k: v for k, v in ANIMATION_REGISTRY.items() if v["type"] == "custom"}
```

- [ ] **Step 2: Add batch template method**

Add to `PixelLabService` class:

```python
async def queue_template_animations(self, character_id: str) -> list[str]:
    """Queue all template animations for a character. Returns flat list of job IDs."""
    all_job_ids: list[str] = []
    for anim_name, config in TEMPLATE_ANIMATIONS.items():
        try:
            job_ids = await self.animate_character(character_id, config["template_id"])
            all_job_ids.extend(job_ids)
            logger.info("Queued template %s for %s: %d jobs", anim_name, character_id, len(job_ids))
        except Exception as e:
            logger.warning("Failed to queue template %s for %s: %s", anim_name, character_id, e)
    logger.info(
        "Queued %d template animation jobs for %s (%d animations)",
        len(all_job_ids), character_id, len(TEMPLATE_ANIMATIONS),
    )
    return all_job_ids
```

- [ ] **Step 3: Add custom animation method with cost confirmation**

Add to `PixelLabService` class:

```python
async def queue_custom_animation(
    self,
    character_id: str,
    animation_name: str,
    action_description: str,
    directions: list[str] | None = None,
) -> list[str]:
    """Queue a custom animation with cost confirmation.

    Two-call flow: first call gets cost estimate, second confirms.
    Returns list of job IDs, or empty list on failure.
    """
    if directions is None:
        directions = ["south", "west", "east", "north"]

    async with httpx.AsyncClient() as client:
        # First call: get cost estimate (no confirm_cost)
        body = {
            "character_id": character_id,
            "action_description": action_description,
            "animation_name": animation_name,
            "directions": directions,
        }
        resp = await client.post(
            f"{PIXELLAB_API_URL}/characters/animations",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=body,
            timeout=30.0,
        )
        if resp.status_code != 200:
            # Some APIs return the cost info in a non-200 response
            logger.info("Custom animation cost check for %s: %s %s", animation_name, resp.status_code, resp.text[:200])

        # Second call: confirm cost
        body["confirm_cost"] = True
        resp = await client.post(
            f"{PIXELLAB_API_URL}/characters/animations",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=body,
            timeout=30.0,
        )
        if resp.status_code != 200:
            logger.error("Custom animation %s failed: %s %s", animation_name, resp.status_code, resp.text[:500])
            return []
        resp.raise_for_status()
        data = resp.json()
        job_ids = data.get("background_job_ids", [])
        logger.info("Queued custom animation %s for %s: %d jobs", animation_name, character_id, len(job_ids))
        return job_ids
```

- [ ] **Step 4: Commit**

```bash
git add apps/backend/core/services/pixellab_service.py
git commit -m "feat: animation registry config and batch/custom animation methods"
```

---

### Task 3: Verify PixelLab custom animation cost via REST API

**Files:** None (manual verification)

This is a one-time check to confirm whether the REST API at `/characters/animations` supports Custom V3 pricing (5 gen/direction) or only Pro pricing (20-40 gen/direction). The UI shows both options; the API may differ.

- [ ] **Step 1: Test custom animation cost with a dry run**

Use an existing character to call the custom animation API without `confirm_cost`:

```bash
cd apps/backend && uv run python -c "
import asyncio
from core.services.pixellab_service import PixelLabService

async def check():
    pxl = PixelLabService()
    # Use the existing pixel-fox character
    char_id = '299aa7e3-4fd3-4493-9714-66ec75a56d35'
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                'https://api.pixellab.ai/v2/characters/animations',
                headers={'Authorization': f'Bearer {pxl.api_key}'},
                json={
                    'character_id': char_id,
                    'action_description': 'sitting relaxed',
                    'animation_name': 'test_sitting',
                    'directions': ['south'],
                },
                timeout=30.0,
            )
            print(f'Status: {resp.status_code}')
            print(f'Response: {resp.text[:500]}')
    except Exception as e:
        print(f'Error: {e}')

asyncio.run(check())
"
```

Expected: Response showing the cost per direction. Check whether it says ~5 gen or ~20-40 gen.

- [ ] **Step 2: Document the actual cost in the animation registry**

If the cost differs from expected, update the spec and registry comments. If the API doesn't support the V3 endpoint via REST, we may need to use a different endpoint path or parameter.

- [ ] **Step 3: Commit any changes**

```bash
git add apps/backend/core/services/pixellab_service.py
git commit -m "docs: verified PixelLab custom animation cost via REST API"
```

---

### Task 4: Add spriteAnimations field to town_service.py

**Files:**
- Modify: `apps/backend/core/services/town_service.py:209-220`

- [ ] **Step 1: Add field mapping**

In `update_agent_state`, add to the `field_map` dict at line 209:

```python
"sprite_animations": "spriteAnimations",
```

Add it after the `"sprite_url"` entry so the dict reads:

```python
field_map = {
    "current_conversation_id": "currentConversationId",
    "last_heartbeat_at": "lastHeartbeatAt",
    "last_conversation_at": "lastConversationAt",
    "status_message": "statusMessage",
    "wake_at": "wakeAt",
    "wake_timezone": "wakeTimezone",
    "sprite_ready": "spriteReady",
    "sprite_url": "spriteUrl",
    "sprite_animations": "spriteAnimations",
    "location_context": "locationContext",
    "current_location": "currentLocation",
}
```

- [ ] **Step 2: Run tests**

Run: `cd apps/backend && uv run pytest tests/ -v`
Expected: All tests PASS. This is a pure addition — no existing behavior changes.

- [ ] **Step 3: Commit**

```bash
git add apps/backend/core/services/town_service.py
git commit -m "feat: add spriteAnimations field mapping to town service"
```

---

### Task 5: Rewrite sprite generation background task

**Files:**
- Modify: `apps/backend/routers/town.py:378-579`

This is the largest change. Replace the entire `_generate_sprite` inner function with the two-phase approach. The outer task creation code (lines 378-384 and 572-579) stays the same.

- [ ] **Step 1: Replace _generate_sprite function body**

The new function replaces everything between `async def _generate_sprite() -> None:` and the `finally:` block. Key changes from the old code:

1. Creation polling: unchanged (30 attempts × 10s)
2. **Phase 1**: Queue all 6 template animations → poll ZIP download (not individual jobs) → extract all → upload batch → mark sprite ready → connect to game server
3. **No Phase 2 here** — lazy custom animation is triggered from the activity handler (Plan B scope)

```python
async def _generate_sprite() -> None:
    try:
        from core.services.management_api_client import ManagementApiClient
        from core.services.sprite_storage import (
            extract_all_spritesheets,
            extract_walk_spritesheet,
            upload_sprite_to_s3,
            upload_sprites_to_s3,
        )
        from core.services.pixellab_service import (
            ANIMATION_REGISTRY,
            PixelLabService,
        )

        pxl = PixelLabService()
        service = TownService()

        def _send_progress(status: str, **extra) -> None:
            try:
                _ws_mgr = get_town_agent_ws_manager()
                _conn = _ws_mgr.get_by_agent_id(str(_agent_id))
                if _conn:
                    _mgmt = ManagementApiClient()
                    msg = {"event": "sprite_progress", "status": status}
                    msg.update(extra)
                    _mgmt.send_message(_conn.connection_id, msg)
            except Exception:
                pass

        # ---- Create character ----
        _send_progress("Creating your character...")
        char_id, creation_job_id = await pxl.create_character(
            description=_appearance
        )
        service.update_agent_state(_agent_id, pixellab_character_id=char_id)
        logger.info("PixelLab returned char_id=%s job=%s for %s", char_id, creation_job_id, _agent_id)

        # Poll creation (up to 5 min)
        if creation_job_id:
            for attempt in range(30):
                await asyncio.sleep(10)
                try:
                    job = await pxl.get_job_status(creation_job_id)
                    status = job.get("status", "unknown")
                    _send_progress(f"Creating your character sprite ({status})...")
                    if status == "completed":
                        break
                except Exception as e:
                    logger.warning("Failed to check creation job: %s", e)
            else:
                logger.warning("Character creation timed out for %s", char_id)

        # ---- Queue all template animations ----
        _send_progress("Generating animations...")
        all_job_ids = await pxl.queue_template_animations(char_id)
        logger.info("Queued %d template animation jobs for %s", len(all_job_ids), char_id)

        # ---- Poll ZIP download (423 while pending, bytes when done) ----
        # Max wait: 45 attempts × 15s = 11.25 minutes
        zip_bytes = None
        for attempt in range(45):
            await asyncio.sleep(15)
            _send_progress(f"Generating animations... ({attempt + 1}/45)")
            try:
                zip_bytes = await pxl.download_character_zip(char_id)
                if zip_bytes is not None:
                    logger.info("All animations ready for %s (attempt %d)", char_id, attempt + 1)
                    break
            except Exception as e:
                logger.warning("ZIP download error for %s: %s", char_id, e)

        if zip_bytes is None:
            logger.warning("Animation generation timed out for %s", char_id)
            # Last resort: try to get whatever is available
            try:
                zip_bytes = await pxl.download_character_zip(char_id)
            except Exception:
                pass

        if zip_bytes is None:
            logger.error("No ZIP available for %s, sprite generation failed", char_id)
            return

        # ---- Extract all animation spritesheets ----
        all_sheets = extract_all_spritesheets(zip_bytes)

        if not all_sheets:
            # Fallback to legacy walk extraction
            walk_png = extract_walk_spritesheet(zip_bytes)
            if walk_png:
                s3_key = await asyncio.to_thread(
                    upload_sprite_to_s3, walk_png, str(_agent_id), settings.SPRITE_S3_BUCKET
                )
                cdn_url = f"{settings.SPRITE_CDN_URL}/{s3_key}"
                service.update_agent_state(_agent_id, sprite_ready=True, sprite_url=cdn_url)
                logger.info("Fallback: walk-only sprite ready for %s", _agent_id)
            else:
                logger.error("No animations extracted from ZIP for %s", char_id)
                return
        else:
            # Upload all spritesheets to S3
            uploaded = await asyncio.to_thread(
                upload_sprites_to_s3, all_sheets, str(_agent_id), settings.SPRITE_S3_BUCKET
            )

            # Build spriteAnimations metadata for DynamoDB
            sprite_animations: dict = {}
            for anim_name, info in uploaded.items():
                registry_entry = ANIMATION_REGISTRY.get(anim_name, {})
                sprite_animations[anim_name] = {
                    "url": f"{settings.SPRITE_CDN_URL}/{info['key']}",
                    "frameCount": info["frame_count"],
                    "fps": registry_entry.get("fps", 8.0),
                }

            walk_url = sprite_animations.get("walk", {}).get("url", "")
            service.update_agent_state(
                _agent_id,
                sprite_ready=True,
                sprite_url=walk_url,
                sprite_animations=sprite_animations,
            )
            cdn_url = walk_url
            logger.info("Sprite ready for %s: %d animations", _agent_id, len(sprite_animations))

        # ---- Connect agent to game server ----
        gs_client = get_game_server_client()
        _ws_mgr = get_town_agent_ws_manager()
        _conn = _ws_mgr.get_by_agent_id(str(_agent_id))

        if _conn:
            try:
                await gs_client.connect_agent(
                    agent_id=str(_agent_id),
                    display_name=_display_name,
                    sprite_url=cdn_url,
                    location=_conn.current_location or "plaza",
                    context=_conn.location_context or "town",
                    user_id=_conn.user_id,
                )
                gs_client.clear_event_handlers()

                async def _forward_event(fwd_agent_id: str, event_data: dict) -> None:
                    try:
                        ws_mgr = get_town_agent_ws_manager()
                        conn = ws_mgr.get_by_agent_id(fwd_agent_id)
                        if conn:
                            fwd_mgmt = ManagementApiClient()
                            fwd_mgmt.send_message(conn.connection_id, event_data)
                    except Exception:
                        pass

                for evt in ("arrived", "move_started", "spawned", "nearby",
                            "nearby_left", "command_error", "agent_state",
                            "locations", "transitioned"):
                    gs_client.on(evt, _forward_event)

                async def _on_arrived(fwd_agent_id: str, event_data: dict) -> None:
                    loc = event_data.get("location", "")
                    if loc:
                        TownService().update_agent_state(fwd_agent_id, current_location=loc)

                gs_client.on("arrived", _on_arrived)
                logger.info("Agent %s connected to game server", _agent_id)
            except Exception as e:
                logger.warning("Failed to connect agent %s to game server: %s", _agent_id, e)

        # ---- Notify daemon ----
        try:
            if _conn:
                _mgmt = ManagementApiClient()
                _mgmt.send_message(_conn.connection_id, {
                    "event": "sprite_ready",
                    "agent_id": str(_agent_id),
                    "sprite_url": cdn_url,
                })
        except Exception:
            pass

    except Exception:
        logger.exception("Sprite generation failed for agent %s", _agent_id)
    finally:
        _sprite_jobs_in_progress.discard(str(_agent_id))
```

- [ ] **Step 2: Run full test suite**

Run: `cd apps/backend && uv run pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 3: Commit**

```bash
git add apps/backend/routers/town.py
git commit -m "feat: two-phase sprite pipeline — batch templates, ZIP-based completion polling"
```

---

### Task 6: Pass sprite_animations through game_server_client.py

**Files:**
- Modify: `apps/backend/core/services/game_server_client.py:42-93`

- [ ] **Step 1: Add sprite_animations parameter to connect_agent**

Update the method signature at line 42:

```python
async def connect_agent(
    self,
    agent_id: str,
    display_name: str,
    sprite_url: str,
    location: str,
    context: str = "town",
    user_id: str = "",
    sprite_animations: dict | None = None,
) -> None:
```

Update `connect_params` dict at line 81 to include:

```python
connect_params = {
    "type": "agent",
    "agent_id": agent_id,
    "name": display_name,
    "sprite_url": sprite_url,
    "location": location,
    "context": context,
    "user_id": user_id,
    "sprite_animations": sprite_animations or {},
}
```

- [ ] **Step 2: Update connect_agent call in town.py sprite task to pass animations**

In the sprite generation task (Task 5), find the `gs_client.connect_agent()` call and add `sprite_animations`:

```python
sprite_anims = sprite_animations if 'sprite_animations' in dir() else {}
await gs_client.connect_agent(
    agent_id=str(_agent_id),
    display_name=_display_name,
    sprite_url=cdn_url,
    location=_conn.current_location or "plaza",
    context=_conn.location_context or "town",
    user_id=_conn.user_id,
    sprite_animations=sprite_anims,
)
```

Also update the reconnect connect_agent call in `town.py` (the `town_agent_connect` handler around line 1307) to pass the agent's stored spriteAnimations:

```python
await gs_client.connect_agent(
    agent_id=agent_id,
    display_name=agent.get("displayName", agent_name),
    sprite_url=agent.get("spriteUrl", ""),
    location=current_location,
    context=location_context,
    user_id=user_id,
    sprite_animations=agent.get("spriteAnimations", {}),
)
```

- [ ] **Step 3: Run tests**

Run: `cd apps/backend && uv run pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 4: Commit**

```bash
git add apps/backend/core/services/game_server_client.py apps/backend/routers/town.py
git commit -m "feat: pass spriteAnimations through game server client connect"
```

---

### Task 7: Update Godot agent.gd for multi-animation

**Files:**
- Modify: `apps/godot/scripts/agent.gd`

- [ ] **Step 1: Replace single-texture variables with animation dict**

Replace lines 35-38:

```gdscript
# OLD:
var _spritesheet: Texture2D = null
var _anim_frame: int = 0
var _anim_timer: float = 0.0
const ANIM_FPS: float = 8.0
```

With:

```gdscript
## animation_name -> Texture2D
var _spritesheets: Dictionary = {}
## animation_name -> { "frame_count": int, "fps": float }
var _anim_configs: Dictionary = {}
## Currently playing animation name
var _current_animation: String = "walk"
var _anim_frame: int = 0
var _anim_timer: float = 0.0
const DEFAULT_FPS: float = 8.0
const DEFAULT_FRAME_COUNT: int = 6
## Per-animation sprite data
var sprite_animations: Dictionary = {}
```

- [ ] **Step 2: Add play_animation() and update set_spritesheet()**

Replace `set_spritesheet` (line 222-225) with:

```gdscript
func set_spritesheet(tex: Texture2D, animation_name: String = "walk") -> void:
	_spritesheets[animation_name] = tex
	if animation_name == _current_animation or _spritesheets.size() == 1:
		_current_animation = animation_name
		_anim_frame = 0
		_update_sprite_frame()


func set_animation_configs(configs: Dictionary) -> void:
	## configs: { "walk": { "frameCount": 6, "fps": 8.0 }, ... }
	for anim_name in configs:
		var cfg: Dictionary = configs[anim_name]
		_anim_configs[anim_name] = {
			"frame_count": int(cfg.get("frameCount", DEFAULT_FRAME_COUNT)),
			"fps": float(cfg.get("fps", DEFAULT_FPS)),
		}


func play_animation(animation_name: String) -> void:
	if animation_name == _current_animation:
		return
	if not _spritesheets.has(animation_name):
		# Fallback: don't switch if we don't have this animation
		return
	_current_animation = animation_name
	_anim_frame = 0
	_anim_timer = 0.0
	_update_sprite_frame()
```

- [ ] **Step 3: Update animation loop in _physics_process**

Replace lines 149-160 (the `# Animate` block):

```gdscript
	# Animate
	if not _spritesheets.is_empty():
		# Auto-select animation based on state
		var target_anim: String = "walk"
		if not _is_moving:
			target_anim = "idle" if _spritesheets.has("idle") else "walk"

		if target_anim != _current_animation and _spritesheets.has(target_anim):
			play_animation(target_anim)

		# Frame cycling
		var cfg: Dictionary = _anim_configs.get(_current_animation, {})
		var fps: float = cfg.get("fps", DEFAULT_FPS)
		var fc: int = cfg.get("frame_count", DEFAULT_FRAME_COUNT)

		if _is_moving or _current_animation != "walk":
			_anim_timer += delta
			if _anim_timer >= 1.0 / fps:
				_anim_timer -= 1.0 / fps
				_anim_frame = (_anim_frame + 1) % fc
				_update_sprite_frame()
		else:
			if _anim_frame != 0:
				_anim_frame = 0
				_update_sprite_frame()
```

- [ ] **Step 4: Update _update_sprite_frame to use current animation**

Replace lines 163-179:

```gdscript
func _update_sprite_frame() -> void:
	var tex: Texture2D = _spritesheets.get(_current_animation)
	if tex == null:
		if _spritesheets.is_empty():
			return
		tex = _spritesheets.values()[0]

	var cfg: Dictionary = _anim_configs.get(_current_animation, {})
	var fc: int = cfg.get("frame_count", DEFAULT_FRAME_COUNT)
	var safe_frame: int = _anim_frame % fc

	var row: int = DIR_ROW.get(facing, 0)
	var region := Rect2(
		safe_frame * FRAME_SIZE,
		row * FRAME_SIZE,
		FRAME_SIZE,
		FRAME_SIZE,
	)
	var atlas := AtlasTexture.new()
	atlas.atlas = tex
	atlas.region = region
	sprite.texture = atlas
```

- [ ] **Step 5: Commit**

```bash
git add apps/godot/scripts/agent.gd
git commit -m "feat: agent.gd multi-animation — dict of textures, play_animation(), auto-switching"
```

---

### Task 8: Update agent_manager.gd to load multiple sprites

**Files:**
- Modify: `apps/godot/scripts/agent_manager.gd`

- [ ] **Step 1: Handle spriteAnimations in world_state**

After line 98 (`_known_agents[agent_id] = agent`), add animation config loading:

```gdscript
			# Load animation configs if present in world_state
			var animations: Dictionary = agent_data.get("spriteAnimations", {})
			if not animations.is_empty():
				agent.set_animation_configs(animations)
				agent.sprite_animations = animations
				for anim_name in animations:
					var anim_url: String = str(animations[anim_name].get("url", ""))
					if anim_url != "" and anim_name != "walk":
						_load_sprite_for_agent(agent_id, anim_url, anim_name)
```

- [ ] **Step 2: Add helper to load additional sprites**

Add after `_on_sprite_loaded`:

```gdscript
func _load_sprite_for_agent(agent_id: String, url: String, animation_name: String) -> void:
	if not OS.has_feature("web"):
		return
	var js_code := """
		(function() {
			fetch('%s')
				.then(function(r) { return r.blob(); })
				.then(function(b) {
					var reader = new FileReader();
					reader.onloadend = function() {
						var base64 = reader.result.split(',')[1];
						if (window.godotUpdateAgentSprite) {
							window.godotUpdateAgentSprite(JSON.stringify({
								id: '%s',
								base64: base64,
								animation: '%s'
							}));
						}
					};
					reader.readAsDataURL(b);
				})
				.catch(function(e) { console.error('Sprite load failed:', e); });
		})();
	""" % [url, agent_id, animation_name]
	JavaScriptBridge.eval(js_code)
```

- [ ] **Step 3: Update _on_sprite_loaded to handle animation name**

Replace the `_on_sprite_loaded` function (lines 157-172):

```gdscript
func _on_sprite_loaded(args: Array) -> void:
	var parsed: Dictionary = JSON.parse_string(str(args[0]))
	if parsed == null:
		return
	var agent_id: String = str(parsed.get("id", ""))
	var base64: String = str(parsed.get("base64", ""))
	var animation_name: String = str(parsed.get("animation", "walk"))
	if agent_id == "" or base64 == "" or agent_id not in agents:
		return

	var raw: PackedByteArray = Marshalls.base64_to_raw(base64)
	var image: Image = Image.new()
	if image.load_png_from_buffer(raw) == OK:
		var tex: ImageTexture = ImageTexture.create_from_image(image)
		agents[agent_id].set_spritesheet(tex, animation_name)
		print("AgentManager: Loaded %s spritesheet for %s (%dx%d)" % [animation_name, agent_id, image.get_width(), image.get_height()])
```

- [ ] **Step 4: Commit**

```bash
git add apps/godot/scripts/agent_manager.gd
git commit -m "feat: agent_manager loads multiple animation sprites from world_state"
```

---

### Task 9: Broadcast spriteAnimations in game server world_state

**Files:**
- Modify: `apps/godot/scripts/server/agent_controller.gd:145-163`

- [ ] **Step 1: Store and broadcast sprite_animations**

In `spawn_agent` (after line 83 `_arrived_at[peer_id] = location_id`), add:

```gdscript
	# Store sprite animation metadata for world_state broadcast
	var sprite_anims: Dictionary = data.get("sprite_animations", {})
	if not sprite_anims.is_empty():
		agent_node.sprite_animations = sprite_anims
```

In `get_world_state_for_viewer`, add `"spriteAnimations"` to the agent dict (after `"spriteUrl"` at line 159):

```gdscript
"spriteAnimations": agent_node.sprite_animations,
```

In `get_agent_data` (line 166-176), add sprite_animations to the returned dict:

```gdscript
return {
    "agent_id": _peer_to_id.get(peer_id, ""),
    "name": agent_node.display_name,
    "sprite_url": agent_node.sprite_url,
    "sprite_animations": agent_node.sprite_animations,
    "facing": agent_node.facing,
}
```

- [ ] **Step 2: Commit**

```bash
git add apps/godot/scripts/server/agent_controller.gd
git commit -m "feat: broadcast spriteAnimations in game server world_state"
```

---

### Task 10: Run tests, export Godot, and deploy

**Files:**
- Modify: `apps/frontend/public/godot/goosetown.pck` (re-export)

- [ ] **Step 1: Run full backend test suite**

Run: `cd apps/backend && uv run pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 2: Run linter**

Run: `cd apps/backend && uv run ruff check . && uv run ruff format --check .`
Expected: No errors.

- [ ] **Step 3: Export Godot project**

Open the Godot project at `apps/godot/` and export the WebGL build to `apps/frontend/public/godot/`.

- [ ] **Step 4: Test with existing agent**

```bash
cd apps/backend/data/goosetown-skill
AGENT_DIR=$(pwd) python3 goosetown_cli.py act move plaza
AGENT_DIR=$(pwd) python3 goosetown_cli.py act move residence
AGENT_DIR=$(pwd) python3 goosetown_cli.py act move exit
AGENT_DIR=$(pwd) python3 goosetown_cli.py act move library
```

All should return `{"status": "arrived", ...}`.

- [ ] **Step 5: Verify DynamoDB has spriteAnimations**

```bash
aws dynamodb get-item --table-name goosetown-dev-agents \
  --key '{"id":{"S":"<agent-id>"}}' \
  --profile isol8-admin --region us-east-1 \
  --query 'Item.spriteAnimations'
```

Expected: Map with walk, idle, run, drink, crouch, pick_up entries.

- [ ] **Step 6: Commit with Godot export**

```bash
git add apps/frontend/public/godot/ apps/godot/scripts/ apps/backend/
git commit -m "feat: multi-animation sprite pipeline — batch templates, Godot animation switching"
```

- [ ] **Step 7: Push and deploy**

```bash
git push
```

Wait for CI/CD deploy to dev. Test with a fresh agent registration to exercise the full new pipeline.

---

## What This Enables Next

With multi-animation support in place:

- **Plan B (Activity System):** Activity execution triggers `play_animation()` in Godot. Lazy custom animation generation on first activity use.
- **Plan C (Conversation Visuals):** `talking` animation triggered when agents are in conversation. Game server facing logic. Hover-to-read UI.
