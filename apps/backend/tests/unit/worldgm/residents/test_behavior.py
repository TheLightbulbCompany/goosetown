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


# ---------------------------------------------------------------------------
# Sleep window helper
# ---------------------------------------------------------------------------


def test_is_in_sleep_window_non_wrapping():
    from worldgm.residents.behavior import is_in_sleep_window

    # Tesla-style window: 4 -> 8 (no midnight wrap).
    assert is_in_sleep_window(5, 4, 8) is True
    assert is_in_sleep_window(4, 4, 8) is True  # start inclusive
    assert is_in_sleep_window(8, 4, 8) is False  # end exclusive (wake hour)
    assert is_in_sleep_window(3, 4, 8) is False
    assert is_in_sleep_window(12, 4, 8) is False


def test_is_in_sleep_window_wraps_midnight():
    from worldgm.residents.behavior import is_in_sleep_window

    # Tobias-style window: 22 -> 6 (wraps midnight).
    assert is_in_sleep_window(23, 22, 6) is True
    assert is_in_sleep_window(0, 22, 6) is True
    assert is_in_sleep_window(3, 22, 6) is True
    assert is_in_sleep_window(22, 22, 6) is True  # start inclusive
    assert is_in_sleep_window(6, 22, 6) is False  # wake exclusive
    assert is_in_sleep_window(7, 22, 6) is False
    assert is_in_sleep_window(12, 22, 6) is False


def test_is_in_sleep_window_equal_hours_means_never_sleeps():
    from worldgm.residents.behavior import is_in_sleep_window

    assert is_in_sleep_window(5, 12, 12) is False
    assert is_in_sleep_window(12, 12, 12) is False


# ---------------------------------------------------------------------------
# Sleep gate in next_action
# ---------------------------------------------------------------------------


def test_sleep_gate_in_window_sends_to_bed():
    rt = _rt(location="plaza", state="idle", idle_ticks=3, hangout="plaza")
    rt.sleep_hour = 22
    rt.wake_hour = 6
    result = next_action(rt, rng_value=0.0, now_utc_hour=23)
    assert result["payload"]["destination"] == "bed"


def test_sleep_gate_at_bed_in_window_noops():
    rt = _rt(location="bed", state="idle", idle_ticks=3, hangout="plaza")
    rt.sleep_hour = 22
    rt.wake_hour = 6
    result = next_action(rt, rng_value=0.0, now_utc_hour=2)
    assert result["action"] == "noop"


def test_sleep_gate_outside_window_uses_normal_logic():
    rt = _rt(location="cafe", state="idle", idle_ticks=3, hangout="plaza")
    rt.sleep_hour = 22
    rt.wake_hour = 6
    # Hour 12 is outside the sleep window → drift to hangout.
    result = next_action(rt, rng_value=0.0, now_utc_hour=12)
    assert result["payload"]["destination"] == "plaza"


def test_sleep_gate_at_bed_outside_window_heads_to_hangout():
    """Just woken up: at bed, wake hour passed → go to hangout."""
    rt = _rt(location="bed", state="idle", idle_ticks=3, hangout="plaza")
    rt.sleep_hour = 22
    rt.wake_hour = 6
    result = next_action(rt, rng_value=0.0, now_utc_hour=12)
    assert result["payload"]["destination"] == "plaza"


def test_sleep_gate_none_hour_ignores_sleep():
    """Backward-compat: when now_utc_hour is None, sleep window is ignored."""
    rt = _rt(location="plaza", state="idle", idle_ticks=3, hangout="plaza")
    rt.sleep_hour = 22
    rt.wake_hour = 6
    # rng=0.9 → no wander; settled at hangout; no sleep override.
    result = next_action(rt, rng_value=0.9, now_utc_hour=None)
    assert result["action"] == "noop"
