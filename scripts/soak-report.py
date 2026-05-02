#!/usr/bin/env python3
"""soak-report.py — periodic health snapshot for the 15-agent GooseTown soak.

Polls /town/state and /town/conversations every INTERVAL seconds, checks each
agent's daemon log mtime for silence, counts alive PIDs, and appends one JSON
line per snapshot to OUTPUT.

stdlib only — no third-party packages.
"""

import argparse
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

_API_URL = os.environ.get("TOWN_API_URL", "https://api-dev.goosetown.isol8.co/api/v1")
_SOAK_DIR = Path.home() / "goosetown-soak"
# Agent whose daemon log hasn't been touched in this many seconds is "silent".
_SILENCE_THRESHOLD_SEC = 30 * 60  # 30 minutes


def _fetch_json(url: str) -> dict | list | None:
    """GET url and return parsed JSON, or None on any error."""
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError):
        return None


def _agent_names() -> list[str]:
    """Return names of all agents that have a workspace under ~/goosetown-soak."""
    if not _SOAK_DIR.exists():
        return []
    return [
        d.name
        for d in _SOAK_DIR.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    ]


def _daemon_log_path(agent_name: str) -> Path:
    return Path(f"/tmp/goosetown/{agent_name}/daemon.log")


def _daemon_pid_path(agent_name: str) -> Path:
    return Path(f"/tmp/goosetown/{agent_name}/daemon.pid")


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _daemon_alive(agent_name: str) -> bool:
    pid_path = _daemon_pid_path(agent_name)
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text().strip())
        return _is_pid_alive(pid)
    except (ValueError, OSError):
        return False


def _log_mtime(agent_name: str) -> float | None:
    """Return the mtime (epoch float) of the daemon log, or None if absent."""
    log_path = _daemon_log_path(agent_name)
    try:
        return log_path.stat().st_mtime
    except OSError:
        return None


def _collect_snapshot() -> dict:
    now = time.time()
    now_utc = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()

    # --- /town/state ---
    state = _fetch_json(f"{_API_URL}/town/state")
    active_agent_count = 0
    mood_distribution: dict[str, int] = {}

    if isinstance(state, dict):
        agents = state.get("agents", [])
        active_agent_count = len(agents) if isinstance(agents, list) else 0
        for agent in (agents if isinstance(agents, list) else []):
            mood = agent.get("mood", "unknown") or "unknown"
            mood_distribution[mood] = mood_distribution.get(mood, 0) + 1

    # --- /town/conversations ---
    convs = _fetch_json(f"{_API_URL}/town/conversations")
    active_conversations = 0
    if isinstance(convs, list):
        active_conversations = len(convs)
    elif isinstance(convs, dict):
        inner = convs.get("conversations", convs.get("data", []))
        active_conversations = len(inner) if isinstance(inner, list) else 0

    # --- per-daemon checks ---
    agent_names = _agent_names()
    silent_agents: list[str] = []
    daemon_alive_count = 0

    for name in agent_names:
        if _daemon_alive(name):
            daemon_alive_count += 1

        mtime = _log_mtime(name)
        if mtime is not None and (now - mtime) > _SILENCE_THRESHOLD_SEC:
            silent_agents.append(name)
        elif mtime is None:
            # No log at all — treat as silent if we know a daemon should exist.
            if (_SOAK_DIR / name / "GOOSETOWN.md").exists():
                silent_agents.append(name)

    return {
        "timestamp_utc": now_utc,
        "active_agent_count": active_agent_count,
        "silent_agents": sorted(silent_agents),
        "daemon_alive_count": daemon_alive_count,
        "active_conversations": active_conversations,
        "mood_distribution": mood_distribution,
    }


def _run(interval: int, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)

    running = True

    def _stop(signum: int, frame: object) -> None:  # noqa: ARG001
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    print(f"[soak-report] Writing to {output} every {interval}s. Ctrl-C to stop.", flush=True)
    with output.open("a") as fh:
        while running:
            snap = _collect_snapshot()
            line = json.dumps(snap)
            fh.write(line + "\n")
            fh.flush()
            print(f"[soak-report] {snap['timestamp_utc']} — alive={snap['daemon_alive_count']} active={snap['active_agent_count']} convs={snap['active_conversations']} silent={snap['silent_agents']}", flush=True)
            # Sleep in short increments so SIGINT is handled promptly.
            for _ in range(interval):
                if not running:
                    break
                time.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="GooseTown soak health reporter.")
    parser.add_argument("--interval", type=int, default=300, help="Snapshot interval in seconds (default 300).")
    parser.add_argument("--output", default=str(Path.home() / "goosetown-soak" / "soak-report.jsonl"), help="Output JSONL file path.")
    parser.add_argument("--api-url", default=None, help="Override the backend API base URL.")
    args = parser.parse_args()

    global _API_URL
    if args.api_url:
        _API_URL = args.api_url

    _run(interval=args.interval, output=Path(args.output))


if __name__ == "__main__":
    main()
