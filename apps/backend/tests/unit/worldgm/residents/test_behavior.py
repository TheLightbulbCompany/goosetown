"""Pure FSM tests for worldgm.residents.behavior.next_action."""

from worldgm.residents.behavior import NOOP, ResidentRuntime, next_action


def _rt(*, name="tobias", location="cafe", state="idle", idle_ticks=0, hangout="plaza"):
    return ResidentRuntime(name=name, location=location, state=state, idle_ticks=idle_ticks, hangout=hangout)


# ---------------------------------------------------------------------------
# Moving resident never issues another command
# ---------------------------------------------------------------------------


def test_moving_resident_noops():
    rt = _rt(location="cafe", state="moving", idle_ticks=0, hangout="plaza")
    assert next_action(rt, rng_value=0.5) == {"action": "noop"}


# ---------------------------------------------------------------------------
# Idle resident drifts toward hangout once threshold reached
# ---------------------------------------------------------------------------


def test_idle_resident_moves_toward_hangout():
    rt = _rt(location="cafe", state="idle", idle_ticks=3, hangout="plaza")
    result = next_action(rt, rng_value=0.0)
    assert result == {"action": "act", "payload": {"action": "move", "destination": "plaza"}}


def test_idle_below_threshold_stays_put():
    """idle_ticks < _IDLE_TICKS_BEFORE_MOVE → no move issued."""
    rt = _rt(location="cafe", state="idle", idle_ticks=1, hangout="plaza")
    assert next_action(rt, rng_value=0.0) == {"action": "noop"}


# ---------------------------------------------------------------------------
# Settled at hangout: wander only when rng < 0.25
# ---------------------------------------------------------------------------


def test_settled_at_hangout_low_rng_stays_put():
    """rng >= 0.25 → no wander; must return NOOP."""
    rt = _rt(location="plaza", state="idle", idle_ticks=5, hangout="plaza")
    assert next_action(rt, rng_value=0.9) == {"action": "noop"}


def test_settled_at_hangout_rng_exactly_at_threshold_stays_put():
    """rng == 0.25 → no wander (strict less-than)."""
    rt = _rt(location="plaza", state="idle", idle_ticks=5, hangout="plaza")
    assert next_action(rt, rng_value=0.25) == {"action": "noop"}


def test_settled_at_hangout_wanders_when_rng_below_threshold():
    """rng < 0.25 → wander to a valid destination other than the hangout."""
    rt = _rt(location="plaza", state="idle", idle_ticks=5, hangout="plaza")
    result = next_action(rt, rng_value=0.1)
    assert result["action"] == "act"
    payload = result["payload"]
    assert payload["action"] == "move"
    assert payload["destination"] != "plaza"


def test_settled_wander_destination_is_valid_location():
    from worldgm.residents.behavior import _VALID_DESTINATIONS

    rt = _rt(location="plaza", state="idle", idle_ticks=5, hangout="plaza")
    result = next_action(rt, rng_value=0.1)
    assert result["payload"]["destination"] in _VALID_DESTINATIONS


def test_settled_wander_deterministic_from_rng():
    """Same rng_value → same destination every time."""
    rt1 = _rt(location="plaza", state="idle", idle_ticks=5, hangout="plaza")
    rt2 = _rt(location="plaza", state="idle", idle_ticks=5, hangout="plaza")
    assert next_action(rt1, rng_value=0.05) == next_action(rt2, rng_value=0.05)


# ---------------------------------------------------------------------------
# NOOP sentinel is a dict (not mutated by callers)
# ---------------------------------------------------------------------------


def test_noop_is_a_fresh_copy():
    """next_action must return a copy of NOOP, not the singleton itself."""
    rt = _rt(location="plaza", state="moving", idle_ticks=0, hangout="plaza")
    result = next_action(rt, rng_value=0.0)
    assert result is not NOOP
    assert result == NOOP
