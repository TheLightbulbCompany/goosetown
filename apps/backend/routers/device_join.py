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
from routers.runtime_binding import StrictRequest
from routers.runtime_binding import _enabled as binding_enabled


def _enabled():
    binding_enabled()
    if not settings.RUNTIME_SESSION_ENFORCEMENT_ENABLED:
        raise HTTPException(404, "not found")


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


def _existing_actor(row, owner):
    from core.services.town_service import TownService

    found = TownService().get_agent_by_name(owner, row["persona"]["agent_name"])
    if not found:
        return None
    actor = TownService().get_town_agent_by_id(found["id"], consistent=True)
    return actor if actor and actor.get("userId") == owner and actor.get("isActive") == "true" else None


def _public_persona(actor):
    return {
        "agent_name": actor["agentName"],
        "display_name": actor.get("displayName", actor["agentName"]),
        "appearance": actor.get("appearance") or "Existing town character",
        "personality": actor.get("personalitySummary") or "",
    }


@router.get("/device-join/{code}")
async def preview(code: str, auth: AuthContext = Depends(get_current_user)):
    row = _read(code)
    if row.get("ownerId") not in (None, auth.user_id):
        raise HTTPException(404, "Request unavailable")
    actor = _existing_actor(row, auth.user_id)
    persona = _public_persona(actor) if actor else row["persona"]
    result = {"state": row["state"], "persona": persona, "expires_at": int(row["expiresAt"])}
    arrival = row.get("arrival") or {}
    if actor and arrival and row["state"] == "consumed":
        active = actor.get("runtimeBinding") or {}
        # A previous same-name binding is not evidence for this request. Only
        # this device's proven runtime connection may complete its arrival.
        if (
            arrival.get("actorId") == actor["id"]
            and active.get("revision") == arrival.get("revision")
            and active.get("sessionId") == arrival.get("sessionId")
            and actor.get("daemonConnectionId") == arrival.get("connectionId")
        ):
            connection = (
                dynamo.connections_table()
                .get_item(Key={"connectionId": arrival["connectionId"]}, ConsistentRead=True)
                .get("Item")
            )
            if connection:
                result["actor_id"] = actor["id"]
    return result


@router.post("/device-join/{code}/approve")
async def approve(code: str, auth: AuthContext = Depends(get_current_user)):
    # Claim BEFORE minting. Concurrent approvals cannot rotate the grant or
    # substitute another owner. Failure is terminal and the device starts fresh.
    row = _read(code)
    actor = _existing_actor(row, auth.user_id)
    persona = _public_persona(actor) if actor else row["persona"]
    now = int(time.time())
    _update(
        code,
        UpdateExpression="SET #s = :claim, ownerId = :owner",
        ConditionExpression="#s = :pending AND expiresAt > :now",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={":claim": "authorizing", ":pending": "pending", ":owner": auth.user_id, ":now": now},
    )
    from core.runtime_enrollment import mint_join_token
    from core.services.town_service import TownService
    from core.town_token import sign_town_token, verify_town_token
    from routers.town import AgentRegisterRequest, get_or_create_instance, register_agent

    result = await get_or_create_instance(auth, enrollment_agent_name=persona["agent_name"])
    owner_token = result["town_token"]  # Remains on the server; never delivered to the device.
    instance_id = verify_town_token(owner_token)["iid"]
    if actor is None:
        registered = await register_agent(
            AgentRegisterRequest(
                agent_name=persona["agent_name"],
                display_name=persona["agent_name"],
                appearance=persona["appearance"],
                personality=persona["personality"],
            ),
            (auth.user_id, owner_token),
        )
        actor = TownService().get_town_agent_by_id(registered["agent_id"], consistent=True)
    if actor.get("instanceId") not in (None, instance_id):
        raise HTTPException(409, "Actor belongs to another town context")
    dynamo.agents_table().update_item(
        Key={"id": actor["id"]},
        UpdateExpression="SET instanceId = :iid",
        ConditionExpression=("userId = :owner AND isActive = :active "
                             "AND (attribute_not_exists(instanceId) OR instanceId = :iid)"),
        ExpressionAttributeValues={":iid": instance_id, ":owner": auth.user_id, ":active": "true"},
    )
    scoped = sign_town_token(auth.user_id, instance_id, actor_id=actor["id"], device_id=code)
    credential = mint_join_token(
        instance_id, auth.user_id, owner_token, agent_name=persona["agent_name"], runtime_token=scoped
    )
    _update(
        code,
        UpdateExpression="SET #s = :approved, credential = :credential, actorId = :actor, persona = :persona",
        ConditionExpression="#s = :claim AND ownerId = :owner AND expiresAt > :now",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={
            ":claim": "authorizing",
            ":approved": "approved",
            ":credential": credential,
            ":actor": actor["id"],
            ":persona": persona,
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


def record_arrival(town_claims, runtime_claims, connection_id):
    """Called only after a verified runtime session connects to the game server."""
    code = town_claims.get("device_id")
    if not code or not runtime_claims or runtime_claims.get("device_id") != code:
        return
    try:
        _update(
            code,
            UpdateExpression="SET arrival = :arrival",
            ConditionExpression="#s = :consumed AND actorId = :actor AND ownerId = :owner AND expiresAt > :now",
            ExpressionAttributeNames={"#s": "state"},
            ExpressionAttributeValues={
                ":consumed": "consumed",
                ":actor": runtime_claims["actor_id"],
                ":owner": town_claims["uid"],
                ":now": int(time.time()),
                ":arrival": {
                    "actorId": runtime_claims["actor_id"],
                    "revision": runtime_claims["revision"],
                    "sessionId": runtime_claims["session"],
                    "connectionId": connection_id,
                    "jti": runtime_claims["jti"],
                },
            },
        )
    except HTTPException:
        # An expired/cancelled request must not prevent a legitimate reconnect.
        pass
