"""GooseTown endpoint constants and environment-override helpers.

Usage
-----
Import ``apply_env_override`` from this module to inject TOWN_API_URL /
TOWN_WS_URL into ``os.environ`` *before* any subcommand reads config.
The existing ``_load_config`` helper in core.py prefers env vars, so the
override is transparent downstream.
"""

import os

ENDPOINTS: dict[str, dict[str, str]] = {
    "prod": {
        "api": "https://api.goosetown.isol8.co/api/v1",
        "ws": "wss://ws.goosetown.isol8.co",
    },
    "dev": {
        "api": "https://api-dev.goosetown.isol8.co/api/v1",
        "ws": "wss://ws-dev.goosetown.isol8.co",
    },
}

# The prod endpoints are the default when neither --dev nor --prod is passed
# *and* the agent has not yet joined (so GOOSETOWN.md is absent).  Once an
# agent has joined, _load_config() reads api_url / ws_url from GOOSETOWN.md
# which already contains the correct endpoint for that environment.
DEFAULT_ENV = "prod"


def apply_env_override(env_name: str) -> None:
    """Set TOWN_API_URL and TOWN_WS_URL in os.environ for the given env.

    Parameters
    ----------
    env_name:
        "prod" or "dev".  Raises ``KeyError`` if the name is unknown.
    """
    endpoints = ENDPOINTS[env_name]
    os.environ["TOWN_API_URL"] = endpoints["api"]
    os.environ["TOWN_WS_URL"] = endpoints["ws"]
