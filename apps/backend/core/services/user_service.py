"""Per-user state: sprite-gen sliding window + token revocation timestamp.

Quotas are global constants in code (no per-user override per spec).
The user record lazy-creates on first read; quota checks read-modify-write
the sprite-jobs window.
"""

from __future__ import annotations

import time
from typing import Any

from botocore.exceptions import ClientError

from core.dynamo import users_table
from core.quota import QuotaExceeded

AGENT_QUOTA: int = 5
SPRITE_QUOTA_DAILY: int = 10
SPRITE_QUOTA_WINDOW_MS: int = 24 * 60 * 60 * 1000


class UserService:
    """CRUD + quota enforcement for the goosetown-{env}-users table.

    Lazy-init: get_or_create inserts a default record on first touch.
    """

    def get_or_create(self, user_id: str) -> dict[str, Any]:
        table = users_table()
        existing = table.get_item(Key={"userId": user_id}).get("Item")
        if existing:
            return existing

        now_ms = int(time.time() * 1000)
        record: dict[str, Any] = {
            "userId": user_id,
            "createdAt": now_ms,
            "sprite_jobs_24h": [],
        }
        # ConditionExpression so a concurrent first-touch from another request
        # doesn't double-create. If we lose the race, re-fetch the winner.
        try:
            table.put_item(
                Item=record,
                ConditionExpression="attribute_not_exists(userId)",
            )
            return record
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            # Another request created it concurrently; re-fetch the winner.
            return table.get_item(Key={"userId": user_id})["Item"]

    def revoke_tokens(self, user_id: str) -> None:
        self.get_or_create(user_id)
        now_ms = int(time.time() * 1000)
        users_table().update_item(
            Key={"userId": user_id},
            UpdateExpression="SET tokenRevokedAt = :now",
            ExpressionAttributeValues={":now": now_ms},
        )

    def is_token_revoked(self, user_id: str, token_iat_ms: int) -> bool:
        user = self.get_or_create(user_id)
        revoked_at = user.get("tokenRevokedAt")
        if revoked_at is None:
            return False
        return token_iat_ms < int(revoked_at)

    def consume_sprite_quota(self, user_id: str) -> None:
        user = self.get_or_create(user_id)
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - SPRITE_QUOTA_WINDOW_MS

        existing_jobs = user.get("sprite_jobs_24h", []) or []
        recent = [j for j in existing_jobs if int(j.get("timestamp_ms", 0)) >= cutoff]

        if len(recent) >= SPRITE_QUOTA_DAILY:
            raise QuotaExceeded(
                kind="sprite_generation",
                limit=SPRITE_QUOTA_DAILY,
                current=len(recent),
            )

        recent.append({"timestamp_ms": now_ms})
        users_table().update_item(
            Key={"userId": user_id},
            UpdateExpression="SET sprite_jobs_24h = :jobs",
            ExpressionAttributeValues={":jobs": recent},
        )

    def check_agent_quota(self, user_id: str, current_count: int) -> None:
        if current_count >= AGENT_QUOTA:
            raise QuotaExceeded(
                kind="agents",
                limit=AGENT_QUOTA,
                current=current_count,
            )
