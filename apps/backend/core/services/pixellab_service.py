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
    "walk":       {"type": "template", "template_id": "walk", "fps": 8.0},
    "idle":       {"type": "template", "template_id": "breathing-idle", "fps": 4.0},
    "run":        {"type": "template", "template_id": "running-6-frames", "fps": 12.0},
    "drink":      {"type": "template", "template_id": "drinking", "fps": 6.0},
    "crouch":     {"type": "template", "template_id": "crouching", "fps": 6.0},
    "pick_up":    {"type": "template", "template_id": "picking-up", "fps": 6.0},
    # Custom V3 (generated lazily on first use)
    # Prompts describe the full movement sequence for PixelLab frame generation.
    "sitting": {
        "type": "custom",
        "frames": 8,
        "fps": 4.0,
        "prompt": (
            "Sitting down relaxed on a low surface, hands resting on knees, "
            "gently shifting weight side to side, head tilting slightly, "
            "occasionally looking around before settling back into a calm posture"
        ),
    },
    "reading": {
        "type": "custom",
        "frames": 8,
        "fps": 4.0,
        "prompt": (
            "Sitting cross-legged holding an open book, eyes scanning the pages, "
            "one hand reaching to turn a page then returning, head nodding "
            "slightly while reading, ears or hair shifting with small movements"
        ),
    },
    "writing": {
        "type": "custom",
        "frames": 8,
        "fps": 6.0,
        "prompt": (
            "Sitting at a surface writing in a notebook, hand moving steadily "
            "across the page in short strokes, pausing briefly to think with "
            "pen raised, then resuming writing with focused posture"
        ),
    },
    "working": {
        "type": "custom",
        "frames": 8,
        "fps": 6.0,
        "prompt": (
            "Sitting hunched forward over a laptop, fingers tapping rapidly on "
            "the keyboard, occasionally leaning back to think then leaning "
            "forward again, shoulders shifting with typing rhythm"
        ),
    },
    "performing": {
        "type": "custom",
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
        "frames": 8,
        "fps": 3.0,
        "prompt": (
            "Lying down on side in a curled sleeping position, chest slowly "
            "rising and falling with deep breaths, small subtle shifts in "
            "posture, completely relaxed with eyes closed, peaceful and still"
        ),
    },
    "talking": {
        "type": "custom",
        "frames": 8,
        "fps": 6.0,
        "prompt": (
            "Standing upright gesturing while speaking, one hand raised making "
            "a point then lowering, head nodding along, weight shifting between "
            "feet, other hand occasionally joining in an open palm gesture"
        ),
    },
}

TEMPLATE_ANIMATIONS = {k: v for k, v in ANIMATION_REGISTRY.items() if v["type"] == "template"}
CUSTOM_ANIMATIONS = {k: v for k, v in ANIMATION_REGISTRY.items() if v["type"] == "custom"}


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
        """Get background job status."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{PIXELLAB_API_URL}/background-jobs/{job_id}",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=30.0,
            )
            resp.raise_for_status()
            return resp.json()

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

    async def has_pending_jobs(self, character_id: str) -> bool:
        """Check if a PixelLab character has any pending animation jobs.

        Uses get_character to check for pending jobs. This is the reliable
        completion signal — ZIP 423 is unreliable.
        """
        try:
            char_data = await self.get_character(character_id)
            pending = char_data.get("pending_jobs", [])
            return len(pending) > 0
        except Exception as e:
            logger.warning("Failed to check pending jobs for %s: %s", character_id, e)
            return True  # Assume pending on error (keep polling)

    async def queue_template_animations(
        self,
        character_id: str,
        on_progress: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    ) -> list[str]:
        """Queue all template animations for a character. Returns flat list of all job IDs.

        Checks PixelLab character for existing animations first to avoid
        duplicate generation. Waits for each animation to finish before
        queuing the next to avoid PixelLab's concurrent job limit.

        Args:
            character_id: PixelLab character UUID.
            on_progress: Optional async callback called every ~10s during waits.
                         Use to send keepalive messages to the daemon.
        """
        import asyncio

        existing = await self.get_existing_animation_names(character_id)
        all_job_ids: list[str] = []
        template_items = list(TEMPLATE_ANIMATIONS.items())
        for idx, (name, config) in enumerate(template_items):
            if config["template_id"] in existing:
                logger.info("Template %s already exists on character %s, skipping", name, character_id)
                continue
            try:
                job_ids = await self.animate_character(character_id, animation=config["template_id"])
                logger.info(
                    "Queued template animation %r for character %s: %d job(s)",
                    name,
                    character_id,
                    len(job_ids),
                )
                all_job_ids.extend(job_ids)
                # Wait for this animation to finish before queuing the next
                for attempt in range(30):
                    await asyncio.sleep(10)
                    if on_progress:
                        await on_progress(
                            f"Generating {name} animation ({idx + 1}/{len(template_items)})..."
                        )
                    if not await self.has_pending_jobs(character_id):
                        break
            except Exception:
                logger.exception(
                    "Failed to queue template animation %r for character %s — skipping",
                    name,
                    character_id,
                )
        logger.info(
            "Queued %d template animation jobs for character %s (%d animations)",
            len(all_job_ids), character_id, len(TEMPLATE_ANIMATIONS),
        )
        return all_job_ids

    async def queue_custom_animation(
        self,
        character_id: str,
        animation_name: str,
        action_description: str,
        directions: list[str] | None = None,
    ) -> list[str]:
        """Queue a custom animation for a character.

        Uses the animate-with-text endpoint. Sends with confirm_cost=true
        to auto-approve. Returns list of job IDs, or empty list on failure.
        """
        if directions is None:
            directions = ["south", "west", "east", "north"]

        body: dict = {
            "character_id": character_id,
            "action_description": action_description,
            "animation_name": animation_name,
            "directions": directions,
            "confirm_cost": True,
        }

        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    f"{PIXELLAB_API_URL}/characters/animations",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=body,
                    timeout=30.0,
                )
                if resp.status_code != 200:
                    logger.error(
                        "PixelLab custom animation %r %s: %s",
                        animation_name,
                        resp.status_code,
                        resp.text[:500],
                    )
                    return []
                data = resp.json()
                job_ids = data.get("background_job_ids", [])
                logger.info(
                    "Custom animation %r queued for %s: %d jobs, response: %s",
                    animation_name, character_id, len(job_ids), data,
                )
                return job_ids
            except Exception:
                logger.exception(
                    "Failed to queue custom animation %r for character %s",
                    animation_name,
                    character_id,
                )
                return []
