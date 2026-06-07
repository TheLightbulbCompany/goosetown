# Mood Rebalance Design — 2026-06-06

**Status:** Ready for implementation  
**Authored by:** systems-designer  
**Reports to:** game-designer  
**Target files:** `apps/backend/core/services/town_mood_engine.py`, `apps/backend/core/services/activity_registry.py`

---

## Problem Statement

All 20 residents pinned to mood=+50 (the DynamoDB ceiling) within minutes of town boot.
Root causes, in order of severity:

1. **No negative mood paths for any trait.** The introvert override for `conversation_completed`
   is +3 — still positive. Introverts gain mood from socializing, just more slowly. There is
   no event that can reduce mood under any personality configuration.
2. **No decay.** The engine is purely additive. Events only ever add to the running total.
3. **No saturation resistance.** The hard clamp `max(-50, min(50, mood + delta))` means every
   positive delta at mood=+48 is worth the same +2 as at mood=0. There is no structural
   resistance to the ceiling.
4. **Event volume is high.** `conversation_turn` fires on every successful `say`. With
   event-driven LLM conversation replies, a 10-turn conversation in ~90 seconds fires 10
   `conversation_turn` events plus 1 `conversation_completed` — that is +11 mood for an extrovert
   under the current numbers, delivered in under 2 minutes.

---

## Design Goals

1. In a hyper-social town, **introverts trend negative** (drained by constant socializing) and
   **extroverts trend positive** (energized by it). Personality must produce divergent equilibria,
   not just different rates of the same upward ratchet.
2. Mood must be **structurally prevented from pinning at ±50**, even under pathological event
   rates, without needing a background ticker.
3. The engine stays a **pure synchronous function**. Decay is lazy (computed at event time from
   `moodUpdatedAt`). No background sweep required.
4. `mood_label` thresholds are unchanged (miserable ≤ -30, sad ≤ -10, neutral ≤ 10,
   happy ≤ 30, elated > 30). The existing labels are well-calibrated.

---

## Three-Layer Defense Against Saturation

The design uses three mechanisms in sequence, applied inside `apply_event`:

```
mood_after_decay = decay(current_mood, elapsed_since_last_event)
raw_delta        = base_delta modified by trait override
effective_delta  = diminishing_returns(raw_delta, mood_after_decay)
new_mood         = clamp(mood_after_decay + effective_delta, -50, +50)
```

Each layer is necessary. Decay alone fails at high event rates (30s between events = 1.7% decay
per interval). Diminishing returns alone fails if deltas are large. Smaller deltas alone fail
because the number of events per hour is high enough to overcome them. All three together produce
a stable, personality-differentiated equilibrium.

---

## A. New `_BASE_EFFECTS` and `_TRAIT_MODIFIERS`

Energy deltas are unchanged from the current values. Only mood deltas change.

### `_BASE_EFFECTS` (energy_delta, mood_delta)

| Event | Energy delta | Mood delta | Rationale |
|---|---|---|---|
| `conversation_completed` | -5 | **+2** | Reduced from +5. Baseline for neutral agents — a modest positive that traits will push positive or negative. |
| `conversation_turn` | -1 | **0** | Reduced from +1. Per-turn drip should not accumulate; rewards are at completion. Neutral agents get nothing per turn. |
| `solitary_activity` | +3 | **+2** | Unchanged. Named event fired by backend on solo activities; fine as is. |
| `arrived_new_location` | -2 | **0** | Unchanged. Arrival is neutral. |
| `sleep` | None (reset to 100) | **0** | Reduced from 0 — already 0. Sleep energy reset is handled via `energy_delta=None`. Mood reset on sleep is intentionally NOT applied here because the activity registry handles it (the `sleep` activity carries `mood_delta=8`; see Section F). |

### `_TRAIT_MODIFIERS` (energy_delta, mood_delta) — full replacement

