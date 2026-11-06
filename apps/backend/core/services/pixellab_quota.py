"""Environment-wide daily request ceiling, not a dollar-spend guarantee."""

from datetime import UTC, datetime

from core.config import settings
from core.dynamo import users_table


class PixelLabQuotaDenied(Exception):
    """No provider request may be sent; signup work remains queued."""


def reserve_request() -> None:
    """Count every billed HTTP attempt, including ambiguous retries.

    Per-user fairness is enforced at registration. This single conditional
    update bounds all generation paths in the configured town environment.
    Never refund failed calls: a timeout does not prove there was no charge.
    """
    limit = settings.PIXELLAB_REQUESTS_PER_DAY
    if limit <= 0:
        raise PixelLabQuotaDenied("Avatar generation awaits a configured daily limit")
    day = datetime.now(UTC).date().isoformat()
    try:
        users_table().update_item(
            Key={"userId": f"__pixellab_quota__:{day}"},
            UpdateExpression="ADD #used :one",
            ConditionExpression="attribute_not_exists(#used) OR #used < :limit",
            ExpressionAttributeNames={"#used": "requests"},
            ExpressionAttributeValues={":one": 1, ":limit": limit},
        )
    except Exception as exc:
        raise PixelLabQuotaDenied("Avatar request limit or quota store unavailable") from exc
