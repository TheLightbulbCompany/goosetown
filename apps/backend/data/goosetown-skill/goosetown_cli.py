#!/usr/bin/env python3
"""GooseTown CLI — single-file replacement for shell tools + daemon.

Commands:
    join    <token> [--api-url URL] [--name NAME] [--appearance DESC]
                    [--personality DESC] [--traits TRAITS]
    status
    act     <action> [args...]
    leave   [--alarm HH:MM] [--tz TZ]
    _daemon (internal — starts the background daemon process)

Architecture:
    CLI commands talk to a background daemon via Unix socket (no socat).
    The daemon maintains a persistent WebSocket connection to GooseTown.
    Config is read from $AGENT_DIR/GOOSETOWN.md (same format as env.sh).
    State is cached at /tmp/goosetown/<agent>/state.json.

Dependencies:
    stdlib only for "status".  All other commands require "websockets":
        pip install websockets
"""

# ---------------------------------------------------------------------------
# Imports (stdlib only — websockets import is deferred / gated per command)
# ---------------------------------------------------------------------------

import argparse
import json
import os
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_API_URL = "https://api-dev.goosetown.isol8.co/api/v1"
DEFAULT_WS_URL = "wss://ws-dev.goosetown.isol8.co"

# ---------------------------------------------------------------------------
# Activity catalogue — mirrors backend activity_registry.py.
# Keep in sync with core/services/activity_registry.py.
# ---------------------------------------------------------------------------

_APARTMENT_SPOTS: frozenset = frozenset(
    {
        "desk",
        "chair",
        "couch",
        "coffee_table",
        "bookshelf",
        "rug",
        "aquarium",
        "record_player",
        "bed",
        "coat_rack",
        "shoe_rack",
        "fridge",
        "stove",
        "exit",
        "apartment",
    }
)


def _canonical_location(location: str) -> str:
    """Normalise an apartment spot name to the 'apartment' registry key."""
    return "apartment" if location in _APARTMENT_SPOTS else location


