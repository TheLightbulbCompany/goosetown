# Sprite Pipeline Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace opaque ZIP folder extraction with API-resolved animation names and 96px frame compositing.

**Architecture:** PixelLabService downloads the ZIP + calls get_character() to resolve opaque folder names to clean animation names. A new `composite_animation_spritesheet` function replaces the old `extract_all_spritesheets`. Frame size increases from 48px to 96px throughout backend and Godot.

**Tech Stack:** Python 3.12, PIL/Pillow, PixelLab V2 API, Godot 4.6 GDScript, pytest

---

## Task 1: Update frame size constants + `_crop_and_resize` target in `sprite_storage.py`

**Files:**
- Modify: `apps/backend/core/services/sprite_storage.py:15-19`
- Test: `apps/backend/tests/unit/services/test_sprite_storage.py`

- [ ] **Step 1: Write the failing test**

Add to `apps/backend/tests/unit/services/test_sprite_storage.py`:

```python
def test_frame_size_is_96():
    from core.services.sprite_storage import FRAME_SIZE, SHEET_WIDTH, SHEET_HEIGHT
    assert FRAME_SIZE == 96
    assert SHEET_WIDTH == 576  # 6 * 96
    assert SHEET_HEIGHT == 384  # 4 * 96
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_sprite_storage.py::test_frame_size_is_96 -v`
Expected: FAIL — `assert 48 == 96`

- [ ] **Step 3: Update the constants**

In `apps/backend/core/services/sprite_storage.py`, lines 15-19:

```python
# Sprite sheet layout: 6 frames x 96px wide, 4 directions x 96px tall
FRAME_COUNT = 6
FRAME_SIZE = 96
SHEET_WIDTH = FRAME_COUNT * FRAME_SIZE  # 576
SHEET_HEIGHT = 4 * FRAME_SIZE  # 384
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_sprite_storage.py::test_frame_size_is_96 -v`
Expected: PASS

- [ ] **Step 5: Run full test suite to check for regressions**

Run: `cd apps/backend && uv run pytest tests/ -v`
Expected: All pass. Existing tests that create mock spritesheets may need dimension updates — fix any that fail by updating their expected dimensions from 288/192 to 576/384.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/sprite_storage.py apps/backend/tests/unit/services/test_sprite_storage.py
git commit -m "feat: increase sprite frame size from 48px to 96px"
```

---

## Task 2: Add `filename` parameter to `upload_sprite_to_s3`

**Files:**
- Modify: `apps/backend/core/services/sprite_storage.py:319-335`
- Test: `apps/backend/tests/unit/services/test_sprite_storage.py`

- [ ] **Step 1: Write the failing test**

Add to `apps/backend/tests/unit/services/test_sprite_storage.py`:

```python
from unittest.mock import patch, MagicMock

def test_upload_sprite_to_s3_custom_filename():
    with patch("core.services.sprite_storage.boto3") as mock_boto3:
        mock_s3 = MagicMock()
        mock_boto3.client.return_value = mock_s3
        from core.services.sprite_storage import upload_sprite_to_s3
        key = upload_sprite_to_s3(b"png-data", "agent-123", "test-bucket", filename="idle.png")
        assert key == "sprites/agent-123/idle.png"
        mock_s3.put_object.assert_called_once()
        call_kwargs = mock_s3.put_object.call_args[1]
        assert call_kwargs["Key"] == "sprites/agent-123/idle.png"


def test_upload_sprite_to_s3_default_filename():
    with patch("core.services.sprite_storage.boto3") as mock_boto3:
        mock_s3 = MagicMock()
        mock_boto3.client.return_value = mock_s3
        from core.services.sprite_storage import upload_sprite_to_s3
        key = upload_sprite_to_s3(b"png-data", "agent-456", "test-bucket")
        assert key == "sprites/agent-456/walk.png"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_sprite_storage.py -v -k "upload_sprite_to_s3"`
Expected: FAIL — `upload_sprite_to_s3() got an unexpected keyword argument 'filename'`

- [ ] **Step 3: Add the `filename` parameter**

In `apps/backend/core/services/sprite_storage.py`, update `upload_sprite_to_s3`:

```python
def upload_sprite_to_s3(
    png_bytes: bytes, agent_id: str, bucket: str | None = None, filename: str = "walk.png"
) -> str:
    """Upload a sprite PNG to S3 and return the S3 key.

    Key pattern: sprites/{agent_id}/{filename}
    """
    bucket = bucket or settings.SPRITE_S3_BUCKET
    s3 = boto3.client("s3")
    key = f"sprites/{agent_id}/{filename}"
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=png_bytes,
        ContentType="image/png",
        CacheControl="public, max-age=31536000",
    )
    logger.info("Uploaded sprite to s3://%s/%s", bucket, key)
    return key
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_sprite_storage.py -v -k "upload_sprite_to_s3"`
Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/sprite_storage.py apps/backend/tests/unit/services/test_sprite_storage.py
git commit -m "feat: upload_sprite_to_s3 accepts filename parameter"
```

