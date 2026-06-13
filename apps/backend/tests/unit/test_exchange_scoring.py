"""Tests for core/services/exchange_scoring.py

Pure marketplace math: platform fee, bid scoring, winner selection, trust EMA,
and trust tiers. Derived from the AEX open-experiments design (MIT).
"""

from __future__ import annotations

import pytest

from core.services.exchange_scoring import (
    INITIAL_TRUST,
    choose_winner,
    platform_fee,
    score_bid,
    trust_tier,
    updated_trust,
)

# ---------------------------------------------------------------------------
# platform_fee
# ---------------------------------------------------------------------------


class TestPlatformFee:
    def test_ten_percent_of_ten(self):
        assert platform_fee(10) == 1

    def test_minimum_one_crumb(self):
        assert platform_fee(1) == 1

    def test_twenty_gives_two(self):
        assert platform_fee(20) == 2

    def test_ceil_applied(self):
        # 25 * 0.10 = 2.5 → ceil → 3
        assert platform_fee(25) == 3

    def test_zero_price_gives_zero(self):
        assert platform_fee(0) == 0

    def test_negative_price_gives_zero(self):
        assert platform_fee(-5) == 0


# ---------------------------------------------------------------------------
# score_bid
# ---------------------------------------------------------------------------


class TestScoreBid:
    def test_max_price_gives_zero_price_component(self):
        # price == max_price → price component = 0
        # With balanced weights (0.40, 0.40, 0.20), trust=1, confidence=1:
        # score = 0.40*0 + 0.40*1 + 0.20*1 = 0.60
        s = score_bid(price=10, max_price=10, trust=1.0, confidence=1.0, strategy="balanced")
        assert s == pytest.approx(0.60)

    def test_zero_price_gives_full_price_component(self):
        # price=0 → price_score = 1.0
        # With balanced weights, trust=0, confidence=0:
        # score = 0.40*1 + 0.40*0 + 0.20*0 = 0.40
        s = score_bid(price=0, max_price=10, trust=0.0, confidence=0.0, strategy="balanced")
        assert s == pytest.approx(0.40)

    def test_lowest_price_cheap_low_trust_beats_pricey_high_trust(self):
        # "lowest_price" weights: (0.70, 0.20, 0.10)
        # Cheap bid: price=1, max=10, trust=0.1, confidence=0.5
        #   price_score = 1 - 1/10 = 0.9
        #   score = 0.70*0.9 + 0.20*0.1 + 0.10*0.5 = 0.63 + 0.02 + 0.05 = 0.70
        cheap = score_bid(price=1, max_price=10, trust=0.1, confidence=0.5, strategy="lowest_price")
        # Pricey bid: price=9, max=10, trust=0.9, confidence=0.9
        #   price_score = 1 - 9/10 = 0.1
        #   score = 0.70*0.1 + 0.20*0.9 + 0.10*0.9 = 0.07 + 0.18 + 0.09 = 0.34
        pricey = score_bid(price=9, max_price=10, trust=0.9, confidence=0.9, strategy="lowest_price")
        assert cheap > pricey

    def test_best_quality_high_trust_beats_cheap_low_trust(self):
        # "best_quality" weights: (0.15, 0.65, 0.20)
        # High-trust bid: price=9, max=10, trust=0.9, confidence=0.9
        #   price_score = 0.1
        #   score = 0.15*0.1 + 0.65*0.9 + 0.20*0.9 = 0.015 + 0.585 + 0.18 = 0.78
        high_trust = score_bid(price=9, max_price=10, trust=0.9, confidence=0.9, strategy="best_quality")
        # Cheap low-trust: price=1, max=10, trust=0.1, confidence=0.1
        #   price_score = 0.9
        #   score = 0.15*0.9 + 0.65*0.1 + 0.20*0.1 = 0.135 + 0.065 + 0.02 = 0.22
        cheap_low = score_bid(price=1, max_price=10, trust=0.1, confidence=0.1, strategy="best_quality")
        assert high_trust > cheap_low

    def test_confidence_clamped_above_one(self):
        # confidence=2.0 should be clamped to 1.0
        s_clamped = score_bid(price=0, max_price=10, trust=0.0, confidence=2.0, strategy="balanced")
        s_normal = score_bid(price=0, max_price=10, trust=0.0, confidence=1.0, strategy="balanced")
        assert s_clamped == pytest.approx(s_normal)

    def test_confidence_clamped_below_zero(self):
        s_clamped = score_bid(price=0, max_price=10, trust=0.0, confidence=-1.0, strategy="balanced")
        s_zero = score_bid(price=0, max_price=10, trust=0.0, confidence=0.0, strategy="balanced")
        assert s_clamped == pytest.approx(s_zero)

    def test_trust_clamped_above_one(self):
        s_clamped = score_bid(price=0, max_price=10, trust=2.0, confidence=0.0, strategy="balanced")
        s_normal = score_bid(price=0, max_price=10, trust=1.0, confidence=0.0, strategy="balanced")
        assert s_clamped == pytest.approx(s_normal)

    def test_trust_clamped_below_zero(self):
        s_clamped = score_bid(price=0, max_price=10, trust=-1.0, confidence=0.0, strategy="balanced")
        s_zero = score_bid(price=0, max_price=10, trust=0.0, confidence=0.0, strategy="balanced")
        assert s_clamped == pytest.approx(s_zero)

    def test_unknown_strategy_falls_back_to_balanced(self):
        s_unknown = score_bid(price=5, max_price=10, trust=0.5, confidence=0.5, strategy="nonexistent")
        s_balanced = score_bid(price=5, max_price=10, trust=0.5, confidence=0.5, strategy="balanced")
        assert s_unknown == pytest.approx(s_balanced)