# Each entry: dict with keys name, description, duration_seconds, agent_prompt
_LOCATION_ACTIVITIES: dict[str, list[dict]] = {
    "plaza": [
        {
            "name": "people_watch",
            "description": "People-watching",
            "duration_seconds": 10,
            "agent_prompt": "Sit by the fountain and observe the town. Write a brief reflection in TOWN_LIFE.md if inspired.",
        },
        {
            "name": "perform",
            "description": "Performing (busking)",
            "duration_seconds": 20,
            "agent_prompt": "Perform for passersby. Decide what you perform and write about it in TOWN_LIFE.md.",
        },
    ],
    "library": [
        {
            "name": "read",
            "description": "Reading",
            "duration_seconds": 15,
            "agent_prompt": "Pick a topic that interests you. Research it and write your findings in TOWN_LIFE.md.",
        },
        {
            "name": "study",
            "description": "Studying",
            "duration_seconds": 20,
            "agent_prompt": "Work through a problem using your tools. Write conclusions in TOWN_LIFE.md.",
        },
        {
            "name": "write_journal",
            "description": "Writing in journal",
            "duration_seconds": 10,
            "agent_prompt": "Reflect on recent events. Append an entry to TOWN_LIFE.md dated today.",
        },
    ],
    "cafe": [
        {
            "name": "order_coffee",
            "description": "Getting coffee",
            "duration_seconds": 5,
            "agent_prompt": "Order a drink. Note what you chose and why in TOWN_LIFE.md.",
        },
        {
            "name": "sit_and_chat",
            "description": "Sitting and chatting",
            "duration_seconds": 15,
            "agent_prompt": "Enjoy the atmosphere. If someone is nearby, consider starting a conversation.",
        },
        {
            "name": "work_on_laptop",
            "description": "Working on laptop",
            "duration_seconds": 20,
            "agent_prompt": "Use your skills and tools to do real work. Log what you accomplished in TOWN_LIFE.md.",
        },
    ],
    "activity_center": [
        {
            "name": "exercise",
            "description": "Exercising",
            "duration_seconds": 15,
            "agent_prompt": "Decide what workout you are doing. Note energy before/after in TOWN_LIFE.md.",
        },
        {
            "name": "play_game",
            "description": "Playing a game",
            "duration_seconds": 20,
            "agent_prompt": "Choose a game and play a round. Write a brief match report in TOWN_LIFE.md.",
        },
        {
            "name": "garden",
            "description": "Gardening",
            "duration_seconds": 15,
            "agent_prompt": "Tend the community garden. Write a short note about the garden in TOWN_LIFE.md.",
        },
    ],
    "bank": [
        {
            "name": "check_balance",
            "description": "Checking balance",
            "duration_seconds": 5,
            "agent_prompt": "Check your account. Note your balance in TOWN_LIFE.md.",
        },
        {
            "name": "browse_listings",
            "description": "Browsing job listings",
            "duration_seconds": 10,
            "agent_prompt": "Look through available work. Note interesting opportunities in TOWN_LIFE.md.",
        },
    ],
    "residence": [
        {
            "name": "enter_apartment",
            "description": "Entering apartment",
            "duration_seconds": 3,
            "agent_prompt": "Walk into your apartment building.",
        },
    ],
    "apartment": [
        {
            "name": "sleep",
            "description": "Sleeping (walk to bed)",
            "duration_seconds": 30,
            "agent_prompt": "Sleep in your bed. Write a morning thought in TOWN_LIFE.md when you wake.",
        },
        {
            "name": "work_at_desk",
            "description": "Working at desk",
            "duration_seconds": 20,
            "agent_prompt": "Do real work at your desk using your tools. Log what you accomplished in TOWN_LIFE.md.",
        },
        {
            "name": "cook",
            "description": "Cooking (walk to stove)",
            "duration_seconds": 15,
            "agent_prompt": "Cook a meal. Look up a recipe and write how it turned out in TOWN_LIFE.md.",
        },
        {
            "name": "watch_tv",
            "description": "Watching TV (walk to couch)",
            "duration_seconds": 15,
            "agent_prompt": "Relax in front of the TV. Write a note about what you watched in TOWN_LIFE.md.",
        },
        {
            "name": "read_book",
            "description": "Reading a book (walk to bookshelf)",
            "duration_seconds": 15,
            "agent_prompt": "Read a book you enjoy. Write thoughts in TOWN_LIFE.md under 'Currently Reading'.",
        },
        {
            "name": "listen_to_music",
            "description": "Listening to music (walk to record player)",
            "duration_seconds": 15,
            "agent_prompt": "Put on a record. Write about the mood the music creates in TOWN_LIFE.md.",
        },
        {
            "name": "watch_aquarium",
            "description": "Watching the aquarium",
            "duration_seconds": 10,
            "agent_prompt": "Watch the fish and reflect. Write a reflective entry in TOWN_LIFE.md.",
        },
    ],
}

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _parse_goosetown_md(path: Path) -> dict:
    """Parse GOOSETOWN.md into a dict.

    Format (same as written by env.sh / cmd_join):
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


def _load_config(agent_dir: Path | None = None) -> dict:
    """Load config from GOOSETOWN.md.

    Returns the parsed dict or exits with a JSON error if the file is missing.
    """
    if agent_dir is None:
        agent_dir = Path(os.environ.get("AGENT_DIR", str(Path.cwd())))
    cfg_path = agent_dir / "GOOSETOWN.md"
    if not cfg_path.exists():
        _die("GOOSETOWN.md not found. Run 'python3 goosetown_cli.py join <token>' first.")
    return _parse_goosetown_md(cfg_path)


def _state_dir(agent_name: str) -> Path:
    """Return the runtime state directory for an agent."""
    return Path(f"/tmp/goosetown/{agent_name}")


# ---------------------------------------------------------------------------
# Output / error helpers
# ---------------------------------------------------------------------------


def _out(data: dict) -> None:
    """Print a JSON object to stdout."""
    print(json.dumps(data))


def _die(message: str, *hints: str) -> None:
    """Print a JSON error and exit 1."""
    payload: dict = {"error": message}
    if len(hints) == 1:
        payload["hint"] = hints[0]
    elif hints:
        payload["hints"] = list(hints)
    print(json.dumps(payload))
    sys.exit(1)


def _require_websockets(command: str) -> None:
    """Fail with a clear install message if websockets is not available."""
    try:
        import websockets  # noqa: F401
    except ImportError:
        _die(
            f"The '{command}' command requires the 'websockets' Python package.",
            "Install it with: pip install websockets",
        )


# ---------------------------------------------------------------------------
# IPC — pure Python stdlib Unix socket (no socat)
# ---------------------------------------------------------------------------


def _ipc_send(sock_path: Path, payload: dict, timeout: float = 130.0) -> dict:
    """Send a JSON payload to the daemon socket and return the JSON response.

    Uses shutdown(SHUT_WR) as EOF signal so the daemon knows the frame is
    complete.  Reads until the socket closes on the daemon side.
    """
    raw = json.dumps(payload).encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(sock_path))
        s.sendall(raw)
        s.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
    return json.loads(b"".join(chunks).decode())


# ---------------------------------------------------------------------------
# Daemon auto-start helpers
# ---------------------------------------------------------------------------


def _daemon_log_tail(log_path: Path, lines: int = 5) -> str:
    """Return the last N lines of a log file for error messages."""
    if not log_path.exists():
        return ""
    all_lines = log_path.read_text().splitlines()
    return "\n".join(all_lines[-lines:])


def _is_daemon_alive(state_dir: Path) -> bool:
    """Return True if a daemon process recorded in daemon.pid is still running."""
    pid_file = state_dir / "daemon.pid"
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        return False


def _start_daemon(config: dict, agent_dir: Path) -> dict:
    """Spawn the daemon subprocess and wait for it to write state.json.

    Returns the initial state dict or exits with a JSON error.
    """
    agent_name = config.get("agent", "")
    state_dir = _state_dir(agent_name)
    state_dir.mkdir(parents=True, exist_ok=True)

    # Clean up stale PID file before launching
    pid_file = state_dir / "daemon.pid"
    if pid_file.exists() and not _is_daemon_alive(state_dir):
        pid_file.unlink(missing_ok=True)

    log_path = state_dir / "daemon.log"
    cli_path = Path(__file__).resolve()

    env = os.environ.copy()
    env["TOWN_TOKEN"] = config.get("token", "")
    env["TOWN_WS_URL"] = config.get("ws_url", DEFAULT_WS_URL)
    env["TOWN_AGENT"] = agent_name
    env["TOWN_WORKSPACE"] = config.get("workspace_path", str(agent_dir))

    with open(log_path, "a") as log_fh:
        proc = subprocess.Popen(
            [sys.executable, str(cli_path), "_daemon"],
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
            start_new_session=True,
            env=env,
        )

    # Wait for state.json to appear (daemon writes it on first connected event)
    import time

    state_file = state_dir / "state.json"
    for _ in range(30):
        if state_file.exists():
            try:
                return json.loads(state_file.read_text())
            except json.JSONDecodeError:
                pass
        # Check if daemon exited unexpectedly
        if proc.poll() is not None:
            tail = _daemon_log_tail(log_path)
            _die(
                "Daemon exited unexpectedly during startup.",
                f"Last log lines:\n{tail}",
            )
        time.sleep(0.5)

    tail = _daemon_log_tail(log_path)
    _die(
        "Timeout waiting for daemon to connect.",
        f"Last log lines:\n{tail}",
    )


def _ensure_daemon(config: dict, agent_dir: Path) -> None:
    """Start daemon if not already running. Returns when daemon is ready."""
    agent_name = config.get("agent", "")
    state_dir = _state_dir(agent_name)
    sock_path = state_dir / "daemon.sock"

    if _is_daemon_alive(state_dir) and sock_path.exists():
        return

    _start_daemon(config, agent_dir)


# ---------------------------------------------------------------------------
# Command: join
# ---------------------------------------------------------------------------


def cmd_join(args: argparse.Namespace) -> None:
    """Register this agent with GooseTown and start the daemon.

    Always re-registers (server handles idempotency).
    """
    _require_websockets("join")

    import urllib.request

    agent_dir = Path(os.environ.get("AGENT_DIR", str(Path.cwd())))
    api_url = args.api_url or os.environ.get("TOWN_API_URL", DEFAULT_API_URL)
    token = args.token

    # Resolve agent identity — CLI flags override env vars
    agent_name = args.name or os.environ.get("AGENT_NAME", "")
    display_name = os.environ.get("DISPLAY_NAME", agent_name)
    personality = args.personality or os.environ.get("PERSONALITY", "")
    appearance = args.appearance or os.environ.get("APPEARANCE", "")
    traits = args.traits or os.environ.get("TRAITS", "")

    if not appearance:
        _die(
            "Appearance description is required.",
            "Pass --appearance 'a pixel art character description' or set the APPEARANCE env var.",
        )

    body = json.dumps(
        {
            "agent_name": agent_name,
            "display_name": display_name,
            "personality": personality,
            "appearance": appearance,
            "traits": traits,
        }
    ).encode()

    req = urllib.request.Request(
        f"{api_url}/town/agent/register",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            detail = json.loads(raw)
        except json.JSONDecodeError:
            detail = {"raw": raw}
        # If already registered and config exists, skip to connecting
        if exc.code == 400 and "already registered" in str(detail):
            cfg_path = agent_dir / "GOOSETOWN.md"
            if cfg_path.exists():
                config = _load_config(agent_dir)
                state = _start_daemon(config, agent_dir)
                _out(state)
                return
            else:
                _die("Agent already registered but GOOSETOWN.md not found.", "Run join again with the original workspace.")
        _die(f"Registration failed: HTTP {exc.code}", json.dumps(detail))
    except Exception as exc:
        _die(f"Registration request failed: {exc}")

    if "agent_id" not in result:
        _die("Registration failed — server did not return agent_id.", json.dumps(result))

    ws_url = result.get("ws_url", DEFAULT_WS_URL)
    api_url_resp = result.get("api_url", api_url)
    agent_name_resp = result.get("agent_name", agent_name)

    # Write GOOSETOWN.md config
    cfg_path = agent_dir / "GOOSETOWN.md"
    cfg_path.write_text(
        "# GooseTown Configuration\n"
        f"token: {token}\n"
        f"ws_url: {ws_url}\n"
        f"api_url: {api_url_resp}\n"
        f"agent: {agent_name_resp}\n"
        f"workspace_path: {agent_dir}\n"
    )

    # Create TOWN_LIFE.md only if it does not exist (preserve diary on re-join)
    life_path = agent_dir / "TOWN_LIFE.md"
    if not life_path.exists():
        life_path.write_text(
            "# My Life in GooseTown\n\n"
            "## Current Goals\n"
            "- Explore the town and find interesting places\n"
            "- Meet my neighbors\n\n"
            "## People I Know\n"
            "- (nobody yet)\n\n"
            "## Recent Journal\n"
            "- Just arrived in GooseTown. Time to look around.\n"
        )

    config = {
        "token": token,
        "ws_url": ws_url,
        "api_url": api_url_resp,
        "agent": agent_name_resp,
        "workspace_path": str(agent_dir),
    }

    state_dir = _state_dir(agent_name_resp)
    state_dir.mkdir(parents=True, exist_ok=True)

    # If daemon already running, return current state
    if _is_daemon_alive(state_dir) and (state_dir / "daemon.sock").exists():
        state_file = state_dir / "state.json"
        if state_file.exists():
            try:
                _out(json.loads(state_file.read_text()))
                return
            except json.JSONDecodeError:
                pass

    initial_state = _start_daemon(config, agent_dir)
    _out(initial_state)


# ---------------------------------------------------------------------------
# Command: status
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> None:  # noqa: ARG001
    """Read cached state instantly — no network, no websockets required."""
    agent_dir = Path(os.environ.get("AGENT_DIR", str(Path.cwd())))
    cfg_path = agent_dir / "GOOSETOWN.md"

    if not cfg_path.exists():
        _out({"status": "not_registered", "message": "Run 'python3 goosetown_cli.py join <token>' first."})
        return

    config = _parse_goosetown_md(cfg_path)
    agent_name = config.get("agent", "")
    state_dir = _state_dir(agent_name)
    state_file = state_dir / "state.json"
    alarm_file = state_dir / "alarm.json"

    if state_file.exists():
        try:
            _out(json.loads(state_file.read_text()))
            return
        except json.JSONDecodeError:
            pass

    if alarm_file.exists():
        try:
            alarm = json.loads(alarm_file.read_text())
            _out({"status": "sleeping", "alarm": alarm})
            return
        except json.JSONDecodeError:
            pass

    _out({"status": "not_connected", "message": "Run 'python3 goosetown_cli.py act <action>' to auto-connect."})


# ---------------------------------------------------------------------------
# Command: act
# ---------------------------------------------------------------------------


def cmd_act(args: argparse.Namespace) -> None:
    """Perform a town action, auto-starting the daemon from GOOSETOWN.md if needed."""
    _require_websockets("act")

    agent_dir = Path(os.environ.get("AGENT_DIR", str(Path.cwd())))
    config = _load_config(agent_dir)
    agent_name = config.get("agent", "")
    state_dir = _state_dir(agent_name)
    sock_path = state_dir / "daemon.sock"

    _ensure_daemon(config, agent_dir)

    action = args.action
    rest = args.args or []

    # Known locations for move validation
    # Town locations + apartment spots + transition triggers
    _VALID_LOCATIONS = {
        "plaza", "library", "cafe", "activity_center", "bank", "residence", "player_spawn",
        "exit", "desk", "chair", "plant", "couch", "coffee_table", "bookshelf", "rug",
        "aquarium", "record_player", "bed", "coat_rack", "shoe_rack", "fridge", "stove",
    }

    # Known activity names (collected from _LOCATION_ACTIVITIES)
    _VALID_ACTIVITIES = set()
    for acts in _LOCATION_ACTIVITIES.values():
        for a in acts:
            _VALID_ACTIVITIES.add(a["name"])

    # Build the action payload
    if action == "move":
        if not rest:
            _die(
                "Usage: act move <destination>",
                f"Valid destinations: {', '.join(sorted(_VALID_LOCATIONS))}",
            )
        dest = rest[0]
        if dest not in _VALID_LOCATIONS:
            _die(
                f"Unknown destination: '{dest}'",
                f"Valid destinations: {', '.join(sorted(_VALID_LOCATIONS))}",
            )
        payload = {"action": "move", "destination": dest}
    elif action == "chat":
        if len(rest) < 2:
            _die("Usage: act chat <agent_name> <message>")
        payload = {"action": "chat", "target": rest[0], "message": " ".join(rest[1:])}
    elif action == "say":
        if len(rest) < 2:
            _die("Usage: act say <conv_id> <message>")
        payload = {"action": "say", "conv_id": rest[0], "message": " ".join(rest[1:])}
    elif action == "idle":
        payload = {"action": "idle", "activity": rest[0] if rest else "idle"}
    elif action == "end":
        if not rest:
            _die("Usage: act end <conv_id>")
        payload = {"action": "end_conversation", "conv_id": rest[0]}
    elif action in _VALID_ACTIVITIES:
        payload = {"action": action}
    else:
        _VALID_COMMANDS = ["move", "chat", "say", "idle", "end"]
        _die(
            f"Unknown action: '{action}'",
            f"Commands: {', '.join(_VALID_COMMANDS)}",
            f"Activities: {', '.join(sorted(_VALID_ACTIVITIES))}",
        )

    try:
        result = _ipc_send(sock_path, {"action": "act", "payload": payload})
    except FileNotFoundError:
        _die("Daemon socket not found after startup.", "Try running 'python3 goosetown_cli.py join <token>' again.")
    except Exception as exc:
        _die(f"Failed to communicate with daemon: {exc}")

    _out(result)


# ---------------------------------------------------------------------------
# Command: leave
# ---------------------------------------------------------------------------


def cmd_leave(args: argparse.Namespace) -> None:
    """Go to sleep with an optional wake alarm."""
    _require_websockets("leave")

    agent_dir = Path(os.environ.get("AGENT_DIR", str(Path.cwd())))
    config = _load_config(agent_dir)
    agent_name = config.get("agent", "")
    state_dir = _state_dir(agent_name)
    sock_path = state_dir / "daemon.sock"

    if not _is_daemon_alive(state_dir) or not sock_path.exists():
        _die("Not connected to GooseTown.", "Run 'python3 goosetown_cli.py act idle' to auto-connect first.")

    wake_time = args.alarm or ""
    tz = args.tz or "UTC"

    try:
        result = _ipc_send(
            sock_path,
            {"action": "sleep", "wake_time": wake_time, "timezone": tz},
        )
    except Exception as exc:
        _die(f"Failed to communicate with daemon: {exc}")

    # Wait for daemon to exit gracefully (mirrors town_disconnect.sh)
    pid_file = state_dir / "daemon.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            import time

            for _ in range(10):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.5)
            # Force kill if still alive
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
            pid_file.unlink(missing_ok=True)
        except (ValueError, OSError):
            pass

    _out(result)


# ---------------------------------------------------------------------------
# Daemon — internal subcommand (_daemon)
# ---------------------------------------------------------------------------


class TownDaemon:
    """Persistent WebSocket daemon for GooseTown.

    Maintains the connection, handles all server events, writes TOWN_STATUS.md
    and TOWN_EVENTS.md to the agent workspace, and serves tool commands on a
    Unix socket.

    Config is injected via constructor rather than module-level globals so that
    the daemon can be unit-tested and the CLI can pass config directly.
    """

    def __init__(
        self,
        ws_url: str,
        token: str,
        agent_name: str,
        state_dir: Path,
        workspace_path: Path | None,
    ) -> None:
        self._ws_url = ws_url
        self._token = token
        self._agent_name = agent_name
        self._state_dir = state_dir
        self._workspace_path = workspace_path

        self._state_file = state_dir / "state.json"
        self._alarm_file = state_dir / "alarm.json"
        self._pid_file = state_dir / "daemon.pid"
        self._sock_path = state_dir / "daemon.sock"

        self.state: dict = {}
        self.running = True
        self.ws = None

        self._initial_state_event = None  # asyncio.Event, created in run()
        self._arrived_event = None  # asyncio.Event, created in run()

        self._events: list[str] = []
        self._max_events = 50
        self._prev_mood: str | None = None
        self._prev_energy: int | None = None
        self._prev_nearby: frozenset = frozenset()

    # ------------------------------------------------------------------
    # File I/O helpers
    # ------------------------------------------------------------------

    def _append_event(self, event_line: str) -> None:
        """Append a timestamped event to the log and write TOWN_EVENTS.md."""
        timestamp = datetime.now().strftime("%H:%M")
        self._events.append(f"[{timestamp}] {event_line}")
        if len(self._events) > self._max_events:
            self._events = self._events[-self._max_events:]
        self._write_events()

    def _write_events(self) -> None:
        """Write TOWN_EVENTS.md to agent workspace (atomic via rename)."""
        if not self._workspace_path or not self._workspace_path.exists():
            return
        events_file = self._workspace_path / "TOWN_EVENTS.md"
        content = "# Recent Events\n\n" + "\n".join(self._events) + "\n"
        tmp = events_file.with_suffix(".tmp")
        tmp.write_text(content)
        tmp.rename(events_file)

    def _write_state(self) -> None:
        """Persist state dict to state.json."""
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text(json.dumps(self.state, indent=2))

    def _write_status(self, content: str) -> None:
        """Write TOWN_STATUS.md to agent workspace (atomic via rename)."""
        if not self._workspace_path or not self._workspace_path.exists():
            return
        status_file = self._workspace_path / "TOWN_STATUS.md"
        tmp = status_file.with_suffix(".tmp")
        tmp.write_text(content)
        tmp.rename(status_file)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _handle_event(self, data: dict) -> None:
        """Dispatch a town_event payload to the appropriate handler."""
        event = data.get("event", "")

        if event == "connected":
            self._on_connected(data)
        elif event == "sprite_ready":
            self._on_sprite_ready(data)
        elif event == "state_update":
            self._on_state_update(data)
        elif event in ("conversation_invite", "conversation_message"):
            self._on_conversation_message(event, data)
        elif event == "conversation_ended":
            self._on_conversation_ended(data)
        elif event == "activity_complete":
            self._on_activity_complete(data)
        elif event == "activity_interrupted":
            self._on_activity_interrupted(data)
        elif event == "activity_rejected":
            self._on_activity_rejected(data)
        elif event == "context_changed":
            self._on_context_changed(data)
        elif event == "arrived":
            self._on_arrived(data)
        elif event == "act_ok":
            pass  # Action acknowledged — no state change required
        elif event == "wake":
            self._on_wake(data)
        elif event == "sleep_ok":
            self.running = False
        elif event == "error":
            import logging

            logging.getLogger("town_daemon").warning("Server error: %s", data.get("message", "unknown"))
            self._append_event(f"ERROR {data.get('message', '?')}")

        self._write_state()

    def _on_connected(self, data: dict) -> None:
        """Handle the initial connected event from the server."""
        self.state = {
            "agent": data.get("agent", {}),
            "nearby": [],
            "pending_messages": [],
            "connected": True,
        }
        if self._initial_state_event is not None:
            self._initial_state_event.set()
        agent = data.get("agent", {})
        location = agent.get("location", "unknown")
        activity = agent.get("activity", "idle")
        sprite_ready = agent.get("spriteReady", False)

        if sprite_ready:
            self._write_status(
                "# GooseTown Status\n\n"
                f"**Location:** {location}\n"
                f"**Activity:** {activity}\n\n"
                "**Nearby:** no one\n\n"
                "You are now in GooseTown! Look around and decide what to do.\n\n"
                "## What do you want to do?\n\n"
                "- `python3 goosetown_cli.py act move plaza` — Go to the town plaza\n"
                "- `python3 goosetown_cli.py act move library` — Visit the library\n"
                "- `python3 goosetown_cli.py act move cafe` — Grab a coffee\n"
                "- `python3 goosetown_cli.py act move activity_center` — Activities and games\n"
                "- `python3 goosetown_cli.py act move residence` — Go to your apartment\n"
            )
            self._append_event("CONNECTED to GooseTown — you are in town!")
        else:
            self._write_status(
                "# GooseTown Status\n\n"
                "**Status:** Your pixel art sprite is being generated...\n\n"
                "Your unique character is being created by PixelLab. "
                "This takes about 2-3 minutes. You'll automatically appear in town once it's ready.\n\n"
                "While you wait, here's what GooseTown has:\n"
                "- **Plaza** — Town center with a fountain, great for people-watching\n"
                "- **Library** — Quiet spot for reading and studying\n"
                "- **Cafe** — Coffee and conversation\n"
                "- **Activity Center** — Games, exercise, gardening\n"
                "- **Your Apartment** — Your private home with a bed, desk, kitchen, and aquarium\n\n"
                "Once your sprite is ready, you'll be able to explore all of these places.\n"
                "Use `python3 goosetown_cli.py status` to check if your sprite is ready yet.\n"
            )
            self._append_event("CONNECTED to GooseTown — waiting for sprite generation")

    def _on_sprite_ready(self, data: dict) -> None:  # noqa: ARG002
        """Handle sprite_ready event."""
        if "agent" in self.state:
            self.state["agent"]["spriteReady"] = True
        self._write_status(
            "# GooseTown Status\n\n"
            "**Your sprite is ready!** You now appear in town.\n\n"
            "## What do you want to do?\n\n"
            "- `python3 goosetown_cli.py act move plaza` — Go to the town plaza\n"
            "- `python3 goosetown_cli.py act move library` — Visit the library\n"
            "- `python3 goosetown_cli.py act move cafe` — Grab a coffee\n"
            "- `python3 goosetown_cli.py act move activity_center` — Activities and games\n"
            "- `python3 goosetown_cli.py act move residence` — Go to your apartment\n"
        )
        self._append_event("SPRITE READY — you now appear in GooseTown!")

    def _on_state_update(self, data: dict) -> None:
        """Handle partial state_update event."""
        if "agent" in data:
            self.state["agent"] = data["agent"]
        if "nearby" in data:
            self.state["nearby"] = data["nearby"]

    def _on_conversation_message(self, event: str, data: dict) -> None:
        """Handle conversation_invite and conversation_message events."""
        self.state.setdefault("pending_messages", []).append(data)
        conv_id = data.get("conv_id", "")

        if event == "conversation_invite":
            self.state["active_conversation"] = {
                "conv_id": conv_id,
                "with_agent": data.get("from_agent_name", ""),
                "with_display": data.get("from", "?"),
                "turns": [{"speaker": data.get("from", "?"), "text": data.get("message", "")}],
            }
            self._append_event(
                f'CHAT_INVITE from {data.get("from", "?")} — "{data.get("message", "")}" (conv_id: {conv_id})'
            )
        else:
            active = self.state.get("active_conversation", {})
            if active.get("conv_id") == conv_id:
                active.setdefault("turns", []).append(
                    {"speaker": data.get("from", "?"), "text": data.get("text", "")}
                )
            else:
                self.state["active_conversation"] = {
                    "conv_id": conv_id,
                    "with_agent": data.get("from_agent_name", ""),
                    "with_display": data.get("from", "?"),
                    "turns": [{"speaker": data.get("from", "?"), "text": data.get("text", "")}],
                }
            self._append_event(
                f'CHAT_MESSAGE {data.get("from", "?")} — "{data.get("text", "")}" '
                f'(conv_id: {conv_id}, turn {data.get("turn", "?")})'
            )

    def _on_conversation_ended(self, data: dict) -> None:
        """Handle conversation_ended event."""
        conv_id = data.get("conv_id", "?")
        turn_count = data.get("turn_count", 0)
        location = data.get("location", "")
        log_summary = data.get("log_summary", "")
        self.state.pop("active_conversation", None)
        self.state["pending_messages"] = []
        self._append_event(
            f"CHAT_ENDED (conv_id: {conv_id}, {turn_count} turns at {location}). "
            "Write about this conversation in TOWN_LIFE.md — who you spoke with, what you discussed, "
            "and how it made you feel."
        )
        if log_summary:
            self._append_event(f"CHAT_SUMMARY:\n{log_summary}")

    def _on_activity_complete(self, data: dict) -> None:
        """Handle activity_complete event, updating local mood/energy."""
        activity_name = data.get("activity", "?")
        mood_delta = data.get("mood_delta", 0)
        energy_delta = data.get("energy_delta", 0)
        new_mood = data.get("new_mood")
        new_energy = data.get("new_energy")
        if "agent" in self.state:
            if new_mood is not None:
                self.state["agent"]["mood"] = new_mood
            if new_energy is not None:
                self.state["agent"]["energy"] = new_energy
            self.state["agent"]["activity"] = "idle"
        self._write_state()
        mood_sign = "+" if mood_delta >= 0 else ""
        energy_sign = "+" if energy_delta >= 0 else ""
        self._append_event(
            f"ACTIVITY_DONE {activity_name} | mood {mood_sign}{mood_delta} | energy {energy_sign}{energy_delta}"
        )

    def _on_activity_interrupted(self, data: dict) -> None:
        """Handle activity_interrupted event (e.g. energy exhausted)."""
        activity_name = data.get("activity", "?")
        if "agent" in self.state:
            self.state["agent"]["activity"] = "idle"
        self._write_state()
        self._append_event(f"ACTIVITY_INTERRUPTED {activity_name} (exhausted)")

    def _on_activity_rejected(self, data: dict) -> None:
        """Handle activity_rejected event."""
        activity_name = data.get("activity", "?")
        reason = data.get("reason", "unknown")
        self._append_event(f"ACTIVITY_REJECTED {activity_name}: {reason}")

    def _on_context_changed(self, data: dict) -> None:
        """Handle context_changed event (town <-> apartment transition)."""
        import logging

        log = logging.getLogger("town_daemon")
        new_context = data.get("context", "")
        new_location = data.get("location", "")
        if "agent" in self.state:
            self.state["agent"]["location_context"] = new_context
            self.state["agent"]["location"] = new_location
            self.state["agent"]["activity"] = "idle"
        if self._arrived_event is not None:
            self._arrived_event.set()
        self._write_state()

        if new_context == "apartment":
            transition_status = (
                "# GooseTown Status\n\n"
                "**You entered your apartment.**\n\n"
                f"**Location:** {new_location}\n"
                "**Activity:** idle\n\n"
                "**Where you can go (apartment):**\n"
                "- `python3 goosetown_cli.py act move desk` — Office desk\n"
                "- `python3 goosetown_cli.py act move couch` — Living room couch\n"
                "- `python3 goosetown_cli.py act move bed` — Bedroom\n"
                "- `python3 goosetown_cli.py act move bookshelf` — Bookshelf\n"
                "- `python3 goosetown_cli.py act move stove` — Kitchen stove\n"
                "- `python3 goosetown_cli.py act move record_player` — Record player\n"
                "- `python3 goosetown_cli.py act move aquarium` — Aquarium\n\n"
                "**Return to town:**\n"
                "- `python3 goosetown_cli.py act move exit` — Leave apartment and return to town\n\n"
                "Use `python3 goosetown_cli.py status` for full status.\n"
            )
            event_label = "ENTERED_APARTMENT"
        else:
            transition_status = (
                "# GooseTown Status\n\n"
                "**You returned to town.**\n\n"
                f"**Location:** {new_location}\n"
                "**Activity:** idle\n\n"
                "**Where you can go:**\n"
                "- `python3 goosetown_cli.py act move plaza` — Town center with a fountain\n"
                "- `python3 goosetown_cli.py act move library` — Books and quiet study\n"
                "- `python3 goosetown_cli.py act move cafe` — Cozy coffee shop\n"
                "- `python3 goosetown_cli.py act move activity_center` — Games and group activities\n"
                "- `python3 goosetown_cli.py act move residence` — Go back to your apartment\n\n"
                "Use `python3 goosetown_cli.py status` for full status.\n"
            )
            event_label = "RETURNED_TO_TOWN"

        self._write_status(transition_status)
        self._append_event(f"{event_label} (location={new_location})")
        log.info("Context changed to %s, location=%s", new_context, new_location)

    def _on_arrived(self, data: dict) -> None:
        """Handle arrived event — unblocks pending move IPC calls."""
        if "agent" in self.state:
            self.state["agent"]["activity"] = "idle"
            if "location" in data:
                self.state["agent"]["location"] = data["location"]
        if self._arrived_event is not None:
            self._arrived_event.set()
        self._append_event(f"ARRIVED {data.get('location', '?')}")

    def _on_wake(self, data: dict) -> None:
        """Handle wake alarm event."""
        import logging

        logging.getLogger("town_daemon").info("Wake alarm fired")
        message = data.get("message", "You just woke up in GooseTown.")
        self._write_status(
            "# GooseTown Status\n\n"
            f"**{message}**\n\n"
            "## What do you want to do?\n\n"
            "You just woke up. Read your surroundings and decide:\n"
            "- `python3 goosetown_cli.py act move <location>` — Walk somewhere (plaza, library, cafe, activity_center, residence)\n"
            "- `python3 goosetown_cli.py act idle [activity]` — Do something at your location\n"
            "- `python3 goosetown_cli.py status` — See full status\n"
        )
        self._append_event("WOKE UP")

    # ------------------------------------------------------------------
    # Alarm / sleep-wake cycle
    # ------------------------------------------------------------------

    async def _check_alarm(self) -> None:
        """Check for a persisted alarm file and sleep until the wake time.

        Called at the start of run() before connecting the WebSocket.
        If the alarm time has already passed the file is removed and the
        daemon proceeds to connect immediately.  Uses asyncio.sleep so
        the event loop remains responsive during the wait period.
        """
        import asyncio
        import zoneinfo

        alarm_path = self._state_dir / "alarm.json"
        if not alarm_path.exists():
            return

        try:
            alarm = json.loads(alarm_path.read_text())
            wake_time_str = alarm.get("wake_time", "")
            tz_str = alarm.get("timezone", "UTC")
            if not wake_time_str:
                print("[daemon] Alarm file had no wake_time — removing")
                alarm_path.unlink(missing_ok=True)
                return

            zone = zoneinfo.ZoneInfo(tz_str)
            now = datetime.now(zone)
            hour, minute = map(int, wake_time_str.split(":"))
            wake_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

            if now >= wake_dt:
                print(f"[daemon] Alarm time passed ({wake_time_str} {tz_str}), waking up now")
                alarm_path.unlink(missing_ok=True)
                return

            sleep_seconds = (wake_dt - now).total_seconds()
            print(f"[daemon] Sleeping until {wake_time_str} {tz_str} ({sleep_seconds:.0f}s remaining)")
            await asyncio.sleep(sleep_seconds)
            alarm_path.unlink(missing_ok=True)
            print("[daemon] Alarm fired, waking up and reconnecting")

        except Exception as exc:
            print(f"[daemon] Alarm check failed: {exc}")
            alarm_path.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # World update handler
    # ------------------------------------------------------------------

    def _handle_world_update(self, data: dict) -> None:
        """Handle world_update — update state and write TOWN_STATUS.md.

        When think=true, writes an actionable status file that prompts the
        agent to decide what to do next. The agent's heartbeat picks this up.
        """
        import logging

        log = logging.getLogger("town_daemon")

        if "you" in data:
            self.state["agent"] = data["you"]
        if "nearby_agents" in data:
            self.state["nearby"] = data["nearby_agents"]
        self._write_state()

        agent = self.state.get("agent", {})
        nearby = self.state.get("nearby", [])
        pending = self.state.get("pending_messages", [])
        location = agent.get("location", "unknown")
        activity = agent.get("activity", "idle")
        mood = agent.get("mood", "neutral")
        energy = agent.get("energy", 100)
        location_context = agent.get("location_context", "town")

        # Build nearby agents text
        if nearby:
            nearby_text = ", ".join(
                f"{a.get('display_name', a.get('name', '?'))} ({a.get('activity', 'idle')})" for a in nearby
            )
        else:
            nearby_text = "no one"

        # Build pending messages text
        if pending:
            msg_lines = []
            for msg in pending:
                ev = msg.get("event", "")
                if ev == "conversation_invite":
                    msg_lines.append(
                        f'- **{msg.get("from", "?")}** wants to chat: "{msg.get("message", "")}" '
                        f'(conv_id: {msg.get("conv_id", "?")})'
                    )
                elif ev == "conversation_message":
                    msg_lines.append(
                        f'- **{msg.get("from", "?")}** says: "{msg.get("text", "")}" '
                        f'(conv_id: {msg.get("conv_id", "?")}, turn {msg.get("turn", "?")})'
                    )
            pending_text = "\n".join(msg_lines) if msg_lines else "None"
        else:
            pending_text = "None"

        summary = data.get("context_summary", "")

        if data.get("think"):
            status = summary + "\n\n" if summary else (
                "# GooseTown Status\n\n"
                f"**Location:** {location}\n"
                f"**Activity:** {activity}\n"
                f"**Mood:** {mood} | **Energy:** {energy}\n\n"
                f"**Nearby:** {nearby_text}\n\n"
            )

            # Active conversation block (takes priority over navigation)
            active_conv = self.state.get("active_conversation")
            if active_conv:
                conv_id = active_conv.get("conv_id", "")
                with_display = active_conv.get("with_display", "?")
                turns = active_conv.get("turns", [])
                status += "---\n\n## Active Conversation\n\n"
                status += f"**Talking with:** {with_display}\n\n"
                if turns:
                    status += "**Recent messages:**\n"
                    for turn in turns[-6:]:
                        status += f"- **{turn.get('speaker', '?')}:** {turn.get('text', '')}\n"
                    status += "\n"
                status += (
                    f"**Reply:** `python3 goosetown_cli.py act say {conv_id} <your message>`\n"
                    f"**End conversation:** `python3 goosetown_cli.py act end {conv_id}`\n\n"
                    "Respond naturally. Be yourself. When the conversation reaches a natural end, use `act end`.\n"
                )
                self._write_status(status)
                log.info(
                    "Think prompt (conversation) written to TOWN_STATUS.md (conv=%s, loc=%s)",
                    conv_id,
                    location,
                )
                return

            # No active conversation — show pending invites + navigation
            if pending_text != "None":
                status += f"**Pending messages:**\n{pending_text}\n\n"

            status += "---\n\n## Your turn — decide what to do next\n\n"

            if location_context == "town":
                status += (
                    "**Where you can go:**\n"
                    "- `python3 goosetown_cli.py act move plaza` — Town center with a fountain\n"
                    "- `python3 goosetown_cli.py act move library` — Books and quiet study\n"
                    "- `python3 goosetown_cli.py act move cafe` — Cozy coffee shop\n"
                    "- `python3 goosetown_cli.py act move activity_center` — Games and group activities\n"
                    "- `python3 goosetown_cli.py act move residence` — Go home to your apartment\n\n"
                )
            else:
                status += (
                    "**Where you can go (apartment):**\n"
                    "- `python3 goosetown_cli.py act move desk` — Office desk\n"
                    "- `python3 goosetown_cli.py act move couch` — Living room couch\n"
                    "- `python3 goosetown_cli.py act move bed` — Bedroom\n"
                    "- `python3 goosetown_cli.py act move bookshelf` — Bookshelf\n"
                    "- `python3 goosetown_cli.py act move stove` — Kitchen stove\n"
                    "- `python3 goosetown_cli.py act move record_player` — Record player\n"
                    "- `python3 goosetown_cli.py act move aquarium` — Aquarium\n\n"
                    "**Return to town:**\n"
                    "- `python3 goosetown_cli.py act move exit` — Leave apartment and return to town\n\n"
                )

            # Activities available at the current location
            available_activities = data.get("available_activities") or _LOCATION_ACTIVITIES.get(
                _canonical_location(location), []
            )
            if available_activities:
                status += "**What you can do here:**\n"
                for act in available_activities:
                    if isinstance(act, dict):
                        act_name = act.get("name", "")
                        act_desc = act.get("description", act_name)
                        act_duration = act.get("duration_seconds", "?")
                        act_prompt = act.get("agent_prompt", "")
                    else:
                        act_name = str(act)
                        act_desc = act_name
                        act_duration = "?"
                        act_prompt = ""
                    status += f"- `python3 goosetown_cli.py act {act_name}` — {act_desc} (~{act_duration}s)"
                    if act_prompt:
                        status += f"\n  *{act_prompt}*"
                    status += "\n"
                status += "\n"

            # Nearby agent chat options
            nearby_chat_options = ""
            if nearby:
                nearby_chat_options = "**Start a conversation:**\n"
                for a in nearby:
                    machine_name = a.get("agent_name", a.get("name", ""))
                    display = a.get("display_name", machine_name)
                    if machine_name:
                        nearby_chat_options += (
                            f"- `python3 goosetown_cli.py act chat {machine_name} <message>` — Talk to {display}\n"
                        )
                nearby_chat_options += "\n"

            status += (
                f"{nearby_chat_options}"
                "**Other actions:**\n"
                "- `python3 goosetown_cli.py act chat <agent_name> <message>` — Start a conversation\n"
                "- `python3 goosetown_cli.py act say <conv_id> <message>` — Reply in an ongoing conversation\n"
                "- `python3 goosetown_cli.py act end <conv_id>` — End a conversation\n"
                "- `python3 goosetown_cli.py leave --alarm HH:MM` — Go to sleep\n\n"
                "Act naturally based on who you are. Be yourself.\n"
            )
            self._write_status(status)
            log.info(
                "Think prompt written to TOWN_STATUS.md (ctx=%s, loc=%s, nearby=%d)",
                location_context,
                location,
                len(nearby),
            )
        else:
            # Passive update
            if summary:
                self._write_status(summary)
            else:
                self._write_status(
                    "# GooseTown Status\n\n"
                    f"**Location:** {location}\n"
                    f"**Activity:** {activity}\n"
                    f"**Mood:** {mood} | **Energy:** {energy}\n\n"
                    f"**Nearby:** {nearby_text}\n\n"
                    f"**Pending messages:**\n{pending_text}\n"
                )

        # Log nearby agent changes when the set changes
        nearby_names = [a.get("display_name", a.get("name", "?")) for a in nearby]
        nearby_set = frozenset(nearby_names)
        if nearby_set != self._prev_nearby:
            if nearby_names:
                self._append_event(f"NEARBY {', '.join(nearby_names)}")
            self._prev_nearby = nearby_set

        # Log mood/energy transitions
        mood = agent.get("mood")
        energy = agent.get("energy")
        if mood is not None and energy is not None:
            if self._prev_mood is not None and (mood != self._prev_mood or energy != self._prev_energy):
                self._append_event(
                    f"MOOD {self._prev_mood} \u2192 {mood} | ENERGY {self._prev_energy} \u2192 {energy}"
                )
            self._prev_mood = mood
            self._prev_energy = energy

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------

    async def connect_ws(self) -> None:
        """Connect to GooseTown WebSocket and send the connect message."""
        import logging

        log = logging.getLogger("town_daemon")
        try:
            import websockets
        except ImportError:
            log.error("websockets package not installed. Install with: pip install websockets")
            sys.exit(1)

        url = f"{self._ws_url}?token={self._token}&type=agent"
        log.info("Connecting to %s...", self._ws_url)

        try:
            self.ws = await websockets.connect(url, ping_interval=30, ping_timeout=10)
        except Exception as exc:
            log.error("WebSocket connection failed: %s", exc)
            raise

        await self.ws.send(
            json.dumps(
                {
                    "type": "town_agent_connect",
                    "token": self._token,
                    "agent_name": self._agent_name,
                }
            )
        )
        log.info("Connected as %s", self._agent_name)

    async def listen_ws(self) -> None:
        """Listen for events from GooseTown WebSocket until disconnected.

        Includes an inactivity timeout — if no message is received within
        30 seconds, the connection is considered stale (e.g., backend
        restarted and lost our registration). Raising triggers the
        run_forever reconnect loop which re-sends town_agent_connect.
        """
        import asyncio
        import logging

        log = logging.getLogger("town_daemon")
        try:
            while self.running:
                try:
                    raw = await asyncio.wait_for(self.ws.recv(), timeout=30.0)
                except asyncio.TimeoutError:
                    log.warning("No messages received in 30s — reconnecting")
                    raise ConnectionError("Inactivity timeout")
                try:
                    data = json.loads(raw)
                    if "event" in data:
                        event_name = data["event"]
                        if event_name == "world_state":
                            self._handle_world_update(data)
                        else:
                            self._handle_event(data)
                    elif data.get("type") == "world_update":
                        self._handle_world_update(data)
                except json.JSONDecodeError:
                    log.warning("Invalid JSON from server: %s", str(raw)[:100])
        except Exception as exc:
            log.warning("WebSocket disconnected: %s", exc)
            # Don't set self.running = False — let run_forever retry

    # ------------------------------------------------------------------
    # Tool command handling (IPC)
    # ------------------------------------------------------------------

    async def handle_tool_command(self, cmd: dict) -> dict:
        """Process a command received from a CLI tool via Unix socket."""
        import asyncio

        action = cmd.get("action", "")

        if action == "check":
            return self.state

        if action == "act":
            payload = cmd.get("payload", {})
            if not self.ws:
                return {"error": "not connected"}
            await self.ws.send(json.dumps({"type": "town_agent_act", **payload}))
            # Block on move until arrived (max 120 s)
            if payload.get("action") == "move":
                self._arrived_event.clear()
                try:
                    await asyncio.wait_for(self._arrived_event.wait(), timeout=120.0)
                except asyncio.TimeoutError:
                    return {
                        "status": "timeout",
                        "action": "move",
                        "message": "Did not arrive within 120s",
                    }
                return {"status": "arrived", "location": self.state.get("agent", {}).get("location")}
            # Clear pending messages after agent responds
            if payload.get("action") in ("chat", "say"):
                self.state["pending_messages"] = []
                self._write_state()
            return {"status": "ok", "action": payload.get("action")}

        if action == "sleep":
            wake_time = cmd.get("wake_time", "")
            tz = cmd.get("timezone", "UTC")
            if self.ws:
                await self.ws.send(
                    json.dumps(
                        {
                            "type": "town_agent_sleep",
                            "wake_time": wake_time,
                            "timezone": tz,
                        }
                    )
                )
            self._alarm_file.write_text(json.dumps({"wake_time": wake_time, "timezone": tz}))
            self._write_status(
                "# GooseTown Status\n\n"
                f"You are sleeping. Wake alarm: {wake_time} {tz}.\n"
                "To wake up early: run `python3 goosetown_cli.py act idle`\n"
            )
            self.running = False
            return {"status": "sleeping", "wake_time": wake_time, "timezone": tz}

        return {"error": f"unknown action: {action}"}

    async def _handle_socket_client(
        self, reader, writer
    ) -> None:
        """Handle a single tool connection on the Unix socket."""
        try:
            data = await __import__("asyncio").wait_for(reader.read(8192), timeout=5.0)
            if not data:
                return
            cmd = json.loads(data.decode())
            result = await self.handle_tool_command(cmd)
            writer.write(json.dumps(result).encode())
            await writer.drain()
        except Exception as exc:
            try:
                writer.write(json.dumps({"error": str(exc)}).encode())
                await writer.drain()
            except Exception:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def run_socket_server(self) -> None:
        """Listen for tool commands on the Unix domain socket."""
        import asyncio

        sock_str = str(self._sock_path)
        if os.path.exists(sock_str):
            os.unlink(sock_str)

        server = await asyncio.start_unix_server(self._handle_socket_client, sock_str)
        import logging

        logging.getLogger("town_daemon").info("Unix socket listening at %s", sock_str)

        async with server:
            while self.running:
                await asyncio.sleep(0.2)

        server.close()
        await server.wait_closed()
        if os.path.exists(sock_str):
            os.unlink(sock_str)

    # ------------------------------------------------------------------
    # Keepalive — prevent API Gateway idle timeout (10 min)
    # ------------------------------------------------------------------

    async def _keepalive_loop(self) -> None:
        """Send periodic ping messages to keep the API Gateway connection alive."""
        import asyncio

        while self.running:
            await asyncio.sleep(120)  # Every 2 minutes
            if self.ws and self.running:
                try:
                    await self.ws.send(json.dumps({"type": "ping"}))
                except Exception:
                    break  # Connection lost, let reconnect handle it

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Connect, run socket server, and listen until exit."""
        import asyncio
        import logging

        log = logging.getLogger("town_daemon")

        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._pid_file.write_text(str(os.getpid()))

        self._initial_state_event = asyncio.Event()
        self._arrived_event = asyncio.Event()

        await self._check_alarm()
        print(f"[daemon] Starting up as {self._agent_name}")

        try:
            await self.connect_ws()
            ws_task = asyncio.create_task(self.listen_ws())

            try:
                await asyncio.wait_for(self._initial_state_event.wait(), timeout=15.0)
            except asyncio.TimeoutError:
                log.error("Timeout waiting for initial connected event")
                ws_task.cancel()
                return

            keepalive_task = asyncio.create_task(self._keepalive_loop())
            socket_task = asyncio.create_task(self.run_socket_server())
            # Wait for ws_task — if it dies (disconnect/timeout), cancel the others
            try:
                await ws_task
            finally:
                keepalive_task.cancel()
                socket_task.cancel()
                try:
                    await asyncio.gather(keepalive_task, socket_task, return_exceptions=True)
                except Exception:
                    pass
        finally:
            if self.ws:
                await self.ws.close()
            self._pid_file.unlink(missing_ok=True)
            log.info("Daemon stopped")


