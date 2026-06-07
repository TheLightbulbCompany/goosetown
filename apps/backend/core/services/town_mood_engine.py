"""Event-driven mood and energy engine for GooseTown agents.

Mood: -50 (miserable) to +50 (elated)
Energy: 0 to 100

Changes are triggered by events, modulated by personality traits.
No passive decay — energy and mood only change when things happen.
"""


def parse_mood(raw) -> int:
    """Parse a mood value from DB (string, int, or None) to int."""
    if raw is None:
        return 0
    if isinstance(raw, int):
        return raw
    try:
        return int(raw)
    except (ValueError, TypeError):
        return 0


def parse_mood_updated_at(raw) -> float:
    """Parse the agent record's ``moodUpdatedAt`` (Decimal / float / str / None)
    to a Unix-seconds float. Absent or unparseable → ``0.0`` (no decay)."""
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def decayed_mood(agent: dict, now: float) -> int:
    """The agent record's current mood, decayed toward neutral by the elapsed
    time since its last mood write (``moodUpdatedAt``).

    Convenience for callers that apply an effect to the agent's *current* mood
    (e.g. activity completion) — decay once, here, on the pre-effect baseline,
    so the freshly-applied effect isn't itself decayed.
    """
    mood = parse_mood(agent.get("mood"))
    elapsed = max(0.0, now - parse_mood_updated_at(agent.get("moodUpdatedAt")))
    return apply_decay(mood, elapsed)


def mood_label(mood: int) -> str:
    """Convert numeric mood to a human-readable label."""
    if mood <= -30:
        return "miserable"
    elif mood <= -10:
        return "sad"
    elif mood <= 10:
        return "neutral"
    elif mood <= 30:
        return "happy"
    else:
        return "elated"


# Base effects: (energy_delta, mood_delta).
#
# Mood deltas are deliberately small. The rebalance (2026-06-06) pairs them with
# personality sign-differentiation (below), time-decay toward neutral, and
# diminishing-returns-near-the-cap so mood settles at a personality-dependent
# equilibrium instead of every resident ratcheting to +50. See
# docs/superpowers/specs/2026-06-06-mood-rebalance-design.md.
_BASE_EFFECTS = {
    "conversation_completed": (-5, 2),
    # Per-turn drip on every successful ``say``. Neutral agents get 0 — the
    # reward (or drain) is at completion and via the trait overrides; a per-turn
    # drip that accumulated was a key saturation driver in a chatty town.
    "conversation_turn": (-1, 0),
    "solitary_activity": (3, 2),
    "arrived_new_location": (-2, 0),
    "sleep": (None, 0),  # None = reset energy to 100; mood reset is the sleep activity
}

# Trait modifiers: trait -> event -> (energy_delta, mood_delta) overrides.
#
# The sign flips are the heart of the rebalance: in a hyper-social town the
# constant conversation stream should DRAIN introverts (trend negative) and
# ENERGIZE extroverts (trend positive), so personality reads as a real,
# divergent signal — not just different rates of the same upward ratchet.
_TRAIT_MODIFIERS = {
    "introvert": {
        # Drained by each completed conversation, and a small per-turn drain
        # during long ones — constant socializing pulls an introvert down.
        "conversation_completed": (-10, -4),
        "conversation_turn": (-1, -1),
        # Recharge alone.
        "solitary_activity": (5, 5),
        # No arrival boost (extroverts get one).
        "arrived_new_location": (-2, 0),
    },
    "extrovert": {
        # Energized by socializing; isolation is mildly draining.
        "conversation_completed": (-2, 6),
        "conversation_turn": (-1, 2),
        "solitary_activity": (2, -3),
        "arrived_new_location": (-2, 3),
    },
}

# Mood drifts back toward neutral with this half-life when no events fire, so
# mood reflects roughly the last 15-30 minutes of activity rather than a
# lifetime accumulation. Lazy (computed at event time from ``moodUpdatedAt``) —
# no background ticker.
MOOD_DECAY_HALF_LIFE_MINUTES = 20.0


