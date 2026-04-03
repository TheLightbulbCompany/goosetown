"""Tests for core/services/activity_registry.py."""

from core.services.activity_registry import (
    Activity,
    apply_activity_effect,
    canonical_location,
    get_activities_for_location,
    get_activity,
)


class TestCanonicalLocation:
    def test_town_location_passes_through(self):
        assert canonical_location("library") == "library"

    def test_plaza_passes_through(self):
        assert canonical_location("plaza") == "plaza"

    def test_apartment_string_maps_to_apartment(self):
        assert canonical_location("apartment") == "apartment"

    def test_desk_spot_maps_to_apartment(self):
        assert canonical_location("desk") == "apartment"

    def test_couch_spot_maps_to_apartment(self):
        assert canonical_location("couch") == "apartment"

    def test_bed_spot_maps_to_apartment(self):
        assert canonical_location("bed") == "apartment"

    def test_bookshelf_spot_maps_to_apartment(self):
        assert canonical_location("bookshelf") == "apartment"

    def test_stove_spot_maps_to_apartment(self):
        assert canonical_location("stove") == "apartment"

    def test_unknown_location_passes_through(self):
        assert canonical_location("marketplace") == "marketplace"


class TestGetActivity:
    def test_returns_activity_for_valid_name(self):
        activity = get_activity("read")
        assert activity is not None
        assert activity.name == "read"
        assert activity.location == "library"

    def test_returns_none_for_unknown_name(self):
        assert get_activity("fly_to_moon") is None

    def test_sleep_activity_exists(self):
        activity = get_activity("sleep")
        assert activity is not None
        assert activity.location == "apartment"
        assert activity.energy_delta == 100

    def test_all_activities_have_required_fields(self):
        """Every registered activity must have name, location, duration."""
        from core.services.activity_registry import ACTIVITY_REGISTRY

        for name, activity in ACTIVITY_REGISTRY.items():
            assert activity.name == name, f"Key mismatch for {name}"
            assert activity.location, f"Missing location for {name}"
            assert activity.duration_seconds > 0, f"Bad duration for {name}"
            assert activity.agent_prompt, f"Missing agent_prompt for {name}"


class TestGetActivitiesForLocation:
    def test_library_has_activities(self):
        activities = get_activities_for_location("library")
        names = {a.name for a in activities}
        assert "read" in names
        assert "study" in names
        assert "write_journal" in names

    def test_plaza_has_activities(self):
        activities = get_activities_for_location("plaza")
        names = {a.name for a in activities}
        assert "people_watch" in names
        assert "perform" in names

    def test_cafe_has_activities(self):
        activities = get_activities_for_location("cafe")
        names = {a.name for a in activities}
        assert "order_coffee" in names
        assert "sit_and_chat" in names
        assert "work_on_laptop" in names

    def test_apartment_spot_returns_apartment_activities(self):
        """Spot names like 'desk' must resolve to the apartment activity list."""
        desk_activities = get_activities_for_location("desk")
        apartment_activities = get_activities_for_location("apartment")
        assert desk_activities == apartment_activities

    def test_apartment_has_sleep_and_work(self):
        activities = get_activities_for_location("apartment")
        names = {a.name for a in activities}
        assert "sleep" in names
        assert "work_at_desk" in names
        assert "cook" in names
        assert "read_book" in names

    def test_unknown_location_returns_empty_list(self):
        activities = get_activities_for_location("moon_base")
        assert activities == []

    def test_residence_has_sit_outside(self):
        activities = get_activities_for_location("residence")
        names = {a.name for a in activities}
        assert "sit_outside" in names

    def test_no_activity_appears_at_wrong_location(self):
        """Library activities must not appear at the café."""
        cafe_names = {a.name for a in get_activities_for_location("cafe")}
        assert "read" not in cafe_names
        assert "study" not in cafe_names


