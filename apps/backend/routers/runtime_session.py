"""Prove an existing runtime binding and issue a short-lived session bearer."""

import base64
import hashlib
import hmac
import json
import secrets

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field

from core import dynamo
from core.config import settings
from core.town_token import _get_town_token_secret
from routers import runtime_binding as binding
from routers.runtime_join import _principal, _town_auth

router = APIRouter()
PREFIX = "gtrs_"
DOMAIN = b"GooseTown runtime session credential v1\n"
TOKEN_TTL = 300


class Request(binding.StrictRequest):
    member_key: str = Field(min_length=52, max_length=52)
    session_id: str = Field(min_length=14, max_length=150)


def _enabled():
    if not settings.RUNTIME_SESSION_ENFORCEMENT_ENABLED:
        raise HTTPException(404, "not found")


def _encode(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _decode(data):
    raw = base64.b64decode(data + "=" * (-len(data) % 4), altchars=b"-_", validate=True)
    if _encode(raw) != data:
        raise ValueError("noncanonical")
    return raw


def _mac(data):
    return hmac.new(_get_town_token_secret().encode(), DOMAIN + data, hashlib.sha256).digest()


def _binding_matches(actor, owner, iid, key, session, rev):
    b = actor.get("runtimeBinding") or {}
    return (
        actor.get("userId") == owner
        and actor.get("isActive") == "true"
        and actor.get("instanceId") == iid
        and int(actor.get("bindingRevision", -1)) == rev
        and b.get("revision") == rev
        and b.get("memberKey") == key
        and b.get("sessionId") == session
        and b.get("audience") == binding.audience()
    )


def validate_live(claims):
    """Strong reads on every call; errors propagate to fail-closed caller."""
    now = binding._now()
    if (
        type(claims.get("iat")) is not int
        or type(claims.get("exp")) is not int
        or not claims["iat"] <= now < claims["exp"]
    ):
        return False
    if claims.get("aud") != binding.audience():
        return False
    actor = binding.TownService().get_town_agent_by_id(claims["actor_id"], consistent=True)
    if not actor or not _binding_matches(
        actor, claims["uid"], claims["iid"], claims["key"], claims["session"], claims["revision"]
    ):
        return False
    instance = dynamo.instances_table().get_item(Key={"id": claims["iid"]}, ConsistentRead=True).get("Item")
    if not instance or instance.get("userId") != claims["uid"] or instance.get("isActive") != "true":
        return False
    user = dynamo.users_table().get_item(Key={"userId": claims["uid"]}, ConsistentRead=True).get("Item")
    if not user:
        return False
    revoked = user.get("tokenRevokedAt")
    return revoked is None or claims["town_iat"] * 1000 >= int(revoked)


def verify(token):
    try:
        if not isinstance(token, str) or not token.startswith(PREFIX) or len(token) > 4096:
            return None
        part, sig = token[len(PREFIX) :].split(".")
        raw = _decode(part)
        if not hmac.compare_digest(_mac(raw), _decode(sig)):
            return None
        claims = json.loads(raw)
        fields = {"uid", "iid", "actor_id", "key", "session", "revision", "aud", "iat", "exp", "town_iat", "jti"}
        if not isinstance(claims, dict) or set(claims) not in (fields, fields | {"device_id"}):
            return None
        if any(
            not isinstance(claims[k], str) or not claims[k]
            for k in ("uid", "iid", "actor_id", "key", "session", "aud", "jti")
        ):
            return None
        if any(type(claims[k]) is not int for k in ("revision", "iat", "exp", "town_iat")):
            return None
        if claims["exp"] - claims["iat"] > TOKEN_TTL or claims["town_iat"] > claims["iat"]:
            return None
        return claims if validate_live(claims) else None
    except Exception:
        return None


@router.post("/agents/{actor_id}/runtime-session/challenge", dependencies=[Depends(_enabled)])
async def challenge(actor_id: str, body: Request, token_info: tuple = Depends(_town_auth)):
    owner, instance, town_iat = _principal(token_info)
    from core.town_token import require_actor_scope

    require_actor_scope(token_info[1], actor_id)
    actor = binding._owned(actor_id, owner)
    rev = int(actor.get("bindingRevision", -1))
    binding._key(body.member_key)
    if not _binding_matches(actor, owner, instance["id"], body.member_key, body.session_id, rev):
        raise HTTPException(403, "runtime binding mismatch")
    now = binding._now()
    payload = {
        "version": 1,
        "actor_id": actor_id,
        "owner_id": owner,
        "member_key": body.member_key,
        "session_id": body.session_id,
        "audience": binding.audience(),
        "revision": rev,
        "challenge_id": secrets.token_urlsafe(32),
        "issued_at": now,
        "expires_at": now + binding.TTL,
    }
    binding._update(
        actor_id,
        UpdateExpression="SET runtimeSessionChallenge = :challenge",
        ConditionExpression=(
            "userId = :owner AND isActive = :active AND instanceId = :iid "
            "AND bindingRevision = :rev AND runtimeBinding.memberKey = :key "
            "AND runtimeBinding.sessionId = :session AND runtimeBinding.audience = :aud "
            "AND runtimeBinding.revision = :rev"
        ),
        ExpressionAttributeValues={
            ":challenge": payload,
            ":owner": owner,
            ":active": "true",
            ":iid": instance["id"],
            ":rev": rev,
            ":key": body.member_key,
            ":session": body.session_id,
            ":aud": binding.audience(),
        },
    )
    return {
        "challenge_id": payload["challenge_id"],
        "expires_at": payload["expires_at"],
        "signing_bytes_b64url": _encode(binding._bytes(payload)),
    }


@router.post("/agents/{actor_id}/runtime-session/complete", dependencies=[Depends(_enabled)])
async def complete(actor_id: str, body: binding.CompletionRequest, token_info: tuple = Depends(_town_auth)):
    from core.town_token import verify_town_token

    owner, instance, town_iat = _principal(token_info)
    town = verify_town_token(token_info[1])
    if not town or type(town.get("exp")) is not int:
        raise HTTPException(401, "invalid onboarding credential")
    from core.town_token import require_actor_scope

    require_actor_scope(token_info[1], actor_id)
    actor = binding._owned(actor_id, owner)
    payload = actor.get("runtimeSessionChallenge") or {}
    now = binding._now()
    if payload.get("challenge_id") != body.challenge_id or payload.get("expires_at", 0) <= now:
        raise HTTPException(409, "challenge unavailable")
    rev = int(payload["revision"])
    if (
        payload.get("actor_id") != actor_id
        or payload.get("owner_id") != owner
        or payload.get("audience") != binding.audience()
        or not _binding_matches(actor, owner, instance["id"], payload["member_key"], payload["session_id"], rev)
    ):
        raise HTTPException(403, "runtime binding mismatch")
    try:
        sig = _decode(body.signature)
        if len(sig) != 64:
            raise ValueError("signature")
        Ed25519PublicKey.from_public_bytes(binding._key(payload["member_key"])).verify(sig, binding._bytes(payload))
    except (ValueError, InvalidSignature):
        raise HTTPException(403, "invalid proof")
    now = binding._now()
    binding._update(
        actor_id,
        UpdateExpression="REMOVE runtimeSessionChallenge",
        ConditionExpression=(
            "userId = :owner AND isActive = :active AND instanceId = :iid AND bindingRevision = :rev "
            "AND runtimeBinding.memberKey = :key AND runtimeBinding.sessionId = :session "
            "AND runtimeBinding.audience = :aud AND runtimeBinding.revision = :rev "
            "AND runtimeSessionChallenge.challenge_id = :cid AND runtimeSessionChallenge.expires_at > :now"
        ),
        ExpressionAttributeValues={
            ":owner": owner,
            ":active": "true",
            ":iid": instance["id"],
            ":rev": rev,
            ":key": payload["member_key"],
            ":session": payload["session_id"],
            ":aud": binding.audience(),
            ":cid": body.challenge_id,
            ":now": now,
        },
    )
    claims = {
        "uid": owner,
        "iid": instance["id"],
        "actor_id": actor_id,
        "key": payload["member_key"],
        "session": payload["session_id"],
        "revision": rev,
        "aud": binding.audience(),
        "iat": now,
        "exp": min(now + TOKEN_TTL, town["exp"]),
        "town_iat": town_iat,
        "jti": secrets.token_urlsafe(24),
    }
    if town.get("device_id"):
        claims["device_id"] = town["device_id"]
    raw = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
    return {"runtime_session_token": PREFIX + _encode(raw) + "." + _encode(_mac(raw)), "expires_at": claims["exp"]}
