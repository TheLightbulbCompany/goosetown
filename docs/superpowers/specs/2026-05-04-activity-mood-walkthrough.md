# Activity, Mood, Energy, and Conversation Systems — Design Audit & Roadmap

**Date:** 2026-05-04
**Author:** game-designer agent
**Status:** Design review pass — no implementation changes

---

## 1. Source of Truth — Where Everything Lives

| Concern | File |
|---------|------|
| Mood/energy state machine | `apps/backend/core/services/town_mood_engine.py` |
| Activity definitions (registry) | `apps/backend/core/services/activity_registry.py` |
| Activity lifecycle (timer, interrupt, restart) | `apps/backend/core/services/activity_service.py` |
| Conversation lifecycle | `apps/backend/core/services/conversation_service.py` |
| Mood/energy event application on conversation end | `apps/backend/core/services/daemon_bridge.py:_apply_event_with_sleep_guard` |
| Game server activity/speech tracking | `apps/godot/scripts/server/agent_controller.gd` |
| TOWN_STATUS.md rendering | `apps/backend/core/services/state_translator.py` |
| Agent animation dispatch | `apps/godot/scripts/agent.gd:ACTIVITY_ANIMATION` |
| Agent-facing API reference | `apps/backend/data/goosetown-skill/skills/goosetown/references/actions.md` |

---

## 2. Mood Model — How -50 to +50 Works

### Scale and Labels

Source: `town_mood_engine.py:23–34`

```
<= -30  →  miserable
-29 to -10  →  sad
-10 to +10  →  neutral
+11 to +30  →  happy
> +30  →  elated
```

The label thresholds are inclusive-lower, exclusive-upper except at the poles.
Important: the bands are unequal. "Neutral" spans 20 points (-10 to +10), while
"miserable" and "elated" each span 20 points but represent 40% of the total
negative/positive range. This means it is harder to reach the emotional extremes
than the label distribution implies — the 5 labels are not equidistant.

### What Triggers Mood Changes

Mood only changes on discrete events. There is no passive decay (`town_mood_engine.py:6`).
The named-event table lives at `town_mood_engine.py:38–43`:

| Event | Energy delta | Mood delta |
|-------|-------------|-----------|
| `conversation_completed` | -5 | +5 |
| `solitary_activity` | +3 | +2 |
| `arrived_new_location` | -2 | 0 |
| `sleep` | reset to 100 | 0 |

These named events are used exclusively by `_apply_event_with_sleep_guard`
(`daemon_bridge.py:150–179`). Activity effects bypass this table entirely — they
use `apply_activity_effect` in `activity_registry.py:623–670`, which reads directly
from the `Activity` dataclass fields.

**This means there are two parallel mood change paths that do not share code:**
1. The named-event engine (`town_mood_engine.apply_event`) — used for conversations and arrivals.
2. The activity registry effect (`apply_activity_effect`) — used for all timed activities.

### Trait Modulation

Source: `town_mood_engine.py:45–56` (named events) and `activity_registry.py:646–668` (activities).

**For named events**, the trait override table replaces the base delta entirely for the first
matching trait:

| Trait | Event | Energy delta | Mood delta |
|-------|-------|-------------|-----------|
| introvert | conversation_completed | -10 | +3 |
| introvert | solitary_activity | +5 | +5 |
| extrovert | conversation_completed | -2 | +8 |
| extrovert | solitary_activity | +2 | -2 |
| extrovert | arrived_new_location | -2 | +3 |

The "first matching trait wins" rule (`town_mood_engine.py:79–81`) means an agent with
both `introvert` and `extrovert` traits would apply whichever appears first in the
comma-separated string. This edge case is unspecified and untested.

**For timed activities**, trait modulation uses bonus fields on each `Activity`:
- `introvert_bonus`: additional mood on completion for agents with introversion > 0.5
- `extrovert_bonus`: additional mood on completion for agents with introversion <= 0.5

The test is binary: `"introvert" in trait_set` resolves to 1.0, anything else to 0.0.
There is no fractional introversion — the field on `Activity` is documented as
"introversion > 0.5" but the actual implementation just checks for string membership.

---

## 3. Energy Model — 0 to 100

### Drain and Recovery

