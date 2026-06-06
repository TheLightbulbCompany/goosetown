from core.services.town_mood_engine import apply_event, mood_label, normalize_traits, parse_mood


class TestMoodLabel:
    def test_negative_mood_miserable(self):
        assert mood_label(-50) == "miserable"

    def test_negative_mood_sad(self):
        assert mood_label(-15) == "sad"

    def test_zero_mood_neutral(self):
        assert mood_label(0) == "neutral"

    def test_positive_mood_happy(self):
        assert mood_label(15) == "happy"

    def test_high_mood_elated(self):
        assert mood_label(40) == "elated"


class TestParseMood:
    def test_parse_int(self):
        assert parse_mood(10) == 10

    def test_parse_string_int(self):
        assert parse_mood("25") == 25

    def test_parse_negative_string(self):
        assert parse_mood("-15") == -15

    def test_parse_none(self):
        assert parse_mood(None) == 0

    def test_parse_label_string(self):
        assert parse_mood("neutral") == 0

    def test_parse_empty_string(self):
        assert parse_mood("") == 0


class TestApplyEvent:
    def test_conversation_completed_base(self):
        energy, mood = apply_event("conversation_completed", energy=80, mood=0, traits="")
        assert energy == 75  # -5
        assert mood == 5  # +5

    def test_conversation_completed_introvert(self):
        energy, mood = apply_event("conversation_completed", energy=80, mood=0, traits="introvert")
        assert energy == 70  # -10
        assert mood == 3  # +3

    def test_conversation_completed_extrovert(self):
        energy, mood = apply_event("conversation_completed", energy=80, mood=0, traits="extrovert")
        assert energy == 78  # -2
        assert mood == 8  # +8

    def test_solitary_activity_base(self):
        energy, mood = apply_event("solitary_activity", energy=50, mood=0, traits="")
        assert energy == 53  # +3
        assert mood == 2  # +2

    def test_solitary_activity_introvert(self):
        energy, mood = apply_event("solitary_activity", energy=50, mood=0, traits="introvert")
        assert energy == 55  # +5
        assert mood == 5  # +5

    def test_solitary_activity_extrovert(self):
        energy, mood = apply_event("solitary_activity", energy=50, mood=0, traits="extrovert")
        assert energy == 52  # +2
        assert mood == -2  # -2

    def test_arrived_new_location_base(self):
        energy, mood = apply_event("arrived_new_location", energy=80, mood=0, traits="")
        assert energy == 78  # -2
        assert mood == 0

    def test_arrived_new_location_extrovert(self):
        energy, mood = apply_event("arrived_new_location", energy=80, mood=0, traits="extrovert")
        assert energy == 78  # -2
        assert mood == 3  # +3

    def test_sleep_resets_energy(self):
        energy, mood = apply_event("sleep", energy=20, mood=-10, traits="")
        assert energy == 100
        assert mood == -10  # mood unchanged

    def test_energy_clamped_to_0_100(self):
        energy, mood = apply_event("conversation_completed", energy=2, mood=0, traits="introvert")
        assert energy == 0  # clamped, not negative

    def test_mood_clamped_to_negative_50_positive_50(self):
        energy, mood = apply_event("conversation_completed", energy=80, mood=48, traits="extrovert")
        assert mood == 50  # clamped

    def test_unknown_event_no_change(self):
        energy, mood = apply_event("unknown_event", energy=80, mood=10, traits="")
        assert energy == 80
        assert mood == 10

    # --- list-typed traits (residents store traits as a DynamoDB list) ---

    def test_list_traits_do_not_crash_and_match_overrides(self):
        # Regression: residents' list traits made traits.split(",") raise
        # AttributeError, so every mood event silently failed and resident
        # mood stayed frozen. A list must behave like the equivalent string.
        e_str, m_str = apply_event("conversation_completed", energy=80, mood=0, traits="introvert")
        e_list, m_list = apply_event("conversation_completed", energy=80, mood=0, traits=["introvert"])
        assert (e_list, m_list) == (e_str, m_str) == (70, 3)

    def test_list_traits_extrovert_among_several(self):
        energy, mood = apply_event(
            "conversation_completed", energy=80, mood=0, traits=["chatty", "extrovert", "warm"]
        )
        assert (energy, mood) == (78, 8)

    def test_none_traits_uses_base_effect(self):
        energy, mood = apply_event("conversation_completed", energy=80, mood=0, traits=None)
        assert (energy, mood) == (75, 5)


class TestNormalizeTraits:
    def test_comma_string(self):
        assert normalize_traits("introvert, Curious ,, melancholic") == {
            "introvert",
            "curious",
            "melancholic",
        }

    def test_list(self):
        assert normalize_traits(["Curious", "literal", "unflappable"]) == {
            "curious",
            "literal",
            "unflappable",
        }

    def test_none_and_empty(self):
        assert normalize_traits(None) == set()
        assert normalize_traits("") == set()
        assert normalize_traits([]) == set()