def apply_decay(mood: int, elapsed_seconds: float) -> int:
    """Decay mood exponentially toward 0. Half-life = MOOD_DECAY_HALF_LIFE_MINUTES."""
    if elapsed_seconds <= 0 or mood == 0:
        return mood
    elapsed_minutes = elapsed_seconds / 60.0
    decay_factor = 0.5 ** (elapsed_minutes / MOOD_DECAY_HALF_LIFE_MINUTES)
    return round(mood * decay_factor)


def apply_diminishing_returns(delta: int, current_mood: int) -> int:
    """Scale a mood delta by distance from the relevant ceiling/floor so ±50 is
    asymptotic, not a hard wall every event slams into.

    Positive deltas scale by (1 - mood/50); negative by (1 + mood/50). At mood 0
    the full delta applies; at the matching cap it scales to 0.
    """
    if delta == 0:
        return 0
    if delta > 0:
        factor = max(0.0, 1.0 - current_mood / 50.0)
    else:
        factor = max(0.0, 1.0 + current_mood / 50.0)
    return round(delta * factor)


def normalize_traits(traits: "str | list[str] | tuple[str, ...] | set[str] | None") -> set[str]:
    """Normalize an agent's traits into a lowercased set, whatever the shape.

    The agent record stores traits two ways: user-registered agents keep a
    comma-separated string, while residents are created with a list (persisted
    as a DynamoDB list, so boto3 hands it back as a Python list). Accept both
    — and ``None`` — so the mood path never crashes on the data's shape.

    Regression: residents' list-typed traits made ``traits.split(",")`` raise
    ``AttributeError: 'list' object has no attribute 'split'`` on every mood
    event (conversation, activity completion, arrival), so the mood engine
    silently failed and resident mood stayed frozen at its initial value.
    """
    if not traits:
        return set()
    parts = traits.split(",") if isinstance(traits, str) else list(traits)
    return {str(t).strip().lower() for t in parts if str(t).strip()}


def apply_event(
    event: str,
    energy: int,
    mood: int,
    traits: "str | list[str] | None",
    now: float = 0.0,
    mood_updated_at: float = 0.0,
) -> tuple[int, int]:
    """Apply an event's effect on energy and mood, modulated by traits.

    ``traits`` may be a comma-separated string or a list (see
    ``normalize_traits``).

    ``now`` / ``mood_updated_at`` are Unix timestamps used for lazy mood decay:
    before applying the event's delta, mood is decayed toward neutral by the
    elapsed time since the last mood write. Both default to ``0.0`` (no decay),
    so callers that don't yet pass timestamps keep the old additive behavior.

    Order: decay → trait-adjusted delta → diminishing-returns → clamp. Returns
    (new_energy, new_mood) clamped to valid ranges.
    """
    base = _BASE_EFFECTS.get(event)
    if base is None:
        # No event delta — but still apply decay so an unrelated event doesn't
        # freeze the mood clock. (Unknown events are rare; keep mood honest.)
        if now > 0 and mood_updated_at > 0:
            mood = apply_decay(mood, max(0.0, now - mood_updated_at))
        return energy, mood

    energy_delta, mood_delta = base

    # Check trait overrides
    trait_set = normalize_traits(traits)
    for trait in trait_set:
        overrides = _TRAIT_MODIFIERS.get(trait, {})
        if event in overrides:
            energy_delta, mood_delta = overrides[event]
            break  # First matching trait wins

    # Energy: simple additive (sleep resets to 100).
    if energy_delta is None:
        energy = 100
    else:
        energy = max(0, min(100, energy + energy_delta))

    # Mood: decay toward neutral, then apply the delta with diminishing returns
    # near the cap, then clamp as a final safety net.
    elapsed = max(0.0, now - mood_updated_at) if now > 0 and mood_updated_at > 0 else 0.0
    mood = apply_decay(mood, elapsed)
    mood = max(-50, min(50, mood + apply_diminishing_returns(mood_delta, mood)))

    return energy, mood
