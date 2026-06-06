"""Resident brain: reads daemon state and issues movement commands.

One brain tick = one call to step_resident_brain().

Phase 2 scope:
  - Movement: drift toward hangout, occasional wander.
  - Sleep cycle: each persona has a UTC sleep window; in-window residents go
    to bed and noop, costing nothing (no Bedrock until the chat brain ships).
  - Conversation is a NO-OP STUB. Bedrock-backed replies are Phase 3.

The rng and now_utc_hour parameters are injectable so tests can drive the
brain deterministically.
"""

import datetime
import logging
import random

from worldgm.residents.agentic_brain import build_decision_context, decide, to_command
from worldgm.residents.behavior import _BED, ResidentRuntime, is_in_sleep_window, next_action

logger = logging.getLogger(__name__)


def read_status(daemon) -> dict:
    """Extract the decision-relevant view from the daemon's live state.

    Reads daemon.state directly (the structured source the daemon maintains),
    NOT the TOWN_*.md markdown files — same data, far more robust.

    Returns a dict with keys:
        location           str   current agent location (or "unknown")
        location_context   str   "town" | "apartment" (default "town")
        activity           str   current activity string (or "idle")
        active_conversation  dict | None
        nearby             list  nearby agents [{agent_id, name/display_name}]
        pending_messages   list  unread incoming conversation turns
    """
    state = getattr(daemon, "state", None) or {}
    agent = state.get("agent") or {}
    return {
        "location": agent.get("location", "unknown"),
        "location_context": agent.get("location_context", "town"),
        "activity": agent.get("activity", "idle"),
        "active_conversation": state.get("active_conversation"),
        "nearby": state.get("nearby", []),
        "pending_messages": state.get("pending_messages", []),
    }


def _now_utc_hour() -> int:
    """Current UTC hour 0-23. Wrapper so tests can stub time without freezegun."""
    return datetime.datetime.now(datetime.UTC).hour


async def step_resident_brain(
    daemon,
    runtime: ResidentRuntime,
    parsed_status: dict,
    *,
    model=None,
    persona=None,
    rng=None,
    now_utc_hour: int | None = None,
) -> None:
    """One brain tick for one resident: update runtime from status, decide, act.

    With ``model`` (and ``persona``) supplied, the LLM decides the action each
    tick from the town state — choosing move / chat / say / end / activity /
    idle. Without a model (or on any failure upstream), the deterministic FSM
    drives movement + sleep, preserving the legacy behavior so a Bedrock outage
    degrades gracefully.

    Deterministic pre-gates skip the LLM on non-decision ticks: asleep (go to
    bed), mid-move (the move await gates re-entry), and in-conversation-but-not-
    my-turn (wait for the other speaker). See the design spec.

    Args:
        daemon:        The resident's TownDaemon (handle_tool_command is async).
        runtime:       Mutable FSM state — updated in-place each tick.
        parsed_status: Output of read_status(daemon) for this tick.
        model:         Optional LLM (BedrockLanguageModel). None → FSM fallback.
        persona:       Optional Persona, required for the agentic prompt.

    A town storyline, if any, rides in ``parsed_status["active_storyline"]``
    (GM-injected) and is read like any other world fact in the snapshot.
        rng:           Optional zero-arg callable returning float in [0,1).
                       Defaults to random.random.  Injectable for tests.
        now_utc_hour:  Optional int 0-23. Defaults to the current UTC hour.
                       Injectable so tests don't need freezegun.
    """
    hour = now_utc_hour if now_utc_hour is not None else _now_utc_hour()

    # ------------------------------------------------------------------ #
    # No model: deterministic FSM fallback (verbatim legacy behavior).    #
    # ------------------------------------------------------------------ #
    if model is None or persona is None:
        if parsed_status.get("active_conversation"):
            return
        location = parsed_status.get("location", "unknown")
        if location != runtime.location:
            runtime.idle_ticks = 0  # just arrived somewhere new
        runtime.location = location
        runtime.location_context = parsed_status.get("location_context", "town")
        runtime.state = "idle"
        runtime.idle_ticks += 1
        cmd = next_action(runtime, (rng or random.random)(), now_utc_hour=hour)
        if cmd.get("action") == "noop":
            return
        runtime.state = "moving"
        runtime.idle_ticks = 0
        # Blocks until arrival/timeout — this await is the movement gate.
        await daemon.handle_tool_command(cmd)
        return

    # ------------------------------------------------------------------ #
    # Agentic path: the LLM decides.                                      #
    # ------------------------------------------------------------------ #
    # Reconcile runtime with reported state (dwell tracking). The blocking
    # handle_tool_command await below is the REAL movement gate: the worldgm
    # tick awaits each resident's brain to completion, so it cannot re-enter
    # this resident until the prior move/chat returns. We therefore reset
    # state to "idle" every tick rather than gating on state=="moving" — a
    # mid-move gate would freeze the resident after its first move, since
    # state stays "moving" once the (completed) move's await returns.
    location = parsed_status.get("location", "unknown")
    if location != runtime.location:
        runtime.idle_ticks = 0  # just arrived somewhere new
    runtime.location = location
    runtime.location_context = parsed_status.get("location_context", "town")
    runtime.state = "idle"
    runtime.idle_ticks += 1

    # Pre-gate: sleep window (deterministic, no LLM spend).
    if is_in_sleep_window(hour, runtime.sleep_hour, runtime.wake_hour):
        if runtime.location == _BED:
            return
        runtime.state = "moving"
        runtime.idle_ticks = 0
        await daemon.handle_tool_command({"action": "act", "payload": {"action": "move", "destination": _BED}})
        return

    # Pre-gate: in a conversation but it isn't my turn → wait (no LLM spend).
    if parsed_status.get("active_conversation") and not parsed_status.get("pending_messages"):
        return

    # Decide via the LLM, then dispatch.
    ctx = build_decision_context(persona, parsed_status)
    action = await decide(model, ctx)
    cmd = to_command(action)
    if cmd is None:
        # Mid-conversation the only legal actions are say/end, so an idle here
        # means the model produced nothing usable. Don't re-bill the LLM every
        # tick waiting for it to recover — end the conversation deterministically
        # (the backend's inactivity timer would do this eventually anyway).
        active_conv = parsed_status.get("active_conversation")
        if active_conv:
            await daemon.handle_tool_command(
                {
                    "action": "act",
                    "payload": {"action": "end_conversation", "conv_id": active_conv.get("conv_id", "")},
                }
            )
        return  # idle
    if cmd["payload"]["action"] == "move":
        runtime.state = "moving"
        runtime.idle_ticks = 0
    # Blocks until arrival/verdict — this await gates re-entry for moves/chats.
    await daemon.handle_tool_command(cmd)