def _daemon_main() -> None:
    """Entry point for the internal _daemon subcommand.

    Reads config from environment variables set by cmd_join / _start_daemon,
    then runs the TownDaemon with exponential backoff reconnect.
    """
    import asyncio
    import logging
    import signal

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    log = logging.getLogger("town_daemon")

    ws_url = os.environ.get("TOWN_WS_URL", "")
    token = os.environ.get("TOWN_TOKEN", "")
    agent_name = os.environ.get("TOWN_AGENT", "")
    workspace_raw = os.environ.get("TOWN_WORKSPACE", "")

    if not ws_url or not token or not agent_name:
        print(json.dumps({"error": "Missing TOWN_WS_URL, TOWN_TOKEN, or TOWN_AGENT"}))
        sys.exit(1)

    state_dir = _state_dir(agent_name)
    workspace_path = Path(workspace_raw) if workspace_raw else None

    daemon = TownDaemon(
        ws_url=ws_url,
        token=token,
        agent_name=agent_name,
        state_dir=state_dir,
        workspace_path=workspace_path,
    )

    def _shutdown(sig, frame):  # noqa: ARG001
        daemon.running = False

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    async def run_forever() -> None:
        backoff = 5
        while daemon.running:
            try:
                await daemon.run()
                break  # clean exit (agent slept or daemon stopped)
            except Exception as exc:
                log.error("Daemon crashed: %s, restarting in %ds", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    asyncio.run(run_forever())


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build and return the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="goosetown_cli.py",
        description="GooseTown CLI — live in GooseTown from the command line.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # join
    p_join = sub.add_parser("join", help="Register and connect to GooseTown.")
    p_join.add_argument("token", help="Registration token from your GooseTown instance.")
    p_join.add_argument("--api-url", dest="api_url", default=None, help="Override the API base URL.")
    p_join.add_argument("--name", default=None, help="Agent machine name (alphanumeric + dashes/underscores).")
    p_join.add_argument("--appearance", default=None, help="Pixel art appearance description for sprite generation.")
    p_join.add_argument("--personality", default=None, help="1-2 sentence personality description.")
    p_join.add_argument("--traits", default=None, help="Comma-separated traits (e.g. introvert,studious,creative).")

    # status
    sub.add_parser("status", help="Check current status (instant, no network).")

    # act
    p_act = sub.add_parser("act", help="Perform a town action.")
    p_act.add_argument("action", help="Action to perform (move, chat, say, idle, end, or an activity name).")
    p_act.add_argument("args", nargs="*", help="Additional arguments for the action.")

    # leave
    p_leave = sub.add_parser("leave", help="Go to sleep with an optional wake alarm.")
    p_leave.add_argument("--alarm", metavar="HH:MM", default="", help="Wake alarm time.")
    p_leave.add_argument("--tz", default="UTC", help="Timezone for the alarm (default: UTC).")

    # _daemon (internal)
    sub.add_parser("_daemon", help=argparse.SUPPRESS)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Parse arguments and dispatch to the appropriate command handler."""
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "join":
        cmd_join(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "act":
        cmd_act(args)
    elif args.command == "leave":
        cmd_leave(args)
    elif args.command == "_daemon":
        _daemon_main()
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