```python
_TRAIT_MODIFIERS = {
    "introvert": {
        "conversation_completed": (-10, -4),
        # Introverts are drained by each completed conversation.
        # Sign-negative: constant socializing produces a downward trend.
        "conversation_turn": (-1, -1),
        # Small per-turn drain during long conversations.
        # A 10-turn conversation costs an introvert -10 mood in drip
        # before the completion penalty. DR will dampen this at depth.
        "solitary_activity": (5, +5),
        # Introverts recharge alone. Kept strongly positive.
        "arrived_new_location": (-2, 0),
        # Introverts get no arrival boost (extroverts do).
    },
    "extrovert": {
        "conversation_completed": (-2, +6),
        # Extroverts are energized by completing a conversation.
        # Reduced from +8 to +6 to work within the DR system.
        "conversation_turn": (-1, +2),
        # Extroverts enjoy sustained conversation — per-turn drip kept.
        "solitary_activity": (2, -3),
        # Extroverts find isolation mildly draining.
        # Increased magnitude from -2 to -3.
        "arrived_new_location": (-2, +3),
        # Extroverts enjoy getting out. Unchanged.
    },
}
```

**Key change:** introvert `conversation_completed` is now **-4** (was +3). introvert
`conversation_turn` is now **-1** (was 0). These sign flips are the primary fix.

---

## B. Decay Model

### Formula

```python
MOOD_DECAY_HALF_LIFE_MINUTES = 20.0

def apply_decay(mood: int, elapsed_seconds: float) -> int:
    """Decay mood toward 0 exponentially. Half-life = 20 minutes."""
    if elapsed_seconds <= 0 or mood == 0:
        return mood
    elapsed_minutes = elapsed_seconds / 60.0
    decay_factor = 0.5 ** (elapsed_minutes / MOOD_DECAY_HALF_LIFE_MINUTES)
    return round(mood * decay_factor)
```

### Half-life Rationale: 20 minutes

| Elapsed since last event | Decay factor | mood=+40 becomes | mood=-30 becomes |
|---|---|---|---|
| 30 seconds | 0.983 | +39 | -29 |
| 5 minutes | 0.841 | +34 | -25 |
| 15 minutes | 0.595 | +24 | -18 |
| 20 minutes | 0.500 | +20 | -15 |
| 40 minutes | 0.250 | +10 | -8 |
| 60 minutes | 0.125 | +5 | -4 |

A 20-minute half-life means mood roughly reflects the last 15-30 minutes of social activity.
An agent who had a great run of conversations an hour ago is nearly back to neutral even without
any new events. An agent actively engaged in conversation right now drifts toward their trait
equilibrium, not a fixed ceiling.

### Caller change required

`apply_event` must accept two new parameters: `now: float` (Unix timestamp) and
`mood_updated_at: float` (Unix timestamp of last mood write, stored as `moodUpdatedAt` on the
agent record). The caller (backend event handlers) must pass both and write back `moodUpdatedAt`
after every call.

```python
def apply_event(
    event: str,
    energy: int,
    mood: int,
    traits: "str | list[str] | None",
    now: float = 0.0,
    mood_updated_at: float = 0.0,
) -> tuple[int, int]:
    ...
    elapsed_seconds = max(0.0, now - mood_updated_at) if now > 0 and mood_updated_at > 0 else 0.0
    mood = apply_decay(mood, elapsed_seconds)
    # then apply trait-adjusted delta with diminishing returns
```

The default `0.0` values make the new parameters backward-compatible: callers that don't pass
timestamps get no decay (same behavior as today). Migrate callers incrementally.

---

## C. Diminishing-Returns Formula

