"""Owner-authorized, one-use proof of control of a Lightbulb runtime key.

The account holder performs all HTTP calls. No signing secret goes to GooseTown.
This establishes a binding only; it does not grant messaging or job authority.
"""

import base64
import json
import re
import secrets
import time

from botocore.exceptions import ClientError
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from core import dynamo
from core.auth import AuthContext, get_current_user
from core.config import settings
from core.runtime_origin import audience
from core.services.town_service import TownService

router = APIRouter()
AUDIENCE = "goosetown-runtime-binding-v1:https://api.goosetown.thelightbulb.company"
DOMAIN = b"GooseTown runtime binding v1\n"
TTL = 120
KEY = re.compile(r"^[a-z2-7]{52}$")
SESSION = re.compile(r"^(?:(?:claude|codex|cursor|grok|opencode):[A-Za-z0-9._-]{1,128}|[A-Za-z0-9._-]{1,64})$")


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ChallengeRequest(StrictRequest):
    member_key: str = Field(min_length=52, max_length=52)
    session_id: str = Field(min_length=14, max_length=150)
    audience: str = Field(max_length=512)
    expected_revision: StrictInt = Field(ge=0)


class CompletionRequest(StrictRequest):
    challenge_id: str = Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")
    signature: str = Field(min_length=86, max_length=86, pattern=r"^[A-Za-z0-9_-]+$")


class UnlinkRequest(StrictRequest):
    expected_revision: StrictInt = Field(ge=0)


def _now():
    return int(time.time())


def _enabled():
    if not settings.RUNTIME_BINDING_ENABLED:
        raise HTTPException(404, "not found")


def _owned(actor_id, owner, *, require_active=True):
    actor = TownService().get_town_agent_by_id(actor_id, consistent=True)
    if not actor or actor.get("userId") != owner or (require_active and actor.get("isActive") != "true"):
        raise HTTPException(404, "agent not found")
    return actor


def _key(value):
    if not KEY.fullmatch(value):
        raise HTTPException(422, "invalid member key")
    try:
        raw = base64.b32decode(value.upper() + "====", casefold=False)
    except ValueError:
        raise HTTPException(422, "invalid member key")
    if len(raw) != 32 or base64.b32encode(raw).decode().lower().rstrip("=") != value:
        raise HTTPException(422, "invalid member key")
    return raw


def _bytes(payload):
    return DOMAIN + json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _revision_condition(rev):
    return "(attribute_not_exists(bindingRevision) OR bindingRevision = :rev)" if rev == 0 else "bindingRevision = :rev"


def _update(actor_id, **kwargs):
    try:
        return dynamo.agents_table().update_item(Key={"id": actor_id}, **kwargs)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise HTTPException(409, "binding state changed") from exc
        raise


@router.get("/agents/{actor_id}/runtime-binding", dependencies=[Depends(_enabled)])
async def get_binding(actor_id: str, auth: AuthContext = Depends(get_current_user)):
    # Read remains available while the actor sleeps; mutating a binding requires active status.
    actor = _owned(actor_id, auth.user_id, require_active=False)
    binding = actor.get("runtimeBinding")
    if binding is not None:
        binding = {key: binding[key] for key in ("memberKey", "sessionId", "audience", "revision", "boundAt")}
    return {"actor_id": actor_id, "revision": int(actor.get("bindingRevision", 0)), "binding": binding}


@router.post("/agents/{actor_id}/runtime-binding/challenge", dependencies=[Depends(_enabled)])
async def challenge(actor_id: str, body: ChallengeRequest, auth: AuthContext = Depends(get_current_user)):
    actor = _owned(actor_id, auth.user_id)
    _key(body.member_key)
    prefix, sep, session = body.session_id.partition("/")
    if not sep or prefix != body.member_key[:12] or not SESSION.fullmatch(session):
        raise HTTPException(422, "invalid session for member key")
    if body.audience != audience():
        raise HTTPException(422, "invalid audience")
    if int(actor.get("bindingRevision", 0)) != body.expected_revision:
        raise HTTPException(409, "binding state changed")
    now = _now()
    payload = {
        "version": 1,
        "actor_id": actor_id,
        "owner_id": auth.user_id,
        "member_key": body.member_key,
        "session_id": body.session_id,
        "audience": audience(),
        "revision": body.expected_revision,
        "challenge_id": secrets.token_urlsafe(32),
        "issued_at": now,
        "expires_at": now + TTL,
    }
    _update(
        actor_id,
        UpdateExpression="SET bindingChallenge = :challenge",
        ConditionExpression=(
            f"userId = :owner AND isActive = :active AND ({_revision_condition(body.expected_revision)})"
        ),
        ExpressionAttributeValues={
            ":challenge": payload,
            ":owner": auth.user_id,
            ":active": "true",
            ":rev": body.expected_revision,
        },
    )
    return {
        "challenge_id": payload["challenge_id"],
        "expires_at": payload["expires_at"],
        "signing_bytes_b64url": base64.urlsafe_b64encode(_bytes(payload)).decode().rstrip("="),
    }


