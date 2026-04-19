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
    WS_LAMBDA_SECRET: str = os.getenv("WS_LAMBDA_SECRET", "")
    # Set WS_STRICT_VALIDATION=false to roll back to legacy dict-based body handling for 48h post-deploy
    WS_STRICT_VALIDATION: bool = os.getenv("WS_STRICT_VALIDATION", "true").lower() != "false"

    def validate(self) -> None:
        """Validate required env vars. Raises RuntimeError in non-dev environments if critical vars are missing."""
        if self.ENVIRONMENT == "dev":
            return

        required = {
            "TOWN_TOKEN_SECRET": self.TOWN_TOKEN_SECRET,
            "WS_MANAGEMENT_API_URL": self.WS_MANAGEMENT_API_URL,
            "WS_LAMBDA_SECRET": self.WS_LAMBDA_SECRET,
            "SPRITE_S3_BUCKET": self.SPRITE_S3_BUCKET,
            "SPRITE_CDN_URL": self.SPRITE_CDN_URL,
            "CLERK_ISSUER": self.CLERK_ISSUER,
        }
        missing = [name for name, val in required.items() if not val]
        if missing:
            raise RuntimeError(f"Missing required environment variables for {self.ENVIRONMENT}: {', '.join(missing)}")
        logger.info("Configuration validated for environment: %s", self.ENVIRONMENT)


settings = Settings()