# ---------------------------------------------------------------------------
# choose_winner
# ---------------------------------------------------------------------------


class TestChooseWinner:
    def test_disqualifies_price_above_max(self):
        bids = {
            "agent-a": {"price": 15, "confidence": 0.9, "bidAt": "2026-01-01T00:00:00"},
            "agent-b": {"price": 10, "confidence": 0.5, "bidAt": "2026-01-01T00:01:00"},
        }
        winner = choose_winner(bids, max_price=10, trust_by_agent={}, strategy="balanced")
        assert winner == "agent-b"

    def test_tie_broken_by_earliest_bidat(self):
        # Both agents same price, trust, confidence → same score; earliest bidAt wins
        bids = {
            "agent-a": {"price": 5, "confidence": 0.5, "bidAt": "2026-01-01T00:01:00"},
            "agent-b": {"price": 5, "confidence": 0.5, "bidAt": "2026-01-01T00:00:00"},
        }
        winner = choose_winner(bids, max_price=10, trust_by_agent={}, strategy="balanced")
        assert winner == "agent-b"

    def test_empty_bids_returns_none(self):
        assert choose_winner({}, max_price=10, trust_by_agent={}, strategy="balanced") is None

    def test_all_disqualified_returns_none(self):
        bids = {
            "agent-a": {"price": 100, "confidence": 0.9, "bidAt": "2026-01-01T00:00:00"},
        }
        assert choose_winner(bids, max_price=10, trust_by_agent={}, strategy="balanced") is None

    def test_uses_initial_trust_when_agent_not_in_trust_map(self):
        # Just verify it doesn't crash and picks someone
        bids = {
            "agent-x": {"price": 5, "confidence": 0.5, "bidAt": "2026-01-01T00:00:00"},
        }
        winner = choose_winner(bids, max_price=10, trust_by_agent={}, strategy="balanced")
        assert winner == "agent-x"

    def test_negative_price_disqualified(self):
        bids = {
            "agent-neg": {"price": -1, "confidence": 0.9, "bidAt": "2026-01-01T00:00:00"},
            "agent-ok": {"price": 5, "confidence": 0.5, "bidAt": "2026-01-01T00:00:01"},
        }
        winner = choose_winner(bids, max_price=10, trust_by_agent={}, strategy="balanced")
        assert winner == "agent-ok"

    def test_missing_bidat_loses_tie_to_timestamped_bid(self):
        # Same price, confidence, and trust — only difference is agent-a has a
        # bidAt timestamp and agent-b omits it entirely.  The one WITH bidAt must win.
        bids = {
            "agent-a": {"price": 5, "confidence": 0.5, "bidAt": "2026-01-01T00:00:00"},
            "agent-b": {"price": 5, "confidence": 0.5},  # no bidAt
        }
        winner = choose_winner(bids, max_price=10, trust_by_agent={}, strategy="balanced")
        assert winner == "agent-a"


# ---------------------------------------------------------------------------
# updated_trust
# ---------------------------------------------------------------------------


class TestUpdatedTrust:
    def test_success_increases_trust(self):
        # EMA: (1 - 0.2) * 0.5 + 0.2 * 1.0 = 0.4 + 0.2 = 0.6
        result = updated_trust(0.5, "SUCCESS")
        assert result == pytest.approx(0.6)

    def test_failure_provider_decreases_trust(self):
        # EMA: (1 - 0.2) * 0.5 + 0.2 * 0.0 = 0.4 + 0.0 = 0.4
        result = updated_trust(0.5, "FAILURE_PROVIDER")
        assert result == pytest.approx(0.4)

    def test_success_auto_uses_point_nine(self):
        # EMA: (1 - 0.2) * 0.5 + 0.2 * 0.9 = 0.4 + 0.18 = 0.58
        result = updated_trust(0.5, "SUCCESS_AUTO")
        assert result == pytest.approx(0.58)

    def test_unknown_outcome_returns_old_unchanged(self):
        result = updated_trust(0.5, "SOMETHING_ELSE")
        assert result == pytest.approx(0.5)

    def test_result_rounded_to_four_decimal_places(self):
        result = updated_trust(0.5, "SUCCESS")
        # 0.6 is exact here, but verify it's a float with ≤4 dp
        assert result == round(result, 4)


# ---------------------------------------------------------------------------
# trust_tier
# ---------------------------------------------------------------------------


class TestTrustTier:
    def test_trusted_tier(self):
        # jobs_completed >= 15, trust >= 0.85
        assert trust_tier(0.9, 20) == "trusted"

    def test_reliable_tier(self):
        # jobs_completed >= 5, trust >= 0.7
        assert trust_tier(0.75, 6) == "reliable"

    def test_newcomer_not_enough_jobs_for_trusted(self):
        # trust >= 0.85 but only 2 jobs → newcomer (not enough jobs for either tier)
        assert trust_tier(0.95, 2) == "newcomer"

    def test_newcomer_trust_too_low(self):
        # jobs_completed >= 5 but trust < 0.7 → newcomer
        assert trust_tier(0.6, 30) == "newcomer"

    def test_reliable_boundary_exactly_five_jobs(self):
        assert trust_tier(0.7, 5) == "reliable"

    def test_trusted_boundary_exactly_fifteen_jobs(self):
        assert trust_tier(0.85, 15) == "trusted"

    def test_initial_trust_is_newcomer(self):
        assert trust_tier(INITIAL_TRUST, 0) == "newcomer"