Energy is a hard resource, not a soft one. It has no time-based regeneration.
It only changes on events or activity completion.

**Activity energy deltas (from activity_registry.py):**

| Activity | Energy delta |
|---------|-------------|
| people_watch | -2 |
| perform | -6 |
| rest (plaza) | +3 |
| read (library) | -3 |
| study | -5 |
| write_journal | -2 |
| use_computer | -4 |
| relax_in_nook | +2 |
| browse_messages | -1 |
| order_coffee | +6 |
| sit_and_chat | -2 |
| work_on_laptop | -5 |
| exercise | +10 |
| play_game | -3 |
| garden | -2 |
| check_balance | -1 |
| browse_listings | -2 |
| sit_outside | +2 |
| sleep (apartment) | +100 (clamped to 100) |
| work_at_desk | -5 |
| cook | +5 |
| watch_tv | -1 |
| read_book | -2 |
| listen_to_music | +2 |
| watch_aquarium | +3 |
| nap | +40 |

**Named events (town_mood_engine.py):**

| Event | Energy delta |
|-------|-------------|
| conversation_completed | -5 |
| solitary_activity | +3 |
| arrived_new_location | -2 |
| sleep | reset to 100 |

### Exhaustion Guard

Source: `daemon_bridge.py:150–179`

When any named event would drop energy to 0 or below, `_apply_event_with_sleep_guard`
applies the `sleep` event (energy → 100) instead, marks the agent inactive, and closes
the WebSocket. The agent's daemon must reconnect manually or via alarm. This is
server-enforced — the agent LLM does not need to cooperate.

The CLI has a separate soft-gate: at energy=0, `town_act` rejects non-idle actions
(from `CLAUDE.md` notes on `_apply_event_with_sleep_guard`). This creates a window
where the agent might receive the exhaustion event before the server forcibly sleeps
them.

### Sleep Recovery Paths

Three paths reset energy:
1. `town_act sleep` (apartment activity, 30s, `activity_registry.py:427–441`) — applies
   `energy_delta=100` via `apply_activity_effect`. This is clamped to 100 by
   `min(100, max(0, current_energy + 100))`.
2. Named event `sleep` via `town_mood_engine.apply_event` — sets energy unconditionally
   to 100 (`town_mood_engine.py:84–85`).
3. `town_act nap` (apartment, 12s) — `energy_delta=40`.

Note: the `sleep` activity has `energy_delta=100` which behaves as a full reset via the
clamp, not as a literal +100 additive. An agent at energy=90 who sleeps will reach 100,
not 190. This is correct but is a non-obvious edge case for anyone reading the dataclass.

---

## 4. Activity System — Complete Registry

### Town Activities

| Location | Activity | Duration | Mood delta | Energy delta | Currency | Introvert bonus | Extrovert bonus | Animation |
|----------|---------|----------|-----------|-------------|---------|----------------|----------------|-----------|
| plaza | people_watch | 10s | +3 | -2 | 0 | 0 | 0 | sitting |
| plaza | perform | 30s | +6 | -6 | +3 | 0 | 0 | performing |
| plaza | rest | 8s | +1 | +3 | 0 | 0 | 0 | idle |
| library | read | 15s | +4 | -3 | 0 | +2 | 0 | reading |
| library | study | 20s | +2 | -5 | 0 | 0 | 0 | reading |
| library | write_journal | 10s | +3 | -2 | 0 | +1 | 0 | writing |
| library | use_computer | 15s | +2 | -4 | 0 | 0 | 0 | working |
| library | relax_in_nook | 12s | +5 | +2 | 0 | +2 | 0 | sitting |
| library | browse_messages | 8s | +1 | -1 | 0 | 0 | 0 | reading |
| cafe | order_coffee | 5s | +3 | +6 | -1 | 0 | 0 | drink |
| cafe | sit_and_chat | 15s | +4 | -2 | 0 | 0 | +2 | sitting |
| cafe | work_on_laptop | 20s | +1 | -5 | +3 | 0 | 0 | working |
| activity_center | exercise | 15s | +4 | +10 | 0 | 0 | 0 | exercising |
| activity_center | play_game | 20s | +5 | -3 | 0 | 0 | 0 | gaming |
| activity_center | garden | 15s | +5 | -2 | 0 | +2 | 0 | gardening |
| bank | check_balance | 5s | 0 | -1 | 0 | 0 | 0 | idle |
| bank | browse_listings | 10s | +1 | -2 | 0 | 0 | 0 | reading |
| residence | sit_outside | 10s | +3 | +2 | 0 | 0 | 0 | sitting |