---

## Task 3: Add `composite_animation_spritesheet` function

**Files:**
- Modify: `apps/backend/core/services/sprite_storage.py` (add new function)
- Test: `apps/backend/tests/unit/services/test_sprite_storage.py`

- [ ] **Step 1: Write the failing test**

Add to `apps/backend/tests/unit/services/test_sprite_storage.py`:

```python
import io
from PIL import Image


def _make_test_frame(width: int = 136, height: int = 136, color: tuple = (255, 0, 0, 255)) -> bytes:
    """Create a test PNG frame with a colored rectangle in the center."""
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    # Draw a 60x80 colored rectangle centered (simulates character content)
    for x in range(38, 98):
        for y in range(28, 108):
            img.putpixel((x, y), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_composite_animation_spritesheet_4_dirs_6_frames():
    from core.services.sprite_storage import composite_animation_spritesheet, FRAME_SIZE

    frames = _make_test_frame()
    direction_frames = {
        "south": [frames] * 6,
        "west": [frames] * 6,
        "east": [frames] * 6,
        "north": [frames] * 6,
    }
    result = composite_animation_spritesheet(direction_frames)
    assert result is not None
    png_bytes, frame_count = result
    assert frame_count == 6

    sheet = Image.open(io.BytesIO(png_bytes))
    assert sheet.width == 6 * FRAME_SIZE   # 576
    assert sheet.height == 4 * FRAME_SIZE  # 384


def test_composite_animation_spritesheet_partial_directions():
    from core.services.sprite_storage import composite_animation_spritesheet, FRAME_SIZE

    frames = _make_test_frame()
    direction_frames = {
        "south": [frames] * 4,
        "east": [frames] * 4,
    }
    result = composite_animation_spritesheet(direction_frames)
    assert result is not None
    png_bytes, frame_count = result
    assert frame_count == 4

    sheet = Image.open(io.BytesIO(png_bytes))
    assert sheet.width == 4 * FRAME_SIZE
    assert sheet.height == 4 * FRAME_SIZE


def test_composite_animation_spritesheet_empty_returns_none():
    from core.services.sprite_storage import composite_animation_spritesheet
    assert composite_animation_spritesheet({}) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_sprite_storage.py -v -k "composite_animation_spritesheet"`
Expected: FAIL — `ImportError: cannot import name 'composite_animation_spritesheet'`

- [ ] **Step 3: Implement `composite_animation_spritesheet`**

Add to `apps/backend/core/services/sprite_storage.py`, after `build_static_spritesheet_from_rotations`:

