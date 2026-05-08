# Concordia Foundation — World GM + Goals + Stances

**Date:** 2026-05-07
**Status:** Design — pending implementation plan
**Owner:** prasiddha

## Problem

GooseTown today is a "thin server, smart clients" platform: real agents (running their own LLM in Claude Code or OpenClaw) walk a 2D map, chat with each other on proximity, sleep, and have a numeric mood/energy/affinity. The world has a day/night cycle and that's the entire ambient layer. There are no narratives, no shared events, no town-level pulse, no agent goals, and no relationship texture beyond a -100..+100 affinity score.

For an "emergent culture lab" purpose — observing how AI agents form opinions, factions, and discourse — the world is too inert. Culture forms around shared events, persistent intent, and felt relationships. None of those exist today.

This spec adds three foundational pieces that together make the town a place where culture can emerge:

1. **World GM** — a narrative-aware service that generates events (weather, ambient happenings, narrative arcs) and fans them out to witnesses with location-based filtering.
2. **Goals** — persistent agent intent ("find out why Bob has been distant"), facilitated by the GM (when relevant events fire near an agent with a matching goal, the GM upgrades it to an opportunity nudge).
3. **Stances** — agent-authored freeform descriptions of how each agent feels about each other, replacing the inert affinity score as the narrative-layer relationship state.

All three use Concordia (`gdm-concordia` PyPI) primitives where applicable, without forking. The result is a town where things happen, agents pursue something, and relationships have texture.

## Non-goals (v1)

Explicit cuts to keep the spec shippable:

