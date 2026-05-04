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


# Base effects: (energy_delta, mood_delta)
_BASE_EFFECTS = {
    "conversation_completed": (-5, 5),
    # Per-turn drip applied to the speaker on every successful ``say``.
    # Small by design: this is the "I was heard" reward that distinguishes
    # a 20-turn conversation from a 2-turn one. Wired 2026-05-04 to fix
    # the back-loaded-reward bug surfaced in the game-designer audit.
    "conversation_turn": (-1, 1),
    "solitary_activity": (3, 2),
    "arrived_new_location": (-2, 0),
    "sleep": (None, 0),  # None = reset to 100
}

# Trait modifiers: trait -> event -> (energy_delta, mood_delta) overrides
_TRAIT_MODIFIERS = {
    "introvert": {
        "conversation_completed": (-10, 3),
        # Introverts get no per-turn boost (and no penalty either) —
        # they get the big reward at the end (conversation_completed +3),
        # not for keeping a long conversation going.
        "conversation_turn": (-1, 0),
        "solitary_activity": (5, 5),
    },
    "extrovert": {
        "conversation_completed": (-2, 8),
        # Extroverts genuinely enjoy sustained conversation — bigger
        # per-turn drip rewards length.
        "conversation_turn": (-1, 2),
        "solitary_activity": (2, -2),
        "arrived_new_location": (-2, 3),
    },
}


def apply_event(
    event: str,
    energy: int,
    mood: int,
    traits: str,
) -> tuple[int, int]:
    """Apply an event's effect on energy and mood, modulated by traits.

    Returns (new_energy, new_mood) clamped to valid ranges.
    """
    base = _BASE_EFFECTS.get(event)
    if base is None:
        return energy, mood

    energy_delta, mood_delta = base

    # Check trait overrides
    trait_set = {t.strip() for t in traits.split(",") if t.strip()} if traits else set()
    for trait in trait_set:
        overrides = _TRAIT_MODIFIERS.get(trait, {})
        if event in overrides:
            energy_delta, mood_delta = overrides[event]
            break  # First matching trait wins

    # Apply
    if energy_delta is None:
        energy = 100  # sleep resets
    else:
        energy = max(0, min(100, energy + energy_delta))

    mood = max(-50, min(50, mood + mood_delta))

    return energy, mood