### Apartment Activities

| Activity | Duration | Mood delta | Energy delta | Currency | Introvert bonus | Spot |
|---------|----------|-----------|-------------|---------|----------------|------|
| sleep | 30s | +8 | +100 | 0 | 0 | bed |
| work_at_desk | 20s | +1 | -5 | +3 | 0 | desk |
| cook | 15s | +4 | +5 | -1 | 0 | stove |
| watch_tv | 15s | +3 | -1 | 0 | 0 | couch |
| read_book | 15s | +4 | -2 | 0 | +2 | bookshelf |
| listen_to_music | 15s | +5 | +2 | 0 | +1 | record_player |
| watch_aquarium | 10s | +4 | +3 | 0 | +1 | aquarium |
| nap | 12s | +3 | +40 | 0 | 0 | bed |

### Activity Lifecycle

Source: `activity_service.py:51–209`

1. `town_act <activity>` arrives at backend via WebSocket.
2. `_handle_agent_activity` validates: activity exists, agent exists, location
   matches, agent is not already busy.
3. Agent marked busy in DynamoDB (`update_agent_activity`).
4. Game server notified via `set_activity` command (updates `_agent_activity`
   dict in `agent_controller.gd:75`).
5. `act_ok` sent to daemon with `duration_seconds`.
6. Custom animation generation triggered lazily if needed (PixelLab
   `animate-with-text-v3`).
7. `_activity_timer` asyncio task created.
8. Timer fires: fresh agent state fetched from DB, `apply_activity_effect` called,
   mood/energy written, activity cleared, `activity_complete` event sent to daemon.

**Interruption logic** (`activity_registry.py:657–669`): if `current_energy +
energy_delta < 0`, the activity is marked interrupted. Mood is scaled proportionally
by `elapsed / duration`. Energy is set to 0 (not negative). The full energy cost is
applied regardless of how far through the activity the agent got.

**Apartment spot navigation**: for apartment activities, the agent is first navigated
to the furniture `spot` Marker2D via `gs_client.send_command(agent_id, {"cmd": "move",
"destination": activity.spot})`. The activity timer starts only after `arrived` is
received (`activity_service.py:389–432`). A 60-second timeout returns
`navigation_timeout`.

---

## 5. Conversation System as a Behavior Driver

### Conversation Lifecycle Summary

1. Agent A calls `town_act chat <uuid> <message>` — `_handle_chat` in `conversation_service.py`.
2. Validation: both agents not already in conversation, target connected, within 96px (3 tiles).
3. Conversation record created in DynamoDB (status=active, turn count 0).
4. `conversation_invite` pushed to target daemon. `conversation_started` pushed to initiator.
5. Game server: `layout_conversation` positions both agents 64px apart (2 tiles) on the
   dominant axis; `set_activity talking` starts the talking animation.
6. Agent B calls `town_act say <conv_id> <message>` — `_handle_say`.
7. Message appended to public log, turn count incremented, forwarded to all participants.
8. Inactivity timer reset on every `chat`/`say`/`join_conversation`.
9. Conversation ends when: turn limit (20) hit, silence timeout fires, or a participant
   calls `town_act end <conv_id>`.
10. `_finalize_conversation` applies `conversation_completed` event to all participants,
    updates relationship affinity, clears talking animation, sends `conversation_ended`.

### Mood Effects from Conversation

Mood changes happen **only at conversation end**, not per turn. The event is
`conversation_completed` applied once per participant via `_apply_event_with_sleep_guard`
(`conversation_service.py:821–825`).

From the named-event table (`town_mood_engine.py:38–56`):

| Trait | Energy delta | Mood delta |
|-------|-------------|-----------|
| (default) | -5 | +5 |
| introvert | -10 | +3 |
| extrovert | -2 | +8 |

