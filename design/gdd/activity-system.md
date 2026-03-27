# Activity System

> **Status**: Designed
> **Last Updated**: 2026-03-26
> **Priority**: MVP | **Layer**: Core

## Overview

The Activity System gives agents things to do at each location beyond walking. When an agent arrives at a location, TOWN_STATUS.md shows available activities. The agent picks one via `town_act <activity>`, the game server runs a timer, and the agent receives mood/energy/currency effects when the activity completes. Activities are location-locked — you can only `read` at the library, only `order_coffee` at the café. The agent's LLM decides what to do; the system just enforces rules and applies effects.

## Player Fantasy

Viewers watch agents develop routines and preferences. One agent always goes to the library first thing in the morning. Another is a café regular. A third spends all day at the activity center. Over time, these patterns emerge naturally from the agent's personality interacting with the activity system's mood/energy feedback loops. It feels like watching a tiny simulated life.

## Detailed Design

### Core Rules

1. Each location defines a list of available activities with their duration and effects.
2. An agent must be at a location to start its activities (`currentLocation` matches).
3. Agent sends `town_act <activity_name>` — game server validates location + activity.
4. Game server sets agent state to `busy` with activity name and start time.
5. During an activity, the agent cannot move or start another activity.
6. After duration elapses, game server sends `activity_complete` event with effects.
7. Backend applies mood/energy changes via `town_mood_engine.py`.
8. Agent returns to `idle` state and can act again on next `think` cycle.

### Activity Registry

| Location | Activity | Duration (s) | Mood | Energy | Currency | Animation |
|----------|----------|-------------|------|--------|----------|-----------|
| **Plaza** | `people_watch` | 10 | +3 | -2 | 0 | sitting |
| **Plaza** | `perform` | 20 | +5 | -5 | +2 | waving |
| **Library** | `read` | 15 | +4 (introvert: +6) | -3 | 0 | reading |
| **Library** | `study` | 20 | +2 | -5 | 0 | reading |
| **Library** | `write_journal` | 10 | +3 | -2 | 0 | writing |
| **Café** | `order_coffee` | 5 | +2 | +5 | -1 | sitting |
| **Café** | `sit_and_chat` | 15 | +4 (extrovert: +6) | -2 | 0 | sitting |
| **Café** | `work_on_laptop` | 20 | +1 | -5 | +3 | working |
| **Activity Center** | `exercise` | 15 | +3 | +8 | 0 | exercising |
| **Activity Center** | `play_game` | 20 | +5 | -3 | 0 | playing |
| **Activity Center** | `garden` | 15 | +4 (introvert: +6) | -3 | 0 | gardening |
| **Bank** | `check_balance` | 5 | 0 | -1 | 0 | standing |
| **Bank** | `browse_listings` | 10 | +1 | -2 | 0 | reading |
| **Residence** | `enter_apartment` | 3 | 0 | 0 | 0 | walking |
| **Apartment** | `sleep` | 30 | +5 | +100 | 0 | sleeping |
| **Apartment** | `work_at_desk` | 20 | +1 | -5 | +3 | working |
| **Apartment** | `cook` | 15 | +3 | +3 | -1 | cooking |
| **Apartment** | `watch_tv` | 15 | +4 | -1 | 0 | sitting |
| **Apartment** | `read_book` | 15 | +4 (introvert: +6) | -2 | 0 | reading |

### States and Transitions

| State | Description | Valid Transitions |
|-------|-------------|-------------------|
| `idle` | Agent is at a location, not doing anything | → `moving` (via `town_act move`), → `busy` (via `town_act <activity>`) |
| `moving` | Agent is navigating to a location | → `idle` (on arrival) |
| `busy` | Agent is performing a timed activity | → `idle` (on activity_complete), → `idle` (on interrupt — energy hits 0) |
| `sleeping` | Agent is asleep (apartment only) | → `idle` (on wake alarm or energy full) |
| `chatting` | Agent is in a conversation (future) | → `idle` (on conversation end) |

### Interactions with Other Systems

**Upstream (reads from):**
- **Town Navigation** [1]: Agent must be at the correct location. Game server checks `currentLocation`.
- **Mood & Energy** [5]: Activity effects are modulated by personality traits. Introvert/extrovert bonuses applied by `town_mood_engine.py`.

**Downstream (writes to):**
- **Apartment System** [7]: `enter_apartment` triggers context transition. Apartment activities share this registry.
- **Conversation System** [8]: `sit_and_chat` at café could initiate proximity-based conversations. Activity state blocks new conversations while `busy`.
- **Economy** [9]: Activities generate/spend currency. The currency column feeds into the wallet system.
- **Viewer UI** [15]: Activity name + animation state broadcast in `world_state` for rendering.

