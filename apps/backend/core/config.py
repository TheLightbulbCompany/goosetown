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
    RESEND_API_KEY: str = os.getenv("RESEND_API_KEY", "")
    RESEND_AUDIENCE_ID: str = os.getenv("RESEND_AUDIENCE_ID", "")
    SPRITE_S3_BUCKET: str = os.getenv("SPRITE_S3_BUCKET", "")
    SPRITE_CDN_URL: str = os.getenv("SPRITE_CDN_URL", "")
    WS_MANAGEMENT_API_URL: str = os.getenv("WS_MANAGEMENT_API_URL", "")
    GAME_SERVER_URL: str = os.getenv("GAME_SERVER_URL", "ws://localhost:8080")
    WS_LAMBDA_SECRET: str = os.getenv("WS_LAMBDA_SECRET", "")
    # Set WS_STRICT_VALIDATION=false to roll back to legacy dict-based body handling for 48h post-deploy
    WS_STRICT_VALIDATION: bool = os.getenv("WS_STRICT_VALIDATION", "true").lower() != "false"
    PIXELLAB_MAX_CONCURRENT_ANIMATIONS: int = int(os.environ.get("PIXELLAB_MAX_CONCURRENT_ANIMATIONS", "2"))
    PIXELLAB_POOL_MAX_CONNECTIONS: int = int(os.environ.get("PIXELLAB_POOL_MAX_CONNECTIONS", "10"))
    PIXELLAB_POOL_MAX_KEEPALIVE: int = int(os.environ.get("PIXELLAB_POOL_MAX_KEEPALIVE", "5"))

    # ── Durable sprite pipeline (spec 2026-06-11) ──────────────────────────
    # Gates ALL lazy-animation enqueues (activity anims + the talking hook).
    # Default true; the dev backend sets ACTIVITY_ANIMATION_GENERATION=false
    # via CDK (compute-stack) so dev never burns the shared PixelLab account
    # on lazy generation. Signup walk generation is unaffected by this flag.
    ACTIVITY_ANIMATION_GENERATION: bool = os.environ.get("ACTIVITY_ANIMATION_GENERATION", "true").lower() != "false"
    SPRITE_JOBS_QUEUE_NAME: str = os.environ.get(
        "SPRITE_JOBS_QUEUE_NAME", f"goosetown-{os.getenv('ENVIRONMENT', 'dev')}-sprite-jobs"
    )
    SPRITE_WORKER_MAX_CONCURRENT_PIPELINES: int = int(os.environ.get("SPRITE_WORKER_MAX_CONCURRENT_PIPELINES", "2"))
    # Base URL of the backend ALB, used by the sprite worker to POST
    # /town/internal/sprite-event. Empty in the backend itself.
    BACKEND_INTERNAL_URL: str = os.environ.get("BACKEND_INTERNAL_URL", "")

    def validate(self) -> None:
        """Validate required env vars. Raises RuntimeError in non-dev environments if critical vars are missing."""
        if self.ENVIRONMENT == "dev":
            return

        # WS_LAMBDA_SECRET is intentionally NOT in the required list. The
        # corresponding shared-secret authentication between the API
        # Gateway WS Lambdas and the backend's /ws-message endpoint was
        # never wired through CDK — the Lambdas at apps/infra/lambda/
        # don't read or send the x-ws-lambda-secret header, so the
        # backend's check at routers/town.py:1219 is vacuously satisfied
        # ("" != "" → False, no rejection). Adding it to ``required``
        # blocks prod startup with no security benefit, since neither
        # side actually enforces the secret today. If we later wire the
        # secret end-to-end (auth-stack creates it; api-stack passes
        # it to Lambdas as env or fetched-from-Secrets-Manager;
        # compute-stack passes it to the backend as a Secret), this
        # check can be reinstated.
        required = {
            "TOWN_TOKEN_SECRET": self.TOWN_TOKEN_SECRET,
            "WS_MANAGEMENT_API_URL": self.WS_MANAGEMENT_API_URL,
            "SPRITE_S3_BUCKET": self.SPRITE_S3_BUCKET,
            "SPRITE_CDN_URL": self.SPRITE_CDN_URL,
            "CLERK_ISSUER": self.CLERK_ISSUER,
        }
        missing = [name for name, val in required.items() if not val]
        if missing:
            raise RuntimeError(f"Missing required environment variables for {self.ENVIRONMENT}: {', '.join(missing)}")
        logger.info("Configuration validated for environment: %s", self.ENVIRONMENT)


settings = Settings()
