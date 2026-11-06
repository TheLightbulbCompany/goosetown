"""Per-user state: quota counters + token revocation timestamp.

Quotas are global constants in code (no per-user override per spec).
The user record lazy-creates on first read. Both quotas are enforced by
conditional writes on this row, so concurrent requests can't overshoot:

* ``spriteDay`` / ``spriteCount`` — sprite generations in the current UTC day.
* ``activeAgents`` — active-agent count, changed only by
  ``TownService.set_agent_active`` and ``TownService.opt_in`` in the same
  transaction as the agent row's ``isActive`` flip.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from botocore.exceptions import ClientError

from core.dynamo import users_table
from core.quota import QuotaExceeded

AGENT_QUOTA: int = 5
SPRITE_QUOTA_DAILY: int = 10
SPRITE_QUOTA_WINDOW_MS: int = 24 * 60 * 60 * 1000  # legacy sprite_jobs_24h only


def utc_day(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def _is_conditional_fail(exc: ClientError) -> bool:
    return exc.response["Error"]["Code"] == "ConditionalCheckFailedException"


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
        """Atomically take one sprite generation from today's (UTC) allowance.

        Raises ``QuotaExceeded`` once ``SPRITE_QUOTA_DAILY`` are used today.
        """
        user = self.get_or_create(user_id)
        now = datetime.now(timezone.utc)
        today = utc_day(now)
        # Legacy rows kept a sliding list; count today's entries once when we
        # start the first counter for a day so the cutover can't reset usage.
        midnight_ms = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        legacy_today = sum(
            1 for j in (user.get("sprite_jobs_24h") or []) if int(j.get("timestamp_ms", 0)) >= midnight_ms
        )
        table = users_table()
        key = {"userId": user_id}
        for _ in range(2):
            try:  # same day: increment while under the cap
                table.update_item(
                    Key=key,
                    UpdateExpression="ADD spriteCount :one",
                    ConditionExpression="spriteDay = :today AND spriteCount < :limit",
                    ExpressionAttributeValues={":one": 1, ":today": today, ":limit": SPRITE_QUOTA_DAILY},
                )
                return
            except ClientError as exc:
                if not _is_conditional_fail(exc):
                    raise
            if legacy_today >= SPRITE_QUOTA_DAILY:
                break
            try:  # first generation of a new UTC day: restart the counter
                table.update_item(
                    Key=key,
                    UpdateExpression="SET spriteDay = :today, spriteCount = :n REMOVE sprite_jobs_24h",
                    ConditionExpression="attribute_not_exists(spriteDay) OR spriteDay <> :today",
                    ExpressionAttributeValues={":today": today, ":n": legacy_today + 1},
                )
                return
            except ClientError as exc:
                if not _is_conditional_fail(exc):
                    raise
            # A concurrent request started today's counter; retry the increment.
        raise QuotaExceeded(kind="sprite_generation", limit=SPRITE_QUOTA_DAILY, current=SPRITE_QUOTA_DAILY)

    def sprites_used_today(self, user_id: str) -> int:
        user = self.get_or_create(user_id)
        return int(user.get("spriteCount", 0)) if user.get("spriteDay") == utc_day() else 0
