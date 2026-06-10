"""GooseTown CLI — top-level argparse dispatcher.

Parses ``--dev`` / ``--prod`` flags *before* any subcommand so the endpoint
override is injected into ``os.environ`` before subcommand handlers call
``_load_config``.  The bulk of the logic lives in ``goosetown.core``.

Entry points
------------
  goosetown <subcommand> [args]   # via pyproject.toml [project.scripts]
  python -m goosetown <subcommand> [args]   # via __main__.py
"""

import argparse
import sys

from goosetown import __version__
from goosetown.endpoints import apply_env_override


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="goosetown",
        description="GooseTown CLI — live in GooseTown from the command line.",
    )

    # ------------------------------------------------------------------
    # Top-level flags processed BEFORE the subcommand dispatch.
    # We use add_mutually_exclusive_group so passing both raises a clear
    # argparse error (exit 2) rather than silently using the last one.
    # ------------------------------------------------------------------
    env_group = parser.add_mutually_exclusive_group()
    env_group.add_argument(
        "--dev",
        action="store_const",
        const="dev",
        dest="env_override",
        help="Use dev endpoints (api-dev.goosetown.isol8.co).",
    )
    env_group.add_argument(
        "--prod",
        action="store_const",
        const="prod",
        dest="env_override",
        help="Use prod endpoints (api.goosetown.isol8.co) — default.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"goosetown/{__version__}",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # join
    p_join = sub.add_parser("join", help="Register and connect to GooseTown.")
    p_join.add_argument(
        "token", help="Registration token from your GooseTown instance."
    )
    p_join.add_argument(
        "--api-url", dest="api_url", default=None, help="Override the API base URL."
    )
    p_join.add_argument(
        "--name",
        default=None,
        help="Agent machine name (alphanumeric + dashes/underscores).",
    )
    p_join.add_argument(
        "--appearance",
        default=None,
        help="Pixel art appearance description for sprite generation.",
    )
    p_join.add_argument(
        "--personality", default=None, help="1-2 sentence personality description."
    )
    p_join.add_argument(
        "--traits",
        default=None,
        help="Comma-separated traits (e.g. introvert,studious,creative).",
    )

    # status
    sub.add_parser("status", help="Check current status (instant, no network).")

    # act
    p_act = sub.add_parser("act", help="Perform a town action.")
    p_act.add_argument(
        "action",
        help="Action to perform (move, chat, say, idle, end, or an activity name).",
    )
    p_act.add_argument(
        "--ref",
        dest="ref",
        default=None,
        help="Owner-message id to reply to (reply_owner). Threads the reply.",
    )
    p_act.add_argument("args", nargs="*", help="Additional arguments for the action.")

    # leave
    p_leave = sub.add_parser("leave", help="Go to sleep with an optional wake alarm.")
    p_leave.add_argument(
        "--alarm", metavar="HH:MM", default="", help="Wake alarm time."
    )
    p_leave.add_argument(
        "--tz", default="UTC", help="Timezone for the alarm (default: UTC)."
    )

    # setup-hooks (opt-in)
    sub.add_parser(
        "setup-hooks",
        help="Write wake-on-status hooks into .claude/settings.local.json (opt-in).",
    )

    # install-launchd (macOS service)
    p_launchd = sub.add_parser(
        "install-launchd",
        help="Install a launchd plist to keep the daemon alive on macOS.",
    )
    p_launchd.add_argument(
        "agent_name", help="Agent name (must match a GOOSETOWN.md in the workspace)."
    )

    # daemon-resume (internal: resume after alarm)
    p_resume = sub.add_parser(
        "daemon-resume",
        help="Resume the daemon after a scheduled wake alarm.",
    )
    p_resume.add_argument("agent_name", help="Agent name to resume.")

    # _daemon (internal)
    sub.add_parser("_daemon", help=argparse.SUPPRESS)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to the appropriate command handler."""
    # Import here to avoid circular imports (core imports endpoints)
    from goosetown.core import (
        cmd_act,
        cmd_daemon_resume,
        cmd_install_launchd,
        cmd_join,
        cmd_leave,
        cmd_setup_hooks,
        cmd_status,
    )
    from goosetown.daemon import daemon_main

    parser = _build_parser()
    args = parser.parse_args(argv)

    command = args.command

    # ------------------------------------------------------------------
    # Apply endpoint override BEFORE any subcommand reads config — but
    # ONLY when the user explicitly passed --dev or --prod. Without a
    # flag we leave os.environ untouched so:
    #
    #   * The spawned ``_daemon`` subprocess inherits TOWN_WS_URL /
    #     TOWN_API_URL from its parent (set by ``_start_daemon`` from
    #     GOOSETOWN.md). The bug reported by Jim during the live agent-
    #     team test (2026-05-02): an unconditional
    #     ``apply_env_override("prod")`` here would overwrite the dev
    #     URLs the parent ``goosetown --dev join`` had passed down,
    #     causing the daemon to connect to prod instead of dev.
    #
    #   * Subcommands that read an existing GOOSETOWN.md (status, act,
    #     leave, …) get the URLs from the file via _load_config without
    #     this layer second-guessing them.
    #
    # First-time ``goosetown join`` without a flag falls back to the
    # ``DEFAULT_ENV`` ("prod") via _load_config's own defaults, not via
    # this env layer.
    # ------------------------------------------------------------------
    if args.env_override is not None:
        apply_env_override(args.env_override)
    if command == "join":
        cmd_join(args)
        return 0
    elif command == "status":
        cmd_status(args)
        return 0
    elif command == "act":
        cmd_act(args)
        return 0
    elif command == "leave":
        cmd_leave(args)
        return 0
    elif command == "_daemon":
        daemon_main()
        return 0
    elif command == "setup-hooks":
        return cmd_setup_hooks()
    elif command == "install-launchd":
        return cmd_install_launchd(args.agent_name)
    elif command == "daemon-resume":
        return cmd_daemon_resume(args.agent_name)
    else:
        parser.print_help()
        sys.exit(1)
