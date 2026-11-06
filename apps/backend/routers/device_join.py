"""Short browser-code handoff to the existing owner enrollment authority.

Device secrets never enter browser URLs or chat. Connections storage supplies
conditional transitions shared across replicas; no additional identity registry.
"""

import hashlib
import secrets
import time

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field

from core import dynamo
from core.auth import AuthContext, get_current_user
from core.config import settings
from routers.runtime_binding import StrictRequest, _enabled

router = APIRouter(dependencies=[Depends(_enabled)])
TTL = 600


class Start(StrictRequest):
    agent_name: str = Field(default="visitor", pattern=r"^[a-zA-Z0-9_-]{1,50}$")
    appearance: str = Field(default="A friendly goose with a blue scarf", min_length=1, max_length=500)
    personality: str = Field(default="Curious and kind", max_length=500)


class Poll(StrictRequest):
    device_secret: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")


def _key(code):
    return {"connectionId": "town-device:" + code}


def _read(code):
    row = dynamo.connections_table().get_item(Key=_key(code), ConsistentRead=True).get("Item")
    if not row or int(row.get("expiresAt", 0)) <= int(time.time()):
        raise HTTPException(404, "This code expired. Ask your agent to join again.")
    return row


def _update(code, **kwargs):
    try:
        return dynamo.connections_table().update_item(Key=_key(code), **kwargs)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise HTTPException(409, "This request changed. Ask your agent to join again.") from exc
        raise


@router.post("/device-join")
async def start(body: Start):
    # 40 bits of public-code entropy; the separate 256-bit secret is required
    # for redemption. A browser code authorizes nothing by itself.
    code = secrets.token_hex(5).upper()
    code = code[:5] + "-" + code[5:]
    secret = secrets.token_urlsafe(32)
    expiry = int(time.time()) + TTL
    dynamo.connections_table().put_item(
        Item={
            **_key(code),
            "state": "pending",
            "secretHash": hashlib.sha256(secret.encode()).hexdigest(),
            "expiresAt": expiry,
            "ttl": expiry,
            "persona": body.model_dump(),
        },
        ConditionExpression="attribute_not_exists(connectionId)",
    )
    return {
        "user_code": code,
        "device_secret": secret,
        "expires_at": expiry,
        "interval": 3,
        "verification_uri": settings.TOWN_BROWSER_ORIGIN + "/connect?code=" + code,
    }


@router.get("/device-join/{code}")
async def preview(code: str, auth: AuthContext = Depends(get_current_user)):
    row = _read(code)
    if row.get("ownerId") not in (None, auth.user_id):
        raise HTTPException(404, "Request unavailable")
    result = {"state": row["state"], "persona": row["persona"], "expires_at": int(row["expiresAt"])}
    if row["state"] == "consumed":
        from core.services.town_service import TownService

        actor = TownService().get_agent_by_name(auth.user_id, row["persona"]["agent_name"])
        if actor and actor.get("userId") == auth.user_id and actor.get("runtimeBinding"):
            result["actor_id"] = actor["id"]
    return result


@router.post("/device-join/{code}/approve")
async def approve(code: str, auth: AuthContext = Depends(get_current_user)):
    # Claim BEFORE minting. Concurrent approvals cannot rotate the grant or
    # substitute another owner. Failure is terminal and the device starts fresh.
    row = _read(code)
    now = int(time.time())
    _update(
        code,
        UpdateExpression="SET #s = :claim, ownerId = :owner",
        ConditionExpression="#s = :pending AND expiresAt > :now",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={":claim": "authorizing", ":pending": "pending", ":owner": auth.user_id, ":now": now},
    )
    from routers.town import get_or_create_instance

    result = await get_or_create_instance(auth, enrollment_agent_name=row["persona"]["agent_name"])
    credential = result.get("join_token")
    if not credential:
        raise HTTPException(503, "Town enrollment is unavailable")
    _update(
        code,
        UpdateExpression="SET #s = :approved, credential = :credential",
        ConditionExpression="#s = :claim AND ownerId = :owner AND expiresAt > :now",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={
            ":claim": "authorizing",
            ":approved": "approved",
            ":credential": credential,
            ":owner": auth.user_id,
            ":now": int(time.time()),
        },
    )
    return {"state": "approved"}


@router.post("/device-join/{code}/cancel")
async def cancel(code: str, auth: AuthContext = Depends(get_current_user)):
    row = _read(code)
    if row.get("ownerId") not in (None, auth.user_id):
        raise HTTPException(404, "Request unavailable")
    _update(
        code,
        UpdateExpression="SET #s = :cancelled REMOVE credential",
        ConditionExpression="(#s = :pending OR ownerId = :owner) AND #s <> :consumed",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={
            ":cancelled": "cancelled",
            ":pending": "pending",
            ":owner": auth.user_id,
            ":consumed": "consumed",
        },
    )
    return {"state": "cancelled"}


@router.post("/device-join/{code}/poll")
async def poll(code: str, body: Poll):
    row = _read(code)
    digest = hashlib.sha256(body.device_secret.encode()).hexdigest()
    if not secrets.compare_digest(row["secretHash"], digest):
        raise HTTPException(404, "Request unavailable")
    if row["state"] != "approved":
        return {"state": row["state"]}
    # Single delivery even under concurrent polling; the stored credential is
    # removed in the same operation. Existing runtime proof consumes its grant.
    _update(
        code,
        UpdateExpression="SET #s = :consumed REMOVE credential",
        ConditionExpression="#s = :approved AND secretHash = :hash AND expiresAt > :now",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={
            ":approved": "approved",
            ":consumed": "consumed",
            ":hash": digest,
            ":now": int(time.time()),
        },
    )
    return {"state": "approved", "join_token": row["credential"]}
