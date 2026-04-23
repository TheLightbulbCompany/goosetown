"""PixelLab sprite generation service for GooseTown agents."""

import logging
from collections.abc import Callable, Coroutine
from typing import Any

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

PIXELLAB_API_URL = "https://api.pixellab.ai/v2"

# ---------------------------------------------------------------------------
# Animation registry — single source of truth for all animation assets.
# Template animations are generated at registration (~5 min).
# Custom animations are generated lazily on first activity use (~8 min each).
# ---------------------------------------------------------------------------
ANIMATION_REGISTRY: dict[str, dict] = {
    # Templates (1 gen/direction, auto all directions)
    "walk": {"type": "template", "template_id": "walk", "fps": 8.0, "auto_generate": True},
    "idle": {"type": "template", "template_id": "breathing-idle", "fps": 4.0, "auto_generate": True},
    "run": {"type": "template", "template_id": "running-6-frames", "fps": 12.0, "auto_generate": False},
    "drink": {"type": "template", "template_id": "drinking", "fps": 6.0, "auto_generate": False},
    "crouch": {"type": "template", "template_id": "crouching", "fps": 6.0, "auto_generate": False},
    "pick_up": {"type": "template", "template_id": "picking-up", "fps": 6.0, "auto_generate": False},
    # Custom V3 (generated lazily on first use)
    # Prompts describe the full movement sequence for PixelLab frame generation.
    "sitting": {
        "type": "custom",
        "auto_generate": False,
        "frames": 8,
        "fps": 1.0,
        "prompt": (
            "Sitting down relaxed on a low surface, hands resting on knees, "
            "gently shifting weight side to side, head tilting slightly, "
            "occasionally looking around before settling back into a calm posture"
        ),
    },
    "reading": {
        "type": "custom",
        "auto_generate": False,
        "frames": 8,
        "fps": 1.0,
        "prompt": (
            "Sitting cross-legged holding an open book, eyes scanning the pages, "
            "one hand reaching to turn a page then returning, head nodding "
            "slightly while reading, ears or hair shifting with small movements"
        ),
    },
    "writing": {
        "type": "custom",
        "auto_generate": False,
        "frames": 8,
        "fps": 2.0,
        "prompt": (
            "Sitting at a surface writing in a notebook, hand moving steadily "
            "across the page in short strokes, pausing briefly to think with "
            "pen raised, then resuming writing with focused posture"
        ),
    },
    "working": {
        "type": "custom",
        "auto_generate": False,
        "frames": 8,
        "fps": 2.0,
        "prompt": (
            "Sitting hunched forward over a laptop, fingers tapping rapidly on "
            "the keyboard, occasionally leaning back to think then leaning "
            "forward again, shoulders shifting with typing rhythm"
        ),
    },
    "performing": {
        "type": "custom",
        "auto_generate": False,
        "frames": 8,
        "fps": 6.0,
        "prompt": (
            "Standing with arms raised expressively, swaying body side to side, "
            "one arm sweeping outward then the other, weight shifting between "
            "feet, head tilting back with energy then forward to engage"
        ),
    },
    "exercising": {
        "type": "custom",
        "auto_generate": False,
        "frames": 8,
        "fps": 8.0,
        "prompt": (
            "Doing jumping jacks with full body motion, arms swinging up "
            "overhead then back down to sides, legs spreading apart and "
            "together, body bouncing with each rep, energetic and rhythmic"
        ),
    },
    "gaming": {
        "type": "custom",
        "auto_generate": False,
        "frames": 8,
        "fps": 6.0,
        "prompt": (
            "Sitting leaning forward excitedly, hands moving pieces on a board, "
            "reacting with a small fist pump, then studying the board intently "
            "with hand on chin before reaching to make the next move"
        ),
    },
    "gardening": {
        "type": "custom",
        "auto_generate": False,
        "frames": 8,
        "fps": 4.0,
        "prompt": (
            "Crouching down close to the ground, gently patting soil around a "
            "small plant with both hands, reaching to pull a weed, then "
            "carefully watering with a small motion, brushing dirt off hands"
        ),
    },
    "cooking": {
        "type": "custom",
        "auto_generate": False,
        "frames": 8,
        "fps": 6.0,
        "prompt": (
            "Standing at a counter stirring a pot with one hand in circular "
            "motions, other hand reaching to add an ingredient, leaning in "
            "to smell the food, then returning to stir with steady rhythm"
        ),
    },
    "sleeping": {
        "type": "custom",
        "auto_generate": False,
        "frames": 8,
        "fps": 0.5,
        "prompt": (
            "Lying down on side in a curled sleeping position, chest slowly "
            "rising and falling with deep breaths, small subtle shifts in "
            "posture, completely relaxed with eyes closed, peaceful and still"
        ),
    },
    "talking": {
        "type": "custom",
        "auto_generate": True,
        "frames": 8,
        # Talking cadence: PixelLab's custom animation endpoint frequently
        # delivers fewer frames than requested (observed 4 instead of 8).
        # At 6 fps a 4-frame loop is 0.67s — way too fast for a natural
        # speaking gesture. 3 fps produces a ~1.3s loop which reads as a
        # measured talking cadence even if only 4 frames arrive.
        "fps": 3.0,
        "prompt": (
            "Standing upright gesturing while speaking, one hand raised making "
            "a point then lowering, head nodding along, weight shifting between "
            "feet, other hand occasionally joining in an open palm gesture"
        ),
    },
}

