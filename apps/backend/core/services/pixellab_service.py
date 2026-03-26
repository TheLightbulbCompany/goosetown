"""PixelLab sprite generation service for GooseTown agents."""

import logging

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

PIXELLAB_API_URL = "https://api.pixellab.ai/v2"


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