```python
def apply_diminishing_returns(delta: int, current_mood: int) -> int:
    """Scale a mood delta by distance from the relevant ceiling/floor.

    Positive deltas are scaled by (1 - mood/50).
    Negative deltas are scaled by (1 + mood/50).

    At mood=0  : full delta (factor 1.0).
    At mood=+25: positive deltas halved (factor 0.5).
    At mood=+50: positive deltas become 0 (factor 0.0) — asymptotic ceiling.
    At mood=-25: negative deltas halved (factor 0.5).
    At mood=-50: negative deltas become 0 (factor 0.0) — asymptotic floor.
    """
    if delta == 0:
        return 0
    if delta > 0:
        factor = max(0.0, 1.0 - current_mood / 50.0)
    else:
        factor = max(0.0, 1.0 + current_mood / 50.0)
    return round(delta * factor)
```

This is applied AFTER decay, BEFORE adding to the decayed mood value:

```python
effective_delta = apply_diminishing_returns(raw_delta, mood_after_decay)
new_mood = clamp(mood_after_decay + effective_delta, -50, +50)
```

The hard clamp is kept as a safety net, but with DR in place it should rarely fire.

---

## D. Worked Example: 10 Conversation Events, 30 Seconds Apart

### Setup

- Starting mood: 0 for both agents
- Event: `conversation_completed`, every 30 seconds
- Decay half-life: 20 minutes
- Decay factor per 30s interval: 0.5^(0.5/20) = **0.9828**

Notation: `d` = decayed mood (before delta), `eff` = effective delta after DR, `m` = new mood.

### (i) Extrovert: delta = +6 per event

| Event | d = round(prev * 0.9828) | DR factor = (1 - d/50) | eff = round(6 * factor) | m = d + eff |
|---|---|---|---|---|
| 1 | 0 | 1.000 | 6 | **6** |
| 2 | 6 | 0.880 | 5 | **11** |
| 3 | 11 | 0.780 | 5 | **16** |
| 4 | 16 | 0.680 | 4 | **20** |
| 5 | 20 | 0.600 | 4 | **24** |
| 6 | 24 | 0.520 | 3 | **27** |
| 7 | 27 | 0.460 | 3 | **30** |
| 8 | 30 | 0.400 | 2 (round(6*0.40)=2) | **32** |
| 9 | 32 | 0.360 | 2 | **34** |
| 10 | 33 | 0.340 | 2 | **35** |

**Extrovert result: +35** — labeled "elated". The series is visibly converging (increments
shrinking from 6 to 2). Mood does not reach +50.

### (ii) Introvert: delta = -4 per event

For negative deltas: DR factor = (1 + d/50). When `d` is negative, this factor < 1
(damping the drain as the agent approaches miserable).

| Event | d = round(prev * 0.9828) | DR factor = (1 + d/50) | eff = round(-4 * factor) | m = d + eff |
|---|---|---|---|---|
| 1 | 0 | 1.000 | -4 | **-4** |
| 2 | -4 | 0.920 | -4 (round(-4*0.92)) | **-8** |
| 3 | -8 | 0.840 | -3 | **-11** |
| 4 | -11 | 0.780 | -3 | **-14** |
| 5 | -14 | 0.720 | -3 | **-17** |
| 6 | -17 | 0.660 | -3 | **-20** |
| 7 | -20 | 0.600 | -2 (round(-4*0.60)) | **-22** |
| 8 | -22 | 0.560 | -2 | **-24** |
| 9 | -24 | 0.520 | -2 | **-26** |
| 10 | -26 | 0.480 | -2 | **-28** |

**Introvert result: -28** — labeled "sad", approaching "miserable" threshold (-30).
The series is also converging (increments shrinking from 4 to 2). Mood does not reach -50.

### Summary

| | Start | After 10 events (5 min) | Label |
|---|---|---|---|
| Extrovert | 0 | **+35** | elated |
| Introvert | 0 | **-28** | sad |
| Spread | — | **63 points** | |

Neither personality pins at their ceiling/floor. The 63-point spread makes personality a
**meaningful, visible signal** in the DynamoDB readings and in TOWN_STATUS.md mood labels.

---

## E. `mood_label` Thresholds — No Change Required

The existing thresholds are already well-calibrated for this design:

```python
# miserable <= -30  |  sad <= -10  |  neutral <= 10  |  happy <= 30  |  elated > 30
```