TEMPLATE_ANIMATIONS = {k: v for k, v in ANIMATION_REGISTRY.items() if v["type"] == "template"}
CUSTOM_ANIMATIONS = {k: v for k, v in ANIMATION_REGISTRY.items() if v["type"] == "custom"}
SIGNUP_ANIMATIONS = {k: v for k, v in ANIMATION_REGISTRY.items() if v["auto_generate"]}


class PixelLabService:
    """Generates character sprites via the PixelLab API."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or settings.PIXELLAB_API_KEY

    async def create_character(
        self,
        description: str,
    ) -> tuple[str, str | None]:
        """Queue character creation. Returns (character_id, background_job_id)."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{PIXELLAB_API_URL}/create-character-with-4-directions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "description": description,
                    "image_size": {"width": 96, "height": 96},
                    "outline": "single color black outline",
                    "shading": "basic shading",
                    "detail": "medium detail",
                    "view": "low top-down",
                },
                timeout=30.0,
            )
            if resp.status_code != 200:
                logger.error("PixelLab create_character %s: %s", resp.status_code, resp.text[:500])
            resp.raise_for_status()
            data = resp.json()
            return data["character_id"], data.get("background_job_id")

    async def get_character(self, character_id: str) -> dict:
        """Get character status and sprite URLs."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{PIXELLAB_API_URL}/characters/{character_id}",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=30.0,
            )
            resp.raise_for_status()
            return resp.json()

    async def animate_character(
        self, character_id: str, animation: str = "walk", action_description: str | None = None
    ) -> list[str]:
        """Queue animation for a character. Returns list of background job IDs."""
        async with httpx.AsyncClient() as client:
            body = {
                "character_id": character_id,
                "template_animation_id": animation,
            }
            if action_description:
                body["action_description"] = action_description
            resp = await client.post(
                f"{PIXELLAB_API_URL}/characters/animations",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=body,
                timeout=30.0,
            )
            if resp.status_code != 200:
                logger.error(f"PixelLab animate_character {resp.status_code}: {resp.text}")
            resp.raise_for_status()
            data = resp.json()
            return data.get("background_job_ids", [])

    async def get_job_status(self, job_id: str) -> dict:
        """Get background job status.

        Returns dict with 'status', 'id', 'last_response' (when completed).
        For animate-with-text-v2 jobs, last_response contains frame images.
        """
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{PIXELLAB_API_URL}/background-jobs/{job_id}",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=30.0,
            )
            resp.raise_for_status()
            return resp.json()

    async def get_v2_animation_frames(self, job_id: str) -> list[bytes]:
        """Get completed V2 animation frames as PNG bytes.

        V2 jobs return frames as raw RGBA byte buffers (type=rgba_bytes).
        This method decodes them into PNG bytes for spritesheet compositing.
        Returns list of PNG byte arrays, or empty list on failure.
        """
        import base64
        import io

        from PIL import Image

        try:
            job = await self.get_job_status(job_id)
            if job.get("status") != "completed":
                logger.warning("Job %s not completed (status=%s)", job_id, job.get("status"))
                return []

            last_response = job.get("last_response", {})
            if not last_response:
                logger.warning("Job %s completed but no last_response", job_id)
                return []

            images = last_response.get("images", [])
            frames: list[bytes] = []
            for img_data in images:
                if not isinstance(img_data, dict):
                    continue
                b64 = img_data.get("base64", "")
                img_type = img_data.get("type", "")
                width = img_data.get("width", 0)
                height = img_data.get("height", 0)

                if not b64:
                    continue

                raw_bytes = base64.b64decode(b64)

                if img_type == "rgba_bytes" and width > 0 and height > 0:
                    # Raw RGBA pixel data — convert to PNG
                    img = Image.frombytes("RGBA", (width, height), raw_bytes)
                    buf = io.BytesIO()
                    img.save(buf, format="PNG")
                    frames.append(buf.getvalue())
                else:
                    # Assume PNG/JPEG encoded
                    frames.append(raw_bytes)

            logger.info(
                "Got %d frames from V2 job %s (%dx%d)",
                len(frames),
                job_id,
                images[0].get("width", 0) if images else 0,
                images[0].get("height", 0) if images else 0,
            )
            return frames
        except Exception:
            logger.exception("Failed to get V2 frames from job %s", job_id)
            return []

    async def download_character_zip(self, character_id: str) -> bytes | None:
        """Download character ZIP (rotations + animations). Returns None if not ready (423)."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{PIXELLAB_API_URL}/characters/{character_id}/zip",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=60.0,
            )
            if resp.status_code == 423:
                return None  # animations still processing
            resp.raise_for_status()
            return resp.content

    async def get_existing_animation_names(self, character_id: str) -> set[str]:
        """Check which animations already exist on a PixelLab character.

        Returns set of animation names (e.g. {"walk", "breathing-idle"}).
        Falls back to empty set on API error.
        """
        try:
            char_data = await self.get_character(character_id)
            animations = char_data.get("animations", [])
            return {a.get("name", "") for a in animations if a.get("name")}
        except Exception as e:
            logger.warning("Failed to check existing animations for %s: %s", character_id, e)
            return set()

    async def wait_for_jobs(
        self,
        job_ids: list[str],
        on_progress: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        progress_label: str = "",
        max_attempts: int = 60,
        poll_interval: float = 10.0,
    ) -> bool:
        """Poll job IDs until all are completed or failed.

        Args:
            job_ids: List of background job IDs to poll.
            on_progress: Optional async callback for keepalive messages.
            progress_label: Label for progress messages.
            max_attempts: Max polling attempts before giving up.
            poll_interval: Seconds between polls.

        Returns:
            True if all jobs completed, False if timed out or all failed.
        """
        import asyncio

        if not job_ids:
            return True

        for attempt in range(max_attempts):
            await asyncio.sleep(poll_interval)

            completed = 0
            failed = 0
            for jid in job_ids:
                try:
                    job = await self.get_job_status(jid)
                    status = job.get("status", "unknown")
                    if status == "completed":
                        completed += 1
                    elif status == "failed":
                        failed += 1
                except Exception as exc:
                    # Don't raise — a single transient PixelLab error
                    # shouldn't abort the whole batch — but make the
                    # failure visible so outages don't look like stalls.
                    logger.warning(
                        "PixelLab poll failed for job %s (%s attempt %d/%d): %s",
                        jid,
                        progress_label,
                        attempt + 1,
                        max_attempts,
                        exc,
                    )

            if on_progress:
                await on_progress(f"{progress_label} {completed}/{len(job_ids)} done")

            if completed + failed == len(job_ids):
                logger.info(
                    "All jobs done for %s: %d completed, %d failed",
                    progress_label,
                    completed,
                    failed,
                )
                return completed > 0

        logger.warning("Jobs timed out for %s: %d/%d completed", progress_label, completed, len(job_ids))
        return completed > 0

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
                            name,
                            character_id,
                        )
                        results[name] = []
                        continue
                    job_ids = await self.animate_character(
                        character_id,
                        animation=template_id,
                    )
                elif config["type"] == "custom":
                    prompt: str = config["prompt"]
                    job_ids = await self.queue_custom_animation(
                        character_id=character_id,
                        animation_name=name,
                        action_description=prompt,
                    )
                else:
                    logger.warning(
                        "Unknown animation type for %s: %r",
                        name,
                        config.get("type"),
                    )
                    continue

                logger.info(
                    "Queued signup animation %r for %s: %d jobs",
                    name,
                    character_id,
                    len(job_ids),
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
                    name,
                    character_id,
                )
                results.setdefault(name, [])

        logger.info(
            "Signup animations done for %s: %s",
            character_id,
            {k: len(v) for k, v in results.items()},
        )
        return results

    async def fetch_all_rotations(self, character_id: str) -> dict[str, bytes]:
        """Download all 4 rotation PNGs for a character.

        Returns a dict of direction -> raw PNG bytes. Directions that fail to
        download or are missing from the character's rotation_urls are omitted
        from the result — the caller decides how to handle partial coverage.
        """
        try:
            char_data = await self.get_character(character_id)
        except Exception:
            logger.exception(
                "Failed to get character %s for rotation fetch",
                character_id,
            )
            return {}

        rotation_urls: dict[str, str] = char_data.get("rotation_urls", {}) or {}
        directions = ("south", "west", "east", "north")
        result: dict[str, bytes] = {}

        async with httpx.AsyncClient() as client:
            for direction in directions:
                url = rotation_urls.get(direction, "")
                if not url:
                    logger.warning(
                        "No %s rotation URL for character %s",
                        direction,
                        character_id,
                    )
                    continue
                try:
                    resp = await client.get(url, timeout=30.0)
                    resp.raise_for_status()
                    result[direction] = resp.content
                except Exception:
                    logger.exception(
                        "Failed to fetch %s rotation for character %s",
                        direction,
                        character_id,
                    )

        return result

    async def fetch_animation_spritesheets(self, character_id: str) -> dict[str, dict[str, list[bytes]]]:
        """Download animation frames grouped by clean API animation name and direction.

        Cross-references the character API (clean names like 'walk', 'breathing-idle')
        with the ZIP download (opaque folder names like 'walking-abc123').

        Matching: for each ZIP folder, find the API animation entry whose
        (direction, num_frames) matches. Last folder wins for duplicates.

        Returns dict mapping animation_name -> direction -> list of frame PNG bytes.
        Empty dict if ZIP unavailable or no animations found.
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

        # Build API lookup: (direction, num_frames) -> animation_name
        api_animations = char_data.get("animations", [])
        api_lookup: dict[tuple[str, int], str] = {}
        for entry in api_animations:
            name = entry.get("name", "")
            direction = entry.get("direction", "")
            num_frames = entry.get("num_frames", 0)
            if name and direction:
                api_lookup[(direction, num_frames)] = name

        # Match ZIP folders to API animation names
        result: dict[str, dict[str, list[bytes]]] = {}
        for folder_name, dir_data in zip_animations.items():
            for direction, frame_paths in dir_data.items():
                num_frames = len(frame_paths)
                anim_name = api_lookup.get((direction, num_frames))
                if anim_name is None:
                    logger.debug(
                        "No API match for ZIP folder %s (%s, %d frames)",
                        folder_name,
                        direction,
                        num_frames,
                    )
                    continue

                frames: list[bytes] = []
                for frame_path in sorted(frame_paths):
                    try:
                        frames.append(zf.read(frame_path))
                    except KeyError:
                        logger.warning("Missing frame in ZIP: %s", frame_path)

                if frames:
                    result.setdefault(anim_name, {})[direction] = frames

        logger.info(
            "Resolved %d animations from ZIP for character %s: %s",
            len(result),
            character_id,
            {k: list(v.keys()) for k, v in result.items()},
        )
        return result

    async def _get_rotation_base64(self, character_id: str, direction: str) -> tuple[str, int, int] | None:
        """Fetch a character's rotation image and return (base64_str, width, height).

        Returns None if the rotation image can't be fetched.
        """
        try:
            char_data = await self.get_character(character_id)
            rotation_urls = char_data.get("rotation_urls", {})
            url = rotation_urls.get(direction, "")
            if not url:
                logger.warning("No %s rotation URL for character %s", direction, character_id)
                return None

            async with httpx.AsyncClient() as client:
                resp = await client.get(url, timeout=30.0)
                resp.raise_for_status()
                import base64

                b64 = base64.b64encode(resp.content).decode()
                size = char_data.get("size", {})
                w = size.get("width", 96)
                h = size.get("height", 96)
                return f"data:image/png;base64,{b64}", w, h
        except Exception:
            logger.exception("Failed to fetch %s rotation for character %s", direction, character_id)
            return None

    async def queue_custom_animation(
        self,
        character_id: str,
        animation_name: str,
        action_description: str,
        directions: list[str] | None = None,
    ) -> list[str]:
        """Queue a custom animation using animate-with-text-v2 (pro).

        For each direction, fetches the character's rotation image and
        sends it to /animate-with-text-v2 with the action description.
        Returns list of background job IDs.
        """
        if directions is None:
            directions = ["south", "west", "east", "north"]

        # Get character info for image size
        try:
            char_data = await self.get_character(character_id)
            image_size = char_data.get("size", {"width": 96, "height": 96})
            view = char_data.get("view", "low top-down")
        except Exception:
            logger.exception("Failed to get character %s for custom animation", character_id)
            return []

        job_ids: list[str] = []
        for direction in directions:
            rotation = await self._get_rotation_base64(character_id, direction)
            if not rotation:
                logger.warning("Skipping %s direction for custom animation %s", direction, animation_name)
                continue

            ref_image, ref_w, ref_h = rotation
            body = {
                "reference_image": {"base64": ref_image},
                "reference_image_size": {"width": ref_w, "height": ref_h},
                "action": action_description,
                "image_size": {"width": image_size["width"], "height": image_size["height"]},
                "direction": direction,
                "view": view,
                "no_background": True,
            }

            async with httpx.AsyncClient() as client:
                try:
                    resp = await client.post(
                        f"{PIXELLAB_API_URL}/animate-with-text-v2",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json=body,
                        timeout=60.0,
                    )
                    if resp.status_code not in (200, 202):
                        logger.error(
                            "PixelLab animate-with-text-v2 %s/%s %s: %s",
                            animation_name,
                            direction,
                            resp.status_code,
                            resp.text[:500],
                        )
                        continue
                    data = resp.json()
                    job_id = data.get("background_job_id") or data.get("job_id")
                    if job_id:
                        job_ids.append(job_id)
                        logger.info(
                            "Custom animation %s/%s queued for %s: job %s",
                            animation_name,
                            direction,
                            character_id,
                            job_id,
                        )
                except Exception:
                    logger.exception(
                        "Failed to queue custom animation %s/%s for %s",
                        animation_name,
                        direction,
                        character_id,
                    )

        logger.info(
            "Custom animation %s: %d/%d directions queued for character %s",
            animation_name,
            len(job_ids),
            len(directions),
            character_id,
        )
        return job_ids
