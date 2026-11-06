"""PixelLab sprite generation service for GooseTown agents."""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

import httpx

from core.config import settings
from core.services.appearance_validation import validate_appearance
from core.services.pixellab_quota import reserve_request

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
    # lazily generated (fast/slow split, spec 2026-06-11) —
    # idle via the activity layer, talking via the conversation-start hook
    "idle": {"type": "template", "template_id": "breathing-idle", "fps": 4.0, "auto_generate": False},
    "drink": {
        "type": "custom",
        "auto_generate": False,
        "frames": 8,
        "fps": 6.0,
        "prompt": (
            "Raising a cup to the mouth with one hand, taking a sip, lowering "
            "it again, then settling back to a relaxed standing pose. " + _LOOP_SUFFIX
        ),
    },
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
    # lazily generated (fast/slow split, spec 2026-06-11) —
    # idle via the activity layer, talking via the conversation-start hook
    "talking": {
        "type": "custom",
        "auto_generate": False,
        "frames": 8,
        "fps": 4.0,
        "prompt": (
            "Standing upright with one hand raised gesturing, then lowering "
            "as head nods, other hand opening palm-up briefly, weight shifting "
            "subtly, ending in a settled speaking posture. " + _LOOP_SUFFIX
        ),
    },
    "dance": {
        "type": "custom",
        "auto_generate": False,
        "frames": 8,
        "fps": 8.0,
        "prompt": (
            "Dancing energetically in place, hips and shoulders swaying side "
            "to side to a beat, arms pumping up and down alternately, small "
            "bounce in the knees on each step. " + _LOOP_SUFFIX
        ),
    },
}

TEMPLATE_ANIMATIONS = {k: v for k, v in ANIMATION_REGISTRY.items() if v["type"] == "template"}
CUSTOM_ANIMATIONS = {k: v for k, v in ANIMATION_REGISTRY.items() if v["type"] == "custom"}
SIGNUP_ANIMATIONS = {k: v for k, v in ANIMATION_REGISTRY.items() if v["auto_generate"]}


class PixelLabService:
    """Generates character sprites via the PixelLab API."""

    def __init__(self, api_key: str | None = None, *, owner_id: str | None = None):
        self.api_key = api_key or settings.PIXELLAB_API_KEY
        self.owner_id = owner_id

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
            if method.upper() != "GET":
                reserve_request(self.owner_id)
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
        description = validate_appearance(description)
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
        ``get_existing_animation_names``) so a degraded PixelLab degrades
        gracefully instead of stalling on backoff.
        """
        resp = await self._request("GET", f"{PIXELLAB_API_URL}/characters/{character_id}", retries=retries)
        resp.raise_for_status()
        return resp.json()

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

    def _frames_from_job(self, job: dict) -> list[bytes]:
        """Decode a completed job's frames (last_response.images, rgba_bytes) into
        PNG bytes for compositing. Returns [] if no usable images."""
        import base64
        import io

        from PIL import Image

        images = (job.get("last_response") or {}).get("images") or []
        frames: list[bytes] = []
        for im in images:
            b64 = im.get("base64")
            if not b64:
                continue
            raw = base64.b64decode(b64)
            t = (im.get("type") or "").lower()
            try:
                if "rgba" in t or t == "bytes":
                    img = Image.frombytes("RGBA", (im["width"], im["height"]), raw)
                else:
                    img = Image.open(io.BytesIO(raw)).convert("RGBA")
            except Exception:
                logger.warning("Could not decode an animation frame (type=%s)", t)
                continue
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            frames.append(buf.getvalue())
        return frames

    async def _poll_and_collect(
        self,
        directions: list[str],
        job_ids: list[str],
        on_progress,
        label: str,
        max_attempts: int = 30,
        poll_interval: float = 10.0,
    ) -> dict[str, list[bytes]]:
        """Poll the per-direction jobs, fetching each direction's frames AS it
        completes (PixelLab cleans finished jobs up — a fetch deferred to after
        the whole batch can 404). Returns {direction: [png_bytes]}."""
        pending = dict(zip(job_ids, directions))  # job_id -> direction
        collected: dict[str, list[bytes]] = {}
        for _attempt in range(max_attempts):
            await asyncio.sleep(poll_interval)
            done_now: list[str] = []
            for jid, direction in pending.items():
                try:
                    job = await self.get_job_status(jid)
                except httpx.HTTPStatusError as exc:
                    if exc.response is not None and exc.response.status_code == 404:
                        logger.warning("Job %s (%s/%s) vanished (404) before fetch", jid, label, direction)
                        done_now.append(jid)
                    continue
                except Exception as exc:
                    # Transient poll error (timeout/connect) — log and retry next
                    # sweep rather than silently dropping the direction.
                    logger.debug("Poll error for job %s (%s/%s): %s", jid, label, direction, exc)
                    continue
                status = job.get("status")
                if status == "completed":
                    frames = self._frames_from_job(job)
                    if frames:
                        collected[direction] = frames
                    done_now.append(jid)
                elif status == "failed":
                    logger.warning("Job %s (%s/%s) failed", jid, label, direction)
                    done_now.append(jid)
            for jid in done_now:
                pending.pop(jid, None)
            if on_progress:
                await on_progress(f"{label} {len(collected)}/{len(directions)} done")
            if not pending:
                break
        return collected

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
    ) -> dict[str, dict[str, list[bytes]]]:
        """Generate every auto_generate signup animation (walk only — idle and
        talking generate lazily) sequentially via generate_animation. Returns
        {anim_name: {direction: [png_bytes]}} for the caller to composite.

        Each animation is generated end-to-end (submit + poll + collect frames)
        behind the global concurrency gate inside ``generate_animation``. Running
        them sequentially keeps us under PixelLab's concurrent-job ceiling.

        Failures PROPAGATE — the SQS worker's retry/DLQ owns them. With
        SIGNUP_ANIMATIONS == {"walk"} there are no "others" to continue with,
        and swallowing the failure would let the pipeline mark the agent
        ``spriteStatus="complete"`` on the Phase B static placeholder (silent
        degradation, invisible to the boot sweep). The agent keeps operating
        on the Phase B static sprite between retries.
        """
        results: dict[str, dict[str, list[bytes]]] = {}
        items = list(SIGNUP_ANIMATIONS.items())
        for idx, (name, _config) in enumerate(items):
            frames = await self.generate_animation(character_id, name, on_progress=on_progress, max_attempts=30)
            results[name] = frames
            logger.info("Signup animation %s: %d/%d directions", name, len(frames), len(CARDINAL_4))
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
        except PixelLabCreditError:
            raise
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

    async def generate_animation(
        self,
        character_id: str,
        anim_name: str,
        *,
        on_progress=None,
        max_attempts: int = 30,
    ) -> dict[str, list[bytes]]:
        """Generate ONE animation (all 4 directions) end-to-end under the global
        concurrency gate: submit the single batched call, then collect frames as
        each direction completes. Returns {direction: [png_bytes]} (empty on no
        jobs). This is the single entry point signup, activity, and admin all use."""
        async with _get_animation_semaphore():
            directions, job_ids = await self.queue_animation(character_id, anim_name)
            if not job_ids:
                return {}
            return await self._poll_and_collect(directions, job_ids, on_progress, anim_name, max_attempts=max_attempts)