Under the new numbers:
- An introvert in an active social session will register "sad" after ~5 conversations.
  That is the correct read: they are genuinely unhappy being around people all the time.
- An extrovert in an active social session will register "elated" at around event 7.
  That is also correct.
- An agent with neither trait, getting occasional events, floats in "neutral" to "happy".

No threshold changes needed.

---

## F. Activity Registry Compatibility and Changes

### `apply_activity_effect` must apply the same DR formula

The activity path is separate from `apply_event` — it calls `apply_activity_effect` directly
in `activity_service.py`. This function currently applies a hard clamp only. It must be updated
to apply the same `apply_diminishing_returns` formula before adding the mood delta:

```python
# In apply_activity_effect, replace:
new_mood = min(50, max(-50, current_mood + base_mood))

# With:
effective_mood = apply_diminishing_returns(base_mood, current_mood)
new_mood = min(50, max(-50, current_mood + effective_mood))
```

The DR function should be extracted to `town_mood_engine.py` and imported from there
in `activity_registry.py` (the import already exists: `from core.services.town_mood_engine import normalize_traits`).

Decay does NOT need to be applied in `apply_activity_effect` because activities have explicit
start/end times managed by `activity_service.py`. The mood is already current at activity-start
(an `apply_event` call will have been made at start, which applies decay then). At completion,
the activity effect is applied to whatever mood the agent has at that moment — this is correct.

### Activity `mood_delta` magnitudes — compatible, with two adjustments

The activity registry deltas (0 to +8) are calibrated for a system without DR. With DR applied,
the effective delta at a high mood state will be smaller than the face value. This is the desired
behavior, but the most generous activities should be reviewed:

| Activity | Current `mood_delta` | Verdict |
|---|---|---|
| `sleep` | +8 | **Reduce to +5.** At mood=0, sleep is a big positive reset; with DR it naturally dampens at high mood. The +8 is generous but acceptable. However, to align with the new scale, reduce to +5. |
| `perform` | +6 | Keep. Performing is an active, effortful positive — +6 face value is reasonable; DR will bring it to +3 at mood=+40. |
| `exercise` | +4 | Keep. |
| `play_game` | +5 | Keep. |
| `garden` | +5 (including introvert_bonus +2 = effective +7 for introverts) | Keep. The introvert bonus is intentional — this is a solitary outdoor activity. |
| `listen_to_music` | +5 (+1 bonus) | Keep. |
| `nap` | +3 | Keep. |

**Only the `sleep` activity `mood_delta` needs to change: +8 → +5.**

The `introvert_bonus` / `extrovert_bonus` fields on activities do NOT need sign-flipping.
These are agent-chosen activities: introverts will self-select solo activities (library, apartment
bookshelf) and get their bonus; extroverts will self-select social venue activities (cafe,
plaza) and get theirs. The sign-differentiation for forced social exposure (involuntary
conversation events) is already handled entirely within `_TRAIT_MODIFIERS` in `apply_event`.

### The `solitary_activity` named event and the activity registry

`solitary_activity` is a named event fired by the backend after completing certain solo
activities. It goes through `apply_event`, not `apply_activity_effect`. The two paths are
additive: completing a solo library activity gives:
1. The activity's own `mood_delta` (via `apply_activity_effect`)
2. The `solitary_activity` named event (via `apply_event`)

The combined effect for an introvert at mood=0 completing `read` (mood_delta=+4, introvert_bonus=+2):
- `apply_activity_effect`: effective = DR(+6, 0) = +6 → mood = +6
- `apply_event("solitary_activity")`: introvert override = +5. DR(+5, 6) = round(5 * (1-6/50)) = round(5 * 0.88) = +4 → mood = +10

Combined: +10 mood for reading at the library. That is correct — introverts should enjoy
solitary intellectual activities, and +10 keeps them in "neutral" territory without spiking.

---

## Implementation Checklist

In order of dependency:

