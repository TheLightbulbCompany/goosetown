"""Tests for the onboarding-phase lattice helper.

The lattice is the single seam through which an agent's
``onboardingPhase`` field gets written. Closes the Vera-finding Q
race (alive → static → alive flips) by enforcing monotonic progression
in code, and consolidates 6 ad-hoc write sites that each used to pair
a DDB write with a cache mirror by hand.

Surfaced 2026-05-04 as audit Candidate 3 + producer's launch-readiness
plan Tier 1 #1.4.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.services.phase_lattice import (
    PHASE_LATTICE,
    InvalidPhaseError,
    advance_phase,
    restore_phase_from_db,
)

# Patch targets — the lattice helper instantiates TownService internally
# unless a test injects one, and reads/writes the ws_manager singleton.
WS_MGR_PATH = "core.services.phase_lattice.get_town_agent_ws_manager"


def _ws_mgr_with_phase(phase: str | None) -> MagicMock:
    """Build a ws_manager mock whose ``get_by_agent_id`` returns a
    connection with ``onboarding_phase`` set as given."""
    mgr = MagicMock()
    if phase is None:
        mgr.get_by_agent_id.return_value = None
    else:
        mgr.get_by_agent_id.return_value = SimpleNamespace(onboarding_phase=phase)
    return mgr


# ---------------------------------------------------------------------------
# Lattice metadata
# ---------------------------------------------------------------------------


class TestLattice:
    def test_phase_order_is_pinned(self):
        """If a future phase needs to be added (e.g. ``awaiting_credits``
        from Tier 1 #1.1), it must be inserted in the right rank — that
        decision deserves its own PR + review, so this test forces the
        conversation."""
        assert PHASE_LATTICE == ["pending_sprite", "static", "alive"]


# ---------------------------------------------------------------------------
# advance_phase — the monotonic-write seam
# ---------------------------------------------------------------------------


class TestAdvancePhase:
    def test_advance_writes_db_and_cache(self):
        svc = MagicMock()
        ws = _ws_mgr_with_phase("static")
        with patch(WS_MGR_PATH, return_value=ws):
            applied = advance_phase("agent-1", "alive", service=svc)
        assert applied is True
        # DDB write happened with the new phase.
        svc.update_agent_state.assert_called_once_with("agent-1", onboarding_phase="alive")
        # Cache write happened with the SAME phase string-cast.
        ws.update_onboarding_phase.assert_called_once_with("agent-1", "alive")

    def test_advance_with_other_fields_bundles_into_db_write(self):
        """The resume-sprite-generation site bundles ``sprite_url`` /
        ``sprite_ready`` etc into the same DDB write to avoid 2 round
        trips. Lattice helper supports that via ``**other_fields``."""
        svc = MagicMock()
        ws = _ws_mgr_with_phase("static")
        with patch(WS_MGR_PATH, return_value=ws):
            advance_phase(
                "agent-1",
                "alive",
                service=svc,
                sprite_ready=True,
                sprite_url="https://example/walk.png",
                sprite_status="complete",
            )
        svc.update_agent_state.assert_called_once_with(
            "agent-1",
            onboarding_phase="alive",
            sprite_ready=True,
            sprite_url="https://example/walk.png",
            sprite_status="complete",
        )

    def test_no_op_when_already_at_target(self):
        svc = MagicMock()
        ws = _ws_mgr_with_phase("alive")
        with patch(WS_MGR_PATH, return_value=ws):
            applied = advance_phase("agent-1", "alive", service=svc)
        assert applied is False
        svc.update_agent_state.assert_not_called()
        ws.update_onboarding_phase.assert_not_called()

    def test_regression_refused(self):
        """The Vera-finding Q race (alive → static → alive flip) is
        impossible by construction — a backward transition refuses to
        write anywhere and returns False."""
        svc = MagicMock()
        ws = _ws_mgr_with_phase("alive")
        with patch(WS_MGR_PATH, return_value=ws):
            applied = advance_phase("agent-1", "static", service=svc)
        assert applied is False
        svc.update_agent_state.assert_not_called()
        ws.update_onboarding_phase.assert_not_called()

    def test_advance_when_no_cache_entry_writes(self):
        """An agent not yet in the ws_manager cache (first call from
        sprite-pipeline Phase B) has no current phase; treat as new
        and apply unconditionally."""
        svc = MagicMock()
        ws = _ws_mgr_with_phase(None)  # no connection yet
        with patch(WS_MGR_PATH, return_value=ws):
            applied = advance_phase("agent-1", "static", service=svc)
        assert applied is True
        svc.update_agent_state.assert_called_once_with("agent-1", onboarding_phase="static")

    def test_invalid_phase_raises(self):
        svc = MagicMock()
        ws = _ws_mgr_with_phase("static")
        with patch(WS_MGR_PATH, return_value=ws):
            with pytest.raises(InvalidPhaseError) as exc_info:
                advance_phase("agent-1", "awaiting_unicorns", service=svc)
            assert "awaiting_unicorns" in str(exc_info.value)
        svc.update_agent_state.assert_not_called()
        ws.update_onboarding_phase.assert_not_called()

    def test_pending_sprite_to_static(self):
        svc = MagicMock()
        ws = _ws_mgr_with_phase("pending_sprite")
        with patch(WS_MGR_PATH, return_value=ws):
            applied = advance_phase("agent-1", "static", service=svc)
        assert applied is True

    def test_pending_sprite_to_alive_skips_static(self):
        """Skipping static is allowed — the lattice is monotonic but
        not contiguous. (The sprite pipeline doesn't do this in
        practice but the helper shouldn't enforce contiguity.)"""
        svc = MagicMock()
        ws = _ws_mgr_with_phase("pending_sprite")
        with patch(WS_MGR_PATH, return_value=ws):
            applied = advance_phase("agent-1", "alive", service=svc)
        assert applied is True

    def test_unknown_current_phase_treated_as_below_lattice(self):
        """Cache holds an unknown legacy value; trying to set a real
        lattice phase is treated as advancing FROM unknown (rank -1)
        UPWARD, so the write succeeds. Defensive: don't refuse just
        because the cache holds something we don't recognise."""
        svc = MagicMock()
        ws = _ws_mgr_with_phase("legacy_value")
        with patch(WS_MGR_PATH, return_value=ws):
            applied = advance_phase("agent-1", "alive", service=svc)
        assert applied is True


# ---------------------------------------------------------------------------
# restore_phase_from_db — the connect / startup-recovery seam
# ---------------------------------------------------------------------------


class TestRestorePhaseFromDb:
    def test_restore_writes_cache_only(self):
        ws = _ws_mgr_with_phase("alive")
        with patch(WS_MGR_PATH, return_value=ws):
            restore_phase_from_db("agent-1", "static")
        ws.update_onboarding_phase.assert_called_once_with("agent-1", "static")

    def test_restore_does_not_check_monotonicity(self):
        """DDB is the source of truth on restore. Even if cache is
        ahead (e.g. previous session's stale 'alive'), DDB's value
        wins."""
        ws = _ws_mgr_with_phase("alive")  # cache claims alive
        with patch(WS_MGR_PATH, return_value=ws):
            restore_phase_from_db("agent-1", "static")  # DDB says static
        ws.update_onboarding_phase.assert_called_once_with("agent-1", "static")

    def test_restore_tolerates_unknown_phase(self):
        """Legacy ``onboardingPhase`` values in DDB pass through to the
        cache; the daemon has its own fallback for unknowns."""
        ws = _ws_mgr_with_phase(None)
        with patch(WS_MGR_PATH, return_value=ws):
            restore_phase_from_db("agent-1", "legacy_value")
        ws.update_onboarding_phase.assert_called_once_with("agent-1", "legacy_value")
