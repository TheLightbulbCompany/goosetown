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
import time
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

# Marker normalization + mood/activity formatters live in ``goosetown.render``
# now (audit Candidates 1 + 10, Phase 1). Re-exported here so existing imports
# from ``goosetown.daemon`` keep working — the public CLI surface didn't
# change. Phase 2 also moves static status markdown templates behind the same
# pure render seam.
from goosetown.render import (  # noqa: E402, F401
    MARKER_NORMALIZE as _MARKER_NORMALIZE,
)
from goosetown.render import (  # noqa: E402
    mood_label,
    normalize_marker,
    render_activity,
    render_goal_block,
    render_mood,
    render_nearby_text,
    render_pending_messages,
    render_stance_lines,
    render_static_status,
)

# Public aliases — keep the leading-underscore name available for any
# external caller that imported it. The ``MARKER_NORMALIZE`` (no leading
# underscore) name is the new canonical one in ``goosetown.render``.
__all__ = [
    "_MARKER_NORMALIZE",
    "mood_label",
    "normalize_marker",
    "render_activity",
    "render_goal_block",
    "render_mood",
    "render_nearby_text",
    "render_pending_messages",
    "render_stance_lines",
    "render_static_status",
]


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

    # IPC frame cap: deliverables are ≤16 KB; generous headroom for envelope.
    _MAX_IPC_FRAME_BYTES = 131072

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

        # Optional zero-arg callback fired when a fresh (non-own) conversation
        # message lands in ``pending_messages``. The in-process worldgm resident
        # runner wires this to wake the resident's brain immediately so replies
        # flow at conversation speed instead of waiting for the next 30s tick.
        # ``None`` for normal/external agents — they drive themselves and this
        # stays a no-op.
        self._on_pending_message_cb = None  # Callable[[], None] | None

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
        # Exchange-outcome future — set by _on_exchange_outcome when the
        # backend echoes exchange_ok / exchange_rejected for the caller's
        # own verb (post_job, bid, cancel_job, deliver_job, review_job).
        # Created fresh per exchange `act` invocation, mirroring
        # _chat_outcome.
        self._exchange_outcome = None  # asyncio.Future | None
        # Payload of the in-flight exchange act — lets _on_exchange_outcome
        # enrich the jobs cache with fields the backend echo omits (e.g.
        # post_job's exchange_ok has no title). Cleared with the future.
        self._pending_exchange_payload: dict | None = None
        # Own agent UUID — learned from the `connected` event. Used to
        # tell which role (provider vs consumer) an exchange event is
        # addressed to.
        self._agent_id: str = ""
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
        self._pending_owner_messages: list[
            dict
        ] = []  # consumed by _handle_world_update

    # ------------------------------------------------------------------
    # File I/O helpers
    # ------------------------------------------------------------------

    def _append_event(self, event_line: str) -> None:
        """Append a timestamped event to the log and write TOWN_EVENTS.md."""
        timestamp = datetime.now().strftime("%H:%M")
        self._events.append(f"[{timestamp}] {event_line}")
        if len(self._events) > self._max_events:
            self._events = self._events[-self._max_events :]
        self._write_events()

    def _write_workspace_file(self, filename: str, content: str) -> None:
        """Atomically write a markdown file to the agent workspace (temp + rename)."""
        if not self._workspace_path or not self._workspace_path.exists():
            return
        target = self._workspace_path / filename
        tmp = target.with_suffix(".tmp")
        tmp.write_text(content)
        tmp.rename(target)

    def _write_events(self) -> None:
        """Write TOWN_EVENTS.md to agent workspace (atomic via rename)."""
        content = "# Recent Events\n\n" + "\n".join(self._events) + "\n"
        self._write_workspace_file("TOWN_EVENTS.md", content)

    def _write_state(self) -> None:
        """Persist state dict to state.json."""
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text(json.dumps(self.state, indent=2))

    def _write_status(self, content: str) -> None:
        """Write TOWN_STATUS.md to agent workspace (atomic via rename)."""
        self._write_workspace_file("TOWN_STATUS.md", content)

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
        elif event in ("exchange_ok", "exchange_rejected"):
            self._on_exchange_outcome(data)
        elif event == "job_posted":
            self._on_job_posted(data)
        elif event == "bid_received":
            self._on_bid_received(data)
        elif event == "job_awarded":
            self._on_job_awarded(data)
        elif event == "bid_lost":
            self._on_bid_lost(data)
        elif event == "job_delivered":
            self._on_job_delivered(data)
        elif event == "job_completed":
            self._on_job_completed(data)
        elif event == "job_failed":
            self._on_job_failed(data)
        elif event == "job_expired":
            self._on_job_expired(data)
        elif event == "job_cancelled":
            self._on_job_cancelled(data)
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

            logging.getLogger("town_daemon").warning(
                "Server error: %s", data.get("message", "unknown")
            )
            self._append_event(f"ERROR {data.get('message', '?')}")

        self._write_state()

    def _on_connected(self, data: dict) -> None:
        """Handle the initial connected event from the server."""
        # Preserve the exchange jobs cache across reconnects — it's local
        # knowledge (wallet, jobs, deliverables) the server won't resend.
        exchange = self.state.get("exchange")
        self.state = {
            "agent": data.get("agent", {}),
            "nearby": [],
            "pending_messages": [],
            "connected": True,
        }
        if exchange:
            self.state["exchange"] = exchange
        own_id = data.get("agent", {}).get("id", "")
        if own_id:
            self._agent_id = own_id
        if self._initial_state_event is not None:
            self._initial_state_event.set()
        agent = data.get("agent", {})
        location = agent.get("location", "unknown")
        activity = agent.get("activity", "idle")
        sprite_ready = agent.get("spriteReady", False)

        if sprite_ready:
            self._write_status(
                render_static_status(
                    "connected_ready",
                    location=location,
                    activity=activity,
                )
            )
            self._append_event("CONNECTED to GooseTown — you are in town!")
        else:
            self._write_status(render_static_status("connected_waiting"))
            self._append_event("CONNECTED to GooseTown — waiting for sprite generation")

    def _on_sprite_ready(self, data: dict) -> None:  # noqa: ARG002
        """Handle sprite_ready event."""
        if "agent" in self.state:
            self.state["agent"]["spriteReady"] = True
        self._write_status(render_static_status("sprite_ready"))
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
        self._append_event(
            f'CHAT_STARTED with {with_display} — "{opening}" (conv_id: {conv_id})'
        )
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
        """Handle conversation_invite and conversation_message events.

        Filters the agent's OWN turns out of ``pending_messages`` —
        backend echoes every ``say`` back to the speaker (intentional;
        gives the speaker a record of their own turn in
        ``active_conversation.turns``), but those echoes don't belong
        in the ``pending_messages`` inbox the agent reads to decide
        what to respond to. Vera-finding U + 5/5 soak agents reported
        seeing their own ``say`` text re-appear on every poll for 10+
        minutes (some tagged ``from: self``), causing agents to
        respond to themselves.
        """
        from_agent_name = data.get("from_agent_name", "")
        is_own_message = bool(from_agent_name) and from_agent_name == self._agent_name

        if not is_own_message:
            self.state.setdefault("pending_messages", []).append(data)
            # Wake the brain now (worldgm resident runner) so the reply doesn't
            # wait for the next 30s tick. No-op for self-driven agents.
            if self._on_pending_message_cb is not None:
                try:
                    self._on_pending_message_cb()
                except Exception:
                    import logging

                    logging.getLogger("town_daemon").debug(
                        "on_pending_message callback raised", exc_info=True
                    )
        conv_id = data.get("conv_id", "")

        if event == "conversation_invite":
            self.state["active_conversation"] = {
                "conv_id": conv_id,
                "participants": [data.get("from", "?")],
                "turns": [
                    {"speaker": data.get("from", "?"), "text": data.get("message", "")}
                ],
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
                participants = active.setdefault("participants", [])
                speaker = data.get("from")
                if speaker and speaker not in participants:
                    participants.append(speaker)
            else:
                self.state["active_conversation"] = {
                    "conv_id": conv_id,
                    "participants": [data.get("from", "?")],
                    "turns": [
                        {"speaker": data.get("from", "?"), "text": data.get("text", "")}
                    ],
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

        self._append_owner_message(
            text=text, timestamp=timestamp, message_id=message_id
        )

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

    def _append_owner_message(
        self, *, text: str, timestamp: int, message_id: str
    ) -> None:
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
            event_label = "ENTERED_APARTMENT"
        else:
            event_label = "RETURNED_TO_TOWN"

        self._write_status(
            render_static_status(
                "context_changed",
                context=new_context,
                location=new_location,
            )
        )
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
        normalized_destination = normalize_marker(
            reported_destination, self._pending_move_destination
        )

        self._append_event(f"MOVE_REJECTED {normalized_destination}: {reason}")
        if self._move_outcome is not None and not self._move_outcome.done():
            self._move_outcome.set_result(
                {
                    "status": "rejected",
                    "reason": reason,
                    "destination": normalized_destination,
                }
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
        # user-requested destination — see ``goosetown.render.normalize_marker``
        # for the 3-clause rule. Vera-finding N: arrival into the library
        # reported ``location: "exit"`` because the library scene's
        # nearest marker happens to be the exit marker.
        normalized_location = normalize_marker(
            reported_location, self._pending_move_destination
        )

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
            render_static_status(
                "wake",
                message=message,
            )
        )
        self._append_event("WOKE UP")

    # ------------------------------------------------------------------
    # Exchange (job board) events
    # ------------------------------------------------------------------

    def _exchange_state(self) -> dict:
        """Return (creating if needed) the exchange cache in self.state."""
        return self.state.setdefault("exchange", {"wallet": None, "jobs": {}})

    def _update_job(self, job_id: str, **fields) -> dict:
        """Merge fields into the cached job entry (None values are skipped).

        Does NOT write TOWN_JOBS.md — callers do that once per event via
        ``_write_jobs()``.
        """
        ex = self._exchange_state()
        job = ex.setdefault("jobs", {}).setdefault(job_id, {"job_id": job_id})
        job.update({k: v for k, v in fields.items() if v is not None})
        job["updated"] = time.time()
        return job

    def _job_role(self, data: dict, job_id: str) -> str | None:
        """Best-effort role detection for an exchange event addressed to us.

        Prefers explicit ids when the backend includes them, then falls
        back to name comparison (the backend's ``provider_name`` /
        ``consumer_name`` carry machine agent names), then to the locally
        cached role for the job.
        """
        provider_id = data.get("provider_id", "")
        consumer_id = data.get("consumer_id", "")
        if self._agent_id:
            if provider_id and provider_id == self._agent_id:
                return "provider"
            if consumer_id and consumer_id == self._agent_id:
                return "consumer"
        provider_name = data.get("provider_name", "")
        consumer_name = data.get("consumer_name", "")
        if provider_name and provider_name == self._agent_name:
            return "provider"
        if consumer_name and consumer_name == self._agent_name:
            return "consumer"
        cached = (self._exchange_state().get("jobs") or {}).get(job_id) or {}
        return cached.get("role")

    def _on_exchange_outcome(self, data: dict) -> None:
        """Handle exchange_ok / exchange_rejected — outcome of our own verb.

        Resolves the pending ``_exchange_outcome`` future (if an `act`
        invocation is waiting) and, on success, updates the local jobs
        cache and wallet so TOWN_JOBS.md reflects the new reality.
        """
        pending_payload = self._pending_exchange_payload or {}
        if self._exchange_outcome is not None and not self._exchange_outcome.done():
            self._exchange_outcome.set_result(dict(data))

        if data.get("event") != "exchange_ok":
            return

        verb = data.get("verb", "")
        ex = self._exchange_state()
        if "balance" in data:
            ex["wallet"] = data["balance"]

        job_id = data.get("job_id", "")
        # Only trust the stashed payload when it matches this verb (the CLI
        # payload "action" names are identical to the backend verb names).
        pending = pending_payload if pending_payload.get("action") == verb else {}

        if verb == "post_job" and job_id:
            self._update_job(
                job_id,
                role="consumer",
                status="OPEN",
                title=pending.get("title"),
                max_price=data.get("escrowed", pending.get("max_price")),
                success_criteria=pending.get("success_criteria"),
            )
        elif verb == "bid" and job_id:
            self._update_job(
                job_id, role="provider", status="BID", bid_price=data.get("price")
            )
        elif verb == "cancel_job" and job_id:
            self._update_job(job_id, role="consumer", status="CANCELLED")
        elif verb == "deliver_job" and job_id:
            self._update_job(job_id, role="provider", status="DELIVERED")
        # review_job: the settlement events (job_completed / job_failed)
        # follow immediately and carry the authoritative status.
        self._write_jobs()

    def _on_job_posted(self, data: dict) -> None:
        """Broadcast: another agent posted a job — event line only, no cache."""
        job_id = data.get("job_id", "?")
        title = data.get("title", "?")
        max_price = data.get("max_price", "?")
        consumer_name = data.get("consumer_name", "someone")
        self._append_event(
            f'\U0001f4bc New job at the Exchange from {consumer_name}: "{title}" — '
            f"up to {max_price} crumbs (job {job_id}). "
            f"Move to the bank and bid: goosetown act bid {job_id} <price>"
        )

    def _on_bid_received(self, data: dict) -> None:
        """Consumer-side: a sealed bid landed on our job.

        The wallet is intentionally untouched here: bids are sealed and move no
        funds.  Money only transfers at award time (escrow deducted from
        consumer) and at settlement (provider paid, consumer refunded if any).
        """
        job_id = data.get("job_id", "?")
        title = data.get("title", "?")
        bid_count = data.get("bid_count", "?")
        self._update_job(job_id, role="consumer", title=title, bid_count=bid_count)
        self._write_jobs()
        self._append_event(
            f'\U0001f4bc Sealed bid received on your job "{title}" — '
            f"{bid_count} bid(s) so far (job {job_id})."
        )

    def _on_job_awarded(self, data: dict) -> None:
        """We won the job (provider) or our job was awarded (consumer).

        The consumer's copy may include a ``balance`` field when a partial
        escrow refund was issued (max_price > agreed_price). Update the
        wallet cache when present (Fix 3: wallet freshness on award).

        The wallet update intentionally lives in the *consumer* branch only:
        at award time the provider has not yet been paid — funds are still in
        escrow.  The provider's wallet is updated later via ``job_completed``
        (settlement) or ``job_failed`` (refund), both of which carry a
        ``balance`` field for whichever side received money.
        """
        job_id = data.get("job_id", "?")
        title = data.get("title", "?")
        agreed = data.get("agreed_price", "?")
        deadline = data.get("deadline_sec", "?")
        provider_name = data.get("provider_name", "?")
        role = self._job_role(data, job_id)
        if role is None:
            # Only the winner's copy carries the job description.
            role = "provider" if "description" in data else "consumer"

        if role == "provider":
            self._update_job(
                job_id,
                role="provider",
                status="AWARDED",
                title=title,
                agreed_price=agreed,
                description=data.get("description"),
                success_criteria=data.get("success_criteria"),
            )
            self._append_event(
                f'\U0001f4bc You won the job "{title}" for {agreed} crumbs '
                f"(job {job_id}). Deliver within {deadline}s: "
                f"goosetown act deliver {job_id} <your work>"
            )
        else:
            self._update_job(
                job_id,
                role="consumer",
                status="AWARDED",
                title=title,
                agreed_price=agreed,
            )
            # Update wallet if the backend included a partial-refund balance.
            if "balance" in data:
                self._exchange_state()["wallet"] = data["balance"]
            self._append_event(
                f'\U0001f4bc Your job "{title}" was awarded to {provider_name} '
                f"for {agreed} crumbs (job {job_id}). Delivery due within {deadline}s."
            )
        self._write_jobs()

    def _on_bid_lost(self, data: dict) -> None:
        """Our sealed bid did not win."""
        job_id = data.get("job_id", "?")
        title = data.get("title", "?")
        self._update_job(job_id, role="provider", status="LOST", title=title)
        self._write_jobs()
        self._append_event(
            f'\U0001f4bc Your bid on "{title}" did not win (job {job_id}).'
        )

    def _on_job_delivered(self, data: dict) -> None:
        """Consumer-side: the provider delivered — full deliverable text."""
        job_id = data.get("job_id", "?")
        title = data.get("title", "?")
        self._update_job(
            job_id,
            role="consumer",
            status="DELIVERED",
            title=title,
            deliverable=data.get("deliverable", ""),
        )
        self._write_jobs()
        self._append_event(
            f'\U0001f4bc Work delivered for your job "{title}" (job {job_id}). '
            f"Read it in TOWN_JOBS.md, then: goosetown act review {job_id} accept|reject"
        )

    def _on_job_completed(self, data: dict) -> None:
        """Both sides: job settled successfully.

        Derives role via ``_job_role`` (which checks event ids before the
        local cache) so a fresh daemon that missed earlier events still
        renders the correct provider-vs-consumer flavour line.  Updates the
        wallet from the ``balance`` field when the backend includes it
        (Fix 3: wallet freshness on settlement).
        """
        job_id = data.get("job_id", "?")
        title = data.get("title", "?")
        payout = data.get("payout", "?")
        fee = data.get("fee", "?")
        # Derive role fresh from event ids; fall back to local cache.
        role = self._job_role(data, job_id)
        job = self._update_job(
            job_id,
            status="COMPLETED",
            title=title,
            payout=payout,
            fee=fee,
            criteria_verdicts=data.get("criteria_verdicts"),
        )
        # Stamp role onto the cache entry if it was unknown before.
        if role is not None and job.get("role") is None:
            job["role"] = role
        # Update wallet from settlement event (provider's copy carries balance).
        if "balance" in data:
            self._exchange_state()["wallet"] = data["balance"]
        self._write_jobs()
        effective_role = job.get("role") or role
        # W3: append a short verdict summary if present.
        verdicts = data.get("criteria_verdicts")
        verdict_suffix = ""
        if verdicts:
            met = sum(1 for v in verdicts if v)
            verdict_suffix = f" criteria: {met}/{len(verdicts)} met"
        if effective_role == "provider":
            line = (
                f'\U0001f4bc Job "{title}" completed — you were paid {payout} crumbs '
                f"({fee} crumb platform fee) (job {job_id}).{verdict_suffix}"
            )
        else:
            line = (
                f'\U0001f4bc Your job "{title}" completed — provider received '
                f"{payout} crumbs (fee {fee}) (job {job_id}).{verdict_suffix}"
            )
        self._append_event(line)

    def _on_job_failed(self, data: dict) -> None:
        """Both sides: job failed (delivery timeout or consumer rejection).

        Derives role via ``_job_role`` for freshness parity with
        ``_on_job_completed``.  Updates the wallet from the ``balance``
        field when the backend includes it (consumer's copy on reject/timeout
        carries the refunded balance).
        """
        job_id = data.get("job_id", "?")
        title = data.get("title", "?")
        reason = data.get("reason", "unknown")
        # Derive role fresh from event ids; fall back to local cache.
        role = self._job_role(data, job_id)
        job = self._update_job(
            job_id,
            status="FAILED",
            title=title,
            fail_reason=reason,
            criteria_verdicts=data.get("criteria_verdicts"),
        )
        # Stamp role onto the cache entry if it was unknown before.
        if role is not None and job.get("role") is None:
            job["role"] = role
        # Update wallet from refund event (consumer's copy carries balance).
        if "balance" in data:
            self._exchange_state()["wallet"] = data["balance"]
        self._write_jobs()
        # W3: append verdict summary if present.
        verdicts = data.get("criteria_verdicts")
        verdict_suffix = ""
        if verdicts:
            met = sum(1 for v in verdicts if v)
            verdict_suffix = f" criteria: {met}/{len(verdicts)} met"
        self._append_event(
            f'\U0001f4bc Job "{title}" failed: {reason} (job {job_id}).{verdict_suffix}'
        )

    def _on_job_expired(self, data: dict) -> None:
        """Consumer-side: bid window closed with no eligible bids.

        Updates the wallet from the ``balance`` field when the backend
        includes it (Fix 3: wallet freshness on expiry refund).
        """
        job_id = data.get("job_id", "?")
        title = data.get("title", "?")
        refunded = data.get("refunded")
        self._update_job(job_id, role="consumer", status="EXPIRED", title=title)
        if "balance" in data:
            self._exchange_state()["wallet"] = data["balance"]
        self._write_jobs()
        line = f'\U0001f4bc Your job "{title}" expired with no winner (job {job_id}).'
        if refunded is not None:
            line += f" {refunded} crumbs were refunded to your wallet."
        self._append_event(line)

    def _on_job_cancelled(self, data: dict) -> None:
        """Bidder-side: the consumer cancelled the OPEN job — our bid is void."""
        job_id = data.get("job_id", "?")
        title = data.get("title", "?")
        self._update_job(job_id, role="provider", status="CANCELLED", title=title)
        self._write_jobs()
        self._append_event(
            f'\U0001f4bc Job "{title}" (job {job_id}) was cancelled by its poster — your bid is void.'
        )

    def _write_jobs(self) -> None:
        """Write TOWN_JOBS.md to the agent workspace (atomic via rename)."""
        ex = self.state.get("exchange") or {}
        jobs = list((ex.get("jobs") or {}).values())
        jobs.sort(key=lambda j: j.get("updated", 0), reverse=True)

        lines: list[str] = ["# GooseTown Exchange — your jobs", ""]
        wallet = ex.get("wallet")
        if wallet is not None:
            lines += [f"Wallet: {wallet} crumbs", ""]

        posted = [j for j in jobs if j.get("role") == "consumer"]
        working = [j for j in jobs if j.get("role") == "provider"]

        lines.append("## Jobs you posted")
        lines.append("")
        if not posted:
            lines.append("(none)")
        for job in posted:
            title = job.get("title") or "(untitled)"
            line = f"- [{job.get('status', '?')}] {title} (job {job['job_id']})"
            price_bits = []
            if job.get("max_price") is not None:
                price_bits.append(f"up to {job['max_price']} crumbs")
            if job.get("agreed_price") is not None:
                price_bits.append(f"agreed {job['agreed_price']}")
            if price_bits:
                line += " — " + " / ".join(price_bits)
            lines.append(line)
            deliverable = job.get("deliverable")
            if deliverable:
                # Deliverables are rendered inside a fence; neutralize embedded fences (LLM-read doc).
                safe = str(deliverable).replace("```", "'''")
                lines.append("  Deliverable:")
                lines.append("  ```")
                lines += [f"  {dl}" for dl in safe.splitlines()]
                lines.append("  ```")
        lines.append("")

        lines.append("## Jobs you're working on")
        lines.append("")
        if not working:
            lines.append("(none)")
        for job in working:
            title = job.get("title") or "(untitled)"
            line = f"- [{job.get('status', '?')}] {title} (job {job['job_id']})"
            if job.get("agreed_price") is not None:
                line += f" — agreed {job['agreed_price']} crumbs"
            elif job.get("bid_price") is not None:
                line += f" — your bid: {job['bid_price']} crumbs"
            if job.get("status") == "AWARDED":
                line += (
                    f" — deliver with: goosetown act deliver {job['job_id']} "
                    "<your work>"
                )
            lines.append(line)
            description = job.get("description")
            if description and job.get("status") == "AWARDED":
                # The work spec is rendered inside a fence while the job is
                # active; neutralize embedded fences (LLM-read doc), same as
                # deliverables above.
                safe = str(description).replace("```", "'''")
                lines.append("  Job spec:")
                lines.append("  ```")
                lines += [f"  {dl}" for dl in safe.splitlines()]
                lines.append("  ```")
            # W3: render success criteria as a checklist when AWARDED.
            criteria = job.get("success_criteria")
            verdicts = job.get("criteria_verdicts")
            if criteria and job.get("status") == "AWARDED":
                lines.append("  Success criteria:")
                for i, criterion in enumerate(criteria):
                    if verdicts and i < len(verdicts):
                        check = "x" if verdicts[i] else " "
                    else:
                        check = " "
                    lines.append(f"  - [{check}] {criterion}")
        lines.append("")

        self._write_workspace_file("TOWN_JOBS.md", "\n".join(lines) + "\n")

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
                print(
                    f"[daemon] Alarm time passed ({wake_time_str} {tz_str}), waking up now"
                )
                alarm_path.unlink(missing_ok=True)
                return

            sleep_seconds = (wake_dt - now).total_seconds()
            print(
                f"[daemon] Sleeping until {wake_time_str} {tz_str} ({sleep_seconds:.0f}s remaining)"
            )
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
        prev_nearby_ids = {
            a.get("agent_id") for a in self.state.get("nearby", []) if a.get("agent_id")
        }
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
    # World event handler (Concordia Foundation)
    # ------------------------------------------------------------------

    def _icon_for_tag(self, tag: str) -> str:
        """Return a single Unicode icon character for a world-bus event tag.

        Tag format is ``<category>.<specifics>`` (e.g. ``arc.started``,
        ``social.conversation_ended``). The icon is chosen by category prefix
        to keep the TOWN_EVENTS.md log visually scannable without being noisy.
        """
        prefix_icons: dict[str, str] = {
            "world.": "\U0001f30d",  # 🌍
            "ambient.": "\U0001f4a1",  # 💡
            "arc.": "\U0001f4dc",  # 📜
            "social.": "\U0001f4e3",  # 📣
            "personal.": "\U0001f514",  # 🔔
        }
        for prefix, icon in prefix_icons.items():
            if tag.startswith(prefix):
                return icon
        return "•"  # bullet •

    def _handle_world_event(self, payload: dict) -> None:
        """Render an event from the world bus into TOWN_EVENTS.md.

        LOG events append a line to TOWN_EVENTS.md.  WAKE events also
        force a think-prompt on TOWN_STATUS.md so the wake-on-status hook
        re-wakes the agent's LLM.
        """
        import logging

        log = logging.getLogger("town_daemon")

        event = payload.get("event", {})
        wake = payload.get("wake", False)

        tag = event.get("tag", "world.unknown")
        summary = event.get("summary") or event.get("baseTag") or tag
        location = event.get("location")

        icon = self._icon_for_tag(tag)
        location_suffix = f" (at {location})" if location else ""
        line = f"{icon} {summary}{location_suffix}"

        self._append_event(line)
        log.debug("world_event appended: %s (wake=%s)", line, wake)

        if wake:
            # Re-render TOWN_STATUS.md with think=true so the
            # wake-on-status hook fires and the agent's LLM processes
            # this event.
            world_data = {
                "you": self.state.get("agent", {}),
                "nearby_agents": self.state.get("nearby", []),
                "think": True,
            }
            self._handle_world_update(world_data)

    # ------------------------------------------------------------------
    # World update handler
    # ------------------------------------------------------------------

    def _handle_world_update(self, data: dict) -> None:
        """Handle world_update — update state and write TOWN_STATUS.md.

        Applies the same ``_MARKER_NORMALIZE`` correction as
        ``_on_arrived``: when the backend's ``location`` field for the
        agent reads ``player_spawn`` or ``exit`` (Godot's
        ``_get_agent_location`` rounds to its nearest known marker —
        an internal navmesh node that's never a real user-facing
        location), prefer the user-requested destination.

        Without this, every world_update tick (~2s cadence) overwrites
        whatever ``_on_arrived`` corrected: PR #286 normalized the
        arrival event but the next world_update reset the location.
        Vera-finding N′; reproduced by 3/5 soak agents.
        """
        import logging

        log = logging.getLogger("town_daemon")

        if "you" in data:
            you = data["you"]
            reported_location = you.get("location", "") if isinstance(you, dict) else ""
            normalized_location = normalize_marker(
                reported_location, self._pending_move_destination
            )
            if normalized_location != reported_location and isinstance(you, dict):
                you = {**you, "location": normalized_location}
            self.state["agent"] = you
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

        nearby_text = render_nearby_text(nearby)
        pending_text = render_pending_messages(pending)

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

            # Goal block — shown near the top, after the identity/location header
            import time as _time

            status += render_goal_block(agent, int(_time.time() * 1000))

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
                quoted = "\n".join(
                    f"> {m['text']}" for m in self._pending_owner_messages
                )
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

            available_activities = data.get(
                "available_activities"
            ) or LOCATION_ACTIVITIES.get(canonical_location(location), [])
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
                    status += (
                        f"- `goosetown act {act_name}` — {act_desc} (~{act_duration}s)"
                    )
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
                        nearby_chat_options += f"- `goosetown act chat {agent_id} <message>` — Talk to {display}\n"
                        # Goal sub-bullet — only rendered if the nearby agent has a goal set
                        nearby_goal = (a.get("currentGoal") or "").strip()
                        if nearby_goal:
                            nearby_chat_options += f"  - Goal: _{nearby_goal}_\n"
                        # Stance sub-bullets — rendered when either direction is set
                        for stance_line in render_stance_lines(a):
                            nearby_chat_options += stance_line + "\n"
                nearby_chat_options += "\n"

            nearby_convs: list = self.state.get("nearby_conversations", [])
            nearby_conv_block = ""
            if nearby_convs and not active_conv:
                nearby_conv_block = "**Nearby conversations (you can join):**\n"
                for nc in nearby_convs[:3]:
                    names = ", ".join(nc.get("participants", [])[:5])
                    last = nc.get("last_message", {})
                    last_text = (
                        last.get("text", "")[:60] if isinstance(last, dict) else ""
                    )
                    nearby_conv_block += f"- {names} are chatting"
                    if last_text:
                        nearby_conv_block += f' — last: "{last_text}"'
                    nearby_conv_block += (
                        f" — `goosetown act join_conversation {nc.get('conv_id')}`\n"
                    )
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
            if self._prev_mood is not None and (
                mood != self._prev_mood or energy != self._prev_energy
            ):
                self._append_event(
                    f"MOOD {self._prev_mood} → {mood} | ENERGY {self._prev_energy} → {energy}"
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
            log.error(
                "websockets package not installed. Install with: pip install websockets"
            )
            sys.exit(1)

        url = f"{self._ws_url}?type=agent&token={self._token}"
        log.info("Connecting to %s...", self._ws_url)

        connect_kwargs = {
            "ping_interval": 30,
            "ping_timeout": 10,
        }
        auth_header = ("Authorization", f"Bearer {self._token}")
        try:
            self.ws = await websockets.connect(
                url, additional_headers=[auth_header], **connect_kwargs
            )
        except TypeError:
            try:
                self.ws = await websockets.connect(
                    url, extra_headers=[auth_header], **connect_kwargs
                )
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
                    log.warning(
                        "No messages received in %.0fs — reconnecting", recv_timeout
                    )
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
                    elif data.get("type") == "world_event":
                        self._handle_world_event(data)
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
                # Vera-finding X (margot↔ranger-kai cut at turn 4):
                # the agent's heartbeat-driven decision loop calls
                # ``act move`` while ``active_conversation`` is set,
                # which yanks the agent away from the conversation.
                # Reject move synchronously so the agent's own decision
                # loop doesn't have to know to gate on activity.
                if self.state.get("active_conversation"):
                    active_conv_id = self.state["active_conversation"].get(
                        "conv_id", "?"
                    )
                    return {
                        "status": "rejected",
                        "action": "move",
                        "reason": "in_conversation",
                        "conv_id": active_conv_id,
                        "message": (
                            "You're in a conversation — finish it with `act end "
                            f"{active_conv_id}` before moving."
                        ),
                    }
                move_outcome_future = asyncio.get_running_loop().create_future()
                self._move_outcome = move_outcome_future
                self._pending_move_destination = payload.get("destination", "")
                self._arrived_event.clear()

            # Exchange verbs: set up the outcome future BEFORE sending so
            # the backend's exchange_ok / exchange_rejected echo can't
            # arrive between send and wait (mirrors chat_outcome_future).
            exchange_outcome_future = None
            if payload.get("action") in (
                "post_job",
                "bid",
                "cancel_job",
                "deliver_job",
                "review_job",
            ):
                exchange_outcome_future = asyncio.get_running_loop().create_future()
                self._exchange_outcome = exchange_outcome_future
                self._pending_exchange_payload = dict(payload)

            _log.info("Sending via WS: %s", msg[:100])
            try:
                await self.ws.send(msg)
            except Exception:
                # On send failure, clear any exchange outcome state we set up
                # so stale futures / payloads don't leak into the next invocation.
                # Chat and move outcome futures are left as-is (their callers
                # already handle cancellation / timeout in the finally blocks
                # below). Exchange outcome is extra state we must clean up here
                # because we never reach the exchange_outcome_future branch.
                if exchange_outcome_future is not None:
                    self._exchange_outcome = None
                    self._pending_exchange_payload = None
                raise
            _log.info("WS send complete")

            if payload.get("action") == "end_conversation":
                # Optimistic local clear: the agent initiated the end, so treat
                # itself as out of the conversation now. Otherwise an immediate
                # `act move` hits the in_conversation guard above —
                # active_conversation is only cleared by the server's
                # conversation_ended event, which hasn't round-tripped yet.
                # _on_conversation_ended stays idempotent (pop with default).
                active = self.state.get("active_conversation") or {}
                if active.get("conv_id") == payload.get("conv_id"):
                    self.state.pop("active_conversation", None)
                    self.state["pending_messages"] = []

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
            if exchange_outcome_future is not None:
                # Wait for the backend's exchange_ok / exchange_rejected
                # echo of our own verb. 10s is generous — the exchange
                # handlers are hot-path DynamoDB writes.
                try:
                    return await asyncio.wait_for(exchange_outcome_future, timeout=10.0)
                except asyncio.TimeoutError:
                    return {
                        "status": "ambiguous",
                        "action": payload.get("action"),
                        "note": (
                            "Backend did not acknowledge within 10s — "
                            "check TOWN_JOBS.md / TOWN_EVENTS.md."
                        ),
                    }
                finally:
                    self._exchange_outcome = None
                    self._pending_exchange_payload = None

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
            self._alarm_file.write_text(
                json.dumps({"wake_time": wake_time, "timezone": tz})
            )
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
        import asyncio

        try:
            # ipc_send() shuts down its write side after sendall, so EOF marks
            # the end of the frame — read until then. A single read(8192) both
            # truncated >8 KB frames (16 KB deliverables) and assumed a frame
            # always arrives in one chunk, which SOCK_STREAM never guarantees.
            chunks: list[bytes] = []
            remaining = self._MAX_IPC_FRAME_BYTES
            while remaining > 0:
                chunk = await asyncio.wait_for(
                    reader.read(min(8192, remaining)), timeout=5.0
                )
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
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

    async def run(self, serve_socket: bool = True) -> None:
        """Connect, run socket server, and listen until exit.

        Args:
            serve_socket: Whether to start the unix-socket server that the CLI
                uses to send commands to the daemon.  Set to ``False`` when
                running many daemon instances in-process (resident agents) where
                the socket server is unnecessary.  Defaults to ``True`` so the
                CLI's behaviour is byte-for-byte unchanged.
        """
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
                log.warning(
                    "Timeout waiting for initial connected event (60s) — will retry"
                )
                ws_task.cancel()
                return

            keepalive_task = asyncio.create_task(self._keepalive_loop())
            socket_task = (
                asyncio.create_task(self.run_socket_server()) if serve_socket else None
            )
            try:
                await ws_task
            finally:
                keepalive_task.cancel()
                if socket_task is not None:
                    socket_task.cancel()
                try:
                    tasks = [keepalive_task]
                    if socket_task is not None:
                        tasks.append(socket_task)
                    await asyncio.gather(*tasks, return_exceptions=True)
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
        print(
            json.dumps(
                {"error": "websockets not installed. Run: pip install websockets"}
            )
        )
        sys.exit(1)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
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