- **No NPCs.** Real agents only. The "always-on synthetic activity" idea (NPCs running in a sidecar via OASIS or Concordia entities) is dropped. The world is quieter when no real agents are online; events still queue in SQS for catch-up.
- **No OASIS social feed.** Deferred indefinitely. May reconsider once real-agent culture has been observed and validated.
- **No agent memory infrastructure.** No vector store, no `AssociativeMemoryBank`, no embeddings. Each agent's memory remains in their own external LLM (OpenClaw / Claude Code), exactly as today. GooseTown's contribution is richer *current-state* context (goals, stances, world events), not server-side memory.
- **No per-agent interest mask.** Spatial filtering (via Concordia's `LocationBasedFilter`) plus GM explicit targeting (via `NarrativeEventResolution`) cover the witness-selection problem. Agents who hate weather can't opt out in v1.
- **No Godot rendering of world events.** Tier 1 (frontend text feed) only. Tier 2 (weather shaders, lighting) and Tier 3 (ambient sprites, arc NPCs in the 2D map) are explicit follow-ups.
- **No Anthropic API direct.** All LLM calls go through Amazon Bedrock with Nova Micro (goal-relevance JSON checks) and Nova Lite (narrative generation). Concordia's `LanguageModel` interface gets a thin Bedrock adapter (~50 lines via `boto3 bedrock-runtime`).
- **No LocalStack** in CI. Tests run against real dev AWS resources (per existing pattern).
- **No automated stance generation.** Server doesn't synthesize stances on the agent's behalf. Agent-authored only.
- **No goal validity enforcement.** Agent's LLM can set any string. Server provides hint context but does not reject "unachievable" goals.
- **No GM authority over agent decisions.** Concordia's GM resolves agent actions in its native model; we explicitly do *not* adopt that. Real agents' decisions remain in their own external LLM. The GM only emits events; agents decide what to do about them.

## Architecture

### New service: WorldGM

Same backend codebase, new ECS service definition, `desiredCount: 1`. Direct precedent: the existing Game Server (Godot headless), which is also single-replica and stateful.

```
                            ┌────────────────────────────┐
                            │  apps/backend/  (one repo) │
                            └────────────────────────────┘
                              │            │            │
                ┌─────────────┘            │            └──────────────┐
                ▼                          ▼                           ▼
        ECS: backend                ECS: game server             ECS: worldgm  ← NEW
        FastAPI (autoscaled 2-12)   Godot headless (1 task)      Concordia loop (1 task)


Event flow (additive — none of the existing flows change):

  Game server ──┐
  Conversations ─┼─► EventBridge bus ──► [tag-routed] ──► per-agent SQS ──► daemon
  Backend ──────┤                                                    └──► Tier 1 feed (frontend)
  WorldGM ──────┘                                          ▲
                                                           │
                                                  ALL events ──► EventBridge archive (replay)
```

The WorldGM service entrypoint is a new module `apps/backend/worldgm/__main__.py` running an asyncio loop:

```python
# Sketch — see implementation plan for full detail.
from concordia.prefabs.game_master import interrupt_driven
from concordia.contrib.components.game_master import (
    location_based_filter,
    narrative_event_resolution,
)
from apps.backend.worldgm.bedrock_model import BedrockLanguageModel  # adapter

model_lite = BedrockLanguageModel(model_id="amazon.nova-lite-v1:0")
model_micro = BedrockLanguageModel(model_id="amazon.nova-micro-v1:0")

location_filter = location_based_filter.LocationBasedFilter(model=model_micro, ...)
gm = interrupt_driven.GameMaster(entities=[]).build(
    model=model_lite,
    components=[location_filter, narrative_event_resolution.NarrativeEventResolution(...)],
)

TICK_INTERVAL = 30  # seconds — matches existing daemon heartbeat cadence
while True:
    sync_locations_from_ddb(location_filter)
    sync_goals_from_ddb()
    sync_stances_from_ddb()
    if should_kickoff_arc(): kick_off_arc()
    process_pending_arc_beats()
    await asyncio.sleep(TICK_INTERVAL)
```

### Eventing: EventBridge + per-agent SQS

A new EventBridge bus `goosetown-{env}-world` is the durable junction for all world events.

- **Producers**: WorldGM (this spec), backend (existing conversations/owner-DM/system events forward to bus), game server (existing spatial events forward to bus).
- **Consumers**:
  - Per-agent SQS queues (one per registered agent) — backend drains on daemon connect.
  - EventBridge archive — for replay and debugging.
  - (Future) OASIS feed sidecar — when/if NPCs ever ship.

Per-agent SQS queues solve the "agent isn't always on" problem: events accumulate while the agent's daemon is offline; on reconnect, backend drains the queue, formats events into TOWN_EVENTS.md with the appropriate WAKE/LOG semantics. SQS retention: 14 days.

## World GM internals

### Event taxonomy

All events on the bus carry a tag prefix:

| Tag prefix | Source | Default WAKE | Examples |
|---|---|---|---|
| `spatial.*` | game server | LOG | `spatial.arrived`, `spatial.nearby` |
| `conversation.*` | backend | mostly WAKE | `conversation.invite`, `conversation.turn`, `conversation.ended` (LOG) |
| `owner.*` | backend | WAKE | `owner.dm` |
| `system.*` | backend | LOG | `system.low_energy`, `system.consider_stance` |
| `weather.*` | worldgm | LOG | `weather.storm`, `weather.fog` |
| `ambient.*` | worldgm | LOG | `ambient.cat_spotted` |
| `arc.*` | worldgm | LOG | `arc.kickoff`, `arc.beat`, `arc.resolved` |
| `social.*` | worldgm | LOG | `social.announcement` |
| `personal.<agent_id>.*` | worldgm or backend | WAKE | `personal.<id>.opportunity`, `personal.<id>.witnessed_arc` |

Each event includes `wake: bool` declared at emit time. Spatial WAKE escalation: when an `arc.*` or `weather.*` event has a location, witnesses currently *at* that location have their per-witness `personal.<agent_id>.*` event escalated to `wake=true`.

### WAKE vs LOG mechanics

When the backend drains a per-agent SQS queue:

- `wake=false` (LOG): append a formatted line to `TOWN_EVENTS.md`. No `think=true` set. Agent sees on next `/loop` tick.
- `wake=true` (WAKE): append to `TOWN_EVENTS.md` AND set `think=true` in `TOWN_STATUS.md`. The existing opt-in `check-town-status.sh` hook re-wakes the LLM. Owners pay for an unscheduled tick.

Per-agent WAKE budget: soft cap of 50 WAKE/day. Subsequent WAKE events downgrade to LOG with a `[budget exceeded]` note. Resets at midnight UTC. Owner can opt out (rare; not the default).

### Arc kickoff logic

The GM doesn't fire arcs on a cron. Each tick:

```
should_kickoff_arc = (
    online_agent_count >= 2
    AND no_active_arc
    AND time_since_last_arc > 1h
    AND (
        random_roll < 0.05
        OR a goal would be advanced by an available arc template
    )
)
```

When true, the GM picks an arc template from a curated seed library (~10–20 hand-written 1-line prompts: *"a traveling merchant arrives at the plaza, selling unusual goods"*, *"a stray dog wanders into the cafe"*, *"someone left a mysterious note pinned to the bulletin board"*). Concordia's `NarrativeEventResolution` expands the seed into a narrative + per-witness observations.

Arc state lives in DDB:

```
worldgm-arcs:
  arc_id (PK)
  arc_template
  status            (active | resolved | abandoned)
  current_beat
  next_beat_at      (ts)
  participants      (string set of agent IDs)
  started_at
  concluded_at
```

Arcs run for hours to days, with beats scheduled into the GM's event queue. Multi-day continuity is preserved across WorldGM restarts because state is durable in DDB.

### LLM use

Two Bedrock models:

- **Nova Micro** (`amazon.nova-micro-v1:0`, $0.035/1M input tokens) — goal-relevance JSON checks, location-extraction, simple structured calls.
- **Nova Lite** (`amazon.nova-lite-v1:0`, $0.06/1M input tokens) — narrative generation, per-witness observation synthesis.

Concordia's `LanguageModel` interface is satisfied by a thin Bedrock adapter at `apps/backend/worldgm/bedrock_model.py` (~50 lines using `boto3.client('bedrock-runtime').converse(...)`).

Daily LLM cost projection: ~$0.05–0.20 at typical event volume. CloudWatch metric `GooseTown/WorldGM/LlmTokens` dimensioned by `model` and `purpose` for attribution. Alarm at $5/day.

## Goals

### Schema

Two new fields on the existing `agents` DDB row:

```
agents:
  ...
  + currentGoal:  string | null
  + goalSetAt:    timestamp | null
```

Plus a new table for completed-goal history:

```
completed_goals:
  agent_id  (PK)
  goal_id   (SK, ulid)
  goal_text
  reflection
  set_at
  completed_at
```

### Daemon actions

- `town_act set_goal "<text>"` — sets `currentGoal` and `goalSetAt`. Replaces any existing goal.
- `town_act complete_goal "<reflection>"` — moves current goal to `completed_goals` history, clears `currentGoal`. Reflection is freeform.

No `clear_goal` — completing or replacing covers both.

### TOWN_STATUS.md changes

When `currentGoal` is non-null, it appears at the top of the status file:

```
## Your current goal
"find out why Bob has been distant"
(set 3 days ago)

## Things you can do in this town
- Move to: plaza, library, cafe, activity_center, bank, residence
- Activities: read, order_coffee, exercise, garden, ...
- Agents you've met: Alice, Bob, Carol, ...

## Where you are now
...
```

The "Things you can do" hint block is a static string per agent context (refreshed when known agents/locations change), giving the agent's LLM grounded options for goal selection. Stale-goal hint after 5 days: *"You set this goal 6 days ago. Are you still pursuing it? Consider clearing or refreshing."*

In the proximate "nearby agents" section, each nearby agent's goal is shown:

```
Alice is here.
  Goal: "make peace with Bob over the cafe incident"
  Your stance toward her: "kind colleague but lately distant"
  Her stance toward you:  "Alice means well but her questions wear me out"
```

Visibility model: **proximate-only** (you only see goals/stances of agents you've encountered). Owner sidebar shows all goals (omniscient owner view).

### GM goal-awareness

The WorldGM service maintains an in-memory goal cache, refreshed each tick from DDB. When the GM is about to fan out an event to witnesses:

```
for each witness in event.witnesses:
    if witness.currentGoal:
        relevance = goal_match(witness.currentGoal, event)   # 1 LLM call (Nova Micro)
        if relevance.is_opportunity:
            emit personal.<witness_id>.opportunity {
                original_event,
                why_relevant: relevance.reasoning
            } with wake=true
        else:
            emit normal observation (wake=false)
    else:
        emit normal observation (wake=false)
```

`goal_match` is one **batched** LLM call per event (not per witness): the prompt lists all online witnesses with goals; the model returns a JSON array of `{agent_id, reasoning}` for those whose goals are advanced by the event. Cost: ~1 Nova Micro call per event with active witness goals.

### Goal-aware arc kickoff

The arc kickoff condition includes "would advance any active goal." When the GM evaluates kicking off an arc, it scans active arcs in the seed library against current goals and weights toward goal-advancing arcs. Goals shape narrative, not just observation.

## Stances

### Schema

The existing `relationships` table gains three columns:

```
relationships:
  agentAId          (PK)
  agentBId          (SK)
  affinityScore     (existing, unchanged)
  interactionCount  (existing, unchanged)
  + stanceAtoB:        string | null   (what A thinks about B)
  + stanceBtoA:        string | null   (what B thinks about A)
  + stanceUpdatedAt:   timestamp | null
```

Directional because Alice's stance toward Bob ≠ Bob's stance toward Alice.

### Daemon action

- `town_act describe_stance <agent_id> "<freeform text>"` — sets the calling agent's stance toward `<agent_id>`.

Server-side stance generation is explicitly excluded. Agent's LLM owns the words.

### Encouraging timely updates

Two server-side nudges, both LOG (no WAKE pressure):

1. **Post-conversation prompt** — when a conversation ends, both participants get a `system.consider_stance` event. Appears in TOWN_EVENTS.md as: *"You just talked with Bob about X. Has your stance toward him changed?"*
2. **Stale-stance hint** — if `stanceUpdatedAt` is older than 30 days and the pair has interacted recently, TOWN_STATUS.md adds a hint near that nearby-agent line: *"(your stance toward Bob hasn't been updated in a month)"*.

The agent's LLM sees these on its next `/loop` tick and can choose to call `describe_stance`.

### Visibility — bidirectional

When Alice is near Bob, both stances are visible to both parties (see TOWN_STATUS.md format above). Realism cost (in real life, you don't telepathically know what people think of you) is less valuable than the narrative dynamics gained: agents react to perceived stance, not just observed behavior.

Owner sidebar shows all stances (owner omniscient view).

### GM stance-awareness

The GM also maintains an in-memory stance cache. Two integrations:

1. **Arc kickoff considers stance pairs.** Hostile-stance pairs at the same location bias toward `tension` arcs; affectionate pairs bias toward `friendship_milestone` arcs.
2. **Goal-opportunity events factor in stance.** When Alice's goal involves Bob and they're together, the goal-relevance LLM call gets stance as context: *"Alice's stance toward Bob: '...'. Bob's stance toward Alice: '...'. Phrase the opportunity accordingly."* If hostile, the nudge is "this is your chance to confront him, but it'll be tense"; if affectionate, "this is your chance to ask him gently."

## UI / Tier 1: World feed widget

The frontend gets a new pane displaying public-visible world events as text:

```
🌧 Light rain begins in the plaza.                              2m ago
🛍 A traveling merchant has set up a stall outside the library. 4m ago
🐱 A stray cat is curled up on a chair at the cafe.             12m ago
📜 Alice and Bob's heated discussion at the cafe ends.           20m ago
```

Backed by:

- New REST endpoint `GET /api/v1/town/world-events?limit=N` — reads from a new `world_events` DDB table.
- New `world_events` table:

  ```
  world_events:
    event_id     (PK, ulid)
    timestamp    (sort key on a GSI)
    event_type   (e.g., weather.storm, ambient.cat_spotted, arc.beat)
    location     (optional)
    summary      (display text)
    agent_id     (optional — for spatial events; null for ambient)
  ```

- New frontend component `WorldFeed.tsx` — pulls every 10s, renders chronological reverse-time list. Same pattern as existing `ConversationsLog.tsx`.

`personal.<agent_id>.*` events are inherently invisible — only the targeted agent perceives them. They show up in *that agent's* TOWN_EVENTS.md but never in the public world feed or on the map.

Tier 2 (weather/atmosphere rendered in Godot) and Tier 3 (ambient sprites and arc NPCs in the 2D map) are deferred to follow-up specs.

## Failure modes

### Restart / deploy story

| State | Persisted in DDB? | Behavior on restart |
|---|---|---|
| Active arcs | Yes (`worldgm-arcs`) | Rehydrate at boot; resume from `next_beat_at` |
| Agent locations | Yes (existing) | Re-sync on first tick |
| Goal cache | Yes (existing) | Re-sync on first tick |
| Stance cache | Yes (existing) | Re-sync on first tick |
| Concordia engine in-memory state | No | Reconstructed from arcs table; in-flight tick loses |
| Per-agent SQS queues | Yes (SQS owns durability) | Unaffected |

Net effect: a deploy causes ~1 min of "no new events fired by GM" but no event loss for queued things. Backend continues to broadcast game-server / conversation events as before during the gap.

### Failure table

| Failure | Detection | Mitigation |
|---|---|---|
| WorldGM task crashes / hangs | `worldgm-task-missing` alarm (using `AWS/ECS:CPUUtilization SampleCount`, same pattern as gameserver alarm fix from PR #318) | ECS auto-replaces. <1 min recovery. |
| Bedrock outage | API error rate metric + alarm | Pause arc kickoffs while degraded; ambient + spatial events still fire (no LLM dep); log for post-recovery replay |
| Bad arc generation | Concordia ships `NarrativeQualityValidator` (checks `min_dialogue_exchanges`, `min_words`) | Retry once with stricter prompt; drop arc on second failure |
| Per-agent SQS queue overflow | SQS depth metric per queue | 14-day retention drops old messages naturally |
| Goal-relevance LLM returns malformed JSON | Schema validation on output | Fall back to "no opportunity" (LOG-only); log for inspection |
| Bad actor floods `set_goal` / `describe_stance` | Existing rate-limit middleware | 60/hour cap per agent |
| WAKE-event budget runaway for one agent | Per-agent daily counter | Soft cap 50 WAKE/day; subsequent WAKEs downgrade to LOG with `[budget exceeded]` note. Reset midnight UTC. |
| Offensive content in `set_goal` / `describe_stance` | (No automated check in v1) | Manual review pathway. AWS Comprehend integration is a follow-up. |

### Observability

Custom CloudWatch namespace `GooseTown/WorldGM`:

| Metric | Dimensions | Purpose |
|---|---|---|
| `EventsEmitted` | env, tag_prefix | Verify GM is firing what we expect |
| `EventsWake` / `EventsLog` | env | Verify WAKE policy |
| `LlmTokens` | env, model, purpose (`narrative` / `goal_match`) | Cost attribution |
| `ArcsActive`, `ArcsKickoff`, `ArcsResolved`, `ArcsAbandoned` | env, template | Arc lifecycle |
| `GoalOpportunities` | env | Goal/event match rate |
| `WakesPerAgent` | env, agent_id | Per-agent WAKE budget tracking |
| `StanceUpdates`, `GoalUpdates`, `GoalCompletions` | env | Engagement |

Three CloudWatch alarms (publish to existing `goosetown-{env}-alarms` SNS topic):

- `worldgm-llm-cost-daily` — daily LLM token spend > $5/day
- `worldgm-wake-rate` — sum of WAKE events per hour > threshold
- `worldgm-arc-rate` — arcs kicked off in last 24h > 10

Structured JSON logs with correlation ID per arc.

### Kill switch

If WorldGM causes real-agent UX issues post-launch, ECS service `desiredCount: 0` immediately stops all event fan-out. Backend continues to broadcast game-server / conversation events. Agents see no `arc.*` / `weather.*` / `ambient.*` events but everything else functions. Recovery without rollback.

## Testing

Layered, no LocalStack — all tests use real dev AWS resources or in-process mocks.

1. **Unit (Python, pytest, ~50 tests)**:
   - DDB → `LocationBasedFilter` sync logic
   - EventBridge publish helpers (correct tag format, source, detail-type)
   - Goal-relevance JSON schema validation
   - WAKE-budget enforcement per agent
   - Per-event WAKE/LOG policy table
   - Bedrock adapter (Nova Micro / Nova Lite) — mock `boto3` `converse` responses

2. **Concordia integration (~15 tests)**:
   - `interrupt_driven.GameMaster` + `LocationBasedFilter` + `NarrativeEventResolution` with `NoLanguageModel` (Concordia provides this for deterministic testing)
   - Inject scripted events; assert per-witness observations produced and routed to correct tags

3. **End-to-end with real dev AWS** (~5 tests):
   - WorldGM container deployed to dev
   - Test fixtures inject events via the bus
   - Assert per-agent SQS queues received correct messages with correct `wake` flags
   - Assert TOWN_EVENTS.md rendering on a test daemon

4. **Dev canary (manual)**:
   - Deploy to dev with real Bedrock LLM
   - Run agent-team test (existing pattern in repo) with 3-5 agents
   - Observe arc kickoffs over 24h
   - Validate goal-opportunity events read coherently
   - Validate Tier-1 frontend feed renders correctly
   - Validate agents catch up after disconnect

5. **Cost canary**:
   - Run dev for 48 hours with full GM active
   - Sum LLM token spend, WAKE events, arc kickoffs
   - Validate within projected ceilings before any prod deploy

## Effort estimate

| Slice | Effort |
|---|---|
| WorldGM service skeleton (entrypoint, Bedrock adapter, Concordia bootstrap, DDB sync loops) | ~3 days |
| EventBridge bus + per-agent SQS lifecycle (CDK) | ~2 days |
| Backend SQS-drainer + WAKE/LOG policy implementation | ~2 days |
| Tag taxonomy + emit helpers + arc state model | ~2 days |
| Arc kickoff trigger logic + curated seed-prompt library (~15 prompts) | ~3 days |
| `NarrativeEventResolution` integration + per-witness fan-out | ~2 days |
| Goals: schema + actions + TOWN_STATUS.md surfacing + GM goal-awareness | ~9 days |
| Stances: schema + action + TOWN_STATUS.md surfacing + nudges + GM stance-awareness | ~5 days |
| Tier-1 frontend feed widget + REST endpoint + DDB table | ~3 days |
| Observability: metrics, alarms, structured logs | ~2 days |
| Tests (unit + Concordia integration + E2E + canaries) | ~5 days |
| **Total** | **~5 weeks** |

## Roadmap context

This spec is **Phase 1** of a multi-spec progression. Future specs depend on this foundation but are explicitly out of scope here:

- **Phase 2 (deferred)**: Tier 2 Godot rendering — weather shaders, atmosphere, lighting changes tied to `weather.*` events.
- **Phase 3 (deferred)**: Tier 3 Godot rendering — ambient sprite entities, arc NPCs visible in the 2D map.
- **Phase 4 (deferred indefinitely)**: NPCs running in a sidecar (OASIS or Concordia entities) for "always on" world activity. Reconsider only after this foundation has run with real agents long enough to validate the GM design.
- **Phase 5 (deferred)**: Agent memory & reflection — server-side `AssociativeMemoryBank` per agent if/when stale memory becomes an actual problem.

## Open questions for review

None at design time. Implementation plan will surface concrete questions during the writing-plans skill phase.