class TestApplyActivityEffect:
    def _make_activity(self, **kwargs) -> Activity:
        defaults = dict(
            name="test",
            location="library",
            duration_seconds=15,
            mood_delta=4,
            energy_delta=-3,
            animation="reading",
            description="Test",
            agent_prompt="Test prompt",
        )
        defaults.update(kwargs)
        return Activity(**defaults)

    def test_base_effect_no_traits(self):
        activity = self._make_activity(mood_delta=4, energy_delta=-3)
        new_energy, new_mood, interrupted = apply_activity_effect(
            activity, current_energy=80, current_mood=0, traits="", elapsed_seconds=15
        )
        assert new_energy == 77
        assert new_mood == 4
        assert not interrupted

    def test_introvert_bonus_applied(self):
        activity = self._make_activity(mood_delta=4, energy_delta=-3, introvert_bonus=2)
        new_energy, new_mood, interrupted = apply_activity_effect(
            activity, current_energy=80, current_mood=0, traits="introvert", elapsed_seconds=15
        )
        assert new_mood == 6  # 4 + 2 introvert bonus
        assert not interrupted

    def test_extrovert_bonus_applied(self):
        activity = self._make_activity(mood_delta=4, energy_delta=-2, extrovert_bonus=2)
        new_energy, new_mood, interrupted = apply_activity_effect(
            activity, current_energy=80, current_mood=0, traits="extrovert", elapsed_seconds=15
        )
        assert new_mood == 6  # 4 + 2 extrovert bonus
        assert not interrupted

    def test_introvert_does_not_get_extrovert_bonus(self):
        activity = self._make_activity(mood_delta=4, energy_delta=-3, extrovert_bonus=2)
        new_energy, new_mood, interrupted = apply_activity_effect(
            activity, current_energy=80, current_mood=0, traits="introvert", elapsed_seconds=15
        )
        assert new_mood == 4  # base only, no extrovert bonus for introvert

    def test_energy_clamped_to_100(self):
        """sleep gives +100 energy — must clamp to 100."""
        activity = self._make_activity(mood_delta=5, energy_delta=100)
        new_energy, new_mood, interrupted = apply_activity_effect(
            activity, current_energy=30, current_mood=0, traits="", elapsed_seconds=30
        )
        assert new_energy == 100
        assert not interrupted

    def test_mood_clamped_at_positive_50(self):
        activity = self._make_activity(mood_delta=10)
        new_energy, new_mood, interrupted = apply_activity_effect(
            activity, current_energy=80, current_mood=45, traits="", elapsed_seconds=15
        )
        assert new_mood == 50

    def test_mood_clamped_at_negative_50(self):
        activity = self._make_activity(mood_delta=-10, energy_delta=0)
        new_energy, new_mood, interrupted = apply_activity_effect(
            activity, current_energy=80, current_mood=-45, traits="", elapsed_seconds=15
        )
        assert new_mood == -50

    def test_interrupted_when_energy_would_go_below_zero(self):
        activity = self._make_activity(mood_delta=4, energy_delta=-10, duration_seconds=15)
        new_energy, new_mood, interrupted = apply_activity_effect(
            activity, current_energy=5, current_mood=0, traits="", elapsed_seconds=15
        )
        assert interrupted
        assert new_energy == 0

    def test_interrupted_partial_mood_proportional(self):
        """Half-way through a 15s activity should give roughly half the mood."""
        activity = self._make_activity(mood_delta=4, energy_delta=-10, duration_seconds=15)
        new_energy, new_mood, interrupted = apply_activity_effect(
            activity, current_energy=5, current_mood=0, traits="", elapsed_seconds=7.5
        )
        assert interrupted
        assert new_mood == 2  # round(4 * 0.5) = 2

    def test_not_interrupted_when_energy_exactly_zero(self):
        """Reaching exactly 0 energy counts as completion, not interruption."""
        activity = self._make_activity(mood_delta=4, energy_delta=-10)
        new_energy, new_mood, interrupted = apply_activity_effect(
            activity, current_energy=10, current_mood=0, traits="", elapsed_seconds=15
        )
        assert not interrupted
        assert new_energy == 0
        assert new_mood == 4

    def test_empty_traits_string_uses_base_effects(self):
        activity = self._make_activity(mood_delta=3, energy_delta=-2, introvert_bonus=2)
        new_energy, new_mood, interrupted = apply_activity_effect(
            activity, current_energy=50, current_mood=10, traits="", elapsed_seconds=10
        )
        assert new_mood == 13  # 10 + 3, no bonus

    def test_multiple_traits_first_matching_wins(self):
        """If an agent has both introvert and extrovert traits, introvert wins (first match)."""
        activity = self._make_activity(mood_delta=4, energy_delta=-3, introvert_bonus=2, extrovert_bonus=3)
        new_energy, new_mood, interrupted = apply_activity_effect(
            activity,
            current_energy=80,
            current_mood=0,
            traits="introvert, extrovert",
            elapsed_seconds=15,
        )
        assert new_mood == 6  # introvert bonus applied