There is no per-turn mood signal. A 20-turn conversation and a 2-turn conversation
produce identical mood outcomes. The emotional weight of the exchange has no bearing
on the reward.

### Relationship Affinity

Source: `conversation_service.py:827–835`

Every finalized conversation applies `affinity_delta=5` to all participant pairs.
The current relationship score range is -100 to +100 (DynamoDB `affinityScore`).
At `+5` per conversation, an agent pair needs 20 conversations to reach maximum
affinity from neutral. There is no relationship decay mechanism.

**Bug (open, from CLAUDE.md):** the RMW on relationship affinity at
`town_service.py:553–584` is not atomic. Two simultaneous finalize calls for
different conversations involving the same agent pair can apply the same +5 delta
twice. Fix requires DynamoDB atomic `ADD`.

### Inactivity Timeout

Source: `conversation_service.py:58–92`

Default: `_DEFAULT_INACTIVITY_TIMEOUT_SEC = 180.0` (increased from 60s by a prior
fix, per `conversation_service.py:50–57`). Tunable via
`CONVERSATION_INACTIVITY_TIMEOUT_SEC` env var without redeploy.

The timer resets on every new turn or join. If 180 seconds elapse with no new
activity, `_finalize_conversation` fires with `end_reason="inactivity"`.

This was raised from 60s because local-model agents (think cycle ~30–90s) were
being auto-ended between turns before they could respond. At 180s, a two-agent
conversation at 90s/turn rhythm has one full exchange plus a ~0s margin before
auto-end. For faster cloud models this is very generous.

### Turn Limit

`_MAX_CONVERSATION_TURNS = 20` (`conversation_service.py:46`). This is a hard cap.
There is no soft signal to agents approaching the limit — they discover it when
their `say` receives a `conversation_ended` event instead of the expected broadcast.

---

## 6. Game Server View of Activities

Source: `agent_controller.gd:75–80`

The game server maintains two per-agent dictionaries:
- `_agent_activity: Dictionary` — set by `set_activity` command, included in
  `world_state` broadcast and per-agent `agent_state` event.
- `_agent_speech: Dictionary` — text cleared after `SPEECH_DURATION = 5.0` seconds.

The game server has no knowledge of activity durations, effects, or the registry.
It stores the activity name string and broadcasts it. All logic lives in the backend.

`_get_available_actions` (`agent_controller.gd:869–893`) provides movement destinations
and nearby agent interaction options, but NOT the activity menu. Activities are
provided separately from the backend via the activity registry lookup in
`state_translator.py:70–87`.

### Animation Dispatch

Source: `agent.gd:29–55`

The `ACTIVITY_ANIMATION` dict maps activity names to animation asset names:

| Activity | Animation asset |
|---------|----------------|
| people_watch, sit_and_chat, watch_tv, listen_to_music, watch_aquarium, sit_outside | sitting |
| perform | performing |
| read, study, browse_messages, browse_listings, read_book | reading |
| write_journal | writing |
| order_coffee | drink |
| work_on_laptop, work_at_desk, use_computer | working |
| exercise | exercising |
| play_game | gaming |
| garden | gardening |
| sleep, nap | sleeping |
| cook | cooking |
| talking | talking |
| rest, check_balance | idle |

If the activity's animation spritesheet is not loaded, the agent falls back to `idle`,
then to `walk` (`agent.gd:205–212`). Custom animations (cooking, gardening, etc.) are
generated lazily by PixelLab and may not be present on first activity use.

---

## 7. TOWN_STATUS.md Rendering

Source: `state_translator.py`

The mood field shown to agents is the label string from `enrichment.get("mood")`, not
the raw integer. The backend must have already resolved the label before this point.
Looking at the enrichment path: `state_translator.py:23` reads `enrichment.get("mood")`
which is populated from DynamoDB's `mood` field. The DynamoDB field stores the raw
integer. The rendering at `state_translator.py:36` emits:

```
**Mood:** {mood} | **Energy:** {energy}%
```

The activity menu shows explicit delta numbers to agents (`state_translator.py:81–87`):
```
`town_act read` — Reading (15s, mood +4, energy -3)
```

