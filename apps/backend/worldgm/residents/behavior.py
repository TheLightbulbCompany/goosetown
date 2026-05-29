"""Pure FSM for resident movement decisions.

No I/O, no daemon coupling — same inputs always produce the same output.
"""

from dataclasses import dataclass

# Town locations a resident may move to.
_VALID_DESTINATIONS = (
    "plaza",
    "library",
    "cafe",
    "activity_center",
    "bank",
    "residence",
)

# Number of idle ticks that must pass before the resident decides to move.
_IDLE_TICKS_BEFORE_MOVE = 2

# Probability (per eligible tick) that a settled resident wanders off-hangout.
_WANDER_CHANCE = 0.25

# Sentinel returned when no action should be taken this tick.
NOOP: dict = {"action": "noop"}


@dataclass
class ResidentRuntime:
    """Mutable per-resident state tracked across ticks by the brain.

    This is the FSM's state bag — it is NOT persisted; a restart resets it.
    """

    name: str
    location: str
    state: str  # "idle" | "moving"
    idle_ticks: int
    hangout: str  # the location the resident gravitates back to


def _move_cmd(destination: str) -> dict:
    """Build a real handle_tool_command move payload."""
    return {"action": "act", "payload": {"action": "move", "destination": destination}}


def next_action(runtime: ResidentRuntime, rng_value: float) -> dict:
    """Decide the resident's next action. Pure: same inputs → same output.

    Args:
        runtime:   Current FSM state for this resident.
        rng_value: A float in [0, 1) used only for the occasional wander roll
                   when the resident is already settled at their hangout.
                   Movement decisions are otherwise deterministic.

    Returns:
        A real handle_tool_command cmd dict, or a fresh copy of NOOP to do
        nothing this tick.
    """
    # Already en route — don't issue another command until arrival is reported.
    if runtime.state == "moving":
        return dict(NOOP)

    # Below threshold: not enough idle time to trigger movement yet.
    if runtime.idle_ticks < _IDLE_TICKS_BEFORE_MOVE:
        return dict(NOOP)

    # Not at hangout → drift home.
    if runtime.location != runtime.hangout:
        return _move_cmd(runtime.hangout)

    # Settled at hangout: occasional wander (rng_value < _WANDER_CHANCE).
    if rng_value < _WANDER_CHANCE:
        others = [d for d in _VALID_DESTINATIONS if d != runtime.location]
        # Rescale rng_value from [0, _WANDER_CHANCE) to [0, 1) so the pick spans
        # ALL other destinations, not just the first one or two. Deterministic.
        idx = int((rng_value / _WANDER_CHANCE) * len(others)) % len(others)
        return _move_cmd(others[idx])

    return dict(NOOP)
