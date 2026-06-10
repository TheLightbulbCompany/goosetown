"""PixelLab sprite generation service for GooseTown agents."""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

PIXELLAB_API_URL = "https://api.pixellab.ai/v2"

# We composite 4-direction spritesheets even though characters carry 8 rotations.
# v3/custom mode defaults to south-only if directions is omitted (template
# defaults to all) — so we ALWAYS pass the 4 cardinals explicitly.
CARDINAL_4: list[str] = ["south", "west", "east", "north"]

# Retry policy for transient PixelLab failures. A single connect/read timeout or
# 429/5xx blip used to kill an entire sprite generation — e.g. a skipped walk
# animation left a grey-box sprite, or a create_character ConnectTimeout dropped
# the whole regen. Retry those with exponential backoff; do NOT retry 4xx or
# caller-handled statuses (423 "not ready", 202 "accepted").
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_RETRY_MAX_ATTEMPTS = 4
_RETRY_BASE_DELAY_SEC = 1.5


class PixelLabCreditError(Exception):
    """Raised on HTTP 402 — out of PixelLab generations/credits. Not retryable;
    alarm-worthy (a fresh-signup spike can drain credits silently)."""


# One pooled client shared across all PixelLab calls — keep-alive reuse + an
# explicit socket cap, instead of a fresh AsyncClient (new TLS handshake) per
# call. asyncio is single-threaded so the lazy create is race-free (no await
# between the None check and the assignment).
_pixellab_client: httpx.AsyncClient | None = None


def _get_pixellab_client() -> httpx.AsyncClient:
    global _pixellab_client
    if _pixellab_client is None or _pixellab_client.is_closed:
        _pixellab_client = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=settings.PIXELLAB_POOL_MAX_CONNECTIONS,
                max_keepalive_connections=settings.PIXELLAB_POOL_MAX_KEEPALIVE,
            ),
            timeout=httpx.Timeout(30.0),
        )
    return _pixellab_client


async def close_pixellab_client() -> None:
    """Close the pooled client on app shutdown."""
    global _pixellab_client
    if _pixellab_client is not None and not _pixellab_client.is_closed:
        await _pixellab_client.aclose()
    _pixellab_client = None


# One global gate on concurrent in-flight animation generations across EVERY
# path (signup, activity, admin). Each animation = up to 4 PixelLab jobs, so the
# default of 2 keeps us ~<=8 concurrent jobs — under PixelLab's (undocumented)
# concurrency cap. Tunable via PIXELLAB_MAX_CONCURRENT_ANIMATIONS.
_animation_semaphore: "asyncio.Semaphore | None" = None


def _get_animation_semaphore() -> asyncio.Semaphore:
    global _animation_semaphore
    if _animation_semaphore is None:
        _animation_semaphore = asyncio.Semaphore(settings.PIXELLAB_MAX_CONCURRENT_ANIMATIONS)
    return _animation_semaphore


# ---------------------------------------------------------------------------
# Animation registry — single source of truth for all animation assets.
# Template animations are generated at registration (~5 min).
# Custom animations are generated lazily on first activity use (~30-180s on v3).
# ---------------------------------------------------------------------------

