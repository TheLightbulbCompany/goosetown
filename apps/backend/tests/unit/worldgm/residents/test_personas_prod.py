"""Integrity tests for the PROD_RESIDENT_PERSONAS slate + slate selection."""

from worldgm.residents.behavior import _VALID_DESTINATIONS, is_in_sleep_window
from worldgm.residents.personas import Persona
from worldgm.residents.personas_prod import PROD_RESIDENT_PERSONAS

# The prod cast we intend to ship — 19 owner-approved figures (2026-06-11).
_EXPECTED_COUNT = 19


def test_prod_personas_is_the_expected_size():
    assert len(PROD_RESIDENT_PERSONAS) == _EXPECTED_COUNT


def test_prod_persona_names_are_unique():
    names = [p.name for p in PROD_RESIDENT_PERSONAS]
    assert len(set(names)) == len(names), f"duplicate name(s) in cast: {names}"


def test_prod_persona_display_names_are_unique():
    displays = [p.display_name for p in PROD_RESIDENT_PERSONAS]
    assert len(set(displays)) == len(displays)


def test_prod_all_required_string_fields_are_non_empty():
    for p in PROD_RESIDENT_PERSONAS:
        for field in ("name", "display_name", "personality", "appearance", "hangout"):
            value = getattr(p, field)
            assert isinstance(value, str) and value.strip(), f"{p.name}.{field} must be a non-empty string"


def test_prod_traits_are_non_empty_tuple_of_strings():
    for p in PROD_RESIDENT_PERSONAS:
        assert isinstance(p.traits, tuple) and len(p.traits) >= 1, f"{p.name} traits must be a non-empty tuple"
        assert all(isinstance(t, str) and t for t in p.traits), f"{p.name} traits must all be non-empty strings"


def test_prod_hangouts_are_valid_town_locations():
    for p in PROD_RESIDENT_PERSONAS:
        assert p.hangout in _VALID_DESTINATIONS, (
            f"{p.name} hangs out at {p.hangout!r}, which is not a valid town location"
        )


def test_prod_sleep_hours_are_valid_clock_values():
    for p in PROD_RESIDENT_PERSONAS:
        assert 0 <= p.sleep_hour <= 23, p.name
        assert 0 <= p.wake_hour <= 23, p.name
        assert p.sleep_hour != p.wake_hour, f"{p.name} has sleep == wake hour"


def test_prod_personas_are_persona_instances():
    for p in PROD_RESIDENT_PERSONAS:
        assert isinstance(p, Persona)


def test_dracula_is_nocturnal():
    dracula = next((p for p in PROD_RESIDENT_PERSONAS if p.name == "dracula"), None)
    assert dracula is not None
    # Sleeps at dawn, wakes at dusk — awake at midnight, asleep at noon.
    assert is_in_sleep_window(12, dracula.sleep_hour, dracula.wake_hour)
    assert not is_in_sleep_window(0, dracula.sleep_hour, dracula.wake_hour)


def test_prod_at_least_one_resident_awake_every_hour():
    for hour in range(24):
        awake = [p for p in PROD_RESIDENT_PERSONAS if not is_in_sleep_window(hour, p.sleep_hour, p.wake_hour)]
        assert awake, f"no resident awake at UTC hour {hour}"


def test_resident_slate_env_selects_prod(monkeypatch):
    """RESIDENT_SLATE=prod -> prod cast; unset/other -> default cast."""
    from worldgm.__main__ import _resident_personas
    from worldgm.residents.personas import RESIDENT_PERSONAS

    monkeypatch.setenv("RESIDENT_SLATE", "prod")
    assert _resident_personas() is PROD_RESIDENT_PERSONAS

    monkeypatch.setenv("RESIDENT_SLATE", "default")
    assert _resident_personas() is RESIDENT_PERSONAS

    monkeypatch.delenv("RESIDENT_SLATE", raising=False)
    assert _resident_personas() is RESIDENT_PERSONAS
