"""Regression tests for Round 1 activity-layer fixes (2026-05-04).

Three behaviors pinned here:

1. ``solitary_activity`` event is now wired — fires on every completed
   (non-interrupted) activity. Previously dead code in
   ``town_mood_engine._BASE_EFFECTS`` (game-designer audit, 2026-05-04).

2. ``conversation_turn`` event is new — applied to the speaker on every
   successful ``say`` to provide per-turn mood drip. Previously the
   only mood reward fired at ``_finalize_conversation``, so a 2-turn
   and a 20-turn conversation produced identical mood outcomes.

3. ``currency_delta`` field is removed from the ``Activity`` dataclass.
   Phase 5 (economy) was dropped 2026-05-04; the field was set on 5
   activities but never read. Removing it from the dataclass means a
   future stray ``currency_delta=...`` literal raises at parse time.
"""

from __future__ import annotations

from core.services.activity_registry import Activity
from core.services.town_mood_engine import _BASE_EFFECTS, _TRAIT_MODIFIERS, apply_event

# ---------------------------------------------------------------------------
# 1. solitary_activity wiring
# ---------------------------------------------------------------------------


class TestSolitaryActivityEvent:
    """The named event was dead code; now it's wired into activity completion."""

    def test_base_effect_unchanged(self):
        """Pinning the values so a future tweak is intentional."""
        assert _BASE_EFFECTS["solitary_activity"] == (3, 2)

    def test_introvert_override_unchanged(self):
        assert _TRAIT_MODIFIERS["introvert"]["solitary_activity"] == (5, 5)

    def test_extrovert_override_unchanged(self):
        assert _TRAIT_MODIFIERS["extrovert"]["solitary_activity"] == (2, -3)

    def test_apply_event_neutral_traits(self):
        new_e, new_m = apply_event("solitary_activity", energy=50, mood=0, traits="")
        # Base: +3 energy, +2 mood
        assert new_e == 53
        assert new_m == 2

    def test_apply_event_introvert_gets_bigger_solitude_bonus(self):
        new_e, new_m = apply_event("solitary_activity", energy=50, mood=0, traits="introvert")
        # Override: +5 energy, +5 mood
        assert new_e == 55
        assert new_m == 5

    def test_apply_event_extrovert_gets_solitude_penalty(self):
        new_e, new_m = apply_event("solitary_activity", energy=50, mood=0, traits="extrovert")
        # Override: +2 energy, -3 mood (rebalanced)
        assert new_e == 52
        assert new_m == -3


# ---------------------------------------------------------------------------
# 2. conversation_turn wiring
# ---------------------------------------------------------------------------


class TestConversationTurnEvent:
    """Per-turn drip fixes the back-loaded reward bug. Distinguishes
    a 2-turn conversation from a 20-turn one in mood-state outcome."""

    def test_base_effect_pinned(self):
        assert _BASE_EFFECTS["conversation_turn"] == (-1, 0)

    def test_neutral_per_turn_drip(self):
        new_e, new_m = apply_event("conversation_turn", energy=50, mood=0, traits="")
        # Rebalanced: -1 energy, 0 mood per turn (neutral agents get no per-turn
        # mood — the per-turn accumulation was a saturation driver).
        assert new_e == 49
        assert new_m == 0

    def test_introvert_per_turn_mood_drain(self):
        """Rebalanced: introverts are drained per-turn during a long
        conversation (-1 mood), so dragging one out actively costs them."""
        new_e, new_m = apply_event("conversation_turn", energy=50, mood=0, traits="introvert")
        assert new_m == -1  # per-turn drain
        assert new_e == 49  # energy still drains

    def test_extrovert_double_per_turn_mood_drip(self):
        """Extroverts genuinely enjoy sustained dialogue — per-turn is +2."""
        new_e, new_m = apply_event("conversation_turn", energy=50, mood=0, traits="extrovert")
        assert new_m == 2
        assert new_e == 49

    def test_20_turn_extrovert_meaningfully_different_from_2_turn(self):
        """The behavior the audit was about: turn-count should matter for
        extroverts. After 20 turns of drip, mood should be substantially
        higher than after 2 turns."""
        # 2 turns: +2 mood per turn = +4 total
        e_2, m_2 = 50, 0
        for _ in range(2):
            e_2, m_2 = apply_event("conversation_turn", e_2, m_2, "extrovert")
        assert m_2 == 4

        # 20 turns: +2 mood per turn, but diminishing-returns near the cap
        # tapers each increment so it converges (to ~+27) instead of pinning +50.
        e_20, m_20 = 50, 0
        for _ in range(20):
            e_20, m_20 = apply_event("conversation_turn", e_20, m_20, "extrovert")
        assert m_20 == 27

        assert m_20 > m_2 + 10, "20-turn conv should give meaningfully more mood than 2-turn"

    def test_energy_clamps_at_zero(self):
        """A long conversation drains energy; the existing apply_event clamp
        means it won't go below 0 (and the speaker's _apply_event_with_sleep_guard
        will trigger sleep before that anyway)."""
        new_e, new_m = apply_event("conversation_turn", energy=0, mood=0, traits="")
        assert new_e == 0  # clamped at 0
        assert new_m == 0  # rebalanced: neutral per-turn mood is 0


# ---------------------------------------------------------------------------
# 3. currency_delta removal
# ---------------------------------------------------------------------------


class TestActivityHasNoCurrencyDelta:
    """Phase 5 was dropped 2026-05-04. The currency_delta field on
    Activity was dead code — set on 5 activities, never read by
    apply_activity_effect, no wallet, no transactions. Removed so a
    future stray ``currency_delta=...`` literal fails at parse time
    rather than silently doing nothing."""

    def test_activity_constructor_rejects_currency_delta(self):
        """Currency_delta is not in the Activity dataclass anymore.
        Trying to pass it must fail."""
        try:
            Activity(
                name="x",
                location="plaza",
                duration_seconds=1,
                mood_delta=0,
                energy_delta=0,
                currency_delta=5,  # type: ignore[call-arg]
            )
            assert False, "Activity accepted currency_delta — should fail at construction"
        except TypeError as exc:
            # Expected: dataclass rejects unknown kwargs.
            assert "currency_delta" in str(exc) or "got an unexpected keyword" in str(exc)

    def test_activity_dataclass_fields(self):
        """The actual field set on Activity. Pinning so a future change
        is intentional."""
        from dataclasses import fields

        names = {f.name for f in fields(Activity)}
        # currency_delta must NOT be in the fields anymore.
        assert "currency_delta" not in names
        # Sanity: the fields we still expect are present.
        for required in {
            "name",
            "location",
            "duration_seconds",
            "mood_delta",
            "energy_delta",
            "introvert_bonus",
            "extrovert_bonus",
            "animation",
            "description",
            "agent_prompt",
            "spot",
            "location_context",
        }:
            assert required in names, f"missing expected field: {required}"
