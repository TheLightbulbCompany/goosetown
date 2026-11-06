"""GooseTown backend configuration."""

import logging
import os

logger = logging.getLogger(__name__)


class Settings:
    AWS_REGION: str = os.getenv("AWS_REGION", "us-east-1")
    DYNAMODB_TABLE_PREFIX: str = os.getenv("DYNAMODB_TABLE_PREFIX", "goosetown")
    CLERK_ISSUER: str = os.getenv("CLERK_ISSUER", "https://clerk.thelightbulb.company")
    CLERK_AUTHORIZED_PARTIES: str = os.getenv("CLERK_AUTHORIZED_PARTIES", "https://goosetown.thelightbulb.company")
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
    # Shared secret for the API Gateway WS Lambdas -> backend /town/ws-message
    # and /town/ws-disconnect seam. Wired end-to-end as of #427: auth-stack
    # generates it, api-stack passes the secret NAME to the ws-default and
    # ws-disconnect Lambdas (which fetch it via boto3 at cold start and send
    # the x-ws-lambda-secret header), and compute-stack injects the value
    # into the backend.
    WS_LAMBDA_SECRET: str = os.getenv("WS_LAMBDA_SECRET", "")
    # Comma-separated Clerk user IDs allowed to call /town/admin/* (#430).
    # When set, only these users pass; when empty, any *authenticated* Clerk
    # user does. Either way the endpoints are no longer anonymous, and the
    # dev-only environment gate still applies on top.
    ADMIN_USER_IDS: str = os.getenv("ADMIN_USER_IDS", "")
    # Shared secret for the sprite worker -> backend /town/internal/sprite-event
    # seam. CDK wires this into BOTH the backend (compute-stack) and the sprite
    # worker (sprite-worker-stack), so the gate is real.
    SPRITE_EVENT_SECRET: str = os.getenv("SPRITE_EVENT_SECRET", "")
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

    # Phase 2: persistent resident memory + reflection. Default false; prod stays
    # off until a dev soak validates Nova-Lite reflection quality.
    RESIDENT_MEMORY_ENABLED: bool = os.environ.get("RESIDENT_MEMORY_ENABLED", "false").lower() == "true"
    RESIDENT_MEMORIES_TABLE: str = os.environ.get("RESIDENT_MEMORIES_TABLE", "")

    # Phase 3: town-level culture. Both default false; prod stays off until a dev soak.
    RESIDENT_SOCIAL_GOALS_ENABLED: bool = os.environ.get("RESIDENT_SOCIAL_GOALS_ENABLED", "false").lower() == "true"
    TOWN_LORE_ENABLED: bool = os.environ.get("TOWN_LORE_ENABLED", "false").lower() == "true"

    def validate(self) -> None:
        """Validate required env vars. Raises RuntimeError in non-dev environments if critical vars are missing."""
        if self.ENVIRONMENT == "dev":
            return

        # WS_LAMBDA_SECRET IS required outside dev as of #427. It is now wired
        # end-to-end through CDK (see the field comment above), so a missing
        # value means the ws-message / ws-disconnect seam would be refusing
        # every call from the API Gateway Lambdas — fail fast at startup with a
        # clear message instead of serving a backend that silently drops all
        # agent WebSocket traffic.
        required = {
            "TOWN_TOKEN_SECRET": self.TOWN_TOKEN_SECRET,
            "WS_MANAGEMENT_API_URL": self.WS_MANAGEMENT_API_URL,
            "SPRITE_S3_BUCKET": self.SPRITE_S3_BUCKET,
            "SPRITE_CDN_URL": self.SPRITE_CDN_URL,
            "CLERK_ISSUER": self.CLERK_ISSUER,
            "WS_LAMBDA_SECRET": self.WS_LAMBDA_SECRET,
        }
        missing = [name for name, val in required.items() if not val]
        if missing:
            raise RuntimeError(f"Missing required environment variables for {self.ENVIRONMENT}: {', '.join(missing)}")
        logger.info("Configuration validated for environment: %s", self.ENVIRONMENT)


settings = Settings()
