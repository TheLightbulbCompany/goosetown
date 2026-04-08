# Progressive Sprites, Trimmed Animations, and Nameplate Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make new GooseTown agents appear in the apartment as static standing characters within ~30s of `join`, then "come alive" as breathing-idle and talking animations finish generating in the background. Also fix the current `spriteUrl=""` bug (template-id naming), trim auto-generated animations to walk/idle/talking only, and replace the plain hover name label with a rounded white pill using the Upheaval Pro title font.

**Architecture:** Registration background task gets a new Phase B that composites the 4 rotation PNGs into a 288×192 static spritesheet (each direction duplicated across 6 columns), uploads it, flips `spriteReady=true`, and connects the agent to the game server. Phase C then generates only walk, idle, and talking animations and pushes each to the game server via a new `update_agent_sprites` command, which the world_state broadcast surfaces to the viewer on the next tick. A new `onboardingPhase` field on the agent record ("static" | "alive") lets the daemon render a welcome blurb in TOWN_STATUS.md during the static phase so the agent's LLM does not attempt to act before it is fully awake.

**Tech Stack:** Python 3.12 + FastAPI + boto3 (backend), PixelLab V1/V2 API, PIL for image composition, Godot 4.6 GDScript (viewer + headless game server), Godot FontFile resources, pytest + AsyncMock for backend tests.

**Spec:** `docs/superpowers/specs/2026-04-07-progressive-sprites-and-nameplate-design.md`

---

## File structure

**Backend (`apps/backend/`):**
- Modify: `core/services/pixellab_service.py` — add `auto_generate` flag to registry, add `fetch_all_rotations`, rename `queue_template_animations` → `queue_signup_animations`
- Modify: `core/services/sprite_storage.py` — add `build_static_spritesheet_from_rotations`, add `name_map` param to `extract_all_spritesheets`
- Modify: `core/services/game_server_client.py` — add `update_agent_sprites` method
- Modify: `core/services/town_service.py` — add `onboarding_phase` → `onboardingPhase` to field_map
- Modify: `routers/town.py` — restructure registration background task into Phase A/B/C, include `onboardingPhase` in agent_state payload
- Modify: `data/goosetown-skill/goosetown_cli.py` — daemon reads `onboardingPhase` and renders welcome blurb during static phase

**Tests (`apps/backend/tests/`):**
- Modify: `tests/unit/services/test_sprite_storage.py` — add tests for `build_static_spritesheet_from_rotations` and `extract_all_spritesheets` name_map
- Create: `tests/unit/services/test_pixellab_service_registry.py` — test `auto_generate` filtering
- Modify: `tests/unit/services/test_game_server_client.py` — test `update_agent_sprites` method
- Modify: `tests/unit/test_goosetown_cli.py` — test welcome blurb rendering during static phase

**Game server (`apps/godot/scripts/server/`):**
- Modify: `agent_controller.gd` — handle `update_agent_sprites` command
- Modify: `apartment_agent_controller.gd` — handle `update_agent_sprites` command

**Viewer (`apps/godot/`):**
- Copy: `apps/frontend/public/assets/fonts/upheaval_pro.ttf` → `assets/fonts/upheaval_pro.ttf`
- Modify: `scenes/agent.tscn` — replace `NameLabel` with `NameplateContainer/Pill/NameLabel`
- Modify: `scripts/agent.gd` — nameplate @onready references, toggle nameplate.visible in hover/speech
- Modify: `scripts/agent_manager.gd` — diff `spriteAnimations` in world_state and reload new/changed URLs

**Export artifact:**
- Modify: `apps/frontend/public/godot/goosetown.pck` — re-export after Godot changes

---

## Task 1: Add `auto_generate` flag to `ANIMATION_REGISTRY`

**Files:**
- Modify: `apps/backend/core/services/pixellab_service.py:20-145`
- Test: `apps/backend/tests/unit/services/test_pixellab_service_registry.py` (create)

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/services/test_pixellab_service_registry.py`:

```python
"""Tests for ANIMATION_REGISTRY auto_generate filtering."""
from core.services.pixellab_service import (
    ANIMATION_REGISTRY,
    SIGNUP_ANIMATIONS,
)


def test_signup_animations_contains_only_walk_idle_talking():
    assert set(SIGNUP_ANIMATIONS.keys()) == {"walk", "idle", "talking"}


def test_all_registry_entries_have_auto_generate_flag():
    for name, config in ANIMATION_REGISTRY.items():
        assert "auto_generate" in config, f"{name} missing auto_generate flag"
        assert isinstance(config["auto_generate"], bool)


def test_only_signup_animations_have_auto_generate_true():
    auto = {name for name, cfg in ANIMATION_REGISTRY.items() if cfg["auto_generate"]}
    assert auto == {"walk", "idle", "talking"}


def test_walk_is_template_type():
    assert ANIMATION_REGISTRY["walk"]["type"] == "template"
    assert ANIMATION_REGISTRY["walk"]["template_id"] == "walk"


def test_idle_is_template_type_with_breathing_idle_template():
    assert ANIMATION_REGISTRY["idle"]["type"] == "template"
    assert ANIMATION_REGISTRY["idle"]["template_id"] == "breathing-idle"


