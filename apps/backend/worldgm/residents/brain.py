"""Resident brain: reads daemon state and issues movement commands.

One brain tick = one call to step_resident_brain().

Phase 1 scope:
  - Movement only: drift toward hangout, occasional wander.
  - Conversation is a NO-OP STUB.  If the resident is in an active
    conversation the brain does nothing — the daemon would reject a move
    anyway, and Bedrock-backed replies are a later phase.

The rng parameter is injectable so tests can deterministically control
the wander roll without monkeypatching random.
"""

import logging
import random

from worldgm.residents.behavior import ResidentRuntime, next_action

logger = logging.getLogger(__name__)


def read_status(daemon) -> dict:
    """Extract the decision-relevant view from the daemon's live state.

    Reads daemon.state directly (the structured source the daemon maintains),
    NOT the TOWN_*.md markdown files — same data, far more robust.

    Returns a dict with keys:
        location          str   current agent location (or "unknown")
        activity          str   current activity string (or "idle")
        active_conversation  dict | None
    """
    state = getattr(daemon, "state", None) or {}
    agent = state.get("agent") or {}
    return {
        "location": agent.get("location", "unknown"),
        "activity": agent.get("activity", "idle"),
        "active_conversation": state.get("active_conversation"),
    }


async def step_resident_brain(
    daemon,
    runtime: ResidentRuntime,
    parsed_status: dict,
    *,
    rng=None,
) -> None:
    """One brain tick for one resident: update runtime from status, decide, act.

    Args:
        daemon:        The resident's TownDaemon (handle_tool_command is async).
        runtime:       Mutable FSM state — updated in-place each tick.
        parsed_status: Output of read_status(daemon) for this tick.
        rng:           Optional zero-arg callable returning float in [0,1).
                       Defaults to random.random.  Injectable for tests.
    """
    # ------------------------------------------------------------------ #
    # Phase 1 conversation stub: if in a conversation, do nothing.        #
    # A later phase will wire Bedrock-backed replies here.                #
    # ------------------------------------------------------------------ #
    if parsed_status.get("active_conversation"):
        return

    # --- Reconcile runtime with the daemon's reported state --- #
    # NOTE: daemon.state exposes no reliable "currently walking" signal — the
    # game server's is_moving flag is NOT propagated into daemon.state, and
    # `activity` is "idle"/an activity name, never "moving". The movement gate
    # is instead the blocking `handle_tool_command` move call below: it does not
    # return until the agent arrives (or times out), so the brain physically
    # cannot re-tick mid-walk and double-issue a move. We therefore treat every
    # tick as a settled point and track dwell time at the reported location.
    #
    # Phase 2 / N residents: ticking brains sequentially here (each awaiting its
    # blocking move) would serialize all resident movement and stall the worldgm
    # tick loop. When scaling the manager to the full cast, run the brains
    # concurrently (or fire-and-forget the move) rather than awaiting in a loop.
    location = parsed_status.get("location", "unknown")
    if location != runtime.location:
        runtime.idle_ticks = 0  # just arrived somewhere new
    runtime.location = location
    runtime.state = "idle"
    runtime.idle_ticks += 1

    # --- Decide and act --- #
    rng_value = (rng or random.random)()
    cmd = next_action(runtime, rng_value)
    if cmd.get("action") == "noop":
        return

    runtime.state = "moving"
    runtime.idle_ticks = 0
    # Blocks until arrival/timeout — this await is the movement gate.
    await daemon.handle_tool_command(cmd)
