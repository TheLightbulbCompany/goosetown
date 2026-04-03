"""PixelLab sprite generation service for GooseTown agents."""

import logging

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

PIXELLAB_API_URL = "https://api.pixellab.ai/v2"

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
    "performing": {"type": "custom", "prompt": "standing arms raised performing expressively", "frames": 8, "fps": 6.0},
    "exercising": {"type": "custom", "prompt": "doing jumping jacks energetically", "frames": 8, "fps": 8.0},
    "gaming":     {"type": "custom", "prompt": "leaning forward excited, hands over a game board", "frames": 8, "fps": 6.0},
    "gardening":  {"type": "custom", "prompt": "crouching tending plants with hands", "frames": 8, "fps": 4.0},
    "cooking":    {"type": "custom", "prompt": "standing at counter stirring a pot", "frames": 8, "fps": 6.0},
    "sleeping":   {"type": "custom", "prompt": "lying down, chest rising and falling slowly", "frames": 8, "fps": 3.0},
    "talking":    {"type": "custom", "prompt": "standing gesturing while speaking to someone", "frames": 8, "fps": 6.0},
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

    async def queue_template_animations(self, character_id: str) -> list[str]:
        """Queue all template animations for a character. Returns flat list of all job IDs."""
        all_job_ids: list[str] = []
        for name, config in TEMPLATE_ANIMATIONS.items():
            try:
                job_ids = await self.animate_character(character_id, config["template_id"])
                logger.info(
                    "Queued template animation %r for character %s: %d job(s)",
                    name,
                    character_id,
                    len(job_ids),
                )
                all_job_ids.extend(job_ids)
            except Exception:
                logger.exception(
                    "Failed to queue template animation %r for character %s — skipping",
                    name,
                    character_id,
                )
        return all_job_ids

    async def queue_custom_animation(
        self,
        character_id: str,
        animation_name: str,
        action_description: str,
        directions: list[str] | None = None,
    ) -> list[str]:
        """Queue a custom V3 animation for a character.

        Makes two calls: first to get a cost estimate, then to confirm and queue.
        Returns list of job IDs, or empty list on failure.
        """
        if directions is None:
            directions = ["south", "west", "east", "north"]

        body = {
            "character_id": character_id,
            "animation_name": animation_name,
            "action_description": action_description,
            "directions": directions,
        }

        async with httpx.AsyncClient() as client:
            # First call: get cost estimate (no confirm_cost)
            try:
                estimate_resp = await client.post(
                    f"{PIXELLAB_API_URL}/characters/animations",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=body,
                    timeout=30.0,
                )
                if estimate_resp.status_code != 200:
                    logger.error(
                        "PixelLab custom animation estimate %r %s: %s",
                        animation_name,
                        estimate_resp.status_code,
                        estimate_resp.text[:500],
                    )
                    estimate_resp.raise_for_status()
                logger.info(
                    "Cost estimate for custom animation %r on character %s: %s",
                    animation_name,
                    character_id,
                    estimate_resp.json(),
                )
            except Exception:
                logger.exception(
                    "Failed to get cost estimate for custom animation %r on character %s",
                    animation_name,
                    character_id,
                )
                return []

            # Second call: confirm cost and queue
            try:
                confirmed_body = {**body, "confirm_cost": True}
                queue_resp = await client.post(
                    f"{PIXELLAB_API_URL}/characters/animations",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=confirmed_body,
                    timeout=30.0,
                )
                if queue_resp.status_code != 200:
                    logger.error(
                        "PixelLab custom animation queue %r %s: %s",
                        animation_name,
                        queue_resp.status_code,
                        queue_resp.text[:500],
                    )
                    queue_resp.raise_for_status()
                data = queue_resp.json()
                job_ids = data.get("background_job_ids", [])
                logger.info(
                    "Queued custom animation %r for character %s: %d job(s)",
                    animation_name,
                    character_id,
                    len(job_ids),
                )
                return job_ids
            except Exception:
                logger.exception(
                    "Failed to queue custom animation %r for character %s",
                    animation_name,
                    character_id,
                )
                return []
