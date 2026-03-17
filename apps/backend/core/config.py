"""GooseTown backend configuration."""

import os


class Settings:
    AWS_REGION: str = os.getenv("AWS_REGION", "us-east-1")
    DYNAMODB_TABLE_PREFIX: str = os.getenv("DYNAMODB_TABLE_PREFIX", "goosetown")
    CLERK_ISSUER: str = os.getenv("CLERK_ISSUER", "https://clerk.isol8.co")
    TOWN_TOKEN_SECRET: str = os.getenv("TOWN_TOKEN_SECRET", "")
    CORS_ORIGINS: str = os.getenv("CORS_ORIGINS", "http://localhost:5173")
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "dev")
    PIXELLAB_API_KEY: str = os.getenv("PIXELLAB_API_KEY", "")
    SPRITE_S3_BUCKET: str = os.getenv("SPRITE_S3_BUCKET", "")
    SPRITE_CDN_URL: str = os.getenv("SPRITE_CDN_URL", "")


settings = Settings()
