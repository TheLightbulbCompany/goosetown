"""Pure FSM for resident movement decisions.

No I/O, no daemon coupling — same inputs always produce the same output.
"""

from dataclasses import dataclass, field

# Town locations a resident may move to.
_VALID_DESTINATIONS = (
    "plaza",
    "library",
    "cafe",
    "activity_center",
    "bank",
    "residence",
)

# Apartment location residents go to when sleeping. The daemon handles the
# town→apartment context transition transparently when the brain emits a
# cross-context destination.
_BED = "bed"

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
    location_context: str = "town"  # "town" | "apartment"
    sleep_hour: int = 22  # UTC hour they go to sleep
    wake_hour: int = 6  # UTC hour they wake up

    # ---- Phase 2 memory bookkeeping (only used by the agentic path) ----
    # The live conversation transcript is cached each in-conversation tick so
    # that when ``active_conversation`` goes set→None the brain can reflect on
    # the just-ended exchange (covers end-action, inactivity, and turn-cap
    # paths uniformly — no _finalize_conversation hook). All gated on
    # settings.RESIDENT_MEMORY_ENABLED; left at defaults when memory is off.
    last_conv_id: str | None = None  # conv_id cached while talking
    last_conv_turns: list = field(default_factory=list)  # cached transcript turns
    last_conv_participants: list = field(default_factory=list)  # cached participant display names
    reflected_conv_ids: set = field(default_factory=set)  # convs already reflected on (dedupe)
    memories_since_reflection: int = 0  # new observation memories since last synthesis

    # ---- Phase 3 social-goal bookkeeping (only used by the agentic path) ----
    # Gated on settings.RESIDENT_SOCIAL_GOALS_ENABLED; left at defaults when off.
    social_goal: str = ""  # current short-term social aim injected into status each tick
    ticks_since_social_goal: int = 0  # ticks elapsed since last goal generation


def _move_cmd(destination: str) -> dict:
    """Build a real handle_tool_command move payload."""
    return {"action": "act", "payload": {"action": "move", "destination": destination}}


def is_in_sleep_window(hour: int, sleep_hour: int, wake_hour: int) -> bool:
    """Return True if ``hour`` (0-23 UTC) is within the sleep window
    ``[sleep_hour, wake_hour)``. Window wraps midnight when sleep_hour > wake_hour.

    Boundaries: ``sleep_hour`` is inclusive (start of sleep), ``wake_hour`` is
    exclusive (start of waking) so the boundary case ``hour == wake_hour`` is
    awake. ``sleep_hour == wake_hour`` means "never sleeps".
    """
    if sleep_hour == wake_hour:
        return False
    if sleep_hour < wake_hour:
        return sleep_hour <= hour < wake_hour
    # Wraps midnight, e.g. sleep=22, wake=6 → asleep when hour ∈ [22,24) ∪ [0,6)
    return hour >= sleep_hour or hour < wake_hour


def next_action(runtime: ResidentRuntime, rng_value: float, now_utc_hour: int | None = None) -> dict:
    """Decide the resident's next action. Pure: same inputs → same output.

    Args:
        runtime:       Current FSM state for this resident.
        rng_value:     Float in [0, 1) used for the occasional wander roll
                       when the resident is settled at their hangout.
        now_utc_hour:  Optional int 0-23 representing the current UTC hour. When
                       provided, the FSM applies the sleep gate: in-window →
                       go to bed; awake → normal hangout/wander logic. When
                       None, sleep is ignored (used by some legacy tests).

    Returns:
        A real handle_tool_command cmd dict, or a fresh copy of NOOP.
    """
    # Already en route — don't issue another command until arrival is reported.
    if runtime.state == "moving":
        return dict(NOOP)

    # Below threshold: not enough idle time to trigger movement yet.
    if runtime.idle_ticks < _IDLE_TICKS_BEFORE_MOVE:
        return dict(NOOP)

    # Sleep gate. If now_utc_hour is within the persona's sleep window, the
    # resident either stays asleep at bed (noop) or heads home to bed. The
    # daemon handles the cross-context transition transparently.
    if now_utc_hour is not None and is_in_sleep_window(now_utc_hour, runtime.sleep_hour, runtime.wake_hour):
        if runtime.location == _BED:
            return dict(NOOP)
        return _move_cmd(_BED)

    # Awake. Are we at the hangout?
    #
    # Two cases are both "at hangout":
    #   1. Flat town hangouts (plaza, cafe, activity_center, bank): the agent's
    #      reported `location` equals the hangout name.
    #   2. Interior-scene hangouts (library): the hangout has its own scene
    #      with sub-locations. On arrival the agent's `location_context`
    #      becomes the hangout name (e.g. "library") and `location` becomes
    #      a sub-location like "exit". Without this branch the brain would
    #      see location ("exit") != hangout ("library") and re-issue the
    #      move forever, observed on dev as residents bouncing in and out
    #      of the library every tick.
    at_hangout = runtime.location == runtime.hangout or runtime.location_context == runtime.hangout
    if not at_hangout:
        return _move_cmd(runtime.hangout)

    # Settled at hangout: occasional wander (rng_value < _WANDER_CHANCE).
    if rng_value < _WANDER_CHANCE:
        others = [d for d in _VALID_DESTINATIONS if d != runtime.hangout]
        # Rescale rng_value from [0, _WANDER_CHANCE) to [0, 1) so the pick spans
        # ALL other destinations, not just the first one or two. Deterministic.
        idx = int((rng_value / _WANDER_CHANCE) * len(others)) % len(others)
        return _move_cmd(others[idx])

    return dict(NOOP)
