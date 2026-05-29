"""Integrity tests for the RESIDENT_PERSONAS slate."""

from worldgm.residents.behavior import _VALID_DESTINATIONS, is_in_sleep_window
from worldgm.residents.personas import RESIDENT_PERSONAS, Persona

# The cast we intend to ship — keep this in sync with the design doc.
_EXPECTED_COUNT = 20


def test_resident_personas_is_the_expected_size():
    assert len(RESIDENT_PERSONAS) == _EXPECTED_COUNT


def test_resident_persona_names_are_unique():
    names = [p.name for p in RESIDENT_PERSONAS]
    assert len(set(names)) == len(names), f"duplicate name(s) in cast: {names}"


def test_resident_persona_display_names_are_unique():
    displays = [p.display_name for p in RESIDENT_PERSONAS]
    assert len(set(displays)) == len(displays)


def test_all_required_string_fields_are_non_empty():
    for p in RESIDENT_PERSONAS:
        for field in ("name", "display_name", "personality", "appearance", "hangout"):
            value = getattr(p, field)
            assert isinstance(value, str) and value.strip(), f"{p.name}.{field} must be a non-empty string"


def test_traits_are_non_empty_tuple_of_strings():
    for p in RESIDENT_PERSONAS:
        assert isinstance(p.traits, tuple) and len(p.traits) >= 1, f"{p.name} traits must be a non-empty tuple"
        assert all(isinstance(t, str) and t for t in p.traits), f"{p.name} traits must all be non-empty strings"


def test_hangouts_are_valid_town_locations():
    for p in RESIDENT_PERSONAS:
        assert p.hangout in _VALID_DESTINATIONS, (
            f"{p.name} hangs out at {p.hangout!r}, which is not a valid town location"
        )


def test_sleep_hours_are_valid_clock_values():
    for p in RESIDENT_PERSONAS:
        assert 0 <= p.sleep_hour <= 23, p.name
        assert 0 <= p.wake_hour <= 23, p.name
        # Sleep and wake should not be identical (would mean "never sleeps").
        assert p.sleep_hour != p.wake_hour, f"{p.name} has sleep == wake hour"


def test_tobias_is_already_live_resident():
    """Tobias is the resident that landed in Phase 1; keep him in the cast."""
    tobias = next((p for p in RESIDENT_PERSONAS if p.name == "tobias"), None)
    assert tobias is not None
    assert tobias.hangout == "plaza"


def test_at_least_one_resident_awake_every_hour():
    """At every UTC hour someone in the cast is awake — keeps the town from
    falling completely silent at any clock time."""
    for hour in range(24):
        awake = [p for p in RESIDENT_PERSONAS if not is_in_sleep_window(hour, p.sleep_hour, p.wake_hour)]
        assert awake, f"no resident awake at UTC hour {hour}"


def test_persona_dataclass_is_hashable_frozen():
    # frozen dataclass means residents can be used as dict keys / set members.
    sample = RESIDENT_PERSONAS[0]
    assert isinstance(sample, Persona)
    assert hash(sample) == hash(sample)