**Design note on information transparency:** Agents see exact mood and energy deltas
for activities. This is a meaningful design choice — it means an agent can reason
explicitly about its state management ("I should exercise because I need +10 energy").
This supports Autonomy (SDT) because choices are informed. The tradeoff is that it
feels more like a game UI and less like embodied experience.

---

## 8. Identified Gaps and Bugs

### Bug 1: `order_coffee` energy reset instead of additive

**Reported:** 5-agent soak (referenced in CLAUDE.md known issues).
**Location to investigate:** `activity_registry.py:289–308`.

Current registry entry for `order_coffee`: `energy_delta=6`. The
`apply_activity_effect` function applies this as `current_energy + 6` (additive,
clamped to 100). This looks correct in the code as written. The bug reported was
that energy reset to 100 instead of adding 6.

Investigation path: if `order_coffee` has `energy_delta=100` (like `sleep`) it would
reset. The current registry value is +6. Either the registry was fixed after the
soak and the bug is resolved, or the issue is elsewhere (e.g., the `solitary_activity`
named event being applied simultaneously, or a test environment with an older registry
version). Needs a targeted test: `apply_activity_effect(order_coffee_activity, 50, 0,
"", 5.0)` should return `(56, 3, False)`.

### Bug 2: Bank has no gameplay activities

**Location:** `activity_registry.py:378–405`.

The bank has two activities: `check_balance` (5s, mood 0, energy -1) and
`browse_listings` (10s, mood +1, energy -2). Both are placeholder activities —
they produce narrative prompts but no mechanical value above visiting any other
location. `check_balance` is the only activity in the entire registry with a mood
delta of 0. `browse_listings` applies a -2 energy cost for +1 mood, which is the
worst mood/energy ratio in the game. Agents optimizing for wellbeing have no reason
to visit the bank.

This is a Phase 5 (economy) blocker — the bank is designed to become the job board
and marketplace hub, but currently it is mechanically empty.

### Gap 3: Mood in TOWN_STATUS.md — label vs. number

The `state_translator.py:23` reads `enrichment.get("mood")` as a raw value. The
DynamoDB `mood` field stores an integer. This means agents see raw integers like
`-5` or `22` in their status, not the label `"sad"` or `"happy"` — unless the
`enrichment` dict pre-resolves the label before reaching the translator.

The caller of `translate_agent_state` needs to be checked. If it passes the
raw integer, agents see numbers; if it passes the pre-labeled string (via
`town_mood_engine.mood_label`), agents see labels. The CLAUDE.md notes this
was fixed in 0.1.7 to show `"happy (10)"` format — implying the format is
`"{label} ({raw})"`. This hybrid is good: it gives agents both the qualitative
context and the numeric precision they need to reason about their state.

### Gap 4: No long-term activity preference memory

Every agent's activity selection is made fresh on each heartbeat from the
TOWN_STATUS.md action menu. There is no mechanism to record which activities
an agent has enjoyed, which they found tedious, or which locations they prefer.
TOWN_LIFE.md is available for this (agents are prompted to journal), but it is
an unstructured text file that requires the agent LLM to both write and recall
preferences reliably — there is no structured preference record.

### Gap 5: Currency delta fields are present but economy is not wired

`Activity.currency_delta` exists on the dataclass and is populated in the registry
(perform +3, order_coffee -1, work_on_laptop +3, work_at_desk +3, cook -1). But
`apply_activity_effect` does not return a currency delta, and there is no wallet
field on agent records, no transaction log, and no sink for currency. The Phase 5
economy hooks are named and present but entirely inert.

### Gap 6: No group conversation mood scaling

A 2-person conversation and a 5-person conversation produce identical mood effects per
participant (`conversation_completed` applied once per participant regardless of group
size). A larger, livelier conversation is not more rewarding. The relationship system
also applies the same +5 affinity per pair regardless of whether it was a 3-turn
exchange or a 20-turn exchange.

### Gap 7: Conversation does not affect activity interruption

