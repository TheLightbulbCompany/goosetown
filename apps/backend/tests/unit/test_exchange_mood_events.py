"""Tests for the Exchange job-lifecycle mood/energy events in town_mood_engine.

All calls use ``traits=""`` (no trait overrides) and ``now=0.0 / mood_updated_at=0.0``
(no decay), so the only effect is the pure additive base delta from _BASE_EFFECTS.
At mood=0 the full delta applies (diminishing-returns factor = 1.0).
"""

from __future__ import annotations

from core.services.town_mood_engine import apply_event


class TestExchangeMoodEvents:
    """Each event is validated from a known baseline (energy=50, mood=0)."""

    def test_job_completed_provider_gives_mood_six(self):
        # base effect: (0, 6) — energy unchanged, mood +6
        energy, mood = apply_event("job_completed_provider", energy=50, mood=0, traits="")
        assert energy == 50
        assert mood == 6

    def test_job_delivered_drains_energy(self):
        # base effect: (-10, 0) — energy -10, mood unchanged
        energy, mood = apply_event("job_delivered", energy=50, mood=0, traits="")
        assert energy == 40
        assert mood == 0

    def test_bid_lost_small_mood_hit(self):
        # base effect: (0, -1)
        energy, mood = apply_event("bid_lost", energy=50, mood=0, traits="")
        assert energy == 50
        assert mood == -1

    def test_job_failed_provider_big_mood_hit(self):
        # base effect: (0, -8)
        energy, mood = apply_event("job_failed_provider", energy=50, mood=0, traits="")
        assert energy == 50
        assert mood == -8

    def test_job_won_small_mood_boost(self):
        # base effect: (0, 3)
        energy, mood = apply_event("job_won", energy=50, mood=0, traits="")
        assert energy == 50
        assert mood == 3

    def test_job_completed_consumer_mood_four(self):
        # base effect: (0, 4)
        energy, mood = apply_event("job_completed_consumer", energy=50, mood=0, traits="")
        assert energy == 50
        assert mood == 4

    def test_job_expired_consumer_mood_minus_two(self):
        # base effect: (0, -2)
        energy, mood = apply_event("job_expired_consumer", energy=50, mood=0, traits="")
        assert energy == 50
        assert mood == -2

    def test_unknown_exchange_event_is_noop(self):
        # Sanity: a made-up event name should not crash and returns unchanged values
        energy, mood = apply_event("exchange_nonexistent", energy=50, mood=0, traits="")
        assert energy == 50
        assert mood == 0
