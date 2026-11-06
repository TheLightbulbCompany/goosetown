"""Atomic daily request ceilings, not a dollar-spend guarantee."""

from datetime import UTC, datetime

from botocore.exceptions import ClientError

from core.config import settings
from core.dynamo import users_table


class PixelLabQuotaDenied(Exception):
    """Generation stays queued; no provider request may be sent."""


def reserve_request(owner_id: str | None) -> None:
    """Count every billed HTTP attempt, including ambiguous retries.

    Both owner and global reservations commit together. Do not refund failed
    calls: a timeout does not prove the provider did not charge for the call.
    Missing owner/configuration or an unavailable store fails closed.
    """
    if not owner_id or not isinstance(owner_id, str):
        raise PixelLabQuotaDenied("Missing avatar owner")
    global_limit = settings.PIXELLAB_REQUESTS_PER_DAY
    owner_limit = settings.PIXELLAB_REQUESTS_PER_OWNER_PER_DAY
    if global_limit <= 0 or owner_limit <= 0:
        raise PixelLabQuotaDenied("Avatar generation awaits configured daily limits")
    day = datetime.now(UTC).date().isoformat()
    try:
        table = users_table()
    except Exception as exc:
        raise PixelLabQuotaDenied("Avatar quota store unavailable") from exc
    updates = []
    for scope, limit in (("global", global_limit), (f"owner:{owner_id}", owner_limit)):
        updates.append(
            {
                "Update": {
                    "TableName": table.name,
                    "Key": {"userId": f"__pixellab_quota__:{day}:{scope}"},
                    "UpdateExpression": "ADD #used :one",
                    "ConditionExpression": "attribute_not_exists(#used) OR #used < :limit",
                    "ExpressionAttributeNames": {"#used": "requests"},
                    "ExpressionAttributeValues": {":one": 1, ":limit": limit},
                }
            }
        )
    try:
        table.meta.client.transact_write_items(TransactItems=updates)
    except ClientError as exc:
        raise PixelLabQuotaDenied("Avatar request limit or quota store unavailable") from exc
    except Exception as exc:
        raise PixelLabQuotaDenied("Avatar quota store unavailable") from exc