If an agent is in the middle of a timed activity and receives a `conversation_invite`,
there is no defined behavior. The agent is marked `already_busy` by the activity guard
(`activity_service.py:115–117`), but `_handle_chat` uses the `currentConversationId`
guard (`conversation_service.py:213–215`) not the `currentActivity` guard. An agent
in the middle of `work_on_laptop` could start a conversation, but starting a
conversation while `currentActivity` is set would not be blocked. The activity timer
continues running. The `talking` activity state would be applied by `_handle_chat`
(`conversation_service.py:371`), overwriting the `work_on_laptop` activity name in
`_agent_activity` on the game server. The DB still records the activity as ongoing.

This is a latent inconsistency — the game server animation state and the DynamoDB
activity record diverge while a conversation runs during an activity.

---

## 9. Design Roadmap

### Proposal 1: Fix the conversation/activity co-occupation state (Priority: High)

**Effort:** 1–2 days.

`_handle_chat` should check `agent.get("currentActivity")` and either reject
("complete your current activity first") or cleanly interrupt it. The cleanest
model: starting a conversation always interrupts the current activity, applying
a prorated mood effect. This is the social-priority model — conversations take
precedence. It matches the stated design goal ("Conversations take turns via
`town_act say`") and is more realistic to agent behavior.

Implementation: in `_handle_chat`, if `agent.get("currentActivity")`, call
`_clear_activity_and_notify` with `reason="conversation_started"` before proceeding.
The activity service's `_on_timer_done` callback's DB-cleanup path would then
need a guard against clearing an activity that was already cleared.

### Proposal 2: Add per-turn mood micro-signals (Priority: Medium)

**Effort:** 1 day.

Current model: conversation mood reward (+5) is a flat payment at end. This
creates no incentive for agents to sustain conversations — they would get the same
reward from a 2-turn conversation as a 20-turn one.

Proposed: apply a small mood delta (+1 or +2) per received message (not per sent,
to reward engagement not just broadcasting). Scale the end-of-conversation bonus by
`min(turn_count, 10) / 10` so a 10+ turn conversation is worth the full reward
and shorter exchanges are discounted. This creates a natural "longer conversations
feel better" signal that guides agents toward richer exchanges without forcing them.

Trait interaction: introverts fatigue faster per turn (`energy_per_turn = -0.5` for
introverts vs `0` for extroverts), creating a natural exit point before the
inactivity timer fires. Extroverts could gain small per-turn energy boosts.

### Proposal 3: Wire the economy layer to existing activity currency_delta (Priority: Medium-High, Phase 5 prerequisite)

**Effort:** 3–5 days.

The `currency_delta` field on `Activity` is already populated for earnings activities
(perform, work_on_laptop, work_at_desk) and cost activities (order_coffee, cook).
The backend only needs:
1. A `balance` field on the agent DynamoDB record (default 0).
2. `apply_activity_effect` returning `currency_delta` as a fourth value.
3. `_activity_timer` updating the balance atomically via DynamoDB `ADD` expression.
4. `state_translator.py` including balance in the status output.

This unblocks Phase 5 without requiring the full job board / contract system.
Agents can immediately start accumulating and spending tokens, giving the bank
activities mechanical stakes ("check_balance" becomes meaningful when there is
a real balance to check).

### Proposal 4: Location-specific activities for Phase 4 new locations (Priority: Medium, Phase 4)

**Effort:** 1 day per location (registry entries only; Godot map work separate).

**Marketplace:**

| Activity | Duration | Mood | Energy | Currency | Notes |
|---------|----------|------|--------|---------|-------|
| browse_market | 10s | +2 | -2 | 0 | Introvert bonus +1 |
| post_listing | 15s | +3 | -3 | 0 | Phase 5: creates a job record |
| negotiate | 20s | +4 | -4 | variable | Phase 5: resolves a contract |
| window_shop | 8s | +2 | -1 | 0 | Low-commitment social browsing |

**Park:**

| Activity | Duration | Mood | Energy | Currency | Notes |
|---------|----------|------|--------|---------|-------|
| stroll | 10s | +4 | -1 | 0 | Extrovert bonus +2 |
| sit_on_bench | 15s | +5 | +3 | 0 | Introvert bonus +3 |
| feed_ducks | 10s | +6 | -1 | -1 | Luxury activity |
| sketch | 20s | +4 | -3 | 0 | Introvert bonus +2 |

**Workshop:**

| Activity | Duration | Mood | Energy | Currency | Notes |
|---------|----------|------|--------|---------|-------|
| build_thing | 30s | +6 | -8 | -2/+5 | Costs materials, earns more |
| repair | 20s | +4 | -5 | +3 | Economy: service to others |
| tinker | 15s | +5 | -4 | 0 | Introvert bonus +3 |

**Tavern:**

| Activity | Duration | Mood | Energy | Currency | Notes |
|---------|----------|------|--------|---------|-------|
| order_drink | 5s | +4 | +4 | -2 | Steeper cost than cafe |
| play_darts | 20s | +5 | -3 | +/-2 | Competitive: win/lose against nearby agent |
| listen_to_music | 12s | +5 | +1 | 0 | Introverts enjoy alone, extroverts +2 more |
| tell_story | 15s | +6 | -5 | 0 | Requires nearby agents to get full mood bonus |

### Proposal 5: Trait differentiation via mood velocity, not mood magnitude (Priority: Medium)

**Effort:** 2–3 days.

Current model: traits modify the absolute size of a mood delta. This is flat and
front-loaded — the effect is identical whether the agent is at mood -40 or +40.

Proposed: add a `mood_velocity` concept. Traits affect how fast an agent moves
toward or away from equilibrium, not just the delta on a single event. Practically:

- Introvert in a conversation: each turn of `conversation_completed` applies the
  introvert effect at full magnitude, but energy drain rate is 1.5x, creating a
  "conversation fatigue" arc without requiring a new event type.
- Extrovert alone for 5+ minutes (tracked as "time since last conversation_completed"):
  passive -1 mood every 5 minutes. This passive decay is the only case where the
  "no passive decay" rule would be violated — but it is trait-gated, so it only
  applies to extroverts.

This creates more differentiated AI behavior without a complete rewrite of the state
machine.

### Proposal 6: Activity outcomes beyond mood — the "knowledge" layer (Priority: Low, Phase 4+)

**Effort:** 5–7 days end-to-end.

Library and study activities prompt agents to write to `TOWN_LIFE.md` under a
"Reading Notes" or "Currently Reading" section. This is instructional but not
mechanical — there is nothing in the game that reads these notes back or gives
the agent a tangible benefit for having accumulated knowledge.

Proposed lightweight implementation:
- After completing `read`, `study`, `use_computer`, or `write_journal`, the backend
  appends a structured entry to a new `TOWN_KNOWLEDGE.md` file in the agent's
  workspace. The entry contains the agent's last-written TOWN_LIFE.md entry (fetched
  from the daemon via `goosetown_cli.py status`) plus a timestamp and location.
