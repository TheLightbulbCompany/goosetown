"""Browser consent for agent-first join; credentials stay off stdout and argv."""
import sys
import time
import webbrowser
from urllib.parse import urlsplit

from .runtime_join import EnrollmentError, _post


def authorize(api_url, args):
    persona = {"agent_name": args.name, "appearance": args.appearance, "personality": args.personality}
    started = _post(f"{api_url}/town/device-join", "", persona)
    url = started["verification_uri"]
    # Only server-configured HTTPS pages (loopback for local testing) can open.
    parsed = urlsplit(url)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")):
        raise EnrollmentError("invalid browser confirmation URL")
    print(f"Confirm in your browser: {url}\nCode: {started['user_code']}", file=sys.stderr, flush=True)
    webbrowser.open(url)
    while time.time() < started["expires_at"]:
        result = _post(f"{api_url}/town/device-join/{started['user_code']}/poll", "", {
            "device_secret": started["device_secret"],
        })
        if result.get("state") == "approved":
            return result["join_token"]
        if result.get("state") not in ("pending", "authorizing"):
            raise EnrollmentError("Browser confirmation ended. Run goosetown join to try again.")
        time.sleep(3)
    raise EnrollmentError("Browser code expired. Run goosetown join to try again.")


def cached_credential(api_url, workspace):
    """Reuse an existing visit credential; the server still verifies ownership."""
    import base64
    import json
    from .config import parse_goosetown_md
    path = workspace / "GOOSETOWN.md"
    if not path.exists() or not (workspace / ".goosetown/member/state").exists():
        return None
    try:
        token = parse_goosetown_md(path)["token"]
        encoded = token.split(".")[0]
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if type(claims.get("exp")) is not int or claims["exp"] <= time.time() + 30:
            return None
        return token if _post(f"{api_url}/town/runtime-join/check", token, {}).get("valid") else None
    except (KeyError, ValueError, TypeError, EnrollmentError):
        return None
