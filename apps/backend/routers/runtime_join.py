"""Automatic first-runtime enrollment using an owner-issued onboarding grant.

No account credential goes to the runtime. The existing owner onboarding action
issues a short-lived capability; possession plus a member-key proof consumes it.
This records binding state only, not permission to execute jobs or town actions.
"""

import base64
import hashlib
import secrets

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import Field

from core import dynamo
from core.config import settings
from core.services.town_service import TownService
from routers import runtime_binding as binding
from routers.runtime_binding import CompletionRequest, StrictRequest

router = APIRouter(dependencies=[Depends(binding._enabled)])


async def _town_auth(authorization: str | None = Header(default=None, alias="Authorization")):
    # Resolve lazily to keep the existing town router import acyclic.
    from routers.town import get_town_token_user
    return await get_town_token_user(authorization)


class StartRequest(StrictRequest):
    agent_name: str = Field(min_length=1, max_length=50, pattern=r"^[a-zA-Z0-9_-]+$")
    member_key: str = Field(min_length=52, max_length=52)
    session_id: str = Field(min_length=14, max_length=150)
    enrollment_token: str | None = Field(default=None, min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")


class PollRequest(StrictRequest):
    poll_secret: str = Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")


class FinishRequest(CompletionRequest):
    poll_secret: str = Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")


def _digest(secret):
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


def _principal(token_info):
    """The new authority path fails closed even if legacy bearer auth does not."""
    from core.town_token import verify_town_token

    owner, token = token_info
    claims = verify_town_token(token)
    now = binding._now()
    if not claims or claims.get("uid") != owner:
        raise HTTPException(401, "invalid onboarding credential")
    try:
        issued = claims["iat"]
        expiry = claims["exp"]
        if type(issued) is not int or type(expiry) is not int or issued > now or expiry <= now:
            raise HTTPException(401, "invalid onboarding credential")
        user = dynamo.users_table().get_item(Key={"userId": owner}, ConsistentRead=True).get("Item", {})
        if issued * 1000 < int(user.get("tokenRevokedAt", 0)):
            raise HTTPException(401, "invalid onboarding credential")
    except HTTPException:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(401, "invalid onboarding credential") from exc
    except Exception as exc:
        raise HTTPException(503, "onboarding authorization unavailable") from exc
    instance = dynamo.instances_table().get_item(Key={"id": claims["iid"]}, ConsistentRead=True).get("Item")
    if not instance or instance.get("userId") != owner or instance.get("isActive") != "true":
        raise HTTPException(404, "onboarding unavailable")
    return owner, instance, issued


def _challenge_response(payload):
    return {
        "challenge_id": payload["challenge_id"],
        "expires_at": int(payload["expires_at"]),
        "signing_bytes_b64url": base64.urlsafe_b64encode(binding._bytes(payload)).decode().rstrip("="),
    }


def _request(actor_id, request_id, secret, token_info):
    owner, instance, issued = _principal(token_info)
    actor = binding._owned(actor_id, owner)
    pending = actor.get("runtimeJoin", {})
    now = binding._now()
    if (
        actor.get("instanceId") != instance["id"]
        or pending.get("requestId") != request_id
        or pending.get("ownerId") != owner
        or not secrets.compare_digest(str(pending.get("secretHash", "")), _digest(secret))
        or int(pending.get("expiresAt", 0)) <= now
    ):
        raise HTTPException(404, "onboarding unavailable")
    grant = instance.get("runtimeEnrollment", {})
    if (
        grant.get("ownerId") != owner
        or grant.get("audience") != binding.audience()
        or not secrets.compare_digest(str(grant.get("secretHash", "")), str(pending.get("enrollmentHash", "")))
        or int(grant.get("expiresAt", 0)) <= now
    ):
        raise HTTPException(409, "onboarding authorization changed")
    challenge = actor.get("bindingChallenge", {})
    if challenge.get("challenge_id") != pending.get("challengeId"):
        raise HTTPException(409, "onboarding authorization changed")
    return owner, instance, pending, challenge, issued


@router.post("/runtime-join")
async def start(body: StartRequest, token_info: tuple = Depends(_town_auth)):
    owner, instance, _ = _principal(token_info)
    service = TownService()
    found = service.get_agent_by_name(owner, body.agent_name)
    if not found:
        raise HTTPException(404, "agent not found")
    actor = binding._owned(found["id"], owner)
    if actor.get("instanceId") not in (None, instance["id"]):
        raise HTTPException(404, "agent not found")
    binding._key(body.member_key)
    prefix, sep, session = body.session_id.partition("/")
    if not sep or prefix != body.member_key[:12] or not binding.SESSION.fullmatch(session):
        raise HTTPException(422, "invalid runtime session")
    active = actor.get("runtimeBinding")
    if active:
        if active.get("memberKey") == body.member_key and active.get("sessionId") == body.session_id:
            # Existing state only: no new credentials, grants or liveness claim.
            return {
                "state": "ready", "actor_id": actor["id"], "revision": int(actor["bindingRevision"]),
                "runtime_session_required": settings.RUNTIME_SESSION_ENFORCEMENT_ENABLED,
            }
        raise HTTPException(409, "another runtime is connected")
    if int(actor.get("bindingRevision", 0)) != 0:
        raise HTTPException(409, "owner-authorized runtime recovery required")
    now = binding._now()
    grant = instance.get("runtimeEnrollment", {})
    if (
        body.enrollment_token is None
        or grant.get("ownerId") != owner
        or grant.get("audience") != binding.audience()
        or int(grant.get("expiresAt", 0)) <= now
        or not secrets.compare_digest(str(grant.get("secretHash", "")), _digest(body.enrollment_token))
    ):
        raise HTTPException(403, "use current owner-authorized onboarding")
    request_id, poll_secret = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    expires = min(now + binding.TTL, int(grant["expiresAt"]))
    payload = {
        "version": 1, "actor_id": actor["id"], "owner_id": owner,
        "member_key": body.member_key, "session_id": body.session_id,
        "audience": binding.audience(), "revision": 0,
        "challenge_id": secrets.token_urlsafe(32), "issued_at": now, "expires_at": expires,
    }
    pending = {
        "requestId": request_id, "secretHash": _digest(poll_secret),
        "enrollmentHash": grant["secretHash"], "instanceId": instance["id"],
        "ownerId": owner, "expiresAt": expires, "memberKey": body.member_key,
        "sessionId": body.session_id, "revision": 0, "challengeId": payload["challenge_id"],
    }
    binding._update(
        actor["id"],
        UpdateExpression="SET runtimeJoin = :pending, bindingChallenge = :challenge, instanceId = :iid",
        ConditionExpression=(
            "userId = :owner AND isActive = :active AND (attribute_not_exists(instanceId) OR instanceId = :iid) "
            "AND (attribute_not_exists(bindingRevision) OR bindingRevision = :zero) "
            "AND attribute_not_exists(runtimeBinding) "
            "AND (attribute_not_exists(runtimeJoin) OR runtimeJoin.expiresAt <= :now "
            "OR (runtimeJoin.memberKey = :key AND runtimeJoin.sessionId = :session "
            "AND runtimeJoin.enrollmentHash = :grant))"
        ),
        ExpressionAttributeValues={
            ":pending": pending, ":challenge": payload, ":owner": owner,
            ":active": "true", ":iid": instance["id"], ":zero": 0, ":now": now,
            ":key": body.member_key, ":session": body.session_id, ":grant": grant["secretHash"],
        },
    )
    return {
        "state": "approved", "actor_id": actor["id"], "request_id": request_id,
        "poll_secret": poll_secret, "expires_at": expires, "challenge": _challenge_response(payload),
    }


@router.post("/agents/{actor_id}/runtime-join/{request_id}/poll")
async def poll(actor_id: str, request_id: str, body: PollRequest, token_info: tuple = Depends(_town_auth)):
    _, _, _, payload, _ = _request(actor_id, request_id, body.poll_secret, token_info)
    return {"state": "approved", "expires_at": int(payload["expires_at"]), "challenge": _challenge_response(payload)}


@router.post("/agents/{actor_id}/runtime-join/{request_id}/complete")
async def finish(actor_id: str, request_id: str, body: FinishRequest, token_info: tuple = Depends(_town_auth)):
    owner, instance, pending, _, issued = _request(actor_id, request_id, body.poll_secret, token_info)

    def commit(update, now):
        # Binding and grant consumption are one cross-item transaction. A grant
        # cannot bind two actors, and a failed proof never consumes the grant.
        update["ConditionExpression"] += " AND instanceId = :iid AND attribute_not_exists(runtimeBinding)"
        update["ExpressionAttributeValues"][":iid"] = instance["id"]
        try:
            dynamo.get_client().meta.client.transact_write_items(TransactItems=[
                {"Update": {"TableName": dynamo.agents_table().name, "Key": {"id": actor_id}, **update}},
                {"Update": {
                    "TableName": dynamo.instances_table().name, "Key": {"id": instance["id"]},
                    "UpdateExpression": "REMOVE runtimeEnrollment",
                    "ConditionExpression": (
                        "userId = :owner AND isActive = :active AND runtimeEnrollment.ownerId = :owner "
                        "AND runtimeEnrollment.secretHash = :grant AND runtimeEnrollment.expiresAt > :now "
                        "AND runtimeEnrollment.audience = :audience"
                    ),
                    "ExpressionAttributeValues": {
                        ":owner": owner, ":active": "true", ":grant": pending["enrollmentHash"], ":now": now,
                        ":audience": binding.audience(),
                    },
                }},
                {"ConditionCheck": {
                    "TableName": dynamo.users_table().name, "Key": {"userId": owner},
                    "ConditionExpression": "attribute_not_exists(tokenRevokedAt) OR tokenRevokedAt <= :issued",
                    "ExpressionAttributeValues": {":issued": issued * 1000},
                }},
            ])
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "TransactionCanceledException":
                reasons = exc.response.get("CancellationReasons", [])
                if reasons and all(r.get("Code") in ("None", "ConditionalCheckFailed") for r in reasons):
                    raise HTTPException(409, "onboarding authorization changed") from exc
            raise

    result = binding._complete_for_owner(
        actor_id, body, owner, join_request=pending, commit=commit,
    )
    return {
        "state": "ready", "actor_id": actor_id, "revision": result["binding"]["revision"],
        "runtime_session_required": settings.RUNTIME_SESSION_ENFORCEMENT_ENABLED,
    }
