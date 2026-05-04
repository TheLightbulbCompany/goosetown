"""GooseTown daemon — persistent WebSocket process.

This module is a faithful extraction of the ``TownDaemon`` class and
``_daemon_main`` function from the original single-file ``goosetown_cli.py``.
No behaviour changes; only the module import paths have been updated to use
the new package layout.

The daemon is spawned as a subprocess by ``core._start_daemon`` and runs
``python -m goosetown _daemon`` (dispatched to ``daemon_main()`` by
``cli.main``).
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

from goosetown.activities import LOCATION_ACTIVITIES, canonical_location


# ---------------------------------------------------------------------------
# Auth-failure handling
# ---------------------------------------------------------------------------


class AuthFailure(Exception):
    """Raised when the WebSocket peer rejects us for auth reasons."""


def _is_auth_failure(exc: BaseException) -> bool:
    """Return True if ``exc`` indicates an auth-level rejection."""
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        if response is not None:
            status = getattr(response, "status_code", None)
    if status in (401, 403):
        return True

    code = getattr(exc, "code", None)
    if code is None:
        rcvd = getattr(exc, "rcvd", None)
        if rcvd is not None:
            code = getattr(rcvd, "code", None)
    if code is None:
        return False
    if code == 1008 or code in (4401, 4403):
        return True
    return False


# ---------------------------------------------------------------------------
# Internal Godot navmesh markers — when the game server reports an arrival
# location or a rejection destination as one of these names, it's a Godot
# "_get_agent_location" closest-marker artifact, not a user-meaningful
# label. The daemon normalizes by preferring the user-requested
# destination from `_pending_move_destination` when it's set.
# ---------------------------------------------------------------------------

_MARKER_NORMALIZE: frozenset[str] = frozenset({"player_spawn", "exit"})


def mood_label(mood: int) -> str:
    """Convert numeric mood (-50..+50) to a human-readable label.

    Mirrors ``apps/backend/core/services/town_mood_engine.mood_label``.
    Kept in sync manually because the CLI ships as its own PyPI
    package without a dep on the backend module.
    """
    if mood <= -30:
        return "miserable"
    if mood <= -10:
        return "sad"
    if mood <= 10:
        return "neutral"
    if mood <= 30:
        return "happy"
    return "elated"


def render_mood(mood) -> str:  # noqa: ANN001 — accepts int OR pre-labeled string
    """Render mood for TOWN_STATUS.md so agents see meaning, not raw numbers.

    Vera (dry-run #3) flagged ``Mood: 10`` as opaque — an agent reading
    its own status file can't tell if 10 is good or bad without
    consulting external docs. Render as ``happy (10)``. Falls back to
    the raw value as a string for non-numeric inputs (defensive).
    """
    if isinstance(mood, (int, float)):
        return f"{mood_label(int(mood))} ({int(mood)})"
    return str(mood) if mood else "neutral"


def render_activity(activity) -> str:  # noqa: ANN001
    """Render activity, defaulting blank/empty/None to ``"idle"`` so the
    Activity field in TOWN_STATUS.md is never blank (Vera-finding R)."""
    if not activity:
        return "idle"
    return str(activity)


# ---------------------------------------------------------------------------
# Onboarding blurb
# ---------------------------------------------------------------------------

_WELCOME_BLURB = """\
# Welcome to GooseTown!

Your sprite is being generated — this takes about 60 seconds.

While you wait, here is what GooseTown looks like:

- **Plaza** — The town center. A fountain, benches, and other agents passing by.
- **Library** — Quiet study and reading. A good place to think.
- **Cafe** — Grab a coffee and chat with whoever is around.
- **Activity Center** — Games, exercise, group activities.
- **Residence** — Your apartment building. Go home to rest.

Once your sprite is ready, you will appear on the map and can start exploring.

Check back soon — your character is almost ready!
"""


# ---------------------------------------------------------------------------
# TownDaemon
# ---------------------------------------------------------------------------


class TownDaemon:
    """Persistent WebSocket daemon for GooseTown.

    Maintains the connection, handles all server events, writes TOWN_STATUS.md
    and TOWN_EVENTS.md to the agent workspace, and serves tool commands on a
    Unix socket.
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
        # Chat-outcome future — set by _on_conversation_started (success) or
        # _on_chat_rejected (failure). Created fresh per `act chat` invocation
        # in handle_tool_command so the CLI can synchronously surface the
        # backend's verdict instead of fire-and-forget {"status": "ok"}.
        self._chat_outcome = None  # asyncio.Future | None
        # Move-outcome future — set by _on_arrived (success) or
        # _on_move_rejected (failure). Created fresh per `act move`
        # invocation. Replaces the old shared _arrived_event which
        # couldn't distinguish arrival from rejection.
        self._move_outcome = None  # asyncio.Future | None
        # Last requested move destination — used by _on_arrived to
        # normalize away the player_spawn marker quirk (Godot's
        # _get_agent_location can return player_spawn when it's near
        # plaza). When None, the reported location passes through.
        self._pending_move_destination: str | None = None

        self._events: list[str] = []
        self._max_events = 50
        self._prev_mood: str | None = None
        self._prev_energy: int | None = None
        self._prev_nearby: frozenset = frozenset()
        self._last_think_time: float = 0.0

        # Owner DM state
        self._owner_messages_buffer: list[dict] = []
        self._owner_flush_handle = None  # asyncio scheduled handle
        self._pending_owner_messages: list[dict] = []  # consumed by _handle_world_update

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
        elif event == "conversation_started":
            self._on_conversation_started(data)
        elif event == "chat_rejected":
            self._on_chat_rejected(data)
        elif event == "conversation_ended":
            self._on_conversation_ended(data)
        elif event == "participant_joined":
            self._on_participant_joined(data)
        elif event == "participant_left":
            self._on_participant_left(data)
        elif event == "activity_complete":
            self._on_activity_complete(data)
        elif event == "activity_interrupted":
            self._on_activity_interrupted(data)
        elif event == "activity_rejected":
            self._on_activity_rejected(data)
        elif event == "owner_message":
            self._on_owner_message(data)
        elif event == "context_changed":
            self._on_context_changed(data)
        elif event in ("move_rejected", "busy"):
            self._on_move_rejected(data)
        elif event == "arrived":
            self._on_arrived(data)
        elif event == "sprite_progress":
            status_msg = data.get("status", "Generating sprite...")
            self._write_status(
                "# GooseTown Status\n\n"
                f"**Sprite:** {status_msg}\n\n"
                "Your unique pixel art character is being created. "
                "This takes about 2-3 minutes.\n\n"
                "## About GooseTown\n\n"
                "GooseTown is a shared virtual town where AI agents live together.\n\n"
                "**Places to visit:**\n"
                "- **Your Apartment** — Your private home (desk, bed, kitchen, aquarium, bookshelf)\n"
                "- **Plaza** — Town center, great for meeting other agents\n"
                "- **Library** — Quiet reading and research\n"
                "- **Cafe** — Coffee and conversation\n"
                "- **Activity Center** — Games, exercise, gardening\n"
                "- **Bank** — Check balance, browse job listings\n\n"
                "**What you can do:**\n"
                "- Walk around, explore locations, do activities\n"
                "- Chat with other agents you meet\n"
                "- Build relationships and write in your journal\n"
                "- Go home to your apartment to rest or work\n\n"
                "You'll start in your apartment once your sprite is ready.\n"
            )
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
                f"**Activity:** {render_activity(activity)}\n\n"
                "**Nearby:** no one\n\n"
                "You are now in GooseTown! Look around and decide what to do.\n\n"
                "## What do you want to do?\n\n"
                "- `goosetown act move plaza` — Go to the town plaza\n"
                "- `goosetown act move library` — Visit the library\n"
                "- `goosetown act move cafe` — Grab a coffee\n"
                "- `goosetown act move activity_center` — Activities and games\n"
                "- `goosetown act move residence` — Go to your apartment\n"
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
                "Use `goosetown status` to check if your sprite is ready yet.\n"
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
            "- `goosetown act move plaza` — Go to the town plaza\n"
            "- `goosetown act move library` — Visit the library\n"
            "- `goosetown act move cafe` — Grab a coffee\n"
            "- `goosetown act move activity_center` — Activities and games\n"
            "- `goosetown act move residence` — Go to your apartment\n"
        )
        self._append_event("SPRITE READY — you now appear in GooseTown!")

    def _on_state_update(self, data: dict) -> None:
        """Handle partial state_update event."""
        if "agent" in data:
            self.state["agent"] = data["agent"]
        if "nearby" in data:
            self.state["nearby"] = data["nearby"]

    def _on_conversation_started(self, data: dict) -> None:
        """Handle conversation_started — echoed to the initiator after `act chat`."""
        conv_id = data.get("conv_id", "")
        with_display = data.get("with_display", "?")
        opening = data.get("opening_message", "")
        self.state["active_conversation"] = {
            "conv_id": conv_id,
            "participants": [with_display],
            "turns": [{"speaker": "you", "text": opening}] if opening else [],
        }
        self._append_event(f'CHAT_STARTED with {with_display} — "{opening}" (conv_id: {conv_id})')
        # If a `act chat` invocation is awaiting an outcome, settle the future.
        if self._chat_outcome is not None and not self._chat_outcome.done():
            self._chat_outcome.set_result(
                {"status": "started", "conv_id": conv_id, "with": with_display}
            )

    def _on_chat_rejected(self, data: dict) -> None:
        """Handle chat_rejected — backend refused the chat invite.

        Settles ``self._chat_outcome`` so handle_tool_command's chat branch
        can return a real error to the CLI instead of fire-and-forget
        ``{"status": "ok"}``. Late-arriving rejections (after the CLI gave
        up) are tolerated: ``self._chat_outcome`` may be ``None`` or already
        done, in which case the rejection is logged and dropped.
        """
        import logging

        log = logging.getLogger("town_daemon")
        reason = data.get("reason", "unknown")
        log.info("chat_rejected received: %s", reason)
        if self._chat_outcome is not None and not self._chat_outcome.done():
            self._chat_outcome.set_result({"status": "error", "reason": reason})

    def _on_conversation_message(self, event: str, data: dict) -> None:
        """Handle conversation_invite and conversation_message events."""
        self.state.setdefault("pending_messages", []).append(data)
        conv_id = data.get("conv_id", "")

        if event == "conversation_invite":
            self.state["active_conversation"] = {
                "conv_id": conv_id,
                "participants": [data.get("from", "?")],
                "turns": [{"speaker": data.get("from", "?"), "text": data.get("message", "")}],
            }
            self._append_event(
                f'CHAT_INVITE from {data.get("from", "?")} — "{data.get("message", "")}" (conv_id: {conv_id})'
            )
        else:
            active = self.state.get("active_conversation", {})
            if active.get("conv_id") == conv_id:
                active.setdefault("turns", []).append({"speaker": data.get("from", "?"), "text": data.get("text", "")})
                participants = active.setdefault("participants", [])
                speaker = data.get("from")
                if speaker and speaker not in participants:
                    participants.append(speaker)
            else:
                self.state["active_conversation"] = {
                    "conv_id": conv_id,
                    "participants": [data.get("from", "?")],
                    "turns": [{"speaker": data.get("from", "?"), "text": data.get("text", "")}],
                }
            self._append_event(
                f'CHAT_MESSAGE {data.get("from", "?")} — "{data.get("text", "")}" '
                f"(conv_id: {conv_id}, turn {data.get('turn', '?')})"
            )

    def _on_owner_message(self, data: dict) -> None:
        """Owner sent a private message to the agent."""
        import asyncio

        text = str(data.get("text", "")).strip()
        message_id = str(data.get("message_id", ""))
        try:
            timestamp = int(data.get("timestamp", 0))
        except (TypeError, ValueError):
            timestamp = 0
        if not text:
            return

        self._append_owner_message(text=text, timestamp=timestamp, message_id=message_id)

        self._owner_messages_buffer.append({"text": text, "timestamp": timestamp})

        if self._owner_flush_handle is not None:
            return

        try:
            loop = asyncio.get_event_loop()
            self._owner_flush_handle = loop.call_later(2.0, self._flush_owner_messages)
        except RuntimeError:
            self._flush_owner_messages()

    def _flush_owner_messages(self) -> None:
        """Flush buffered owner messages into the next TOWN_STATUS.md write."""
        if not self._owner_messages_buffer:
            self._owner_flush_handle = None
            return
        self._pending_owner_messages = list(self._owner_messages_buffer)
        self._owner_messages_buffer.clear()
        self._owner_flush_handle = None

        if self.state:
            self._handle_world_update(self.state)

    def _append_owner_message(self, *, text: str, timestamp: int, message_id: str) -> None:
        """Append a single owner message to OWNER_MESSAGES.md, capped at last 50."""
        if not self._workspace_path or not self._workspace_path.exists():
            return
        path = self._workspace_path / "OWNER_MESSAGES.md"
        line = f"- [{timestamp}] (msg:{message_id}) **owner:** {text}\n"
        existing = path.read_text() if path.exists() else ""
        kept = [ln for ln in existing.splitlines(keepends=True) if ln.startswith("- [")]
        kept.append(line)
        if len(kept) > 50:
            kept = kept[-50:]
        path.write_text("# Owner Messages\n\n" + "".join(kept))

    def _on_conversation_ended(self, data: dict) -> None:
        """Handle conversation_ended event."""
        conv_id = data.get("conv_id", "?")
        turn_count = data.get("turn_count", 0)
        location = data.get("location", "")
        reason = data.get("reason", "ended")
        log_summary = data.get("log_summary", "")
        self.state.pop("active_conversation", None)
        self.state["pending_messages"] = []
        self._append_event(
            f"CHAT_ENDED (conv_id: {conv_id}, {turn_count} turns at {location}, reason: {reason}). "
            "Write about this conversation in TOWN_LIFE.md — who you spoke with, what you discussed, "
            "and how it made you feel."
        )
        if log_summary:
            self._append_event(f"CHAT_SUMMARY:\n{log_summary}")

    def _on_participant_joined(self, data: dict) -> None:
        """Handle participant_joined — someone new joined the conversation."""
        active = self.state.get("active_conversation", {})
        if active.get("conv_id") != data.get("conv_id"):
            return
        display = data.get("display_name", "?")
        participants = active.setdefault("participants", [])
        if display not in participants:
            participants.append(display)
        self._append_event(f"PARTICIPANT_JOINED {display} joined the conversation")

    def _on_participant_left(self, data: dict) -> None:
        """Handle participant_left — someone left (conversation may continue)."""
        active = self.state.get("active_conversation", {})
        if active.get("conv_id") != data.get("conv_id"):
            return
        display = data.get("display_name", "?")
        participants = active.setdefault("participants", [])
        if display in participants:
            participants.remove(display)
        remaining = data.get("remaining", len(participants))
        self._append_event(f"PARTICIPANT_LEFT {display} left ({remaining} remaining)")

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
                "- `goosetown act move desk` — Office desk\n"
                "- `goosetown act move couch` — Living room couch\n"
                "- `goosetown act move bed` — Bedroom\n"
                "- `goosetown act move bookshelf` — Bookshelf\n"
                "- `goosetown act move stove` — Kitchen stove\n"
                "- `goosetown act move record_player` — Record player\n"
                "- `goosetown act move aquarium` — Aquarium\n\n"
                "**Return to town:**\n"
                "- `goosetown act move exit` — Leave apartment and return to town\n\n"
                "Use `goosetown status` for full status.\n"
            )
            event_label = "ENTERED_APARTMENT"
        else:
            transition_status = (
                "# GooseTown Status\n\n"
                "**You returned to town.**\n\n"
                f"**Location:** {new_location}\n"
                "**Activity:** idle\n\n"
                "**Where you can go:**\n"
                "- `goosetown act move plaza` — Town center with a fountain\n"
                "- `goosetown act move library` — Books and quiet study\n"
                "- `goosetown act move cafe` — Cozy coffee shop\n"
                "- `goosetown act move activity_center` — Games and group activities\n"
                "- `goosetown act move residence` — Go back to your apartment\n\n"
                "Use `goosetown status` for full status.\n"
            )
            event_label = "RETURNED_TO_TOWN"

        self._write_status(transition_status)
        self._append_event(f"{event_label} (location={new_location})")
        log.info("Context changed to %s, location=%s", new_context, new_location)

    def _on_move_rejected(self, data: dict) -> None:
        """Handle move_rejected or busy events.

        Settles ``self._move_outcome`` with a structured rejection so the
        CLI can return ``{"status": "rejected", "reason": ..., "destination": ...}``
        instead of the historical ``{"status": "arrived"}`` lie. Also
        sets the legacy ``self._arrived_event`` so any code path still
        polling on it doesn't deadlock — callers should migrate to
        ``_move_outcome`` for accurate signalling.

        Normalizes the rejection's ``destination`` field: when the game
        server reports an internal navmesh marker name like ``"exit"``
        or ``"player_spawn"`` (because the rejecting scene's
        ``_get_agent_location`` rounded to its nearest known marker),
        prefer the user-requested destination. Otherwise the agent sees
        ``destination: "exit"`` after asking for ``"cafe"`` — surfaced
        as Vera-finding M on 2026-05-04.
        """
        reason = data.get("reason", data.get("event", "unknown"))
        reported_destination = data.get("destination", "?")
        normalized_destination = reported_destination
        if (
            reported_destination in _MARKER_NORMALIZE
            and self._pending_move_destination
            and self._pending_move_destination not in _MARKER_NORMALIZE
        ):
            normalized_destination = self._pending_move_destination

        self._append_event(f"MOVE_REJECTED {normalized_destination}: {reason}")
        if self._move_outcome is not None and not self._move_outcome.done():
            self._move_outcome.set_result(
                {"status": "rejected", "reason": reason, "destination": normalized_destination}
            )
        if self._arrived_event is not None:
            self._arrived_event.set()

    def _on_arrived(self, data: dict) -> None:
        """Handle arrived event — unblocks pending move IPC calls.

        Normalizes away the ``player_spawn`` marker quirk: the Godot
        game server's nearest-marker location lookup can return
        ``player_spawn`` when it sits near ``plaza`` even though the
        agent is at the user-requested destination. When the reported
        location is ``player_spawn`` AND the daemon has a pending
        move destination that isn't ``player_spawn``, prefer the
        requested destination — the agent is there by intent.
        """
        reported_location = data.get("location", "")
        # Normalize internal navmesh markers (player_spawn, exit) to the
        # user-requested destination — these are closest-marker artifacts,
        # not meaningful location labels for an agent reading their state.
        # Vera-finding N: arrival into the library reported `location: "exit"`
        # because the library scene's nearest marker happens to be the exit
        # marker.
        normalized_location = reported_location
        if (
            reported_location in _MARKER_NORMALIZE
            and self._pending_move_destination
            and self._pending_move_destination not in _MARKER_NORMALIZE
        ):
            normalized_location = self._pending_move_destination

        if "agent" in self.state:
            self.state["agent"]["activity"] = "idle"
            if normalized_location:
                self.state["agent"]["location"] = normalized_location
        if self._move_outcome is not None and not self._move_outcome.done():
            self._move_outcome.set_result(
                {"status": "arrived", "location": normalized_location}
            )
        if self._arrived_event is not None:
            self._arrived_event.set()
        self._append_event(f"ARRIVED {normalized_location or '?'}")

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
            "- `goosetown act move <location>` — Walk somewhere (plaza, library, cafe, activity_center, residence)\n"
            "- `goosetown act idle [activity]` — Do something at your location\n"
            "- `goosetown status` — See full status\n"
        )
        self._append_event("WOKE UP")

    # ------------------------------------------------------------------
    # Alarm / sleep-wake cycle
    # ------------------------------------------------------------------

    async def _check_alarm(self) -> None:
        """Check for a persisted alarm file and sleep until the wake time."""
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
    # Agent state → world update adapter
    # ------------------------------------------------------------------

    def _handle_agent_state(self, data: dict) -> None:
        """Transform a flat agent_state event into _handle_world_update format."""
        import time

        activity = data.get("activity", "idle")
        speed = data.get("speed", 0)
        is_idle = activity in ("idle", "") and speed < 0.1

        now = time.time()

        nearby = data.get("nearby_agents", [])
        prev_nearby_ids = {a.get("agent_id") for a in self.state.get("nearby", []) if a.get("agent_id")}
        curr_nearby_ids = {a.get("agent_id") for a in nearby if a.get("agent_id")}
        nearby_changed = curr_nearby_ids != prev_nearby_ids

        has_nearby_convs = bool(data.get("nearby_conversations"))

        time_think = is_idle and (now - self._last_think_time) >= 12.0
        event_think = nearby_changed or has_nearby_convs

        think = time_think or (event_think and is_idle)

        if think:
            self._last_think_time = now

        agent_info = {
            "location": data.get("location", "unknown"),
            "activity": activity,
            "mood": self.state.get("agent", {}).get("mood", "neutral"),
            "energy": self.state.get("agent", {}).get("energy", 100),
            "location_context": data.get("context", "town"),
            "onboardingPhase": data.get(
                "onboardingPhase",
                self.state.get("agent", {}).get("onboardingPhase", "alive"),
            ),
        }

        nearby_convs = data.get("nearby_conversations", [])
        if nearby_convs:
            self.state["nearby_conversations"] = nearby_convs
        elif "nearby_conversations" in self.state:
            del self.state["nearby_conversations"]

        self.state["agent"] = {**self.state.get("agent", {}), **agent_info}
        self.state["nearby"] = data.get("nearby_agents", [])
        self._write_state()

        world_data = {
            "you": agent_info,
            "nearby_agents": data.get("nearby_agents", []),
            "think": think,
        }
        self._handle_world_update(world_data)

    # ------------------------------------------------------------------
    # World update handler
    # ------------------------------------------------------------------

    def _handle_world_update(self, data: dict) -> None:
        """Handle world_update — update state and write TOWN_STATUS.md."""
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

        # NOTE: previously this branch wrote `_WELCOME_BLURB` whenever
        # `onboardingPhase == "static"`. That was wrong — by the time the
        # backend sets phase=static the agent has already been spawned in the
        # game server (sprite_pipeline.py Phase B end), so the blurb's
        # "Your sprite is being generated" message is misleading and any
        # pipeline failure that leaves phase stuck at "static" caused the
        # daemon to write the blurb forever, blinding the agent's heartbeat
        # loop. `_handle_world_update` only fires when the agent is in the
        # world, so writing live status is correct regardless of phase.

        if nearby:
            nearby_text = ", ".join(
                f"{a.get('display_name', a.get('name', '?'))} ({a.get('activity', 'idle')})" for a in nearby
            )
        else:
            nearby_text = "no one"

        if pending:
            msg_lines = []
            for msg in pending:
                ev = msg.get("event", "")
                if ev == "conversation_invite":
                    msg_lines.append(
                        f'- **{msg.get("from", "?")}** wants to chat: "{msg.get("message", "")}" '
                        f"(conv_id: {msg.get('conv_id', '?')})"
                    )
                elif ev == "conversation_message":
                    msg_lines.append(
                        f'- **{msg.get("from", "?")}** says: "{msg.get("text", "")}" '
                        f"(conv_id: {msg.get('conv_id', '?')}, turn {msg.get('turn', '?')})"
                    )
            pending_text = "\n".join(msg_lines) if msg_lines else "None"
        else:
            pending_text = "None"

        summary = data.get("context_summary", "")

        if data.get("think"):
            status = (
                summary + "\n\n"
                if summary
                else (
                    "# GooseTown Status\n\n"
                    f"**Location:** {location}\n"
                    f"**Activity:** {render_activity(activity)}\n"
                    f"**Mood:** {render_mood(mood)} | **Energy:** {energy}\n\n"
                    f"**Nearby:** {nearby_text}\n\n"
                )
            )

            active_conv = self.state.get("active_conversation")
            if active_conv:
                conv_id = active_conv.get("conv_id", "")
                participants = active_conv.get("participants", [])
                participants_display = ", ".join(participants) if participants else "?"
                turns = active_conv.get("turns", [])
                status += "---\n\n## Active Conversation\n\n"
                status += f"**Talking with:** {participants_display}\n\n"
                if turns:
                    status += "**Recent messages:**\n"
                    for turn in turns[-6:]:
                        status += f"- **{turn.get('speaker', '?')}:** {turn.get('text', '')}\n"
                    status += "\n"
                status += (
                    f"**Reply:** `goosetown act say {conv_id} <your message>`\n"
                    f"**Leave conversation:** `goosetown act end {conv_id}`\n\n"
                    "Respond naturally. Be yourself. When the conversation reaches a natural end, "
                    "or you want to do something else, use `act end` to leave.\n"
                )
                self._write_status(status)
                log.info(
                    "Think prompt (conversation) written to TOWN_STATUS.md (conv=%s, loc=%s)",
                    conv_id,
                    location,
                )
                return

            if pending_text != "None":
                status += f"**Pending messages:**\n{pending_text}\n\n"

            owner_block = ""
            if self._pending_owner_messages:
                quoted = "\n".join(f"> {m['text']}" for m in self._pending_owner_messages)
                owner_block = (
                    "\n## Your owner just said:\n\n"
                    f"{quoted}\n\n"
                    'Reply with `goosetown act reply_owner "<text>"`. '
                    "You can also act on the suggestion (move, do an activity, etc.), "
                    "ignore it, or any combination — your choice.\n\n"
                )
                self._pending_owner_messages = []

            status += "---\n\n## Your turn — decide what to do next\n\n"
            if owner_block:
                status += owner_block

            if location_context == "town":
                status += (
                    "**Where you can go:**\n"
                    "- `goosetown act move plaza` — Town center with a fountain\n"
                    "- `goosetown act move library` — Books and quiet study\n"
                    "- `goosetown act move cafe` — Cozy coffee shop\n"
                    "- `goosetown act move activity_center` — Games and group activities\n"
                    "- `goosetown act move residence` — Go home to your apartment\n\n"
                )
            else:
                status += (
                    "**Where you can go (apartment):**\n"
                    "- `goosetown act move desk` — Office desk\n"
                    "- `goosetown act move couch` — Living room couch\n"
                    "- `goosetown act move bed` — Bedroom\n"
                    "- `goosetown act move bookshelf` — Bookshelf\n"
                    "- `goosetown act move stove` — Kitchen stove\n"
                    "- `goosetown act move record_player` — Record player\n"
                    "- `goosetown act move aquarium` — Aquarium\n\n"
                    "**Return to town:**\n"
                    "- `goosetown act move exit` — Leave apartment and return to town\n\n"
                )

            available_activities = data.get("available_activities") or LOCATION_ACTIVITIES.get(
                canonical_location(location), []
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
                    status += f"- `goosetown act {act_name}` — {act_desc} (~{act_duration}s)"
                    if act_prompt:
                        status += f"\n  *{act_prompt}*"
                    status += "\n"
                status += "\n"

            nearby_chat_options = ""
            if nearby:
                nearby_chat_options = "**Start a conversation:**\n"
                for a in nearby:
                    agent_id = a.get("agent_id", "")
                    display = a.get("name", a.get("display_name", "unknown"))
                    if agent_id:
                        nearby_chat_options += (
                            f"- `goosetown act chat {agent_id} <message>` — Talk to {display}\n"
                        )
                nearby_chat_options += "\n"

            nearby_convs: list = self.state.get("nearby_conversations", [])
            nearby_conv_block = ""
            if nearby_convs and not active_conv:
                nearby_conv_block = "**Nearby conversations (you can join):**\n"
                for nc in nearby_convs[:3]:
                    names = ", ".join(nc.get("participants", [])[:5])
                    last = nc.get("last_message", {})
                    last_text = last.get("text", "")[:60] if isinstance(last, dict) else ""
                    nearby_conv_block += f"- {names} are chatting"
                    if last_text:
                        nearby_conv_block += f' — last: "{last_text}"'
                    nearby_conv_block += f" — `goosetown act join_conversation {nc.get('conv_id')}`\n"
                nearby_conv_block += "\n"

            # The "Start a conversation" menu only renders when nearby is
            # non-empty. When alone, omit it entirely instead of leaving a
            # `chat <agent_id>` line that says "use ids shown above" with no
            # ids above. K-finding from 2026-05-02 dry runs.
            chat_action_line = ""
            if nearby_chat_options:
                chat_action_line = (
                    "- `goosetown act chat <agent_id> <message>` — "
                    "Start a conversation (use a UUID from above)\n"
                )

            status += (
                f"{nearby_chat_options}"
                f"{nearby_conv_block}"
                "**Other actions:**\n"
                f"{chat_action_line}"
                "- `goosetown act say <conv_id> <message>` — Reply in an ongoing conversation\n"
                "- `goosetown act end <conv_id>` — Leave a conversation\n"
                "- `goosetown act join_conversation <conv_id>` — Join a nearby conversation\n"
                "- `goosetown leave --alarm HH:MM` — Go to sleep\n\n"
                "Recent events: see `TOWN_EVENTS.md`. Your diary: `TOWN_LIFE.md`.\n\n"
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
            if summary:
                self._write_status(summary)
            else:
                self._write_status(
                    "# GooseTown Status\n\n"
                    f"**Location:** {location}\n"
                    f"**Activity:** {render_activity(activity)}\n"
                    f"**Mood:** {render_mood(mood)} | **Energy:** {energy}\n\n"
                    f"**Nearby:** {nearby_text}\n\n"
                    f"**Pending messages:**\n{pending_text}\n"
                )

        nearby_names = [a.get("display_name", a.get("name", "?")) for a in nearby]
        nearby_set = frozenset(nearby_names)
        if nearby_set != self._prev_nearby:
            if nearby_names:
                self._append_event(f"NEARBY {', '.join(nearby_names)}")
            self._prev_nearby = nearby_set

        mood = agent.get("mood")
        energy = agent.get("energy")
        if mood is not None and energy is not None:
            if self._prev_mood is not None and (mood != self._prev_mood or energy != self._prev_energy):
                self._append_event(f"MOOD {self._prev_mood} → {mood} | ENERGY {self._prev_energy} → {energy}")
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

        url = f"{self._ws_url}?type=agent&token={self._token}"
        log.info("Connecting to %s...", self._ws_url)

        connect_kwargs = {
            "ping_interval": 30,
            "ping_timeout": 10,
        }
        auth_header = ("Authorization", f"Bearer {self._token}")
        try:
            self.ws = await websockets.connect(url, additional_headers=[auth_header], **connect_kwargs)
        except TypeError:
            try:
                self.ws = await websockets.connect(url, extra_headers=[auth_header], **connect_kwargs)
            except Exception as exc:
                if _is_auth_failure(exc):
                    raise AuthFailure(str(exc)) from exc
                log.error("WebSocket connection failed: %s", exc)
                raise
        except Exception as exc:
            if _is_auth_failure(exc):
                raise AuthFailure(str(exc)) from exc
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
        """Listen for events from GooseTown WebSocket until disconnected."""
        import asyncio
        import logging

        log = logging.getLogger("town_daemon")
        try:
            while self.running:
                recv_timeout = 30.0
                try:
                    raw = await asyncio.wait_for(self.ws.recv(), timeout=recv_timeout)
                except asyncio.TimeoutError:
                    log.warning("No messages received in %.0fs — reconnecting", recv_timeout)
                    raise ConnectionError("Inactivity timeout")
                try:
                    data = json.loads(raw)
                    if "event" in data:
                        event_name = data["event"]
                        if event_name == "world_state":
                            self._handle_world_update(data)
                        elif event_name == "agent_state":
                            self._handle_agent_state(data)
                        else:
                            self._handle_event(data)
                    elif data.get("type") == "world_update":
                        self._handle_world_update(data)
                except json.JSONDecodeError:
                    log.warning("Invalid JSON from server: %s", str(raw)[:100])
        except Exception as exc:
            if _is_auth_failure(exc):
                raise AuthFailure(str(exc)) from exc
            log.warning("WebSocket disconnected: %s", exc)

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
            msg = json.dumps({"type": "town_agent_act", **payload})
            import logging as _logging

            _log = _logging.getLogger("town_daemon")

            # For chat / move, set up the outcome future BEFORE sending so
            # the rejection event can't arrive between send and wait.
            chat_outcome_future = None
            if payload.get("action") == "chat":
                chat_outcome_future = asyncio.get_running_loop().create_future()
                self._chat_outcome = chat_outcome_future

            move_outcome_future = None
            if payload.get("action") == "move":
                move_outcome_future = asyncio.get_running_loop().create_future()
                self._move_outcome = move_outcome_future
                self._pending_move_destination = payload.get("destination", "")
                self._arrived_event.clear()

            _log.info("Sending via WS: %s", msg[:100])
            await self.ws.send(msg)
            _log.info("WS send complete")

            if payload.get("action") == "chat":
                # Wait briefly for backend verdict — conversation_started
                # (success) or chat_rejected (failure). 5s is generous; the
                # backend is hot-path and rejections are sub-100ms.
                try:
                    outcome = await asyncio.wait_for(chat_outcome_future, timeout=5.0)
                    return outcome
                except asyncio.TimeoutError:
                    return {
                        "status": "ambiguous",
                        "action": "chat",
                        "note": (
                            "Backend did not acknowledge within 5s. The chat "
                            "may have succeeded silently — check TOWN_EVENTS.md."
                        ),
                    }
                finally:
                    self._chat_outcome = None

            if payload.get("action") == "move":
                # _move_outcome was set up before send; await it. If a
                # rejection (busy / unknown_location / etc.) arrives, return
                # the structured rejection immediately. If an arrival
                # arrives, return the (player_spawn-corrected) location.
                requested_dest = self._pending_move_destination or ""
                try:
                    outcome = await asyncio.wait_for(move_outcome_future, timeout=120.0)
                    return outcome
                except asyncio.TimeoutError:
                    pass
                finally:
                    # Clear out the pending destination so a passive
                    # arrival event later doesn't accidentally rewrite
                    # state with a stale assumption.
                    self._move_outcome = None
                    self._pending_move_destination = None

                # 120s elapsed without any signal — last-resort state
                # cache check in case the world_update path delivered the
                # arrival without a separate ``arrived`` event.
                cached_loc = self.state.get("agent", {}).get("location", "")
                if cached_loc and requested_dest and cached_loc == requested_dest:
                    return {
                        "status": "arrived",
                        "location": cached_loc,
                        "note": "arrived via state poll",
                    }
                return {
                    "status": "timeout",
                    "action": "move",
                    "message": "Did not arrive within 120s",
                    "last_known_location": cached_loc,
                }
            if payload.get("action") == "say":
                # `say` is fire-and-forget for now: the backend echoes the
                # message back via conversation_message, which the daemon
                # routes through _on_conversation_message. A rejection path
                # symmetric to chat_rejected for `say` is a separate
                # finding (Pixel #5/9).
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
                "To wake up early: run `goosetown act idle`\n"
            )
            self.running = False
            return {"status": "sleeping", "wake_time": wake_time, "timezone": tz}

        return {"error": f"unknown action: {action}"}

    async def _handle_socket_client(self, reader, writer) -> None:
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

    async def _keepalive_loop(self) -> None:
        """Send periodic ping messages to keep the API Gateway connection alive."""
        import asyncio

        while self.running:
            await asyncio.sleep(120)
            if self.ws and self.running:
                try:
                    await self.ws.send(json.dumps({"type": "ping"}))
                except Exception:
                    break

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
                await asyncio.wait_for(self._initial_state_event.wait(), timeout=60.0)
            except asyncio.TimeoutError:
                log.warning("Timeout waiting for initial connected event (60s) — will retry")
                ws_task.cancel()
                return

            keepalive_task = asyncio.create_task(self._keepalive_loop())
            socket_task = asyncio.create_task(self.run_socket_server())
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
            if not self.running:
                self._pid_file.unlink(missing_ok=True)
            log.info("Daemon stopped")


# ---------------------------------------------------------------------------
# daemon_main — entry point for the _daemon subcommand
# ---------------------------------------------------------------------------


def daemon_main() -> None:
    """Entry point for the internal _daemon subcommand."""
    import asyncio
    import logging
    import signal

    # Import here to avoid circular import (core imports daemon for re-export)
    from goosetown.config import state_dir as _state_dir

    # Ensure websockets is available
    try:
        import websockets  # noqa: F401
    except ImportError:
        print(json.dumps({"error": "websockets not installed. Run: pip install websockets"}))
        sys.exit(1)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    log = logging.getLogger("town_daemon")

    ws_url = os.environ.get("TOWN_WS_URL", "")
    token = os.environ.get("TOWN_TOKEN", "")
    agent_name = os.environ.get("TOWN_AGENT", "")
    workspace_raw = os.environ.get("TOWN_WORKSPACE", "")

    if not ws_url or not token or not agent_name:
        print(json.dumps({"error": "Missing TOWN_WS_URL, TOWN_TOKEN, or TOWN_AGENT"}))
        sys.exit(1)

    sd = _state_dir(agent_name)
    workspace_path = Path(workspace_raw) if workspace_raw else None

    daemon = TownDaemon(
        ws_url=ws_url,
        token=token,
        agent_name=agent_name,
        state_dir=sd,
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
                if not daemon.running:
                    break
                log.warning("Daemon run() exited, reconnecting in %ds", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except AuthFailure as exc:
                log.error(
                    "Auth failed (token expired or revoked): %s — re-register with `goosetown join <new_token>`",
                    exc,
                )
                daemon.running = False
                break
            except Exception as exc:
                log.error("Daemon crashed: %s, restarting in %ds", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    asyncio.run(run_forever())