1. **Add `apply_decay` and `apply_diminishing_returns` to `town_mood_engine.py`.**
   Both are pure functions, easily unit-tested.

2. **Update `_BASE_EFFECTS` and `_TRAIT_MODIFIERS`** per Section A.

3. **Update `apply_event` signature** to accept `now` and `mood_updated_at`, apply decay and DR
   in the body. Keep defaults of `0.0` for backward compatibility.

4. **Add `moodUpdatedAt` field to the agent DynamoDB record** (ISO timestamp string or Unix
   float). Write it back alongside `mood` in every `apply_event` call-site. The field can be
   absent for existing agents — treat absence as `mood_updated_at = 0.0` (no decay on first call).

5. **Update `apply_activity_effect` in `activity_registry.py`** to apply `apply_diminishing_returns`
   before the hard clamp.

6. **Change `sleep` activity `mood_delta` from +8 to +5** in `activity_registry.py`.

7. **Update call sites** in `activity_service.py`, `daemon_bridge.py`, and `conversation_service.py`
   to pass `now=time.time()` and the agent's `moodUpdatedAt` field to `apply_event`.

8. **Unit tests** — add cases to `test_town_mood_engine.py`:
   - Introvert after 10 conversation_completed events converges near -28 (within ±3 for rounding).
   - Extrovert after 10 conversation_completed events converges near +35 (within ±3).
   - Neither personality reaches ±50 after 50 events.
   - Decay: mood=+40 with 20-minute elapsed → result is +20 (±1 for rounding).
   - DR: delta=+6 at mood=+50 → effective=0. Delta=-4 at mood=-50 → effective=0.

---

## Tuning Parameters and Safe Ranges

| Parameter | Current | New | Safe range | Impact |
|---|---|---|---|---|
| `MOOD_DECAY_HALF_LIFE_MINUTES` | N/A | 20.0 | 10–40 | Lower = faster drift to neutral; 10 min makes long absences reset mood strongly. |
| introvert `conversation_completed` mood delta | +3 | -4 | -8 to -1 | More negative = faster depletion for introverts in social sessions. |
| extrovert `conversation_completed` mood delta | +8 | +6 | +3 to +10 | Higher = faster elation for extroverts; +10 still converges with DR. |
| introvert `conversation_turn` mood delta | 0 | -1 | -2 to 0 | -2 makes long conversations punishing very fast for introverts. |
| extrovert `conversation_turn` mood delta | +2 | +2 | +1 to +3 | Unchanged; +3 would accelerate extrovert elation. |
| introvert `solitary_activity` mood delta | +5 | +5 | +3 to +8 | Unchanged; this is the introvert "recharge" signal. |
| extrovert `solitary_activity` mood delta | -2 | -3 | -5 to -1 | More negative = faster drain for extroverts in solitary mode. |
| `sleep` activity `mood_delta` | +8 | +5 | +2 to +8 | Sleep mood reset; lower values make sleep a functional reset (energy) not a mood spike. |

---

## Simulation Validation Parameters

Run before shipping. For each scenario, the expected outcome range is given.

| Scenario | Agent | Events | Rate | Expected final mood | Label |
|---|---|---|---|---|---|
| Hyper-social introverts | introvert | 20x conversation_completed | every 30s | -35 to -45 | miserable |
| Hyper-social extroverts | extrovert | 20x conversation_completed | every 30s | +40 to +48 | elated |
| Neutral agents, moderate activity | none | 10x conversation_completed | every 5 min | +5 to +15 | neutral–happy |
| Introvert, solitary session | introvert | 10x solitary_activity | every 2 min | +20 to +30 | happy |
| Extrovert, idle (no events, 1 hour) | extrovert | 0 events, starting mood +40 | — | +5 to +10 | neutral |
| No events, 20 min, starting mood +40 | any | 0 events | — | +18 to +22 | happy |
| No events, 40 min, starting mood +40 | any | 0 events | — | +8 to +12 | neutral |
