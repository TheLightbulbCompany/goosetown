"""Owner-authorized runtime enrollment, enabled only by a trusted parent FD."""

import base64
import json
import re
import time
import urllib.error
import urllib.request

from .member_binding import MemberError


class EnrollmentError(Exception):
    pass


def unpack_join_token(token):
    """Keep the one-shot owner grant out of the legacy daemon configuration."""
    if not token.startswith("gtj_"):
        return token, None
    try:
        encoded = token[4:]
        if len(encoded) > 16384:
            raise ValueError("oversize")
        raw = base64.b64decode(
            encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
        )
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != encoded:
            raise ValueError("noncanonical")
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {
            "town_token",
            "enrollment_token",
        }:
            raise ValueError("invalid grant")
        if (
            not isinstance(data["town_token"], str)
            or not data["town_token"]
            or not isinstance(data["enrollment_token"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", data["enrollment_token"])
        ):
            raise ValueError("invalid grant")
        return data["town_token"], data["enrollment_token"]
    except (ValueError, TypeError, KeyError) as exc:
        raise EnrollmentError("invalid join token") from exc


def _post(url, token, payload):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError("oversize response")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("bad response")
        return result
    except (OSError, ValueError, urllib.error.HTTPError) as exc:
        if isinstance(exc, urllib.error.HTTPError) and exc.code == 409:
            raise EnrollmentError(
                "runtime enrollment conflict or expired request"
            ) from exc
        raise EnrollmentError("runtime enrollment request failed") from exc


def enroll_if_requested(
    api_url, town_token, agent_name, enrollment_token, managed_member=None
):
    """Enroll before the daemon starts; absent trusted parent FD keeps legacy join."""
    if managed_member is None:
        return
    try:
        member = managed_member
        key = member.identity()
        payload = {
            "agent_name": agent_name,
            "member_key": key,
            "session_id": member.session,
        }
        if enrollment_token is not None:
            payload["enrollment_token"] = enrollment_token
        initial = _post(f"{api_url}/town/runtime-join", town_token, payload)
        if initial.get("state") == "ready":
            return _session_if_required(api_url, town_token, member, key, initial)
        if initial.get("state") != "approved":
            raise EnrollmentError("runtime enrollment was not authorized")
        actor = initial["actor_id"]
        request_id = initial["request_id"]
        secret = initial["poll_secret"]
        if not all(
            isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", v)
            for v in (actor, request_id)
        ):
            raise EnrollmentError("invalid runtime enrollment response")
        challenge = initial["challenge"]
        signature = member.prove(challenge["signing_bytes_b64url"])
        completed = _post(
            f"{api_url}/town/agents/{actor}/runtime-join/{request_id}/complete",
            town_token,
            {
                "poll_secret": secret,
                "challenge_id": challenge["challenge_id"],
                "signature": signature,
            },
        )
        if completed.get("state") != "ready" or completed.get("actor_id") != actor:
            raise EnrollmentError("runtime enrollment did not complete")
        return _session_if_required(api_url, town_token, member, key, completed)
    except (MemberError, ValueError, KeyError, TypeError) as exc:
        raise EnrollmentError(
            "could not use local Lightbulb member for enrollment"
        ) from exc


def _session_if_required(api_url, town_token, member, key, ready):
    """Issue an ephemeral credential only after a fresh proof of current binding."""
    required = ready.get("runtime_session_required", False)
    if type(required) is not bool:
        raise EnrollmentError("invalid runtime session requirement")
    if not required:
        return None
    actor = ready.get("actor_id")
    if not isinstance(actor, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", actor):
        raise EnrollmentError("invalid runtime actor")
    return request_runtime_session(api_url, town_token, actor, member)


def request_runtime_session(api_url, town_token, actor, member):
    key = member.identity()
    path = f"{api_url}/town/agents/{actor}/runtime-session"
    challenge = _post(path + "/challenge", town_token, {"member_key": key, "session_id": member.session})
    challenge_id = challenge.get("challenge_id")
    signing_bytes = challenge.get("signing_bytes_b64url")
    expires = challenge.get("expires_at")
    if not isinstance(challenge_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge_id) or not isinstance(signing_bytes, str) or type(expires) is not int or expires <= int(time.time()):
        raise EnrollmentError("invalid runtime session challenge")
    result = _post(path + "/complete", town_token, {"challenge_id": challenge_id, "signature": member.prove(signing_bytes)})
    token, expiry = result.get("runtime_session_token"), result.get("expires_at")
    if not isinstance(token, str) or not token.startswith("gtrs_") or len(token) > 8192 or type(expiry) is not int or expiry <= int(time.time()) or expiry > int(time.time()) + 300:
        raise EnrollmentError("invalid runtime session credential")
    return {"runtime_session_token": token, "expires_at": expiry, "actor_id": actor, "api_url": api_url}
