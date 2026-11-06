"""Config loading helpers — GOOSETOWN.md parsing."""

import os
import sys
from pathlib import Path


def parse_goosetown_md(path: Path) -> dict:
    """Parse GOOSETOWN.md into a dict.

    Format (same as written by cmd_join):
        token: <value>
        ws_url: <value>
        api_url: <value>
        agent: <value>
        workspace_path: <value>
    """
    config: dict = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if ":" in line and not line.startswith("#"):
            key, _, value = line.partition(":")
            config[key.strip()] = value.strip()
    return config


def default_agent_dir() -> Path:
    """Resolve the selected town home while preserving explicit legacy homes."""
    if os.environ.get("AGENT_DIR"):
        return Path(os.environ["AGENT_DIR"])
    if (Path.cwd() / "GOOSETOWN.md").exists():
        return Path.cwd()
    selected = Path.home() / ".goosetown" / "active-agent"
    import re
    name = selected.read_text().strip() if selected.exists() else "visitor"
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", name):
        raise ValueError("Invalid selected town agent")
    return Path.home() / ".goosetown" / "agents" / name


def load_config(agent_dir: Path | None = None) -> dict:
    """Load config from GOOSETOWN.md.

    Returns the parsed dict or exits with a JSON error if the file is missing.
    Env vars TOWN_API_URL / TOWN_WS_URL override the values in the file so
    that the ``--dev`` / ``--prod`` flags applied by ``cli.main`` take effect
    transparently.
    """
    import json

    if agent_dir is None:
        agent_dir = default_agent_dir()
    cfg_path = agent_dir / "GOOSETOWN.md"
    if not cfg_path.exists():
        payload = {
            "error": "GOOSETOWN.md not found. Run 'goosetown join' first."
        }
        print(json.dumps(payload))
        sys.exit(1)
    cfg = parse_goosetown_md(cfg_path)

    # Env-var overrides (set by apply_env_override / --dev / --prod)
    api_override = os.environ.get("TOWN_API_URL")
    ws_override = os.environ.get("TOWN_WS_URL")
    if api_override:
        cfg["api_url"] = api_override
    if ws_override:
        cfg["ws_url"] = ws_override

    return cfg


def state_dir(agent_name: str) -> Path:
    """Return the runtime state directory for an agent."""
    return Path(f"/tmp/goosetown/{agent_name}")