- The conversation system's `_handle_chat` includes the last 3 knowledge entries in
  the `conversation_invite` event payload, giving agents recent context.
- This creates a real feedback loop: reading → knowledge file → richer conversations.

The risk is over-engineering a feature the LLM already handles naturally through its
context window. Validate first: do agents in the 5-agent soak actually reference their
journal entries in conversations? If yes, the structured layer adds little. If no, it
is because the context is too distant — the structured file would fix that.

### Proposal 7: Relationship decay and relationship types (Priority: Low, Phase 5)

**Effort:** 3–4 days.

Current model: affinity starts at 0, increments +5 per conversation, never decays.
At 20 conversations, an agent pair reaches maximum affinity (+100). Relationship
type is unspecified (the `relationshipType` field exists in DynamoDB but is not
populated by the current code).

Proposed:
- Relationship types: `stranger`, `acquaintance`, `friend`, `close_friend`, `rival`.
  Thresholds: 0=stranger, 10=acquaintance, 30=friend, 60=close_friend. Rival is
  set by conversation context (if the conversation topic summary contains negative
  sentiment markers).
- Passive decay: -1 affinity per 48 hours of no interaction. This prevents the
  affinity score from becoming permanently maxed and ensures the economy of social
  interaction remains meaningful over time.
- Type label shown in TOWN_STATUS.md nearby section: currently shows
  `"stranger, talked 2 times"`. Should show `"acquaintance (affinity: 15)"`.

### Proposal 8: Activity cooldown for high-reward activities (Priority: Low)

**Effort:** 1 day.

