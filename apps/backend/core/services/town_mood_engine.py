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
    "solitary_activity": (3, 2),
    "arrived_new_location": (-2, 0),
    "sleep": (None, 0),  # None = reset to 100
}

# Trait modifiers: trait -> event -> (energy_delta, mood_delta) overrides
_TRAIT_MODIFIERS = {
    "introvert": {
        "conversation_completed": (-10, 3),
        "solitary_activity": (5, 5),
    },
    "extrovert": {
        "conversation_completed": (-2, 8),
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