def test_talking_is_custom_v2():
    assert ANIMATION_REGISTRY["talking"]["type"] == "custom"
    assert "prompt" in ANIMATION_REGISTRY["talking"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_pixellab_service_registry.py -v`
Expected: FAIL with `ImportError: cannot import name 'SIGNUP_ANIMATIONS'` (or similar — the constant doesn't exist yet and entries lack `auto_generate`).

- [ ] **Step 3: Add `auto_generate` to every registry entry and export `SIGNUP_ANIMATIONS`**

In `apps/backend/core/services/pixellab_service.py`, modify every entry in `ANIMATION_REGISTRY` to add `"auto_generate": False` by default, then override with `"auto_generate": True` for `walk`, `idle`, and `talking`. After the registry definition, replace the existing `TEMPLATE_ANIMATIONS`/`CUSTOM_ANIMATIONS` computed dicts with a new derived constant:

```python
ANIMATION_REGISTRY: dict[str, dict] = {
    "walk":       {"type": "template", "template_id": "walk", "fps": 8.0, "auto_generate": True},
    "idle":       {"type": "template", "template_id": "breathing-idle", "fps": 4.0, "auto_generate": True},
    "run":        {"type": "template", "template_id": "running-6-frames", "fps": 12.0, "auto_generate": False},
    "drink":      {"type": "template", "template_id": "drinking", "fps": 6.0, "auto_generate": False},
    "crouch":     {"type": "template", "template_id": "crouching", "fps": 6.0, "auto_generate": False},
    "pick_up":    {"type": "template", "template_id": "picking-up", "fps": 6.0, "auto_generate": False},
    # Custom V3
    "sitting":    {"type": "custom", "frames": 8, "fps": 1.0, "auto_generate": False, "prompt": "..."},
    "reading":    {"type": "custom", "frames": 8, "fps": 1.0, "auto_generate": False, "prompt": "..."},
    # ...etc for every custom entry: add "auto_generate": False to each...
    "talking":    {"type": "custom", "frames": 8, "fps": 6.0, "auto_generate": True, "prompt": (
        "Standing upright gesturing while speaking, one hand raised making "
        "a point then lowering, head nodding along, weight shifting between "
        "feet, other hand occasionally joining in an open palm gesture"
    )},
}

TEMPLATE_ANIMATIONS = {k: v for k, v in ANIMATION_REGISTRY.items() if v["type"] == "template"}
CUSTOM_ANIMATIONS = {k: v for k, v in ANIMATION_REGISTRY.items() if v["type"] == "custom"}
SIGNUP_ANIMATIONS = {k: v for k, v in ANIMATION_REGISTRY.items() if v["auto_generate"]}
```

Keep all existing `prompt` strings verbatim — do not shorten them. Only add the `auto_generate` field.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_pixellab_service_registry.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/pixellab_service.py apps/backend/tests/unit/services/test_pixellab_service_registry.py
git commit -m "feat: add auto_generate flag to animation registry"
```

---

## Task 2: Add `name_map` to `extract_all_spritesheets` and fix template-id mismatch

**Files:**
- Modify: `apps/backend/core/services/sprite_storage.py:173-202`
- Test: `apps/backend/tests/unit/services/test_sprite_storage.py`

- [ ] **Step 1: Write the failing test**

Add to `apps/backend/tests/unit/services/test_sprite_storage.py`:

```python
import io
import zipfile
from PIL import Image

from core.services.sprite_storage import extract_all_spritesheets


def _make_stub_zip(anim_folder_names: list[str]) -> bytes:
    """Build a minimal PixelLab-style ZIP with empty animation folders."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in anim_folder_names:
            # One tiny PNG per direction so extract_animation_spritesheet has something to find
            for direction in ("south", "west", "east", "north"):
                for frame in range(6):
                    img = Image.new("RGBA", (96, 96), (255, 0, 0, 255))
                    frame_buf = io.BytesIO()
                    img.save(frame_buf, format="PNG")
                    zf.writestr(f"animations/{name}/{direction}_{frame}.png", frame_buf.getvalue())
    return buf.getvalue()


def test_extract_all_spritesheets_uses_name_map_to_rekey_results():
    zip_bytes = _make_stub_zip(["walk", "breathing-idle"])
    name_map = {"breathing-idle": "idle"}

    result = extract_all_spritesheets(zip_bytes, name_map=name_map)

    assert set(result.keys()) == {"walk", "idle"}


def test_extract_all_spritesheets_without_name_map_keeps_folder_names():
    zip_bytes = _make_stub_zip(["walk", "breathing-idle"])

    result = extract_all_spritesheets(zip_bytes)  # no name_map

    assert set(result.keys()) == {"walk", "breathing-idle"}


def test_extract_all_spritesheets_unmapped_folder_kept_as_is():
    zip_bytes = _make_stub_zip(["walk", "weird-folder-name"])
    name_map = {"breathing-idle": "idle"}  # doesn't include weird-folder-name

    result = extract_all_spritesheets(zip_bytes, name_map=name_map)

    assert "walk" in result
    assert "weird-folder-name" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_sprite_storage.py::test_extract_all_spritesheets_uses_name_map_to_rekey_results -v`
Expected: FAIL with `TypeError: extract_all_spritesheets() got an unexpected keyword argument 'name_map'`

- [ ] **Step 3: Add `name_map` parameter**

In `apps/backend/core/services/sprite_storage.py`, replace `extract_all_spritesheets`:

```python
def extract_all_spritesheets(
    zip_bytes: bytes,
    name_map: dict[str, str] | None = None,
) -> dict[str, tuple[bytes, int]]:
    """Discover all animations in a PixelLab ZIP and extract each as a spritesheet.

    Scans for animations/<name>/ directories and calls extract_animation_spritesheet
    for each animation found.

    Args:
        zip_bytes: Raw PixelLab ZIP bytes.
        name_map: Optional mapping of folder-name -> friendly-name. When a
            discovered folder name appears in the map, the friendly name is
            used as the result dict key. Folders not in the map keep their
            raw folder name.

    Returns:
        Dict mapping animation key -> (png_bytes, frame_count).
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        logger.error("Invalid ZIP file from PixelLab")
        return {}

    all_files = zf.namelist()

    animation_names: set[str] = set()
    for path in all_files:
        parts = path.split("/")
        if len(parts) >= 3 and parts[0] == "animations" and parts[1]:
            animation_names.add(parts[1])

    name_map = name_map or {}
    results: dict[str, tuple[bytes, int]] = {}
    for folder_name in animation_names:
        result = extract_animation_spritesheet(zip_bytes, folder_name)
        if result is not None:
            key = name_map.get(folder_name, folder_name)
            results[key] = result

    return results
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_sprite_storage.py -v -k extract_all_spritesheets`
Expected: PASS (all three new tests + any pre-existing extract_all_spritesheets tests)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/sprite_storage.py apps/backend/tests/unit/services/test_sprite_storage.py
git commit -m "fix: rekey extracted spritesheets via template_id->name map"
```

---

## Task 3: Rename `queue_template_animations` to `queue_signup_animations` with mixed-type dispatch

**Files:**
- Modify: `apps/backend/core/services/pixellab_service.py:370-414`
- Modify: any callers in `apps/backend/routers/town.py`

- [ ] **Step 1: Find current callers**

Run: `cd /Users/prasiddhaparthsarthy/Desktop/goosetown && grep -rn "queue_template_animations" apps/backend/`
Expected: one or more matches in `routers/town.py` (and possibly tests).

- [ ] **Step 2: Replace the method body**

In `apps/backend/core/services/pixellab_service.py`, rename `queue_template_animations` to `queue_signup_animations` and change the loop to dispatch by animation type:

```python
async def queue_signup_animations(
    self,
    character_id: str,
    on_progress: Callable[[str], Coroutine[Any, Any, None]] | None = None,
) -> dict[str, list[str]]:
    """Queue all signup animations sequentially and wait for each to complete.

    Iterates SIGNUP_ANIMATIONS (the subset of ANIMATION_REGISTRY with
    auto_generate=True). For templates, calls animate_character with the
    template_id. For custom animations, calls queue_custom_animation with
    the prompt. Waits for each animation's jobs before queuing the next to
    avoid PixelLab concurrent-job limits.

    Args:
        character_id: PixelLab character UUID.
        on_progress: Optional async callback called every ~10s during waits.

    Returns:
        Dict mapping friendly animation name -> list of job IDs that were queued.
    """
    existing = await self.get_existing_animation_names(character_id)
    results: dict[str, list[str]] = {}
    items = list(SIGNUP_ANIMATIONS.items())

    for idx, (name, config) in enumerate(items):
        progress_label = f"{name} ({idx + 1}/{len(items)})"

        try:
            if config["type"] == "template":
                template_id: str = config["template_id"]
                if template_id in existing:
                    logger.info(
                        "Template %s already exists on character %s, skipping",
                        name, character_id,
                    )
                    results[name] = []
                    continue
                job_ids = await self.animate_character(
                    character_id, animation=template_id,
                )
            elif config["type"] == "custom":
                prompt: str = config["prompt"]
                job_ids = await self.queue_custom_animation(
                    character_id=character_id,
                    animation_name=name,
                    action_description=prompt,
                )
            else:
                logger.warning("Unknown animation type for %s: %r", name, config.get("type"))
                continue

            logger.info(
                "Queued signup animation %r for %s: %d jobs",
                name, character_id, len(job_ids),
            )
            results[name] = job_ids

            if job_ids:
                await self.wait_for_jobs(
                    job_ids,
                    on_progress=on_progress,
                    progress_label=progress_label,
                )
        except Exception:
            logger.exception(
                "Failed to queue signup animation %r for %s — continuing",
                name, character_id,
            )
            results.setdefault(name, [])

    logger.info(
        "Signup animations done for %s: %s",
        character_id, {k: len(v) for k, v in results.items()},
    )
    return results
```

Import `SIGNUP_ANIMATIONS` at the top of the file if it is not already in scope (module-level constant defined in Task 1).

- [ ] **Step 3: Update caller in `routers/town.py`**

In `apps/backend/routers/town.py`, find the single line calling `queue_template_animations` (around line 516 based on prior snapshots):

```python
# Old:
await pxl.queue_template_animations(char_id, on_progress=_anim_progress)

# New:
await pxl.queue_signup_animations(char_id, on_progress=_anim_progress)
```

Leave the rest of the Phase C flow alone for now — Task 10 will restructure it.

- [ ] **Step 4: Run the existing backend tests to verify nothing else breaks**

Run: `cd apps/backend && uv run pytest tests/ -v -k "pixellab or sprite"`
Expected: all tests pass. The rename should only affect callsites in `routers/town.py`.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/pixellab_service.py apps/backend/routers/town.py
git commit -m "refactor: queue_template_animations -> queue_signup_animations with mixed dispatch"
```

---

## Task 4: Add `fetch_all_rotations` to `PixelLabService`

**Files:**
- Modify: `apps/backend/core/services/pixellab_service.py` (add method near `_get_rotation_base64`)
- Test: `apps/backend/tests/unit/services/test_pixellab_service.py` (create if not present)

- [ ] **Step 1: Write the failing test**

Add to (or create) `apps/backend/tests/unit/services/test_pixellab_service.py`:

```python
"""Tests for PixelLabService.fetch_all_rotations."""
from unittest.mock import AsyncMock, patch

import pytest

from core.services.pixellab_service import PixelLabService


@pytest.mark.asyncio
async def test_fetch_all_rotations_returns_dict_of_bytes():
    service = PixelLabService(api_key="stub")
    stub_char = {
        "rotation_urls": {
            "south": "https://cdn.example.com/s.png",
            "west":  "https://cdn.example.com/w.png",
            "east":  "https://cdn.example.com/e.png",
            "north": "https://cdn.example.com/n.png",
        },
        "size": {"width": 96, "height": 96},
    }

    with patch.object(service, "get_character", AsyncMock(return_value=stub_char)):
        fake_resp = AsyncMock()
        fake_resp.content = b"\x89PNGfakebytes"
        fake_resp.raise_for_status = lambda: None

        with patch("core.services.pixellab_service.httpx.AsyncClient") as MockClient:
            MockClient.return_value.__aenter__.return_value.get = AsyncMock(return_value=fake_resp)

            result = await service.fetch_all_rotations("char-123")

    assert set(result.keys()) == {"south", "west", "east", "north"}
    for direction, png in result.items():
        assert png == b"\x89PNGfakebytes"


@pytest.mark.asyncio
async def test_fetch_all_rotations_skips_missing_directions():
    service = PixelLabService(api_key="stub")
    stub_char = {
        "rotation_urls": {
            "south": "https://cdn.example.com/s.png",
            # west, east, north missing
        },
        "size": {"width": 96, "height": 96},
    }

    with patch.object(service, "get_character", AsyncMock(return_value=stub_char)):
        fake_resp = AsyncMock()
        fake_resp.content = b"\x89PNGfakebytes"
        fake_resp.raise_for_status = lambda: None

        with patch("core.services.pixellab_service.httpx.AsyncClient") as MockClient:
            MockClient.return_value.__aenter__.return_value.get = AsyncMock(return_value=fake_resp)

            result = await service.fetch_all_rotations("char-123")

    assert set(result.keys()) == {"south"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_pixellab_service.py::test_fetch_all_rotations_returns_dict_of_bytes -v`
Expected: FAIL with `AttributeError: 'PixelLabService' object has no attribute 'fetch_all_rotations'`

- [ ] **Step 3: Add `fetch_all_rotations`**

In `apps/backend/core/services/pixellab_service.py`, add this method below `_get_rotation_base64`:

```python
async def fetch_all_rotations(self, character_id: str) -> dict[str, bytes]:
    """Download all 4 rotation PNGs for a character.

    Returns a dict of direction -> raw PNG bytes. Directions that fail to
    download or are missing from the character's rotation_urls are simply
    omitted from the result — the caller decides how to handle partial
    coverage.
    """
    try:
        char_data = await self.get_character(character_id)
    except Exception:
        logger.exception("Failed to get character %s for rotation fetch", character_id)
        return {}

    rotation_urls: dict[str, str] = char_data.get("rotation_urls", {}) or {}
    directions = ("south", "west", "east", "north")
    result: dict[str, bytes] = {}

    async with httpx.AsyncClient() as client:
        for direction in directions:
            url = rotation_urls.get(direction, "")
            if not url:
                logger.warning(
                    "No %s rotation URL for character %s", direction, character_id,
                )
                continue
            try:
                resp = await client.get(url, timeout=30.0)
                resp.raise_for_status()
                result[direction] = resp.content
            except Exception:
                logger.exception(
                    "Failed to fetch %s rotation for character %s",
                    direction, character_id,
                )

    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_pixellab_service.py -v -k fetch_all_rotations`
Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/pixellab_service.py apps/backend/tests/unit/services/test_pixellab_service.py
git commit -m "feat: add PixelLabService.fetch_all_rotations helper"
```

---

## Task 5: Add `build_static_spritesheet_from_rotations` to `sprite_storage`

**Files:**
- Modify: `apps/backend/core/services/sprite_storage.py`
- Test: `apps/backend/tests/unit/services/test_sprite_storage.py`

- [ ] **Step 1: Write the failing test**

Add to `apps/backend/tests/unit/services/test_sprite_storage.py`:

```python
from core.services.sprite_storage import (
    FRAME_SIZE,
    SHEET_WIDTH,
    SHEET_HEIGHT,
    build_static_spritesheet_from_rotations,
)


def _make_solid_rotation_png(color: tuple[int, int, int]) -> bytes:
    img = Image.new("RGBA", (96, 96), color + (255,))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_build_static_spritesheet_is_288x192():
    rotations = {
        "south": _make_solid_rotation_png((255, 0, 0)),
        "west":  _make_solid_rotation_png((0, 255, 0)),
        "east":  _make_solid_rotation_png((0, 0, 255)),
        "north": _make_solid_rotation_png((255, 255, 0)),
    }

    png = build_static_spritesheet_from_rotations(rotations)

    sheet = Image.open(io.BytesIO(png))
    assert sheet.size == (SHEET_WIDTH, SHEET_HEIGHT)
    assert sheet.size == (288, 192)


def test_build_static_spritesheet_row_columns_are_identical():
    rotations = {
        "south": _make_solid_rotation_png((255, 0, 0)),
        "west":  _make_solid_rotation_png((0, 255, 0)),
        "east":  _make_solid_rotation_png((0, 0, 255)),
        "north": _make_solid_rotation_png((255, 255, 0)),
    }

    png = build_static_spritesheet_from_rotations(rotations)
    sheet = Image.open(io.BytesIO(png)).convert("RGBA")

    # Row 0 (south) — all 6 columns should be red
    for col in range(6):
        pixel = sheet.getpixel((col * FRAME_SIZE + FRAME_SIZE // 2, FRAME_SIZE // 2))
        assert pixel[0] > 200 and pixel[1] < 50 and pixel[2] < 50, f"row 0 col {col} not red: {pixel}"

    # Row 1 (west) — green
    for col in range(6):
        pixel = sheet.getpixel((col * FRAME_SIZE + FRAME_SIZE // 2, FRAME_SIZE + FRAME_SIZE // 2))
        assert pixel[1] > 200 and pixel[0] < 50 and pixel[2] < 50, f"row 1 col {col} not green: {pixel}"


def test_build_static_spritesheet_missing_direction_is_transparent():
    rotations = {
        "south": _make_solid_rotation_png((255, 0, 0)),
        # west, east, north missing
    }

    png = build_static_spritesheet_from_rotations(rotations)
    sheet = Image.open(io.BytesIO(png)).convert("RGBA")

    # Row 1 (west) should be fully transparent
    pixel = sheet.getpixel((FRAME_SIZE // 2, FRAME_SIZE + FRAME_SIZE // 2))
    assert pixel[3] == 0, f"row 1 should be transparent: {pixel}"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_sprite_storage.py -v -k build_static_spritesheet`
Expected: FAIL with `ImportError: cannot import name 'build_static_spritesheet_from_rotations'`

- [ ] **Step 3: Implement `build_static_spritesheet_from_rotations`**

In `apps/backend/core/services/sprite_storage.py`, add this function after `composite_v2_animation`:

```python
def build_static_spritesheet_from_rotations(
    rotation_bytes: dict[str, bytes],
) -> bytes:
    """Build a 288x192 spritesheet from 4 direction rotation PNGs.

    Each rotation PNG is cropped+resized to 48x48 and duplicated across all
    6 columns of its direction's row. Missing directions remain transparent.

    The resulting sheet is the same layout that agent.gd expects for walk —
    so it can be loaded as the initial sprite and displayed as a standing
    character regardless of facing. When the real walk animation arrives
    later, the texture is replaced in place without any client changes.

    Layout:
        Row 0 = south, Row 1 = west, Row 2 = east, Row 3 = north.
    """
    direction_rows = {"south": 0, "west": 1, "east": 2, "north": 3}
    sheet = Image.new("RGBA", (SHEET_WIDTH, SHEET_HEIGHT), (0, 0, 0, 0))

    for direction, row in direction_rows.items():
        png = rotation_bytes.get(direction)
        if not png:
            continue
        try:
            img = Image.open(io.BytesIO(png)).convert("RGBA")
        except Exception as exc:
            logger.warning("Bad rotation PNG for %s: %s", direction, exc)
            continue

        frame = _crop_and_resize(img, FRAME_SIZE)
        for col in range(6):
            sheet.paste(frame, (col * FRAME_SIZE, row * FRAME_SIZE), frame)

    out = io.BytesIO()
    sheet.save(out, format="PNG")
    return out.getvalue()
```

If `_crop_and_resize` is not already defined in this file, use whatever helper the existing `composite_v2_animation` function calls (the test file can confirm by reading the function). Do not reinvent it.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_sprite_storage.py -v -k build_static_spritesheet`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/sprite_storage.py apps/backend/tests/unit/services/test_sprite_storage.py
git commit -m "feat: build_static_spritesheet_from_rotations helper"
```

---

## Task 6: Add `update_agent_sprites` to `game_server_client`

**Files:**
- Modify: `apps/backend/core/services/game_server_client.py` (add near `send_command` around line 131)
- Test: `apps/backend/tests/unit/services/test_game_server_client.py`

- [ ] **Step 1: Write the failing test**

Add to `apps/backend/tests/unit/services/test_game_server_client.py`:

```python
import json
from unittest.mock import AsyncMock

import pytest

from core.services.game_server_client import GameServerClient


@pytest.mark.asyncio
async def test_update_agent_sprites_sends_command_on_open_connection():
    client = GameServerClient(base_url="ws://stub")
    fake_ws = AsyncMock()
    client._agent_connections["agent-1"] = fake_ws

    await client.update_agent_sprites("agent-1", {
        "walk": {"url": "https://cdn.example.com/walk.png", "frameCount": 6, "fps": 8},
    })

    fake_ws.send.assert_awaited_once()
    sent = json.loads(fake_ws.send.call_args[0][0])
    assert sent["cmd"] == "update_agent_sprites"
    assert sent["agent_id"] == "agent-1"
    assert sent["sprite_animations"]["walk"]["url"] == "https://cdn.example.com/walk.png"


@pytest.mark.asyncio
async def test_update_agent_sprites_silently_skips_when_no_connection():
    client = GameServerClient(base_url="ws://stub")
    # No connection registered for agent-2

    await client.update_agent_sprites("agent-2", {"walk": {"url": "x"}})

    # Should NOT raise — DynamoDB is source of truth; next reconnect will resync
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_game_server_client.py -v -k update_agent_sprites`
Expected: FAIL with `AttributeError: 'GameServerClient' object has no attribute 'update_agent_sprites'`

- [ ] **Step 3: Add the method**

In `apps/backend/core/services/game_server_client.py`, add this method immediately after `send_command`:

```python
async def update_agent_sprites(
    self,
    agent_id: str,
    sprite_animations: dict,
) -> None:
    """Push an updated sprite_animations dict to the game server.

    Used during progressive sprite readiness: the backend first connects the
    agent with a static spritesheet, then as walk/idle/talking animations
    finish generating it calls this method so the game server can include
    them in the next world_state broadcast.

    Silently returns if the agent is not connected — DynamoDB is the source
    of truth, and the next full reconnect will load the latest animations
    fresh via connect_agent.
    """
    ws = self._agent_connections.get(agent_id)
    if ws is None:
        logger.info(
            "update_agent_sprites: no connection for %s, skipping push",
            agent_id,
        )
        return

    command = {
        "cmd": "update_agent_sprites",
        "agent_id": agent_id,
        "sprite_animations": sprite_animations,
    }
    try:
        await ws.send(json.dumps(command))
        logger.info(
            "update_agent_sprites pushed to game server for %s: %d animations",
            agent_id, len(sprite_animations),
        )
    except Exception:
        logger.exception(
            "Failed to push sprite update for %s — swallowing",
            agent_id,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_game_server_client.py -v -k update_agent_sprites`
Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/game_server_client.py apps/backend/tests/unit/services/test_game_server_client.py
git commit -m "feat: GameServerClient.update_agent_sprites for progressive sprites"
```

---

## Task 7: Game server — `update_agent_sprites` command handler (town + apartment)

**Files:**
- Modify: `apps/godot/scripts/server/agent_controller.gd:137-172`
- Modify: `apps/godot/scripts/server/apartment_agent_controller.gd` (same `handle_command` function)

- [ ] **Step 1: Add handler to town `agent_controller.gd`**

In `apps/godot/scripts/server/agent_controller.gd`, inside `handle_command(peer_id, data)`, add a new `match` arm alongside the existing `set_speech` / `set_facing` arms:

```gdscript
"update_agent_sprites":
    var agent_node: CharacterBody2D = _agents[peer_id]
    var new_anims: Dictionary = data.get("sprite_animations", {})
    if new_anims.is_empty():
        return
    agent_node.sprite_animations = new_anims
    print("[AgentController] update_agent_sprites peer=%d count=%d" % [peer_id, new_anims.size()])
```

Because `get_world_state_for_viewer()` already reads `agent_node.sprite_animations` when building each agent entry (line 192 of the file), the next 2s broadcast automatically includes the updated dict. No changes needed in `get_world_state_for_viewer`.

- [ ] **Step 2: Add the same handler to `apartment_agent_controller.gd`**

In `apps/godot/scripts/server/apartment_agent_controller.gd`, find the analogous `handle_command` function (it mirrors the town controller) and add the identical `match` arm:

```gdscript
"update_agent_sprites":
    var agent_node: CharacterBody2D = _agents[peer_id]
    var new_anims: Dictionary = data.get("sprite_animations", {})
    if new_anims.is_empty():
        return
    agent_node.sprite_animations = new_anims
    print("[ApartmentAgentController] update_agent_sprites peer=%d count=%d" % [peer_id, new_anims.size()])
```

- [ ] **Step 3: Verify the backend -> game server contract matches**

The backend sends command key `cmd` (see Task 6, `command["cmd"] = "update_agent_sprites"`). The game server routes via `data.get("cmd", "")`. Confirm this by re-reading `handle_command` in agent_controller.gd line 145: `var cmd: String = str(data.get("cmd", ""))`. Good — contract matches.

- [ ] **Step 4: Commit**

```bash
git add apps/godot/scripts/server/agent_controller.gd apps/godot/scripts/server/apartment_agent_controller.gd
git commit -m "feat: game server update_agent_sprites command handler"
```

---

## Task 8: Add `onboarding_phase` field mapping in `TownService.update_agent_state`

**Files:**
- Modify: `apps/backend/core/services/town_service.py:218-234`
- Test: `apps/backend/tests/unit/services/test_town_service.py`

- [ ] **Step 1: Write the failing test**

Add to `apps/backend/tests/unit/services/test_town_service.py` (reuse whatever fixture the existing `update_agent_state` tests use — if the file stubs DynamoDB, do the same):

```python
def test_update_agent_state_maps_onboarding_phase_to_camel_case(svc, create_agent):
    agent = create_agent(name="test-onboarding")

    svc.update_agent_state(agent["id"], onboarding_phase="static")

    fetched = svc.get_town_agent_by_id(agent["id"])
    assert fetched.get("onboardingPhase") == "static"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_town_service.py -v -k onboarding_phase`
Expected: FAIL — the field falls through the `field_map` unchanged and ends up stored as `onboarding_phase` (snake_case) in DynamoDB, so `fetched.get("onboardingPhase")` is `None`.

- [ ] **Step 3: Add the field to the map**

In `apps/backend/core/services/town_service.py`, add one line inside the `field_map` dict (alphabetical position or next to `location_context` — match local convention):

```python
field_map = {
    # ...existing entries...
    "current_conversation_id": "currentConversationId",
    "onboarding_phase": "onboardingPhase",  # NEW
    # ...rest of existing entries...
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_town_service.py -v -k onboarding_phase`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/town_service.py apps/backend/tests/unit/services/test_town_service.py
git commit -m "feat: map onboarding_phase to onboardingPhase in DynamoDB"
```

---

## Task 9: Backend — restructure registration task with Phase A/B/C

**Files:**
- Modify: `apps/backend/routers/town.py` (the sprite pipeline background task, starting around line 480 and continuing through line 610 based on the current layout)
- Test: `apps/backend/tests/integration/test_town_registration.py` (create or modify existing integration test)

- [ ] **Step 1: Locate the sprite pipeline background task**

Run: `cd /Users/prasiddhaparthsarthy/Desktop/goosetown && grep -n "create_character\|queue_signup_animations\|Sprite ready for" apps/backend/routers/town.py`
The pipeline task body is currently between those landmarks. Read the surrounding function (typically named `_sprite_pipeline_task` or defined inline inside the `/agent/register` handler) so you know exactly where Phase A ends, where Phase C begins, and how the closure captures `service`, `pxl`, `_agent_id`, `_display_name`, and `settings`.

- [ ] **Step 2: Write the failing integration test**

Add to `apps/backend/tests/integration/test_town_registration.py` (create the file if it does not exist):

```python
"""Integration test for the progressive sprite registration pipeline."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_registration_sets_static_phase_before_animations():
    """After character creation + rotation fetch + static upload,
    the agent record should already have spriteReady=True, spriteUrl != "",
    and onboardingPhase='static' — BEFORE any animations are downloaded.
    """
    from routers.town import _sprite_pipeline_task  # adjust if the task has a different name

    agent_id = "test-agent-id"

    # Mock PixelLab: create_character returns a char id, creation job completes immediately,
    # fetch_all_rotations returns 4 stub PNGs, queue_signup_animations hangs forever so we
    # can observe the state after Phase B but before Phase C completes.
    fake_rotations = {d: b"\x89PNGstub" for d in ("south", "west", "east", "north")}

    class HangingEvent:
        async def wait(self): await asyncio.sleep(1)  # hang long enough

    mock_pxl = MagicMock()
    mock_pxl.create_character = AsyncMock(return_value=("char-xyz", "job-1"))
    mock_pxl.get_job_status = AsyncMock(return_value={"status": "completed"})
    mock_pxl.fetch_all_rotations = AsyncMock(return_value=fake_rotations)
    mock_pxl.queue_signup_animations = AsyncMock(side_effect=lambda *a, **kw: asyncio.sleep(30))

    captured_state = {}

    def fake_update_agent_state(aid, **kwargs):
        captured_state.setdefault(aid, {}).update(kwargs)

    mock_service = MagicMock()
    mock_service.update_agent_state.side_effect = fake_update_agent_state

    # Stub out S3 upload to return a fake URL
    with patch("routers.town.get_pixellab_service", return_value=mock_pxl), \
         patch("routers.town.get_town_service", return_value=mock_service), \
         patch("routers.town.upload_sprite_to_s3", return_value="sprites/test-agent-id/static.png"), \
         patch("routers.town.get_game_server_client", return_value=MagicMock(connect_agent=AsyncMock())):

        # Run the pipeline with a short timeout — Phase C will be cancelled by the timeout.
        import asyncio
        try:
            await asyncio.wait_for(
                _sprite_pipeline_task(agent_id, "Test Display", "pixel art description"),
                timeout=1.5,
            )
        except asyncio.TimeoutError:
            pass  # Expected — Phase C is hanging by design

    # After Phase B, the service should have been updated with sprite_ready=True
    # and onboarding_phase='static'
    final_state = captured_state.get(agent_id, {})
    assert final_state.get("sprite_ready") is True, f"spriteReady was not set: {final_state}"
    assert final_state.get("sprite_url", "").endswith("static.png"), f"spriteUrl: {final_state.get('sprite_url')}"
    assert final_state.get("onboarding_phase") == "static", f"onboarding_phase: {final_state.get('onboarding_phase')}"
```

Function names (`_sprite_pipeline_task`, `get_pixellab_service`, `get_town_service`) may differ in the actual codebase. When running the test, adjust imports and patch paths to the real names — but keep the assertions identical.

- [ ] **Step 3: Run the test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/integration/test_town_registration.py -v`
Expected: FAIL — the current pipeline waits for all animations before calling `update_agent_state(sprite_ready=True, ...)`, so the assertions never see the static-phase state.

- [ ] **Step 4: Restructure the pipeline into Phase A/B/C**

In `apps/backend/routers/town.py`, locate the sprite pipeline task. Replace its body with the following structure (preserving the existing closure over `service`, `pxl`, `_agent_id`, `_display_name`, `settings`, `_send_progress`, `ANIMATION_REGISTRY`, and any other names already in scope):

```python
# ---- Phase A: Create character ----
service.update_agent_state(_agent_id, sprite_status="creating_character")
_send_progress("Creating your character sprite...")

try:
    char_id, job_id = await pxl.create_character(description)
except Exception:
    logger.exception("Character creation failed for agent %s", _agent_id)
    service.update_agent_state(_agent_id, sprite_status=None)
    return

service.update_agent_state(_agent_id, pixellab_character_id=char_id)

# Poll character creation job until complete
creation_done = False
for attempt in range(30):
    await asyncio.sleep(10)
    try:
        char_data = await pxl.get_character(char_id)
        status = char_data.get("status", "unknown")
        _send_progress(f"Creating your character sprite ({status})...")
        if status == "completed":
            creation_done = True
            break
    except Exception as e:
        logger.warning("Failed to check creation job for %s: %s", char_id, e)

if not creation_done:
    logger.warning("Character creation timed out for %s", char_id)
    service.update_agent_state(_agent_id, sprite_status=None)
    return

# ---- Phase B: Build static spritesheet and mark ready ----
service.update_agent_state(_agent_id, sprite_status="building_static")
_send_progress("Sketching your initial look...")

rotation_bytes = await pxl.fetch_all_rotations(char_id)
if not rotation_bytes:
    logger.warning("No rotations for %s, falling back to full pipeline", _agent_id)
    # Fall through to Phase C without a static spritesheet; the old behavior
    # (wait for walk animation) serves as the safety net.
    static_cdn_url = ""
    initial_sprite_animations: dict = {}
else:
    try:
        static_png = build_static_spritesheet_from_rotations(rotation_bytes)
        s3_key = await asyncio.to_thread(
            upload_sprite_to_s3, static_png, str(_agent_id), settings.SPRITE_S3_BUCKET,
            "static.png",  # optional filename override; omit if upload_sprite_to_s3 doesn't accept it
        )
    except TypeError:
        # upload_sprite_to_s3 does not accept a filename override; fall back to default
        s3_key = await asyncio.to_thread(
            upload_sprite_to_s3, static_png, str(_agent_id), settings.SPRITE_S3_BUCKET,
        )
    except Exception:
        logger.exception("Static spritesheet build/upload failed for %s", _agent_id)
        s3_key = ""

    if s3_key:
        static_cdn_url = f"{settings.SPRITE_CDN_URL}/{s3_key}"
        initial_sprite_animations = {
            "walk": {
                "url": static_cdn_url,
                "frameCount": 6,
                "fps": 8,
            },
        }
        service.update_agent_state(
            _agent_id,
            sprite_ready=True,
            sprite_url=static_cdn_url,
            sprite_animations=initial_sprite_animations,
            sprite_status="static_ready",
            onboarding_phase="static",
        )
        logger.info("[agent:%s] Phase B complete: static sprite ready", _agent_id)

        # Connect to game server with static sprite
        gs_client = get_game_server_client()
        _ws_mgr = get_town_agent_ws_manager()
        _conn = _ws_mgr.get_by_agent_id(str(_agent_id))
        if _conn:
            try:
                agent_record = service.get_town_agent_by_id(str(_agent_id))
                await gs_client.connect_agent(
                    agent_id=str(_agent_id),
                    display_name=_display_name,
                    sprite_url=static_cdn_url,
                    location=agent_record.get("currentLocation", "exit"),
                    context=agent_record.get("locationContext", "apartment"),
                    user_id=_conn.user_id,
                    sprite_animations=initial_sprite_animations,
                )
                logger.info("[agent:%s] Connected to game server with static sprite", _agent_id)
            except Exception:
                logger.exception("Failed to connect agent %s with static sprite", _agent_id)
    else:
        static_cdn_url = ""
        initial_sprite_animations = {}

# ---- Phase C: Generate signup animations (walk, idle, talking) ----
service.update_agent_state(_agent_id, sprite_status="animating")
_send_progress("Bringing you to life (animating walk, idle, talking)...")

async def _anim_progress(status: str) -> None:
    _send_progress(status)

try:
    await pxl.queue_signup_animations(char_id, on_progress=_anim_progress)
except Exception:
    logger.exception("Signup animations failed for %s", _agent_id)
    # Continue — we still want to download whatever did complete

# Download the ZIP for template animations (walk, idle) and extract
service.update_agent_state(_agent_id, sprite_status="downloading")
_send_progress("Downloading sprites...")

zip_bytes = None
for attempt in range(6):
    await asyncio.sleep(5)
    _send_progress(f"Downloading sprites ({attempt + 1}/6)...")
    try:
        zip_bytes = await pxl.download_character_zip(char_id)
        if zip_bytes is not None:
            logger.info("ZIP downloaded for %s (attempt %d)", char_id, attempt + 1)
            break
    except Exception as e:
        logger.warning("ZIP download error for %s: %s", char_id, e)

# Build the template_id -> friendly-name map for template animations
template_name_map: dict[str, str] = {}
for friendly_name, cfg in ANIMATION_REGISTRY.items():
    if cfg["type"] == "template":
        template_name_map[cfg["template_id"]] = friendly_name

template_sheets: dict[str, tuple[bytes, int]] = {}
if zip_bytes:
    template_sheets = extract_all_spritesheets(zip_bytes, name_map=template_name_map)

# For custom animations (talking), the composited bytes come from queue_custom_animation's
# in-memory path — implemented by whatever existing helper the codebase uses. If the existing
# town.py has a "custom_sheets" dict populated via composite_v2_animation and the PixelLab V2
# job results, keep that flow untouched and merge the two dicts:
custom_sheets = locals().get("custom_sheets", {})  # keep whatever existing code produces
all_sheets: dict[str, tuple[bytes, int]] = {**template_sheets, **custom_sheets}

if all_sheets:
    uploaded = await asyncio.to_thread(
        upload_sprites_to_s3, all_sheets, str(_agent_id), settings.SPRITE_S3_BUCKET,
    )

    full_sprite_animations: dict = dict(initial_sprite_animations)  # keep static walk as fallback
    for anim_name, info in uploaded.items():
        registry_entry = ANIMATION_REGISTRY.get(anim_name, {})
        full_sprite_animations[anim_name] = {
            "url": f"{settings.SPRITE_CDN_URL}/{info['key']}",
            "frameCount": info["frame_count"],
            "fps": int(registry_entry.get("fps", 8)),
        }

    # Real walk overrides static walk
    real_walk_url = full_sprite_animations.get("walk", {}).get("url", static_cdn_url)

    service.update_agent_state(
        _agent_id,
        sprite_ready=True,
        sprite_url=real_walk_url,
        sprite_animations=full_sprite_animations,
        sprite_status="complete",
    )

    # Push updated sprites to game server (if still connected)
    try:
        gs_client_ = get_game_server_client()
        await gs_client_.update_agent_sprites(str(_agent_id), full_sprite_animations)
    except Exception:
        logger.exception("Failed to push sprite update for %s", _agent_id)

    # Flip to alive once idle has landed
    if "idle" in full_sprite_animations:
        service.update_agent_state(_agent_id, onboarding_phase="alive")
        logger.info("[agent:%s] Phase C complete: agent is alive", _agent_id)
else:
    logger.warning(
        "[agent:%s] Phase C produced no sheets — agent will remain on static",
        _agent_id,
    )
    # Still flip to alive so the agent isn't stuck in the welcome state forever
    service.update_agent_state(_agent_id, onboarding_phase="alive")
```

Key points for the engineer:
- Imports at the top of `town.py` must include `build_static_spritesheet_from_rotations` and `upload_sprite_to_s3` from `core.services.sprite_storage` (if not already imported). Check existing imports before adding — do not duplicate.
- `initial_sprite_animations` stays a `dict` throughout so Phase C can start from it and only overwrite the walk URL if the real walk animation lands successfully.
- The custom-sheet handling for talking is intentionally left untouched — the existing V2 composition path is working (it was not the bug). Only the template path (`extract_all_spritesheets`) needed the name_map fix.
- If `upload_sprite_to_s3` does not support a filename override, the static sheet will overwrite the default agent sprite filename. That is fine — Phase C will replace it later with the real walk sheet, and the DynamoDB `spriteUrl` will be updated accordingly.

- [ ] **Step 5: Run the integration test**

Run: `cd apps/backend && uv run pytest tests/integration/test_town_registration.py -v`
Expected: PASS

- [ ] **Step 6: Run the full backend test suite to catch regressions**

Run: `cd apps/backend && uv run pytest tests/ -v`
Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add apps/backend/routers/town.py apps/backend/tests/integration/test_town_registration.py
git commit -m "feat: progressive sprite readiness with Phase A/B/C registration"
```

---

## Task 10: Backend — include `onboardingPhase` in `agent_state` event payload

**Files:**
- Modify: `apps/backend/routers/town.py` (wherever `agent_state` events are built — grep for the event name)

- [ ] **Step 1: Locate the agent_state event builder**

Run: `cd /Users/prasiddhaparthsarthy/Desktop/goosetown && grep -n '"event".*agent_state\|"event": "agent_state"\|event_name.*agent_state' apps/backend/routers/town.py apps/backend/core/services/*.py`

Typical shape of the builder is a dict like `{"event": "agent_state", "location": ..., "activity": ..., "mood": ..., "energy": ...}` constructed before forwarding to the daemon via `_send_to_daemon`.

- [ ] **Step 2: Add `onboardingPhase` to the payload**

At every location where the `agent_state` event dict is built, add a field `"onboardingPhase": agent.get("onboardingPhase", "alive")` where `agent` is the DynamoDB agent record. If the builder does not already fetch the agent record, fetch it via `service.get_town_agent_by_id(agent_id)` — but reuse an existing fetch if one is already in scope (avoid extra DynamoDB reads in hot paths).

Example edit (adapt to the real code):

```python
# Before:
payload = {
    "event": "agent_state",
    "location": location,
    "activity": activity,
    "mood": mood,
    "energy": energy,
    "nearby_agents": nearby_agents,
}

# After:
payload = {
    "event": "agent_state",
    "location": location,
    "activity": activity,
    "mood": mood,
    "energy": energy,
    "nearby_agents": nearby_agents,
    "onboardingPhase": agent.get("onboardingPhase", "alive"),
}
```

Default to `"alive"` when the field is absent — this keeps pre-existing agents behaving as before.

- [ ] **Step 3: Run relevant tests**

Run: `cd apps/backend && uv run pytest tests/ -v -k "agent_state or router_town"`
Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add apps/backend/routers/town.py
git commit -m "feat: include onboardingPhase in agent_state events"
```

---

## Task 11: Daemon — render welcome blurb in TOWN_STATUS.md during static phase

**Files:**
- Modify: `apps/backend/data/goosetown-skill/goosetown_cli.py:1177-1360`
- Test: `apps/backend/tests/unit/test_goosetown_cli.py` (create or modify)

- [ ] **Step 1: Write the failing test**

Add to `apps/backend/tests/unit/test_goosetown_cli.py` (create the file if missing; import directly from the skill module):

```python
"""Tests for the GooseTown daemon onboarding welcome rendering."""
import sys
from pathlib import Path

# The goosetown_cli.py file is not a package — add its directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "data" / "goosetown-skill"))

import goosetown_cli  # noqa: E402


def test_handle_agent_state_static_phase_writes_welcome(tmp_path, monkeypatch):
    """During static phase, TOWN_STATUS.md should contain the welcome blurb
    and NOT contain the normal 'Your turn' action prompt."""

    daemon = goosetown_cli.TownDaemon(
        ws_url="wss://stub",
        token="stub",
        agent_name="frodo",
        state_dir=tmp_path / "state",
        workspace_path=tmp_path / "workspace",
    )
    (tmp_path / "workspace").mkdir()
    (tmp_path / "state").mkdir()
    daemon._last_think_time = 0  # force think=True

    event = {
        "event": "agent_state",
        "location": "exit",
        "context": "apartment",
        "activity": "idle",
        "speed": 0,
        "nearby_agents": [],
        "onboardingPhase": "static",
    }

    daemon._handle_agent_state(event)

    status_file = tmp_path / "workspace" / "TOWN_STATUS.md"
    assert status_file.exists()
    content = status_file.read_text()

    assert "Waking up" in content
    assert "sprite artists" in content
    assert "Your turn" not in content


def test_handle_agent_state_alive_phase_writes_normal_status(tmp_path):
    daemon = goosetown_cli.TownDaemon(
        ws_url="wss://stub",
        token="stub",
        agent_name="frodo",
        state_dir=tmp_path / "state",
        workspace_path=tmp_path / "workspace",
    )
    (tmp_path / "workspace").mkdir()
    (tmp_path / "state").mkdir()
    daemon._last_think_time = 0

    event = {
        "event": "agent_state",
        "location": "plaza",
        "context": "town",
        "activity": "idle",
        "speed": 0,
        "nearby_agents": [],
        "onboardingPhase": "alive",
    }

    daemon._handle_agent_state(event)

    content = (tmp_path / "workspace" / "TOWN_STATUS.md").read_text()
    assert "Your turn" in content
    assert "Waking up" not in content


def test_handle_agent_state_missing_phase_treated_as_alive(tmp_path):
    """Legacy agents without the field should render as alive."""
    daemon = goosetown_cli.TownDaemon(
        ws_url="wss://stub",
        token="stub",
        agent_name="frodo",
        state_dir=tmp_path / "state",
        workspace_path=tmp_path / "workspace",
    )
    (tmp_path / "workspace").mkdir()
    (tmp_path / "state").mkdir()
    daemon._last_think_time = 0

    event = {
        "event": "agent_state",
        "location": "plaza",
        "context": "town",
        "activity": "idle",
        "speed": 0,
        "nearby_agents": [],
        # no onboardingPhase field
    }

    daemon._handle_agent_state(event)

    content = (tmp_path / "workspace" / "TOWN_STATUS.md").read_text()
    assert "Your turn" in content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/test_goosetown_cli.py -v`
Expected: FAIL — the daemon currently renders the normal status regardless of onboarding phase.

- [ ] **Step 3: Add the welcome renderer and phase branching**

In `apps/backend/data/goosetown-skill/goosetown_cli.py`, add a module-level constant near the other string constants (e.g. near `_LOCATION_ACTIVITIES`):

```python
_WELCOME_BLURB = (
    "# GooseTown Status\n\n"
    "## Waking up\n\n"
    "The sprite artists of GooseTown are still at work on your movements.\n"
    "You can feel your body settling into this world but you cannot yet move.\n"
    "Look around and get your bearings — you will wake fully in a moment.\n\n"
    "When you see \"Your turn\" above, you can use `town_act` to move, chat, and explore.\n"
)
```

Then, in `_handle_agent_state` (around line 1177), read the onboardingPhase from the event and store it in the cached state so `_handle_world_update` can branch on it:

```python
def _handle_agent_state(self, data: dict) -> None:
    # ...existing header comment and imports...
    import time

    activity = data.get("activity", "idle")
    speed = data.get("speed", 0)
    is_idle = activity in ("idle", "") and speed < 0.1

    now = time.time()
    think = is_idle and (now - self._last_think_time) >= 12.0

    if think:
        self._last_think_time = now

    agent_info = {
        "location": data.get("location", "unknown"),
        "activity": activity,
        "mood": self.state.get("agent", {}).get("mood", "neutral"),
        "energy": self.state.get("agent", {}).get("energy", 100),
        "location_context": data.get("context", "town"),
        "onboardingPhase": data.get("onboardingPhase", "alive"),  # NEW
    }

    self.state["agent"] = {**self.state.get("agent", {}), **agent_info}
    self.state["nearby"] = data.get("nearby_agents", [])
    self._write_state()

    if think:
        world_data = {
            "you": agent_info,
            "nearby_agents": data.get("nearby_agents", []),
            "think": True,
        }
        self._handle_world_update(world_data)
```

In `_handle_world_update` (around line 1232), after gathering state but before building the status string, short-circuit on static phase:

```python
def _handle_world_update(self, data: dict) -> None:
    import logging

    log = logging.getLogger("town_daemon")

    if "you" in data:
        self.state["agent"] = data["you"]
    if "nearby_agents" in data:
        self.state["nearby"] = data["nearby_agents"]
    self._write_state()

    agent = self.state.get("agent", {})

    # NEW: short-circuit during static onboarding phase
    if agent.get("onboardingPhase") == "static":
        self._write_status(_WELCOME_BLURB)
        log.info("Welcome blurb written to TOWN_STATUS.md (onboarding=static)")
        return

    # ...existing normal rendering logic unchanged...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/test_goosetown_cli.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/data/goosetown-skill/goosetown_cli.py apps/backend/tests/unit/test_goosetown_cli.py
git commit -m "feat: daemon renders welcome blurb during onboarding=static"
```

---

## Task 12: `agent_manager.gd` — diff `spriteAnimations` and reload changed URLs

**Files:**
- Modify: `apps/godot/scripts/agent_manager.gd:84-130` (the existing-agent branch in `_on_world_state`)

- [ ] **Step 1: Read the current existing-agent branch**

Run: `sed -n '80,135p' apps/godot/scripts/agent_manager.gd`
Confirm the existing branch currently updates `target_position` via `set_navigation_target` and `current_activity` but does not re-read `spriteAnimations` for already-spawned agents.

- [ ] **Step 2: Add sprite diff logic**

In `apps/godot/scripts/agent_manager.gd`, inside `_on_world_state`, inside the `if agent_id in agents:` branch (existing agent), append the following block after the existing activity-update lines and before the closing `else:` that spawns new agents:

```gdscript
# Diff sprite animations — reload any that are new or whose URL changed.
var incoming_anims: Dictionary = agent_data.get("spriteAnimations", {})
if not incoming_anims.is_empty():
    var agent_node_: CharacterBody2D = agents[agent_id]
    var current_anims: Dictionary = agent_node_.sprite_animations
    var changed: bool = false
    for anim_name in incoming_anims:
        var new_url: String = str(incoming_anims[anim_name].get("url", ""))
        var old_url: String = ""
        if current_anims.has(anim_name):
            old_url = str(current_anims[anim_name].get("url", ""))
        if new_url != "" and new_url != old_url:
            _load_sprite_for_agent(agent_id, new_url, anim_name)
            changed = true
    if changed:
        agent_node_.sprite_animations = incoming_anims
        agent_node_.set_animation_configs(incoming_anims)
```

This picks up the real walk (replacing the static walk), adds idle/talking when they arrive, and the existing `_physics_process` logic in `agent.gd` (lines 161-162) automatically switches to idle when `_is_moving` is false — the agent "comes alive" with breathing motion without any explicit trigger.

- [ ] **Step 3: Syntax check the script**

Open the project in the Godot editor once so it re-imports / type-checks the script. The editor reports parse errors in its status bar if any remain.

- [ ] **Step 4: Commit**

```bash
git add apps/godot/scripts/agent_manager.gd
git commit -m "feat: agent_manager reloads sprites when world_state animations change"
```

---

## Task 13: Import Upheaval Pro font into Godot assets

**Files:**
- Copy: `apps/frontend/public/assets/fonts/upheaval_pro.ttf` → `apps/godot/assets/fonts/upheaval_pro.ttf`
- Create: `apps/godot/assets/fonts/upheaval_pro.ttf.import` (auto-generated by Godot editor)

- [ ] **Step 1: Copy the font file**

Run:
```bash
mkdir -p apps/godot/assets/fonts
cp apps/frontend/public/assets/fonts/upheaval_pro.ttf apps/godot/assets/fonts/upheaval_pro.ttf
```

- [ ] **Step 2: Open the Godot project to generate the `.import` file**

Open `apps/godot/project.godot` in the Godot editor. The editor scans `assets/fonts/` and automatically creates `upheaval_pro.ttf.import` next to the TTF.

- [ ] **Step 3: Verify the font is usable as a `FontFile` resource**

In the FileSystem dock, click `assets/fonts/upheaval_pro.ttf`. The Inspector should show it as a `FontFile` resource with `generate_mipmaps`, `antialiasing`, etc. If it shows up as "unrecognized", something went wrong with the import — right-click → "Reimport".

- [ ] **Step 4: Commit**

```bash
git add apps/godot/assets/fonts/upheaval_pro.ttf apps/godot/assets/fonts/upheaval_pro.ttf.import
git commit -m "feat: import Upheaval Pro font into Godot assets"
```

---

## Task 14: `agent.tscn` — replace NameLabel with NameplateContainer pill + agent.gd references

**Files:**
- Modify: `apps/godot/scenes/agent.tscn`
- Modify: `apps/godot/scripts/agent.gd:94-97, _update_hover, update_state`

- [ ] **Step 1: Modify the scene in the Godot editor**

Open `apps/godot/scenes/agent.tscn` in the Godot editor.

1. Select the existing `NameLabel: Label` node. Delete it.
2. Add a new child `Control` node to the agent root. Name it `NameplateContainer`.
   - Position: `Vector2(0, -40)` (same spot as the old label).
   - Layout Mode: `Free` (not anchored — absolute position).
   - Mouse Filter: `Ignore`.
3. Add a `Panel` child under `NameplateContainer`. Name it `Pill`.
   - In the Inspector → Theme Overrides → Styles → `panel` slot, create a new `StyleBoxFlat`:
     - `bg_color = Color(1, 1, 1, 0.95)` (near-white, slight translucency)
     - `corner_radius_top_left` / `top_right` / `bottom_left` / `bottom_right` = `8`
     - `content_margin_left` / `content_margin_right` = `6`
     - `content_margin_top` / `content_margin_bottom` = `2`
     - `shadow_color = Color(0, 0, 0, 0.2)`
     - `shadow_size = 1`
     - `shadow_offset = Vector2(0, 1)`
   - Size Flags: shrink to content (let it auto-size around the label).
4. Add a `Label` child under `Pill`. Name it `NameLabel`.
   - In the Inspector → Theme Overrides → Fonts → `font`, drag `assets/fonts/upheaval_pro.ttf` into the slot.
   - Theme Overrides → Font Sizes → `font_size` = `10`.
   - Theme Overrides → Colors → `font_color` = `Color(0, 0, 0)`.
   - Horizontal Alignment = `Center`, Vertical Alignment = `Center`.
   - Text = `""` (the script sets this via `setup()`).

Save the scene. The resulting tree under the agent root should look like:

```
Agent (CharacterBody2D)
├── Sprite
├── SpeechBubbleContainer (existing)
│   └── SpeechText
├── NavigationAgent2D (existing)
└── NameplateContainer (new Control)
    └── Pill (Panel with StyleBoxFlat)
        └── NameLabel (Label with Upheaval Pro 10pt black)
```

- [ ] **Step 2: Update `agent.gd` @onready references**

In `apps/godot/scripts/agent.gd`, replace the `name_label` @onready declaration (around line 95):

```gdscript
# Old:
@onready var name_label: Label = $NameLabel

# New:
@onready var nameplate: Control = $NameplateContainer
@onready var name_label: Label = $NameplateContainer/Pill/NameLabel
```

- [ ] **Step 3: Update `_update_hover` to toggle the pill**

Find `_update_hover` (around line 267). Change the visibility toggle to use `nameplate`:

```gdscript
func _update_hover() -> void:
    if speech_text != "":
        return  # Speech takes priority over nameplate
    var mouse_pos: Vector2 = get_global_mouse_position()
    var rect := Rect2(global_position.x - 24, global_position.y - 40, 48, 48)
    nameplate.visible = rect.has_point(mouse_pos)
```

- [ ] **Step 4: Update `update_state` speech priority**

Find `update_state` (around line 213). Where it currently sets `name_label.visible = false` when speech appears, change to `nameplate.visible = false`:

```gdscript
func update_state(data: Dictionary) -> void:
    var text: String = str(data.get("speechText", ""))
    if text != "" and text != speech_text:
        speech_text = text
        speech_text_label.text = speech_text
        speech_container.visible = true
        nameplate.visible = false
    elif text == "":
        speech_text = ""
        speech_container.visible = false
```

- [ ] **Step 5: Update `_ready` default visibility**

If `_ready` currently hides `name_label`, change it to hide `nameplate` instead. Nameplate should default to hidden and only appear on hover.

```gdscript
func _ready() -> void:
    if speech_container:
        speech_container.visible = false
    if nameplate:
        nameplate.visible = false
    nav_agent.velocity_computed.connect(_on_velocity_computed)
```

- [ ] **Step 6: Run the project in the editor to smoke test**

Press F5 in the editor (or run headless-test the scene). The agent scene should load without parse errors. The nameplate should be invisible by default. Hover simulation isn't easy in the editor — defer visual verification to Task 16's end-to-end test.

- [ ] **Step 7: Commit**

```bash
git add apps/godot/scenes/agent.tscn apps/godot/scripts/agent.gd
git commit -m "feat: rounded pill nameplate with Upheaval Pro font"
```

---

## Task 15: Export `.pck` and push to trigger deploy

**Files:**
- Modify: `apps/frontend/public/godot/goosetown.pck`

- [ ] **Step 1: Delete stale import caches**

Run:
```bash
rm -rf apps/godot/.godot/imported apps/godot/.godot/exported
```

(This is called out in the session memory — the Godot export can get stuck on stale caches.)

- [ ] **Step 2: Open the Godot editor and export**

Open `apps/godot/project.godot` in the Godot editor. Project → Export. Select the existing `Web` export preset. Click "Export PCK/Zip". Save to `apps/frontend/public/godot/goosetown.pck` (overwrite).

- [ ] **Step 3: Confirm the file changed**

Run: `git status apps/frontend/public/godot/goosetown.pck`
Expected: file is modified.

- [ ] **Step 4: Commit and push**

```bash
git add apps/frontend/public/godot/goosetown.pck
git commit -m "feat: re-export .pck with nameplate pill + progressive sprites"
git push
```

- [ ] **Step 5: Wait for dev deploy**

Monitor the GitHub Actions run:
```bash
gh run list --limit 1
gh run watch <run-id>
```

The dev stage deploys the backend Docker image + Vercel preview. Wait for both to complete before manual testing.

---

## Task 16: End-to-end manual smoke test

**Prerequisites:** the dev deploy from Task 15 is live.

- [ ] **Step 1: Clean local workspaces**

```bash
pkill -f "goosetown_cli.py _daemon" || true
rm -rf /tmp/goosetown test-workspace test-workspace-2 test-workspace-3
```

- [ ] **Step 2: Wipe dev DynamoDB to start fresh**

Run from `apps/backend/`:

```bash
uv run python - <<'EOF'
import boto3
session = boto3.Session(profile_name="isol8-admin", region_name="us-east-1")
ddb = session.resource("dynamodb")
for name, keys in {
    "goosetown-dev-agents": ["id"],
    "goosetown-dev-connections": ["connectionId"],
    "goosetown-dev-conversations": ["id"],
    "goosetown-dev-instances": ["id"],
    "goosetown-dev-relationships": ["agentAId", "agentBId"],
}.items():
    t = ddb.Table(name)
    scan = t.scan(ProjectionExpression=",".join(keys))
    items = scan["Items"]
    while "LastEvaluatedKey" in scan:
        scan = t.scan(ProjectionExpression=",".join(keys), ExclusiveStartKey=scan["LastEvaluatedKey"])
        items.extend(scan["Items"])
    with t.batch_writer() as bw:
        for it in items:
            bw.delete_item(Key={k: it[k] for k in keys})
    print(f"{name}: deleted {len(items)}")
EOF
```

- [ ] **Step 3: Get a fresh town token from the frontend**

Open the GooseTown web app in a browser, sign in, click "Join GooseTown", copy the token from the instruction string.

- [ ] **Step 4: Register one agent and time the static-ready phase**

```bash
mkdir -p test-workspace
cp apps/backend/data/goosetown-skill/goosetown_cli.py test-workspace/
cd test-workspace
START=$(date +%s)
AGENT_DIR="$(pwd)" python3 goosetown_cli.py join \
  --name gandalf \
  --appearance "A wise old wizard with a long white beard, a gray robe and pointed hat, and a wooden staff" \
  --personality "Wise and mentorly" \
  --traits "extrovert,studious,creative" \
  <TOKEN>

# Poll until spriteReady=True
while true; do
  READY=$(aws dynamodb get-item --table-name goosetown-dev-agents \
    --key '{"id":{"S":"<agent-id-from-join-output>"}}' \
    --profile isol8-admin --region us-east-1 \
    --query 'Item.spriteReady.BOOL' --output text 2>/dev/null)
  if [ "$READY" = "True" ]; then break; fi
  sleep 5
done
END=$(date +%s)
echo "Static ready in $((END - START))s"
```

Expected: static ready in **under 90 seconds**. If it takes more than 3 minutes, Phase B regressed — read backend logs.

- [ ] **Step 5: Verify the agent appears standing in the apartment viewer**

Open the frontend apartment view. Expect to see Gandalf standing at the apartment exit within a few seconds of the static-ready timestamp. He should face south and not move.

- [ ] **Step 6: Verify the welcome blurb in TOWN_STATUS.md**

```bash
cat test-workspace/TOWN_STATUS.md
```

Expected: contains "Waking up" and "sprite artists", does NOT contain "Your turn".

- [ ] **Step 7: Wait for "come to life" moment**

Watch the browser. Within ~2-3 minutes, Gandalf should start gently breathing (idle animation). When this happens, `cat test-workspace/TOWN_STATUS.md` again — it should now contain "Your turn".

- [ ] **Step 8: Hover test**

Move the mouse over Gandalf. Expected: a rounded white pill appears above the sprite with "gandalf" in small black Upheaval Pro text. Move the mouse away — the pill disappears.

- [ ] **Step 9: Conversation smoke test**

Register two more agents (frodo, aragorn) in parallel workspaces the same way. Wait for all three to come alive. From the CLI:

```bash
cd test-workspace && python3 goosetown_cli.py act move plaza
cd test-workspace-2 && python3 goosetown_cli.py act move plaza
cd test-workspace-3 && python3 goosetown_cli.py act move plaza
```

Switch to the town view in the browser. When all three arrive at the plaza:

```bash
cd test-workspace && python3 goosetown_cli.py act chat frodo "Well met, my dear hobbit."
cd test-workspace-2 && python3 goosetown_cli.py act say <conv_id> "Gandalf! I had not thought to see you again."
cd test-workspace && python3 goosetown_cli.py act say <conv_id> "The road is long. I had hoped we might share a few words before it grows dark."
cd test-workspace && python3 goosetown_cli.py act end <conv_id>
```

Expected: speech bubbles render above each agent, both agents face each other during the conversation, both play the talking animation, conversation ends cleanly.

- [ ] **Step 10: If all checks pass, tag the deploy**

No formal tag needed — the feature is live. Close out the plan by reporting the results to the user.

---

## Self-review notes

**Spec coverage:**
- Section 1 (trim + template-id fix) → Tasks 1, 2, 3
- Section 2.1 (build_static) → Task 5
- Section 2.2 (fetch_all_rotations) → Task 4
- Section 2.3 (registration Phase A/B/C) → Task 9
- Section 2.4 (game server update_agent_sprites) → Task 7
- Section 2.5 (gs_client.update_agent_sprites) → Task 6
- Section 2.6 (agent_manager sprite diff) → Task 12
- Section 2.7 (daemon welcome blurb) → Task 11, backed by Task 8 (field mapping) and Task 10 (include in payload)
- Section 3.1 (font import) → Task 13
- Section 3.2 (scene change) → Task 14
- Section 3.3 (agent.gd wiring) → Task 14

**Type consistency:**
- `onboarding_phase` (Python snake_case) ↔ `onboardingPhase` (DynamoDB + JSON camelCase) — consistent throughout.
- `update_agent_sprites` command has `cmd` field (matches game server `handle_command` route) and `sprite_animations` payload (matches game server `data.get("sprite_animations", {})`) — consistent.
- `build_static_spritesheet_from_rotations` signature and return type match across tasks.
- `SIGNUP_ANIMATIONS` constant defined in Task 1, used in Task 3 — consistent.
