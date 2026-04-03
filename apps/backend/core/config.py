"""GooseTown backend configuration."""

import logging
import os

logger = logging.getLogger(__name__)


class Settings:
    AWS_REGION: str = os.getenv("AWS_REGION", "us-east-1")
    DYNAMODB_TABLE_PREFIX: str = os.getenv("DYNAMODB_TABLE_PREFIX", "goosetown")
    CLERK_ISSUER: str = os.getenv("CLERK_ISSUER", "https://clerk.isol8.co")
    CLERK_AUDIENCE: str | None = os.getenv("CLERK_AUDIENCE", None)
    TOWN_TOKEN_SECRET: str = os.getenv("TOWN_TOKEN_SECRET", "")
    CORS_ORIGINS: str = os.getenv("CORS_ORIGINS", "http://localhost:5173")
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "dev")
    PIXELLAB_API_KEY: str = os.getenv("PIXELLAB_API_KEY", "")
    SPRITE_S3_BUCKET: str = os.getenv("SPRITE_S3_BUCKET", "")
    SPRITE_CDN_URL: str = os.getenv("SPRITE_CDN_URL", "")
    WS_MANAGEMENT_API_URL: str = os.getenv("WS_MANAGEMENT_API_URL", "")
    GAME_SERVER_URL: str = os.getenv("GAME_SERVER_URL", "ws://localhost:8080")

    def validate(self) -> None:
        """Validate required env vars. Raises RuntimeError in non-dev environments if critical vars are missing."""
        if self.ENVIRONMENT == "dev":
            return

        missing = []
        if not self.TOWN_TOKEN_SECRET:
            missing.append("TOWN_TOKEN_SECRET")
        if not self.WS_MANAGEMENT_API_URL:
            missing.append("WS_MANAGEMENT_API_URL")
        if not self.CLERK_ISSUER:
            missing.append("CLERK_ISSUER")

        if missing:
            raise RuntimeError(f"Missing required environment variables for {self.ENVIRONMENT}: {', '.join(missing)}")
        logger.info("Configuration validated for environment: %s", self.ENVIRONMENT)


settings = Settings()
