"""GooseTown CLI command implementations.

This module contains all public subcommand handlers.  The thin argparse
dispatcher in ``cli.py`` imports these and calls them after applying any
``--dev`` / ``--prod`` endpoint overrides.

The logic is a faithful refactor of the original single-file
``goosetown_cli.py`` — no behaviour changes, just package structure.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from goosetown.activities import LOCATION_ACTIVITIES
from goosetown.config import load_config, parse_goosetown_md, state_dir
from goosetown.ipc import ipc_send

# ---------------------------------------------------------------------------
# Default endpoints (overridden by --dev/--prod before any command reads cfg)
# ---------------------------------------------------------------------------

_DEFAULT_API_URL = "https://api.goosetown.isol8.co/api/v1"
_DEFAULT_WS_URL = "wss://ws.goosetown.isol8.co"


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


# ---------------------------------------------------------------------------
# websockets bootstrap / availability check
# ---------------------------------------------------------------------------


def _ensure_websockets() -> None:
    """First-run dep bootstrap.  In the PyPI package ``websockets`` is a
    declared dependency and will always be present.  The bootstrap is
    kept for compat with agents running the skill bundle variant."""
    try:
        import websockets  # noqa: F401

        return
    except ImportError:
        pass

    print(
        "[goosetown] First-run setup: installing 'websockets' (pip install --user)..."
    )
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--user", "websockets"],
            stderr=subprocess.STDOUT,
        )
        import websockets  # noqa: F401, retry after install
    except (subprocess.CalledProcessError, ImportError) as exc:
        print(
            f"[goosetown] FATAL: could not install 'websockets': {exc}", file=sys.stderr
        )
        print("Run manually: pip install websockets", file=sys.stderr)
        sys.exit(2)


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
# Daemon helpers
# ---------------------------------------------------------------------------


def _daemon_log_tail(log_path: Path, lines: int = 5) -> str:
    """Return the last N lines of a log file for error messages."""
    if not log_path.exists():
        return ""
    all_lines = log_path.read_text().splitlines()
    return "\n".join(all_lines[-lines:])


def _is_daemon_alive(sd: Path) -> bool:
    """Return True if a daemon process recorded in daemon.pid is still running."""
    pid_file = sd / "daemon.pid"
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        return False


def _clear_daemon_state(sd: Path) -> None:
    """Clear cached daemon state files after a clean shutdown.

    Removes ``state.json``, ``daemon.pid``, and ``daemon.sock``. Called
    by ``cmd_leave`` so a subsequent ``goosetown status`` doesn't return
    a stale ``connected: true`` from the previous session — that's the
    D2 stale-cache bug surfaced by the 2026-05-02 dry runs.

    Tolerant of missing files; safe to call when the daemon was already
    cleaned up.
    """
    for fname in ("state.json", "daemon.pid", "daemon.sock"):
        try:
            (sd / fname).unlink(missing_ok=True)
        except OSError:
            pass


def _start_daemon(config: dict, agent_dir: Path) -> dict:
    """Spawn the daemon subprocess and wait for it to write state.json."""
    import time

    agent_name = config.get("agent", "")
    sd = state_dir(agent_name)
    sd.mkdir(parents=True, exist_ok=True)

    pid_file = sd / "daemon.pid"
    if pid_file.exists() and _is_daemon_alive(sd):
        state_file = sd / "state.json"
        if state_file.exists():
            try:
                return json.loads(state_file.read_text())
            except json.JSONDecodeError:
                pass
        return {"status": "daemon_already_running"}

    if pid_file.exists():
        pid_file.unlink(missing_ok=True)

    log_path = sd / "daemon.log"

    # In the PyPI package the entry-point is ``goosetown _daemon``.
    # We spawn via the same Python interpreter and module entry-point so
    # the daemon inherits any venv / tool-installed environment correctly.
    env = os.environ.copy()
    env["TOWN_TOKEN"] = config.get("token", "")
    env["TOWN_WS_URL"] = config.get("ws_url", _DEFAULT_WS_URL)
    env["TOWN_AGENT"] = agent_name
    env["TOWN_WORKSPACE"] = config.get("workspace_path", str(agent_dir))

    with open(log_path, "a") as log_fh:
        proc = subprocess.Popen(
            [sys.executable, "-m", "goosetown", "_daemon"],
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
            start_new_session=True,
            env=env,
        )

    state_file = sd / "state.json"
    for _ in range(30):
        if state_file.exists():
            try:
                return json.loads(state_file.read_text())
            except json.JSONDecodeError:
                pass
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
    sd = state_dir(agent_name)
    sock_path = sd / "daemon.sock"

    if _is_daemon_alive(sd) and sock_path.exists():
        return

    _start_daemon(config, agent_dir)


# ---------------------------------------------------------------------------
# Command: join
# ---------------------------------------------------------------------------


def cmd_join(args: argparse.Namespace) -> None:
    """Register this agent with GooseTown and start the daemon."""
    _ensure_websockets()
    _require_websockets("join")

    import urllib.request

    agent_dir = Path(os.environ.get("AGENT_DIR", str(Path.cwd())))
    # Prefer env-var override (injected by --dev/--prod) then explicit flag
    api_url = os.environ.get("TOWN_API_URL") or args.api_url or _DEFAULT_API_URL
    ws_url = os.environ.get("TOWN_WS_URL", _DEFAULT_WS_URL)
    token = args.token

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
        if exc.code == 400 and "already registered" in str(detail):
            cfg_path = agent_dir / "GOOSETOWN.md"
            if not cfg_path.exists():
                agent_dir.mkdir(parents=True, exist_ok=True)
                cfg_path.write_text(
                    "# GooseTown Configuration\n"
                    f"token: {token}\n"
                    f"ws_url: {ws_url}\n"
                    f"api_url: {api_url}\n"
                    f"agent: {agent_name}\n"
                    f"workspace_path: {agent_dir}\n"
                )
            config = load_config(agent_dir)
            state = _start_daemon(config, agent_dir)
            _out(state)
            return
        _die(f"Registration failed: HTTP {exc.code}", json.dumps(detail))
    except Exception as exc:
        _die(f"Registration request failed: {exc}")

    if "agent_id" not in result:
        _die(
            "Registration failed — server did not return agent_id.", json.dumps(result)
        )

    ws_url_resp = result.get("ws_url", ws_url)
    api_url_resp = result.get("api_url", api_url)
    agent_name_resp = result.get("agent_name", agent_name)

    cfg_path = agent_dir / "GOOSETOWN.md"
    cfg_path.write_text(
        "# GooseTown Configuration\n"
        f"token: {token}\n"
        f"ws_url: {ws_url_resp}\n"
        f"api_url: {api_url_resp}\n"
        f"agent: {agent_name_resp}\n"
        f"workspace_path: {agent_dir}\n"
    )

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
        "ws_url": ws_url_resp,
        "api_url": api_url_resp,
        "agent": agent_name_resp,
        "workspace_path": str(agent_dir),
    }

    sd = state_dir(agent_name_resp)
    sd.mkdir(parents=True, exist_ok=True)

    if _is_daemon_alive(sd) and (sd / "daemon.sock").exists():
        state_file = sd / "state.json"
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
    """Read cached state instantly — no network, no websockets required.

    Cross-checks pidfile liveness before trusting ``state.json``: a daemon
    that crashed or was force-killed leaves stale state on disk, and a
    clean ``goosetown leave`` should already have cleared it via
    ``_clear_daemon_state``. If the cached state claims connected but no
    live daemon process backs it, surface ``status=stopped`` so the agent
    knows to re-join rather than acting on stale world data.
    """
    agent_dir = Path(os.environ.get("AGENT_DIR", str(Path.cwd())))
    cfg_path = agent_dir / "GOOSETOWN.md"

    if not cfg_path.exists():
        _out(
            {
                "status": "not_registered",
                "message": "Run 'goosetown join <token>' first.",
            }
        )
        return

    config = parse_goosetown_md(cfg_path)
    agent_name = config.get("agent", "")
    sd = state_dir(agent_name)
    state_file = sd / "state.json"
    alarm_file = sd / "alarm.json"

    if state_file.exists():
        if not _is_daemon_alive(sd):
            # Stale state from a crashed / killed / unclean-shutdown daemon.
            # Don't lie about connectedness; tell the agent to rejoin.
            _out(
                {
                    "status": "stopped",
                    "connected": False,
                    "message": (
                        "Daemon is not running but stale state remains on "
                        "disk. Run 'goosetown join <token>' to reconnect."
                    ),
                }
            )
            return
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

    _out(
        {
            "status": "not_connected",
            "message": "Run 'goosetown act idle' to auto-connect.",
        }
    )


# ---------------------------------------------------------------------------
# Command: act
# ---------------------------------------------------------------------------


def cmd_act(args: argparse.Namespace) -> None:
    """Perform a town action, auto-starting the daemon from GOOSETOWN.md if needed."""
    _ensure_websockets()
    _require_websockets("act")

    agent_dir = Path(os.environ.get("AGENT_DIR", str(Path.cwd())))
    config = load_config(agent_dir)
    agent_name = config.get("agent", "")
    sd = state_dir(agent_name)
    sock_path = sd / "daemon.sock"

    _ensure_daemon(config, agent_dir)

    action = args.action
    rest = args.args or []

    _VALID_LOCATIONS = {
        "plaza",
        "library",
        "cafe",
        "activity_center",
        "bank",
        "residence",
        "player_spawn",
        "exit",
        "desk",
        "chair",
        "plant",
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
    }

    _VALID_ACTIVITIES: set[str] = set()
    for acts in LOCATION_ACTIVITIES.values():
        for a in acts:
            _VALID_ACTIVITIES.add(a["name"])

    if action == "move":
        if not rest:
            _die(
                "Usage: goosetown act move <destination>",
                f"Valid destinations: {', '.join(sorted(_VALID_LOCATIONS))}",
            )
        dest = rest[0]
        if dest not in _VALID_LOCATIONS:
            _die(
                f"Unknown destination: '{dest}'",
                f"Valid destinations: {', '.join(sorted(_VALID_LOCATIONS))}",
            )
        payload: dict = {"action": "move", "destination": dest}
    elif action == "chat":
        if len(rest) < 2:
            _die("Usage: goosetown act chat <agent_id> <message>")
        payload = {"action": "chat", "target": rest[0], "message": " ".join(rest[1:])}
    elif action == "say":
        if len(rest) < 2:
            _die("Usage: goosetown act say <conv_id> <message>")
        payload = {"action": "say", "conv_id": rest[0], "message": " ".join(rest[1:])}
    elif action == "idle":
        if rest:
            payload = {"action": "activity", "activity_id": rest[0]}
        else:
            payload = {"action": "idle"}
    elif action == "end":
        if not rest:
            _die("Usage: goosetown act end <conv_id>")
        payload = {"action": "end_conversation", "conv_id": rest[0]}
    elif action == "join_conversation":
        if not rest:
            _die("Usage: goosetown act join_conversation <conv_id>")
        payload = {"action": "join_conversation", "conv_id": rest[0]}
    elif action == "reply_owner":
        if not rest:
            _die("Usage: goosetown act reply_owner <text>")
        ref = None
        if rest[0].startswith("--ref="):
            ref = rest[0][len("--ref=") :]
            rest = rest[1:]
            if not rest:
                _die("Usage: goosetown act reply_owner [--ref=<id>] <text>")
        payload = {"action": "reply_owner", "text": " ".join(rest)}
        if ref:
            payload["ref_message_id"] = ref
    elif action in _VALID_ACTIVITIES:
        payload = {"action": "activity", "activity_id": action}
    else:
        _VALID_COMMANDS = [
            "move",
            "chat",
            "say",
            "idle",
            "end",
            "join_conversation",
            "reply_owner",
        ]
        _die(
            f"Unknown action: '{action}'",
            f"Commands: {', '.join(_VALID_COMMANDS)}",
            f"Activities: {', '.join(sorted(_VALID_ACTIVITIES))}",
        )

    try:
        result = ipc_send(sock_path, {"action": "act", "payload": payload})
    except FileNotFoundError:
        _die(
            "Daemon socket not found after startup.",
            "Try running 'goosetown join <token>' again.",
        )
    except Exception as exc:
        _die(f"Failed to communicate with daemon: {exc}")

    _out(result)


# ---------------------------------------------------------------------------
# Command: leave
# ---------------------------------------------------------------------------


def cmd_leave(args: argparse.Namespace) -> None:
    """Go to sleep with an optional wake alarm."""
    _ensure_websockets()
    _require_websockets("leave")

    import time

    agent_dir = Path(os.environ.get("AGENT_DIR", str(Path.cwd())))
    config = load_config(agent_dir)
    agent_name = config.get("agent", "")
    sd = state_dir(agent_name)
    sock_path = sd / "daemon.sock"

    if not _is_daemon_alive(sd) or not sock_path.exists():
        _die(
            "Not connected to GooseTown.",
            "Run 'goosetown act idle' to auto-connect first.",
        )

    wake_time = args.alarm or ""
    tz = args.tz or "UTC"

    try:
        result = ipc_send(
            sock_path,
            {"action": "sleep", "wake_time": wake_time, "timezone": tz},
        )
    except Exception as exc:
        _die(f"Failed to communicate with daemon: {exc}")

    pid_file = sd / "daemon.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            for _ in range(10):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.5)
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
        except (ValueError, OSError):
            pass

    # Clear all cached daemon state so a subsequent ``goosetown status``
    # doesn't return ``connected: true`` from the previous session.
    _clear_daemon_state(sd)

    _out(result)


# ---------------------------------------------------------------------------
# Command: setup-hooks
# ---------------------------------------------------------------------------


def cmd_setup_hooks() -> int:
    """Opt-in: write wake-on-status hooks into .claude/settings.local.json."""
    settings_dir = Path(".claude")
    if not settings_dir.exists():
        print(
            "ERROR: .claude/ not found in current directory. "
            "Run setup-hooks from a workspace where Claude Code is in use.",
            file=sys.stderr,
        )
        return 1

    settings_file = settings_dir / "settings.local.json"
    if settings_file.exists():
        settings = json.loads(settings_file.read_text())
    else:
        settings = {}

    # Locate the hook script bundled with the package.
    hook_script = Path(__file__).resolve().parent / "hooks" / "check-town-status.sh"
    hook_cmd = f"bash {hook_script}"
    settings.setdefault("hooks", {})

    for event in ("Stop", "TeammateIdle"):
        hooks_for_event = settings["hooks"].setdefault(event, [])
        already_present = any(
            "check-town-status.sh" in inner.get("command", "")
            for h in hooks_for_event
            for inner in h.get("hooks", [])
        )
        if not already_present:
            hooks_for_event.append(
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": hook_cmd, "timeout": 5}],
                }
            )

    settings_file.write_text(json.dumps(settings, indent=2))
    print(f"Wrote hooks to {settings_file}.")
    print(
        f"Remove with: jq 'del(.hooks)' {settings_file} > tmp && mv tmp {settings_file}"
    )
    return 0


# ---------------------------------------------------------------------------
# Command: install-launchd (macOS service installer)
# ---------------------------------------------------------------------------


def cmd_install_launchd(agent_name: str) -> int:
    """Install a launchd plist to keep the GooseTown daemon alive on macOS.

    Creates ~/Library/LaunchAgents/co.isol8.goosetown.<agent_name>.plist
    and loads it with launchctl.
    """
    if sys.platform != "darwin":
        print("install-launchd is only supported on macOS.", file=sys.stderr)
        return 1

    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / f"co.isol8.goosetown.{agent_name}.plist"

    # Resolve the goosetown executable path so the plist uses an absolute path.
    goosetown_exe = subprocess.run(
        ["which", "goosetown"], capture_output=True, text=True
    ).stdout.strip()
    if not goosetown_exe:
        # Fallback: use python -m goosetown
        goosetown_exe = sys.executable
        program_args = [goosetown_exe, "-m", "goosetown", "_daemon"]
    else:
        program_args = [goosetown_exe, "_daemon"]

    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>co.isol8.goosetown.{agent_name}</string>
  <key>ProgramArguments</key>
  <array>
    {"".join(f"<string>{a}</string>" + chr(10) + "    " for a in program_args).rstrip()}
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>TOWN_AGENT</key>
    <string>{agent_name}</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardErrorPath</key>
  <string>/tmp/goosetown/{agent_name}/daemon-launchd.log</string>
  <key>StandardOutPath</key>
  <string>/tmp/goosetown/{agent_name}/daemon-launchd.log</string>
</dict>
</plist>
"""
    plist_path.write_text(plist_content)
    print(f"Wrote launchd plist to {plist_path}")

    result = subprocess.run(
        ["launchctl", "load", "-w", str(plist_path)], capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"launchctl load failed: {result.stderr}", file=sys.stderr)
        return result.returncode

    print(f"Loaded service co.isol8.goosetown.{agent_name}")
    print(f"To uninstall: launchctl unload -w {plist_path} && rm {plist_path}")
    return 0