## Formulas

### Mood Change
```
base_mood_change = activity.mood
trait_bonus = 0

if agent.traits.introversion > 0.5 and activity.introvert_bonus:
    trait_bonus = activity.introvert_bonus - activity.mood  # e.g., +6 - +4 = +2
elif agent.traits.introversion <= 0.5 and activity.extrovert_bonus:
    trait_bonus = activity.extrovert_bonus - activity.mood

final_mood = clamp(current_mood + base_mood_change + trait_bonus, -50, +50)
```

### Energy Change
```
final_energy = clamp(current_energy + activity.energy, 0, 100)

# If energy hits 0 during an activity, interrupt it
if final_energy <= 0:
    emit("activity_interrupted", reason="exhausted")
    agent.state = "idle"
```

### Currency Change
```
# Only applied when economy system is active
final_balance = max(0, current_balance + activity.currency)

# Cannot start an activity that costs currency if balance is insufficient
if activity.currency < 0 and current_balance + activity.currency < 0:
    emit("activity_rejected", reason="insufficient_funds")
```

## Edge Cases

| Case | Resolution |
|------|------------|
| Agent starts activity then disconnects | Activity timer continues; effects applied on completion. If agent reconnects, they see remaining time. |
| Agent tries activity at wrong location | Game server rejects with `activity_rejected` event. |
| Agent tries activity while already busy | Game server rejects. Agent must wait for current activity to finish. |
| Energy hits 0 during activity | Activity interrupted early. Partial mood effect (proportional to time completed). No currency change. |
| Game server restarts during activity | Activity state lost. Agent returns to `idle` at their location. Acceptable for MVP — state persistence (#14) fixes this later. |
| Two agents do `sit_and_chat` at same location | Both get mood bonus independently. Future: could trigger automatic conversation. |
| Unknown activity name sent | Game server rejects with `activity_rejected, reason="unknown_activity"`. |

## Dependencies

| System | Direction | Interface | Hard/Soft |
|--------|-----------|-----------|-----------|
| Town Navigation [1] | Upstream | `currentLocation` field on agent | Hard |
| Mood & Energy [5] | Upstream | `apply_event()` to modify mood/energy | Hard |
| Agent Daemon [6] | Upstream | Receives `activity_complete` event, writes to TOWN_STATUS.md | Hard |
| Apartment System [7] | Downstream | `enter_apartment` activity triggers context transition | Soft |
| Conversation System [8] | Downstream | `busy` state blocks conversation invites | Soft |
| Economy [9] | Downstream | Currency changes from activities | Soft (ignored until economy built) |
| Viewer UI [15] | Downstream | `activity` field in `world_state` broadcast | Soft |

## Tuning Knobs

| Knob | Default | Range | Effect if too high | Effect if too low |
|------|---------|-------|-------------------|-------------------|
| Activity duration multiplier | 1.0 | 0.5–3.0 | Activities feel tedious, agents do fewer things | Activities feel instant, no sense of time passing |
| Mood effect multiplier | 1.0 | 0.5–2.0 | Mood swings wildly, feels unrealistic | Mood barely changes, feels unresponsive |
| Energy cost multiplier | 1.0 | 0.5–2.0 | Agents exhaust fast, go home constantly | Agents never tire, no sleep pressure |
| Currency earn rate | 1.0 | 0.1–5.0 | Inflation, currency loses meaning | Agents never earn enough to spend |
| Introvert/extrovert bonus | +2 | 0–5 | Personality dominates all decisions | Personality has no meaningful effect |

## Visual/Audio Requirements

Each activity needs a visual state for the agent sprite:
- **Animation**: Use PixelLab `animate-with-text-v3` to generate per-activity animations (reading, working, sitting, sleeping, cooking, etc.)
- **Activity label**: Text label above agent sprite showing current activity (e.g., "📖 Reading")
- **Progress indicator**: Optional subtle progress bar or timer near the agent (Phase 2)
- **Completion effect**: Small particle burst or icon when activity completes

## Acceptance Criteria

- [ ] Agent at library can `town_act read` → enters `busy` state for 15s → receives `activity_complete` with mood +4, energy -3
- [ ] Agent at café cannot `town_act read` → receives `activity_rejected`
- [ ] Agent already `busy` cannot start another activity → receives `activity_rejected`
- [ ] Introvert agent at library gets mood +6 (not +4) from reading
- [ ] Agent with 0 energy gets interrupted mid-activity
- [ ] `world_state` broadcast includes `activity` field for busy agents
- [ ] TOWN_STATUS.md shows available activities when agent arrives at location
- [ ] Activity list in TOWN_STATUS.md only shows activities for current location
