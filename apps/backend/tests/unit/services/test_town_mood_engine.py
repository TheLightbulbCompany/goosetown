from core.services.town_mood_engine import (
    apply_decay,
    apply_diminishing_returns,
    apply_event,
    mood_label,
    normalize_traits,
    parse_mood,
)


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
        assert mood == 2  # +2 (rebalanced; DR(+2,0)=+2)

    def test_conversation_completed_introvert(self):
        # Sign-flipped: socializing now DRAINS introverts.
        energy, mood = apply_event("conversation_completed", energy=80, mood=0, traits="introvert")
        assert energy == 70  # -10
        assert mood == -4  # -4

    def test_conversation_completed_extrovert(self):
        energy, mood = apply_event("conversation_completed", energy=80, mood=0, traits="extrovert")
        assert energy == 78  # -2
        assert mood == 6  # +6

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
        assert mood == -3  # -3 (rebalanced; solitude drains extroverts more)

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

    def test_diminishing_returns_damps_near_cap(self):
        # Near +50, diminishing returns scales the +6 extrovert delta to ~0, so
        # mood asymptotes toward the ceiling instead of slamming into it.
        # DR(+6, 48) = round(6 * (1 - 48/50)) = round(0.24) = 0 → mood stays 48.
        energy, mood = apply_event("conversation_completed", energy=80, mood=48, traits="extrovert")
        assert mood == 48
        assert mood <= 50

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
        assert (e_list, m_list) == (e_str, m_str) == (70, -4)

    def test_list_traits_extrovert_among_several(self):
        energy, mood = apply_event(
            "conversation_completed", energy=80, mood=0, traits=["chatty", "extrovert", "warm"]
        )
        assert (energy, mood) == (78, 6)

    def test_none_traits_uses_base_effect(self):
        energy, mood = apply_event("conversation_completed", energy=80, mood=0, traits=None)
        assert (energy, mood) == (75, 2)


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


class TestDecay:
    def test_half_life_20_min(self):
        # +40 after one half-life (20 min) decays to ~+20.
        assert apply_decay(40, 20 * 60) == 20

    def test_negative_decays_toward_zero(self):
        assert apply_decay(-30, 20 * 60) == -15

    def test_zero_elapsed_no_change(self):
        assert apply_decay(40, 0) == 40

    def test_zero_mood_no_change(self):
        assert apply_decay(0, 9999) == 0


class TestDiminishingReturns:
    def test_full_delta_at_neutral(self):
        assert apply_diminishing_returns(6, 0) == 6
        assert apply_diminishing_returns(-4, 0) == -4

    def test_positive_delta_zero_at_ceiling(self):
        assert apply_diminishing_returns(6, 50) == 0

    def test_negative_delta_zero_at_floor(self):
        assert apply_diminishing_returns(-4, -50) == 0

    def test_halved_at_quarter(self):
        assert apply_diminishing_returns(6, 25) == 3  # 6 * 0.5
        assert apply_diminishing_returns(-4, -25) == -2


def _simulate_conversations(trait: str, n: int, gap_seconds: float = 30.0) -> int:
    """Run ``n`` conversation_completed events ``gap_seconds`` apart, returning
    the final mood. Energy is reset each step so we isolate the mood trajectory."""
    mood = 0
    t = 1_000_000.0
    for _ in range(n):
        _energy, mood = apply_event(
            "conversation_completed", 80, mood, trait, now=t, mood_updated_at=t - gap_seconds
        )
        t += gap_seconds
    return mood


class TestPersonalityConvergesWithoutSaturating:
    """The whole point of the rebalance: in a chatty town introverts trend
    negative and extroverts positive, both converging WELL short of ±50."""

    def test_extrovert_converges_around_plus_35(self):
        mood = _simulate_conversations("extrovert", 10)
        assert 31 <= mood <= 39  # spec: ~+35

    def test_introvert_converges_around_minus_28(self):
        mood = _simulate_conversations("introvert", 10)
        assert -32 <= mood <= -24  # spec: ~-28

    def test_personalities_diverge_by_a_wide_margin(self):
        extro = _simulate_conversations("extrovert", 10)
        intro = _simulate_conversations("introvert", 10)
        assert extro - intro >= 50  # spec: ~63-point spread

    def test_no_saturation_after_50_events(self):
        # Even under a pathological event stream nobody pins at the ceiling/floor.
        assert abs(_simulate_conversations("extrovert", 50)) < 50
        assert abs(_simulate_conversations("introvert", 50)) < 50