```python
def composite_animation_spritesheet(
    direction_frames: dict[str, list[bytes]],
    frame_size: int = FRAME_SIZE,
) -> tuple[bytes, int] | None:
    """Composite per-direction frame PNGs into a multi-direction spritesheet.

    Takes pre-grouped ``{direction: [frame_png_bytes]}`` (as returned by
    ``PixelLabService.fetch_animation_spritesheets``) and composites into a
    single spritesheet with ``frame_count`` columns and 4 direction rows.

    Each frame is cropped and resized to ``frame_size`` via ``_crop_and_resize``.

    Args:
        direction_frames: Mapping of direction name to list of raw PNG bytes
            for each animation frame. Directions not present remain transparent.
        frame_size: Pixel size for each cell in the output sheet.

    Returns:
        ``(spritesheet_png_bytes, frame_count)`` or ``None`` if no valid frames.
    """
    if not direction_frames:
        return None

    frame_count = max(len(frames) for frames in direction_frames.values())
    if frame_count == 0:
        return None

    sheet_width = frame_count * frame_size
    sheet_height = 4 * frame_size
    sheet = Image.new("RGBA", (sheet_width, sheet_height), (0, 0, 0, 0))
    directions_found = 0

    for direction_name, row_index in DIRECTION_ROWS.items():
        frames = direction_frames.get(direction_name)
        if not frames:
            continue
        directions_found += 1
        for frame_idx, frame_bytes in enumerate(frames[:frame_count]):
            try:
                frame_img = Image.open(io.BytesIO(frame_bytes)).convert("RGBA")
                frame_img = _crop_and_resize(frame_img, frame_size)
                sheet.paste(frame_img, (frame_idx * frame_size, row_index * frame_size))
            except Exception as e:
                logger.warning(
                    "Failed to process animation frame %s/%d: %s",
                    direction_name, frame_idx, e,
                )

    if directions_found == 0:
        return None

    logger.info(
        "Composited animation spritesheet: %d directions, %d frames, %dpx",
        directions_found, frame_count, frame_size,
    )
    buf = io.BytesIO()
    sheet.save(buf, format="PNG")
    return buf.getvalue(), frame_count
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_sprite_storage.py -v -k "composite_animation_spritesheet"`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/sprite_storage.py apps/backend/tests/unit/services/test_sprite_storage.py
git commit -m "feat: composite_animation_spritesheet for API-resolved frames"
```

---

## Task 4: Add `fetch_animation_spritesheets` to `PixelLabService`

**Files:**
- Modify: `apps/backend/core/services/pixellab_service.py` (add method after `fetch_all_rotations`)
- Test: `apps/backend/tests/unit/services/test_pixellab_service.py`

- [ ] **Step 1: Write the failing test**

Add to `apps/backend/tests/unit/services/test_pixellab_service.py`:

```python
import io
import json
import zipfile
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from core.services.pixellab_service import PixelLabService