@router.post("/agents/{actor_id}/runtime-binding/complete", dependencies=[Depends(_enabled)])
async def complete(actor_id: str, body: CompletionRequest, auth: AuthContext = Depends(get_current_user)):
    return _complete_for_owner(actor_id, body, auth.user_id)


def _complete_for_owner(actor_id, body, owner_id, *, join_request=None, commit=None):
    actor = _owned(actor_id, owner_id)
    payload = actor.get("bindingChallenge")
    now = _now()
    if not isinstance(payload, dict) or payload.get("challenge_id") != body.challenge_id:
        raise HTTPException(409, "challenge unavailable")
    if now >= int(payload["expires_at"]):
        raise HTTPException(409, "challenge expired")
    if payload["actor_id"] != actor_id or payload["owner_id"] != owner_id or payload["audience"] != audience():
        raise HTTPException(409, "challenge invalid")
    if join_request is not None and any(
        payload[field] != join_request[stored]
        for field, stored in (("member_key", "memberKey"), ("session_id", "sessionId"), ("revision", "revision"))
    ):
        raise HTTPException(409, "challenge invalid")
    try:
        sig = base64.b64decode(body.signature + "=" * (-len(body.signature) % 4), altchars=b"-_", validate=True)
        if len(sig) != 64 or base64.urlsafe_b64encode(sig).decode().rstrip("=") != body.signature:
            raise ValueError("invalid signature encoding")
        Ed25519PublicKey.from_public_bytes(_key(payload["member_key"])).verify(sig, _bytes(payload))
    except (ValueError, InvalidSignature):
        raise HTTPException(403, "invalid proof")
    now = _now()
    if now >= int(payload["expires_at"]):
        raise HTTPException(409, "challenge expired")
    rev = int(payload["revision"])
    binding = {
        "memberKey": payload["member_key"],
        "sessionId": payload["session_id"],
        "audience": audience(),
        "revision": rev + 1,
        "boundAt": now,
    }
    condition = (
        f"userId = :owner AND isActive = :active AND ({_revision_condition(rev)}) "
        "AND bindingChallenge.challenge_id = :cid AND bindingChallenge.expires_at > :now"
    )
    values = {
        ":binding": binding, ":next": rev + 1, ":owner": owner_id,
        ":active": "true", ":rev": rev, ":cid": body.challenge_id, ":now": now,
    }
    if join_request is not None:
        # Approval and challenge were stored together. Consume that exact grant,
        # never infer account approval from possession of the town/poll token.
        condition += (
            " AND runtimeJoin.requestId = :rid AND runtimeJoin.secretHash = :hash"
            " AND runtimeJoin.challengeId = :cid AND runtimeJoin.ownerId = :owner"
            " AND runtimeJoin.expiresAt > :now"
        )
        values.update({":rid": join_request["requestId"], ":hash": join_request["secretHash"]})
    update = dict(
        UpdateExpression="SET runtimeBinding = :binding, bindingRevision = :next REMOVE bindingChallenge, runtimeJoin",
        ConditionExpression=condition,
        ExpressionAttributeValues=values,
    )
    if commit is None:
        _update(actor_id, **update)
    else:
        commit(update, now)
    return {"actor_id": actor_id, "binding": binding}


@router.delete("/agents/{actor_id}/runtime-binding", dependencies=[Depends(_enabled)])
async def unlink(actor_id: str, body: UnlinkRequest, auth: AuthContext = Depends(get_current_user)):
    _owned(actor_id, auth.user_id)
    rev = body.expected_revision
    _update(
        actor_id,
        UpdateExpression="SET bindingRevision = :next REMOVE runtimeBinding, bindingChallenge, runtimeJoin",
        ConditionExpression=f"userId = :owner AND isActive = :active AND ({_revision_condition(rev)})",
        ExpressionAttributeValues={":next": rev + 1, ":owner": auth.user_id, ":active": "true", ":rev": rev},
    )
    return {"actor_id": actor_id, "revision": rev + 1}