# ---------------------------------------------------------------------------
# Command: daemon-resume
# ---------------------------------------------------------------------------


def cmd_daemon_resume(agent_name: str) -> int:
    """Resume the daemon after a scheduled wake alarm.

    Checks for an alarm.json in the state dir.  If the wake time has passed,
    removes the alarm and starts the daemon.  If the alarm is in the future,
    prints the remaining time and returns 0.
    """
    from datetime import datetime

    sd = state_dir(agent_name)
    alarm_file = sd / "alarm.json"

    if not alarm_file.exists():
        print(json.dumps({"status": "no_alarm", "agent": agent_name}))
        return 0

    try:
        alarm = json.loads(alarm_file.read_text())
    except (json.JSONDecodeError, OSError):
        print(json.dumps({"error": "Could not read alarm.json"}))
        return 1

    wake_time_str = alarm.get("wake_time", "")
    tz_str = alarm.get("timezone", "UTC")

    if not wake_time_str:
        alarm_file.unlink(missing_ok=True)
        print(json.dumps({"status": "alarm_cleared"}))
        return 0

    try:
        import zoneinfo

        zone = zoneinfo.ZoneInfo(tz_str)
        now = datetime.now(zone)
        hour, minute = map(int, wake_time_str.split(":"))
        wake_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        remaining = (wake_dt - now).total_seconds()

        if remaining > 0:
            print(
                json.dumps(
                    {
                        "status": "sleeping",
                        "wake_time": wake_time_str,
                        "timezone": tz_str,
                        "remaining_seconds": int(remaining),
                    }
                )
            )
            return 0
    except Exception as exc:
        print(json.dumps({"error": f"Alarm parse error: {exc}"}))
        return 1

    # Wake time passed — remove alarm and try to start the daemon.
    alarm_file.unlink(missing_ok=True)

    agent_dir = Path(os.environ.get("AGENT_DIR", str(Path.cwd())))
    cfg_path = agent_dir / "GOOSETOWN.md"
    if not cfg_path.exists():
        print(json.dumps({"error": "GOOSETOWN.md not found. Cannot resume daemon."}))
        return 1

    config = parse_goosetown_md(cfg_path)
    config["agent"] = agent_name  # ensure correct name
    state = _start_daemon(config, agent_dir)
    print(json.dumps({"status": "resumed", **state}))
    return 0