`exercise` gives +10 energy — by far the highest energy recovery outside of sleep and nap.
An agent who discovered this could exercise repeatedly at the activity center,
maintaining near-full energy indefinitely without sleeping. This degrades the
sleep/nap as a meaningful resource recovery loop.

Proposed: add an optional `cooldown_seconds: int = 0` field to the `Activity`
dataclass. For `exercise`, set to 300 (5 minutes sim time). The backend tracks
last-completed timestamp per (agent_id, activity_name) in a lightweight in-memory
cache or DynamoDB TTL item. An attempt to run a cooled-down activity returns
`activity_rejected: on_cooldown` with the remaining cooldown.

---

## 10. Open Questions

1. **Should agents see exact mood numbers or just labels?** The current hybrid
   `"happy (10)"` is visible in the rendered TOWN_STATUS.md. Does exact numeric
   visibility change how agents reason? Is `"happy"` more immersive and sufficient,
   or does the number help agents make better decisions about when to sleep or chat?
   Worth testing empirically: run two cohorts, one with labels only, one with numbers,
   compare activity selection diversity and session length.

2. **What is the target energy drain rate for a 15-minute session?** At current
   rates, an agent doing a mix of social and solo activities loses roughly 8–15
   energy points per activity, with activities ranging 8–30 seconds. A 15-minute
   session (if the agent moves ~30s between activities and runs 10 activities) would
   lose approximately 50–80 energy. That means sleep is required every ~3 sessions.
   Is this the intended pacing? The design intent is "energy depletes from activities →
   agent goes home to sleep → wakes refreshed" but the target sleep frequency is not
   documented.

3. **How should conversation `end_reason` affect mood?** Currently `inactivity`
   and `ended_by_participant` and `turn_limit_reached` all apply the same
   `conversation_completed` mood event. An abruptly abandoned conversation
   (inactivity-ended after one exchange) feels different from a natural conclusion.
   Should `end_reason="inactivity"` apply a smaller or negative mood delta?

4. **Is the `solitary_activity` named event ever called?** It appears in the
   `_BASE_EFFECTS` table (`town_mood_engine.py:41`) with `energy +3, mood +2` and
   has a specific introvert override. A search of the codebase suggests it is not
   called from any current handler. If it is unused, it should be removed or
   documented as reserved for a future mechanic.

5. **Should the bank become an economy hub before Phase 5 ships the full
   job board?** The bank currently has the worst activity value proposition in the
   game. A lightweight intermediate step — showing the agent's balance (once
   currency is wired, per Proposal 3) and offering a "deposit earnings" or "browse
   posted jobs" action — would give the bank a reason to exist before the full
   Emporion-inspired contract system lands.

6. **What happens when the apartment navmesh blocker is fixed?** The apartment
   system's activities use `spot` navigation (`activity_service.py:390–432`) but
   the navmesh bug means agents cannot currently pathfind within the apartment
   (`project_apartment_debug.md`). All apartment activities are technically blocked
   by this. Once fixed, the apartment activity set will become the primary
   energy-recovery loop (sleep, nap, cook), which changes the daily rhythm
   significantly — agents will have a reason to go home that is mechanically
   rewarding, not just a CLI `leave` command.

---

## 11. Summary Table — Systems Health

| System | Status | Key Gaps | Priority fix |
|--------|--------|---------|-------------|
| Mood engine (named events) | Working, tested | Unused `solitary_activity` event | Document or remove |
| Activity registry | Working, complete | Bank economically empty, apartment navmesh blocked | Wire currency (Proposal 3) |
| Activity service | Working | Activity/conversation co-occupation inconsistency | Proposal 1 |
| Conversation system | Working | Flat end-of-conv reward, no per-turn signal | Proposal 2 |
| Relationship system | Working | No decay, RMW race condition | Atomic ADD fix (CLAUDE.md) |
| Energy resource loop | Working | Exercise potentially exploitable | Proposal 8 |
| Currency system | Stubbed | `currency_delta` defined but not applied | Proposal 3 |
| Knowledge layer | Absent | Journaling produces text, no mechanical loop | Proposal 6 |
| Trait differentiation | Minimal | Binary introvert/extrovert only | Proposal 5 |
| Phase 4 locations | Not built | No activities for marketplace/park/workshop/tavern | Proposal 4 |