# Appended to every custom-animation prompt. animate-with-text-v3 has no loop
# flag in the API, so loop intent has to live in the prompt itself.
_LOOP_SUFFIX = "Looping animation cycle: the final frame returns to the same posture and pose as the first frame."

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
    # Each custom prompt ends with the LOOP_SUFFIX so the final frame matches
    # the first — animate-with-text-v3 has no looping flag in the API, so the
    # loop intent has to live in the action description.
    "sitting": {
        "type": "custom",
        "auto_generate": False,
        "frames": 12,
        "fps": 4.0,
        "prompt": (
            "Sitting upright on a low surface with hands on knees, gently "
            "shifting weight side to side, head turning slowly to look around, "
            "then settling back to relaxed posture. " + _LOOP_SUFFIX
        ),
    },
    "reading": {
        "type": "custom",
        "auto_generate": False,
        "frames": 12,
        "fps": 4.0,
        "prompt": (
            "Sitting cross-legged holding an open book in both hands, head "
            "moving slowly down the page, one hand lifting to turn a page "
            "then returning, eyes refocused on text. " + _LOOP_SUFFIX
        ),
    },
    "writing": {
        "type": "custom",
        "auto_generate": False,
        "frames": 12,
        "fps": 6.0,
        "prompt": (
            "Sitting at a desk leaning over a notebook, writing hand moving "
            "in short rhythmic strokes, pausing briefly with pen lifted, "
            "then resuming the writing motion. " + _LOOP_SUFFIX
        ),
    },
    "working": {
        "type": "custom",
        "auto_generate": False,
        "frames": 12,
        "fps": 6.0,
        "prompt": (
            "Sitting forward at a laptop with hands typing, fingers tapping "
            "the keyboard in steady rhythm, leaning back briefly to think, "
            "then leaning forward to keep typing. " + _LOOP_SUFFIX
        ),
    },
    "performing": {
        "type": "custom",
        "auto_generate": False,
        "frames": 12,
        "fps": 8.0,
        "prompt": (
            "Standing with both arms raised wide, swaying body side to side "
            "in rhythm, one arm sweeping outward then the other, head tilting "
            "back then forward, energetic. " + _LOOP_SUFFIX
        ),
    },
    "exercising": {
        "type": "custom",
        "auto_generate": False,
        "frames": 16,
        "fps": 12.0,
        "prompt": (
            "Doing jumping jacks: arms swing up overhead and back to sides, "
            "legs spread out then together, body bounces with each repetition, "
            "ending hands at sides ready to repeat. " + _LOOP_SUFFIX
        ),
    },
    "gaming": {
        "type": "custom",
        "auto_generate": False,
        "frames": 12,
        "fps": 6.0,
        "prompt": (
            "Sitting forward over a tabletop game, one hand moving a piece "
            "with care, then leaning back with hand on chin to think, "
            "leaning in again to make a move. " + _LOOP_SUFFIX
        ),
    },
    "gardening": {
        "type": "custom",
        "auto_generate": False,
        "frames": 12,
        "fps": 5.0,
        "prompt": (
            "Crouching beside a small plant, patting soil with both hands, "
            "reaching to pull a weed, then gently watering, ending crouched "
            "ready to start again. " + _LOOP_SUFFIX
        ),
    },
    "cooking": {
        "type": "custom",
        "auto_generate": False,
        "frames": 12,
        "fps": 6.0,
        "prompt": (
            "Standing at a counter stirring a pot in circular motion, other "
            "hand reaching to add an ingredient, leaning forward to smell, "
            "then returning to steady stirring. " + _LOOP_SUFFIX
        ),
    },
    "sleeping": {
        "type": "custom",
        "auto_generate": False,
        "frames": 8,
        "fps": 1.0,
        "prompt": (
            "Lying curled on side with eyes closed, chest slowly rising and "
            "falling with deep breaths, small shift of arm or shoulder, "
            "settling back into peaceful stillness. " + _LOOP_SUFFIX
        ),
    },
    "talking": {
        "type": "custom",
        "auto_generate": True,
        "frames": 8,
        "fps": 4.0,
        "prompt": (
            "Standing upright with one hand raised gesturing, then lowering "
            "as head nods, other hand opening palm-up briefly, weight shifting "
            "subtly, ending in a settled speaking posture. " + _LOOP_SUFFIX
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

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        timeout: float = 30.0,
        retries: int = _RETRY_MAX_ATTEMPTS,
    ) -> httpx.Response:
        """PixelLab HTTP request with retry-on-transient-failure + exponential backoff.

        Retries connect/read timeouts, transport errors, and 429/5xx — the
        transient blips that previously killed a whole sprite generation. Does
        NOT retry on other statuses (4xx, or caller-handled 423/202): those are
        returned for the caller to inspect (``resp.status_code`` / ``raise_for_status``).
        Raises the last transient error after ``retries`` attempts.

        Pass ``retries=1`` (fail-fast, no backoff) for calls that already sit
        inside a polling loop with its own retry cadence — e.g. ``get_job_status``
        under ``wait_for_jobs``. Retrying there would multiply the deliberately
        capped poll timeout (PR #314) into a multi-minute stall during an outage.
        """
        headers = {"Authorization": f"Bearer {self.api_key}"}
        endpoint = url.rsplit("/", 1)[-1].split("?")[0]
        last_exc: Exception | None = None
        client = _get_pixellab_client()
        for attempt in range(retries):
            try:
                resp = await client.request(method, url, headers=headers, json=json, timeout=timeout)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                logger.warning(
                    "PixelLab %s %s transient %s (attempt %d/%d)",
                    method,
                    endpoint,
                    type(exc).__name__,
                    attempt + 1,
                    retries,
                )
            else:
                if resp.status_code == 402:
                    logger.error(
                        "PixelLab CREDIT EXHAUSTED (402) on %s %s — sprite gen will fail until topped up",
                        method,
                        endpoint,
                    )
                    raise PixelLabCreditError(f"PixelLab 402 on {endpoint}")
                if resp.status_code not in _RETRYABLE_STATUS:
                    return resp
                last_exc = httpx.HTTPStatusError(f"PixelLab {resp.status_code}", request=resp.request, response=resp)
                logger.warning(
                    "PixelLab %s %s -> %d (attempt %d/%d), retrying",
                    method,
                    endpoint,
                    resp.status_code,
                    attempt + 1,
                    retries,
                )
            if attempt < retries - 1:
                await asyncio.sleep(_RETRY_BASE_DELAY_SEC * (2**attempt))
        assert last_exc is not None
        raise last_exc

    async def create_character(
        self,
        description: str,
    ) -> tuple[str, str | None]:
        """Queue character creation. Returns (character_id, background_job_id)."""
        resp = await self._request(
            "POST",
            f"{PIXELLAB_API_URL}/create-character-with-4-directions",
            json={
                "description": description,
                "image_size": {"width": 96, "height": 96},
                "outline": "single color black outline",
                "shading": "basic shading",
                "detail": "medium detail",
                "view": "low top-down",
            },
        )
        if resp.status_code != 200:
            logger.error("PixelLab create_character %s: %s", resp.status_code, resp.text[:500])
        resp.raise_for_status()
        data = resp.json()
        return data["character_id"], data.get("background_job_id")

    async def get_character(self, character_id: str, *, retries: int = _RETRY_MAX_ATTEMPTS) -> dict:
        """Get character status and sprite URLs.

        Pass ``retries=1`` from fail-fast fallback callers (e.g.
        ``get_existing_animation_names``, ``_get_rotation_base64``) so a degraded
        PixelLab degrades gracefully instead of stalling on backoff.
        """
        resp = await self._request("GET", f"{PIXELLAB_API_URL}/characters/{character_id}", retries=retries)
        resp.raise_for_status()
        return resp.json()

    async def animate_character(
        self, character_id: str, animation: str = "walk", action_description: str | None = None
    ) -> list[str]:
        """Queue animation for a character. Returns list of background job IDs."""
        body: dict[str, Any] = {
            "character_id": character_id,
            "template_animation_id": animation,
        }
        if action_description:
            body["action_description"] = action_description
        resp = await self._request("POST", f"{PIXELLAB_API_URL}/characters/animations", json=body)
        if resp.status_code != 200:
            logger.error(f"PixelLab animate_character {resp.status_code}: {resp.text}")
        resp.raise_for_status()
        data = resp.json()
        return data.get("background_job_ids", [])

    async def get_job_status(self, job_id: str) -> dict:
        """Get background job status.

        Returns dict with 'status', 'id', 'last_response' (when completed).
        For animate-with-text-v2 jobs, last_response contains frame images.

        Fail-fast (``retries=1``): this is always called from a polling loop
        (``wait_for_jobs`` / the creation poll) that already retries each cycle.
        Internal backoff here would multiply the capped poll timeout (PR #314).
        """
        resp = await self._request("GET", f"{PIXELLAB_API_URL}/background-jobs/{job_id}", retries=1)
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
        resp = await self._request("GET", f"{PIXELLAB_API_URL}/characters/{character_id}/zip", timeout=60.0)
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
            char_data = await self.get_character(character_id, retries=1)
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
                    # Cap signup-animation polling at 5 min (30 × 10s) instead
                    # of the default 10 min. The agent is already operational
                    # in "static" phase by the time Phase C runs (Phase B
                    # connects them to the game server with a static sprite),
                    # so animations are a nice-to-have, not a blocker. A 10-min
                    # wait left the user staring at "walk (1/3) 0/4 done" in
                    # TOWN_STATUS.md when PixelLab itself was slow — surfaced
                    # by the 2026-05-06 integration test (celeste + atlas
                    # both stalled). If the cap expires, ``advance_phase`` to
                    # "alive" still happens with the static sprite.
                    await self.wait_for_jobs(
                        job_ids,
                        on_progress=on_progress,
                        progress_label=progress_label,
                        max_attempts=30,
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
            char_data = await self.get_character(character_id, retries=1)
            rotation_urls = char_data.get("rotation_urls", {})
            url = rotation_urls.get(direction, "")
            if not url:
                logger.warning("No %s rotation URL for character %s", direction, character_id)
                return None

            async with httpx.AsyncClient() as client:
                resp = await client.get(url, timeout=30.0)
                resp.raise_for_status()
                import base64

                # Raw base64 only — PixelLab's Base64Image model expects the
                # base64 string without a ``data:image/png;base64,`` prefix.
                # Including the prefix made every animate-with-text-v2/v3 call
                # silently fall back to generation without our reference image,
                # which produced a gray background and inconsistent character.
                b64 = base64.b64encode(resp.content).decode()
                size = char_data.get("size", {})
                w = size.get("width", 96)
                h = size.get("height", 96)
                return b64, w, h
        except Exception:
            logger.exception("Failed to fetch %s rotation for character %s", direction, character_id)
            return None

    async def queue_custom_animation(
        self,
        character_id: str,
        animation_name: str,
        action_description: str,
        frame_count: int = 8,
        directions: list[str] | None = None,
    ) -> list[str]:
        """Queue a custom animation using animate-with-text-v3.

        For each direction, fetches the character's rotation image and
        sends it to /animate-with-text-v3 as ``first_frame`` with the action
        description. v3 takes a single reference frame, returns a configurable
        ``frame_count`` (4-16, default 8), and infers view/direction from the
        reference image — much simpler payload than v2 plus smoother motion.
        Returns list of background job IDs in direction order.
        """
        if directions is None:
            directions = ["south", "west", "east", "north"]

        job_ids: list[str] = []
        for direction in directions:
            rotation = await self._get_rotation_base64(character_id, direction)
            if not rotation:
                logger.warning("Skipping %s direction for custom animation %s", direction, animation_name)
                continue

            ref_image, _ref_w, _ref_h = rotation
            # Full PixelLab Base64Image shape: ``{type, base64, format}``.
            # Without the ``type``/``format`` fields the model deserializer
            # silently dropped our reference image and generated from prompt
            # alone — yielding a gray background and a drifting character.
            body = {
                "first_frame": {"type": "base64", "base64": ref_image, "format": "png"},
                "action": action_description,
                "frame_count": frame_count,
            }

            try:
                resp = await self._request("POST", f"{PIXELLAB_API_URL}/animate-with-text-v3", json=body, timeout=60.0)
                if resp.status_code not in (200, 202):
                    logger.error(
                        "PixelLab animate-with-text-v3 %s/%s %s: %s",
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
                        "Custom animation %s/%s queued for %s: job %s (frames=%d)",
                        animation_name,
                        direction,
                        character_id,
                        job_id,
                        frame_count,
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

    async def queue_animation(self, character_id: str, anim_name: str) -> tuple[list[str], list[str]]:
        """Queue ALL cardinal directions of one animation in a single
        ``POST /characters/animations`` call. Returns ``(directions, job_ids)``
        aligned by index. Handles template + v3 from ANIMATION_REGISTRY; v3 uses
        the character's own rotations as the per-direction reference (no manual
        reference-frame fetch). Returns ([], []) for an unknown animation."""
        config = ANIMATION_REGISTRY.get(anim_name)
        if not config:
            logger.warning("queue_animation: unknown animation %r — skipping", anim_name)
            return [], []

        body: dict[str, Any] = {"character_id": character_id, "directions": CARDINAL_4}
        if config["type"] == "template":
            body["mode"] = "template"
            body["template_animation_id"] = config["template_id"]
        else:
            body["mode"] = "v3"
            body["action_description"] = config["prompt"]
            body["frame_count"] = int(config.get("frames", 8))
            body["enhance_prompt"] = True
            body["animation_name"] = anim_name

        resp = await self._request("POST", f"{PIXELLAB_API_URL}/characters/animations", json=body, timeout=60.0)
        if resp.status_code not in (200, 202):
            logger.error("PixelLab characters/animations %s -> %s: %s", anim_name, resp.status_code, resp.text[:300])
            resp.raise_for_status()
        data = resp.json()
        directions = data.get("directions") or CARDINAL_4
        job_ids = data.get("background_job_ids") or []
        logger.info("queue_animation %s/%s: %d jobs (%s)", character_id[:8], anim_name, len(job_ids), config["type"])
        return directions, job_ids
