"""Issue a single-use initial runtime enrollment credential during owner onboarding.

The consuming transaction must enforce owner/instance, expiry, and initial binding
(revision zero, no existing binding); this helper only issues the capability.
"""
import base64
import hashlib
import json
import secrets
import time

from botocore.exceptions import ClientError

from core import dynamo
from core.runtime_origin import audience

TTL_SECONDS = 600


class EnrollmentStateChanged(Exception):
    """Instance credentials changed during onboarding."""


def mint_join_token(instance_id: str, owner_id: str, town_token: str) -> str:
    secret = secrets.token_urlsafe(32)
    grant = {
        "secretHash": hashlib.sha256(secret.encode("ascii")).hexdigest(),
        "ownerId": owner_id,
        "expiresAt": int(time.time()) + TTL_SECONDS,
        "audience": audience(),
    }
    try:
        dynamo.instances_table().update_item(
            Key={"id": instance_id},
            UpdateExpression="SET runtimeEnrollment = :grant",
            ConditionExpression="userId = :owner AND isActive = :active AND townToken = :token",
            ExpressionAttributeValues={":grant": grant, ":owner": owner_id, ":active": "true", ":token": town_token},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise EnrollmentStateChanged from exc
        raise
    payload = json.dumps({"town_token": town_token, "enrollment_token": secret}, separators=(",", ":"))
    return "gtj_" + base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
