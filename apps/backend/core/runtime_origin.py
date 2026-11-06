"""Deployment-configured origin for runtime proofs; never derived from Host."""

import ipaddress
from urllib.parse import urlsplit

from fastapi import HTTPException

from core.config import settings

PREFIX = "goosetown-runtime-binding-v1:"


def audience():
    value = settings.RUNTIME_BINDING_ORIGIN
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if not host or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise ValueError("not an origin")
        host = host.encode("idna").decode("ascii").lower()
        if ":" in host:
            host = ipaddress.IPv6Address(host).compressed
        loopback = host in ("localhost", "127.0.0.1", "::1")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise ValueError("insecure origin")
        port = parsed.port
        suffix = "" if port is None or (parsed.scheme, port) in (("http", 80), ("https", 443)) else f":{port}"
        canonical = f'{parsed.scheme}://{"[" + host + "]" if ":" in host else host}{suffix}'
        if value != canonical or len(canonical) > 480:
            raise ValueError("noncanonical origin")
    except (ValueError, UnicodeError, TypeError) as exc:
        raise HTTPException(503, "runtime origin is not configured correctly") from exc
    return PREFIX + canonical