def _make_test_zip() -> bytes:
    """Build a minimal PixelLab-style ZIP with metadata.json and frame PNGs."""
    from PIL import Image

    def _png(w=96, h=96):
        img = Image.new("RGBA", (w, h), (255, 0, 0, 128))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    metadata = {
        "character": {"id": "char-1"},
        "frames": {
            "rotations": {},
            "animations": {
                "walking-abc123": {"south": ["animations/walking-abc123/south/frame_000.png"]},
                "walking-def456": {"west": ["animations/walking-def456/west/frame_000.png"]},
                "animating-ghi789": {"south": ["animations/animating-ghi789/south/frame_000.png",
                                                "animations/animating-ghi789/south/frame_001.png"]},
            },
        },
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("metadata.json", json.dumps(metadata))
        zf.writestr("animations/walking-abc123/south/frame_000.png", _png())
        zf.writestr("animations/walking-def456/west/frame_000.png", _png())
        zf.writestr("animations/animating-ghi789/south/frame_000.png", _png())
        zf.writestr("animations/animating-ghi789/south/frame_001.png", _png())
    return buf.getvalue()


@pytest.mark.asyncio
async def test_fetch_animation_spritesheets_resolves_names():
    """ZIP folders with opaque names should resolve to clean API animation names."""
    pxl = PixelLabService(api_key="test")

    # Mock get_character: API says this character has "walk" (1 frame) and "breathing-idle" (2 frames)
    mock_char = {
        "animations": [
            {"name": "walk", "direction": "south", "num_frames": 1},
            {"name": "walk", "direction": "west", "num_frames": 1},
            {"name": "breathing-idle", "direction": "south", "num_frames": 2},
        ]
    }
    pxl.get_character = AsyncMock(return_value=mock_char)
    pxl.download_character_zip = AsyncMock(return_value=_make_test_zip())

    result = await pxl.fetch_animation_spritesheets("char-1")

    assert "walk" in result
    assert "breathing-idle" in result
    assert "south" in result["walk"]
    assert "west" in result["walk"]
    assert len(result["walk"]["south"]) == 1  # 1 frame
    assert "south" in result["breathing-idle"]
    assert len(result["breathing-idle"]["south"]) == 2  # 2 frames


@pytest.mark.asyncio
async def test_fetch_animation_spritesheets_no_zip_returns_empty():
    pxl = PixelLabService(api_key="test")
    pxl.get_character = AsyncMock(return_value={"animations": []})
    pxl.download_character_zip = AsyncMock(return_value=None)

    result = await pxl.fetch_animation_spritesheets("char-1")
    assert result == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_pixellab_service.py -v -k "fetch_animation_spritesheets"`
Expected: FAIL — `AttributeError: 'PixelLabService' object has no attribute 'fetch_animation_spritesheets'`

- [ ] **Step 3: Implement `fetch_animation_spritesheets`**

Add to `apps/backend/core/services/pixellab_service.py`, after `fetch_all_rotations`:

```python
    async def fetch_animation_spritesheets(
        self, character_id: str
    ) -> dict[str, dict[str, list[bytes]]]:
        """Download animation frames grouped by clean API animation name and direction.

        Cross-references the character API (which has clean names like ``walk``,
        ``breathing-idle``) with the ZIP download (which uses opaque folder
        names like ``walking-abc123``). Each ZIP folder contains frames for one
        direction of one animation.

        Matching: for each ZIP folder, find the API animation entry whose
        ``(direction, num_frames)`` matches. When multiple folders match the
        same ``(animation, direction)`` pair (duplicate jobs from re-queuing),
        the last folder wins (most recent generation).

        Returns:
            Dict mapping animation name -> direction -> list of frame PNG bytes.
            Empty dict if ZIP is unavailable or no animations found.
        """
        try:
            char_data = await self.get_character(character_id)
        except Exception:
            logger.exception("Failed to get character %s for animation fetch", character_id)
            return {}

        zip_bytes = await self.download_character_zip(character_id)
        if zip_bytes is None:
            logger.warning("No ZIP available for character %s", character_id)
            return {}

        # Parse metadata.json from ZIP
        import io
        import json
        import zipfile

        try:
            zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
            metadata = json.loads(zf.read("metadata.json"))
        except Exception:
            logger.exception("Failed to parse ZIP metadata for character %s", character_id)
            return {}

        zip_animations = metadata.get("frames", {}).get("animations", {})
        if not zip_animations:
            return {}

        # Build API animation lookup: (direction, num_frames) -> animation_name
        # Multiple API entries can share the same (direction, num_frames) if
        # there are duplicates — the name is the same for all of them.
        api_animations = char_data.get("animations", [])
        api_lookup: dict[tuple[str, int], str] = {}
        for entry in api_animations:
            name = entry.get("name", "")
            direction = entry.get("direction", "")
            num_frames = entry.get("num_frames", 0)
            if name and direction:
                api_lookup[(direction, num_frames)] = name

        # Match ZIP folders to API animation names
        # Each ZIP folder: { direction: [frame_paths] }
        result: dict[str, dict[str, list[bytes]]] = {}
        for folder_name, dir_data in zip_animations.items():
            for direction, frame_paths in dir_data.items():
                num_frames = len(frame_paths)
                anim_name = api_lookup.get((direction, num_frames))
                if anim_name is None:
                    logger.debug(
                        "No API match for ZIP folder %s (%s, %d frames)",
                        folder_name, direction, num_frames,
                    )
                    continue

                # Extract frame PNGs from ZIP
                frames: list[bytes] = []
                for frame_path in sorted(frame_paths):
                    try:
                        frames.append(zf.read(frame_path))
                    except KeyError:
                        logger.warning("Missing frame in ZIP: %s", frame_path)

                if frames:
                    # Last-wins for duplicates (most recent generation)
                    result.setdefault(anim_name, {})[direction] = frames

        logger.info(
            "Resolved %d animations from ZIP for character %s: %s",
            len(result), character_id,
            {k: list(v.keys()) for k, v in result.items()},
        )
        return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_pixellab_service.py -v -k "fetch_animation_spritesheets"`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/pixellab_service.py apps/backend/tests/unit/services/test_pixellab_service.py
git commit -m "feat: fetch_animation_spritesheets resolves ZIP names via API"
```

---

## Task 5: Delete old extraction functions from `sprite_storage.py`

**Files:**
- Modify: `apps/backend/core/services/sprite_storage.py`

- [ ] **Step 1: Delete `extract_animation_spritesheet` (lines 106-170)**

Remove the entire function.

- [ ] **Step 2: Delete `extract_all_spritesheets` (lines 173-215)**

Remove the entire function including the broken `_resolve_name` call.

- [ ] **Step 3: Delete `extract_walk_spritesheet` (lines 263-286)**

Remove the entire function.

- [ ] **Step 4: Delete `_extract_rotation_fallback` (lines 63-103)**

Remove the entire function. The static sprite is now built from API rotation images via `build_static_spritesheet_from_rotations`, not from ZIP rotations.

- [ ] **Step 5: Run the full test suite**

Run: `cd apps/backend && uv run pytest tests/ -v`
Expected: Any tests that import the deleted functions will fail. Fix those by removing the imports/tests that reference the deleted functions. The new `composite_animation_spritesheet` tests should still pass.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/sprite_storage.py apps/backend/tests/
git commit -m "refactor: remove old ZIP extraction functions from sprite_storage"
```

---

## Task 6: Rewrite Phase C in `town.py`

**Files:**
- Modify: `apps/backend/routers/town.py:425-680` (the `_sprite_pipeline_task` function, Phase C section)

- [ ] **Step 1: Update imports at the top of `_sprite_pipeline_task`**

Replace:

```python
    from core.services.sprite_storage import (
        build_static_spritesheet_from_rotations,
        extract_all_spritesheets,
        extract_walk_spritesheet,
        upload_sprite_to_s3,
        upload_sprites_to_s3,
    )
```

With:

```python
    from core.services.sprite_storage import (
        build_static_spritesheet_from_rotations,
        composite_animation_spritesheet,
        upload_sprite_to_s3,
    )
```

- [ ] **Step 2: Replace the Phase C body**

Find the section starting with `# ---- Phase C:` (after Phase B's game server connect). Replace everything from Phase C through the end of the function with:

```python
        # ---- Phase C: Generate signup animations (walk, idle, talking) ----
        service.update_agent_state(agent_id, sprite_status="animating")
        _send_progress("Bringing you to life (animating walk, idle, talking)...")

        async def _anim_progress(status: str) -> None:
            _send_progress(status)

        try:
            await pxl.queue_signup_animations(char_id, on_progress=_anim_progress)
        except Exception:
            logger.exception("Signup animations failed for %s", agent_id)

        # Download and resolve animation frames via API cross-reference
        service.update_agent_state(agent_id, sprite_status="downloading")
        _send_progress("Downloading sprites...")

        raw_animations = await pxl.fetch_animation_spritesheets(char_id)

        # Map PixelLab API names to our friendly names
        template_id_to_friendly: dict[str, str] = {
            cfg["template_id"]: name
            for name, cfg in ANIMATION_REGISTRY.items()
            if cfg.get("template_id")
        }

        full_sprite_animations: dict = dict(initial_sprite_animations)

        for pixellab_name, direction_frames in raw_animations.items():
            friendly = template_id_to_friendly.get(pixellab_name, pixellab_name)
            sheet = composite_animation_spritesheet(direction_frames)
            if sheet:
                png_bytes, frame_count = sheet
                s3_key = await asyncio.to_thread(
                    upload_sprite_to_s3, png_bytes, str(agent_id),
                    settings.SPRITE_S3_BUCKET, f"{friendly}.png",
                )
                registry_entry = ANIMATION_REGISTRY.get(friendly, {})
                full_sprite_animations[friendly] = {
                    "url": f"{settings.SPRITE_CDN_URL}/{s3_key}",
                    "frameCount": frame_count,
                    "fps": int(registry_entry.get("fps", 8)),
                }

        # Handle custom animations (talking) — V2 path already keyed by friendly name
        # Custom animation compositing happens inside queue_signup_animations via
        # queue_custom_animation. If any custom sheets were produced, they are
        # already uploaded by the existing V2 flow. Check if talking exists in
        # full_sprite_animations; if not, the V2 path didn't produce it (OK — optional).

        # Real walk URL overrides the static placeholder
        real_walk_url = full_sprite_animations.get("walk", {}).get("url", static_cdn_url)

        service.update_agent_state(
            agent_id,
            sprite_ready=True,
            sprite_url=real_walk_url,
            sprite_animations=full_sprite_animations,
            sprite_status="complete",
        )
        logger.info("[agent:%s] Phase C complete: %d animations", agent_id, len(full_sprite_animations))

        # Push updated sprites to game server
        try:
            gs_client_c = get_game_server_client()
            await gs_client_c.update_agent_sprites(str(agent_id), full_sprite_animations)
        except Exception:
            logger.exception("Failed to push sprite update for %s", agent_id)

        # If agent was not connected in Phase B, connect now
        if not static_cdn_url:
            gs_client = get_game_server_client()
            _ws_mgr = get_town_agent_ws_manager()
            _conn = _ws_mgr.get_by_agent_id(str(agent_id))
            if _conn:
                try:
                    agent_record = service.get_town_agent_by_id(str(agent_id))
                    await gs_client.connect_agent(
                        agent_id=str(agent_id),
                        display_name=display_name,
                        sprite_url=real_walk_url,
                        location=agent_record.get("currentLocation", "plaza") if agent_record else "plaza",
                        context=_conn.location_context or "town",
                        user_id=_conn.user_id,
                        sprite_animations=full_sprite_animations,
                        on_event=_make_agent_event_callback(),
                    )
                except Exception:
                    logger.exception("Failed to connect agent %s with animations", agent_id)

        # Flip to alive
        service.update_agent_state(agent_id, onboarding_phase="alive")
        logger.info("[agent:%s] Agent is alive", agent_id)
```

- [ ] **Step 3: Remove deleted import references**

Search for any remaining references to `extract_all_spritesheets`, `extract_walk_spritesheet`, `extract_animation_spritesheet`, `template_name_map`, or `name_map=` in `town.py` and remove them.

- [ ] **Step 4: Run the full test suite**

Run: `cd apps/backend && uv run pytest tests/ -v`
Expected: All pass. Fix any test that references deleted functions.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/routers/town.py
git commit -m "feat: Phase C uses API-resolved animation names"
```

---

## Task 7: Rewrite `resume_sprite_generation`

**Files:**
- Modify: `apps/backend/routers/town.py:771+` (the `resume_sprite_generation` function)

- [ ] **Step 1: Read the current function**

Run: `grep -n "async def resume_sprite_generation" apps/backend/routers/town.py`
Read the full function to understand its current structure.

- [ ] **Step 2: Update imports at the top of the function**

The function has its own import block. Replace references to deleted functions with `composite_animation_spritesheet`. Remove `extract_all_spritesheets`, `extract_walk_spritesheet`.

- [ ] **Step 3: Replace the downloading phase**

Find the `if current_status == "downloading"` block and replace:

```python
        # Phase: downloading
        if current_status == "downloading" and char_id:
            _progress("Downloading sprites...")

            raw_animations = await pxl.fetch_animation_spritesheets(char_id)

            template_id_to_friendly: dict[str, str] = {
                cfg["template_id"]: name
                for name, cfg in ANIMATION_REGISTRY.items()
                if cfg.get("template_id")
            }

            sprite_animations: dict = {}
            for pixellab_name, direction_frames in raw_animations.items():
                friendly = template_id_to_friendly.get(pixellab_name, pixellab_name)
                sheet = composite_animation_spritesheet(direction_frames)
                if sheet:
                    png_bytes, frame_count = sheet
                    s3_key = await asyncio.to_thread(
                        upload_sprite_to_s3, png_bytes, agent_id,
                        settings.SPRITE_S3_BUCKET, f"{friendly}.png",
                    )
                    registry_entry = ANIMATION_REGISTRY.get(friendly, {})
                    sprite_animations[friendly] = {
                        "url": f"{settings.SPRITE_CDN_URL}/{s3_key}",
                        "frameCount": frame_count,
                        "fps": int(registry_entry.get("fps", 8)),
                    }

            walk_url = sprite_animations.get("walk", {}).get("url", existing_sprite_url)
            service.update_agent_state(
                agent_id,
                sprite_ready=True,
                sprite_url=walk_url,
                sprite_animations=sprite_animations,
                sprite_status="complete",
                onboarding_phase="alive",
            )
            logger.info("[resume:%s] Complete: %d animations", agent_id, len(sprite_animations))
```

- [ ] **Step 4: Ensure `existing_sprite_url` is read at the top**

Verify this line exists near the top of `resume_sprite_generation`:

```python
    existing_agent = service.get_town_agent_by_id(agent_id)
    existing_sprite_url = (existing_agent or {}).get("spriteUrl", "")
```

- [ ] **Step 5: Remove old `template_name_map` construction**

Delete any lines building `template_name_map` for the old `name_map` parameter that no longer exists.

- [ ] **Step 6: Run the full test suite**

Run: `cd apps/backend && uv run pytest tests/ -v`
Expected: All pass.

- [ ] **Step 7: Commit**

```bash
git add apps/backend/routers/town.py
git commit -m "feat: resume_sprite_generation uses API-resolved names"
```

---

## Task 8: Update `composite_v2_animation` to use 96px frames

**Files:**
- Modify: `apps/backend/core/services/sprite_storage.py:218-260` (the `composite_v2_animation` function)

- [ ] **Step 1: Update `composite_v2_animation` to use `FRAME_SIZE` constant**

The function currently hardcodes `FRAME_SIZE` references — verify it already uses the module constant (not a local 48). If it does, the constant change in Task 1 already handles this. If it hardcodes 48 anywhere, replace with `FRAME_SIZE`.

Read the function and confirm. The current code at lines 234-235:

```python
    sheet_width = frame_count * FRAME_SIZE
    sheet = Image.new("RGBA", (sheet_width, SHEET_HEIGHT), (0, 0, 0, 0))
```

This already uses `FRAME_SIZE` and `SHEET_HEIGHT` — so the constant change from Task 1 propagates automatically. No code change needed.

- [ ] **Step 2: Run tests to verify**

Run: `cd apps/backend && uv run pytest tests/ -v`
Expected: All pass.

- [ ] **Step 3: Commit (if any changes were made)**

Skip if no changes needed.

---

## Task 9: Update Godot `agent.gd` constants to 96px

**Files:**
- Modify: `apps/godot/scripts/agent.gd:12-15`

- [ ] **Step 1: Update constants**

In `apps/godot/scripts/agent.gd`, replace lines 12-15:

```gdscript
# Spritesheet constants (matches backend sprite_storage.py)
const FRAME_SIZE: int = 96
const FRAME_COUNT: int = 6
const SHEET_WIDTH: int = 576  # 6 * 96
const SHEET_HEIGHT: int = 384  # 4 * 96
```

- [ ] **Step 2: Commit**

```bash
git add apps/godot/scripts/agent.gd
git commit -m "feat: agent.gd 96px frame size for higher quality sprites"
```

---

## Task 10: Re-export `.pck` and push

**Files:**
- Modify: `apps/frontend/public/godot/goosetown.pck` (and other export artifacts)

- [ ] **Step 1: Delete stale import caches**

```bash
rm -rf apps/godot/.godot/imported apps/godot/.godot/exported
```

- [ ] **Step 2: Open the Godot editor and export**

Open `apps/godot/project.godot` in the Godot editor. Project → Export → Web → Export PCK/Zip → save to `apps/frontend/public/godot/goosetown.pck` (overwrite). This step is for the user.

- [ ] **Step 3: Commit and push**

```bash
git add apps/frontend/public/godot/ apps/godot/scripts/agent.gd
git commit -m "feat: re-export .pck with 96px sprite frame size"
git push
```

---

## Self-review

**Spec coverage:**
- Section 1 (fetch_animation_spritesheets) → Task 4
- Section 2 (composite_animation_spritesheet + 96px + filename) → Tasks 1, 2, 3
- Section 2 deletions → Task 5
- Section 2 (composite_v2_animation 96px) → Task 8
- Section 3 (Phase C rewrite) → Task 6
- Section 4 (resume_sprite_generation) → Task 7
- Section 5 (Godot 96px) → Task 9
- Section 5 (re-export .pck) → Task 10

**Placeholder scan:** All tasks have complete code. No TBDs or "similar to Task N" references.

**Type consistency:**
- `fetch_animation_spritesheets` returns `dict[str, dict[str, list[bytes]]]` — matches Task 4 tests and Task 6/7 usage
- `composite_animation_spritesheet` takes `dict[str, list[bytes]]` — matches Task 3 tests and Task 6/7 where it's called with `direction_frames` per animation
- `upload_sprite_to_s3` signature `(png_bytes, agent_id, bucket, filename)` — matches Task 2 tests and Task 6/7 callers
- `FRAME_SIZE=96` — consistent across Tasks 1, 3, 8, 9
