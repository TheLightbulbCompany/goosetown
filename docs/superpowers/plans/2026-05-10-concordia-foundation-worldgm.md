# Concordia Foundation — World GM Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Stand up a new ECS service `worldgm` that runs Concordia's interrupt-driven GameMaster + LocationBasedFilter + NarrativeEventResolution primitives. The service ingests game-server + backend events, generates narrative arcs and ambient events, fans them out per-witness via EventBridge to per-agent SQS queues, and the backend drains those queues into TOWN_EVENTS.md (LOG) or sets `think=true` (WAKE) per the policy. Goal-aware: when an event would advance an agent's `currentGoal`, GM emits a `personal.<agent_id>.opportunity` event with `wake=true`. Stance-aware: arc kickoff biases toward hostile or affectionate stance pairs.

**Architecture:** New ECS service definition (single-replica, same Docker image, different entrypoint `python -m apps.backend.worldgm`). Concordia from `gdm-concordia` PyPI as a library — no fork. Bedrock Nova Lite for narrative generation, Nova Micro for goal-relevance JSON checks. EventBridge bus `goosetown-{env}-world` is the durable junction. Per-agent SQS queues created on agent register, deleted on hard-leave. Backend gets a SQS-drainer task that runs when daemon connects. Curated seed-prompt library for arc templates (~10 hand-written 1-line prompts) — Concordia's `NarrativeEventResolution` expands them.

**Tech Stack:** Python 3.12 (matches existing backend), `gdm-concordia` from PyPI, `boto3` for Bedrock + EventBridge + SQS, FastAPI for the SQS-drainer trigger endpoint, CDK TypeScript for infra. Single Bedrock adapter (~50 lines) implements Concordia's `LanguageModel` interface against `bedrock-runtime.converse`.

**Spec:** `docs/superpowers/specs/2026-05-07-concordia-foundation-design.md` (Sections "World GM internals", "Failure modes", "Observability")

**Branch:** `plan/concordia-foundation-worldgm` (off `origin/main` after Plans 1+2 merge).

**Out of scope (deferred to Plan 4):** Tier-1 frontend feed widget, `world_events` REST endpoint (separate plan).

**No-ops we're explicitly NOT doing:**
- Memory / AssociativeMemoryBank (dropped in spec)
- Per-agent interest masks (dropped in spec — spatial filtering + GM explicit targeting cover it)
- Tier-2 weather rendering in Godot (deferred)
- Tier-3 ambient sprite entities in Godot (deferred)
- NPCs (dropped in spec)

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `apps/infra/lib/stacks/database-stack.ts` | Modify | Add `worldgm-arcs` and `world_events` tables |
| `apps/infra/lib/stacks/eventing-stack.ts` | Create | EventBridge bus + per-agent SQS queue lifecycle (CDK constructs) |
| `apps/infra/lib/stacks/worldgm-stack.ts` | Create | New ECS service definition (single-replica, command override) |
| `apps/infra/lib/app.ts` and/or `goosetown-stage.ts` | Modify | Wire new stacks in deployment order |
| `apps/backend/core/dynamo.py` | Modify | Add `worldgm_arcs_table()` and `world_events_table()` accessors |
| `apps/backend/worldgm/__init__.py` | Create | Module marker |
| `apps/backend/worldgm/__main__.py` | Create | Service entrypoint: build Concordia GM, run async tick loop |
| `apps/backend/worldgm/bedrock_model.py` | Create | Concordia `LanguageModel` adapter over `bedrock-runtime` (~50 lines) |
| `apps/backend/worldgm/location_sync.py` | Create | Periodic DDB → `LocationBasedFilter._entity_locations` sync |
| `apps/backend/worldgm/arc_templates.py` | Create | Curated seed-prompt library (10-15 hand-written 1-liners) |
| `apps/backend/worldgm/arc_orchestrator.py` | Create | `should_kickoff_arc()` logic, arc state read/write to `worldgm-arcs`, schedules beats into Concordia event queue |
| `apps/backend/worldgm/event_publisher.py` | Create | EventBridge `put_events` helpers + tag taxonomy enum |
| `apps/backend/worldgm/goal_relevance.py` | Create | `goal_match(events, witnesses_with_goals)` — batched LLM call (Nova Micro) |
| `apps/backend/worldgm/stance_bias.py` | Create | `arc_template_bias_from_stances(active_pairs)` — pure-function logic |
| `apps/backend/worldgm/tests/...` | Create | Unit tests for each module |
| `apps/backend/core/services/sqs_drainer.py` | Create | Backend service: drain per-agent SQS on daemon connect, format events into TOWN_EVENTS.md / set think=true |
| `apps/backend/routers/town.py` | Modify | Trigger `sqs_drainer.drain_for_agent` on daemon connect; create/delete agent SQS queue on register/leave |
| `apps/backend/data/goosetown-skill/skills/goosetown/goosetown_cli.py` | Modify | Render `personal.opportunity` events with the goal nudge text in TOWN_STATUS.md / TOWN_EVENTS.md |
| `apps/backend/pyproject.toml` | Modify | Add `gdm-concordia` dep |

---

## Phase A — Infrastructure (CDK)

### Task A1: DDB tables

**Files:** `apps/infra/lib/stacks/database-stack.ts`, `apps/backend/core/dynamo.py`

Add two new tables:

```ts
// worldgm-arcs — narrative arc state
const worldgmArcs = new dynamodb.Table(this, "WorldgmArcsTable", {
  tableName: `goosetown-${env}-worldgm-arcs`,
  partitionKey: { name: "arcId", type: dynamodb.AttributeType.STRING },
  billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
  pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: env === "prod" },
  removalPolicy,
});
worldgmArcs.addGlobalSecondaryIndex({
  indexName: "status-nextBeatAt-index",
  partitionKey: { name: "status", type: dynamodb.AttributeType.STRING },
  sortKey: { name: "nextBeatAt", type: dynamodb.AttributeType.STRING },
});

// world_events — durable event log for Tier-1 feed (Plan 4 will read this)
const worldEvents = new dynamodb.Table(this, "WorldEventsTable", {
  tableName: `goosetown-${env}-world-events`,
  partitionKey: { name: "eventId", type: dynamodb.AttributeType.STRING },
  billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
  pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: env === "prod" },
  removalPolicy,
});
worldEvents.addGlobalSecondaryIndex({
  indexName: "byTimestamp",
  partitionKey: { name: "envBucket", type: dynamodb.AttributeType.STRING },  // env constant for partition spread
  sortKey: { name: "timestamp", type: dynamodb.AttributeType.STRING },
});
```

Add accessors in `dynamo.py`:

```python
def worldgm_arcs_table():
    return get_client().Table(table_name("worldgm-arcs"))

def world_events_table():
    return get_client().Table(table_name("world-events"))
```

Both go into the `DatabaseTables` interface + `this.tables` assignment.

- [ ] **Step A1.1:** Add tables to CDK
- [ ] **Step A1.2:** Add accessors to dynamo.py
- [ ] **Step A1.3:** Run synth (with .venv stash dance) + jest
- [ ] **Step A1.4:** Commit `feat(infra): add worldgm_arcs + world_events DDB tables`

### Task A2: EventBridge bus + SQS lifecycle

**Files:** Create `apps/infra/lib/stacks/eventing-stack.ts`. Wire in `app.ts` + `goosetown-stage.ts`.

```ts
// Create the bus + archive
const bus = new events.EventBus(this, "WorldBus", {
  eventBusName: `goosetown-${env}-world`,
});
new events.Archive(this, "WorldBusArchive", {
  sourceEventBus: bus,
  archiveName: `goosetown-${env}-world-archive`,
  eventPattern: { source: events.Match.prefix("goosetown.") },
  retention: cdk.Duration.days(14),
});

// SQS DLQ for the per-agent queues
const dlq = new sqs.Queue(this, "WorldDLQ", {
  queueName: `goosetown-${env}-world-dlq`,
  retentionPeriod: cdk.Duration.days(14),
});
```

Per-agent SQS queues are NOT defined in CDK — they're created/deleted dynamically via SDK on agent register/leave. CDK creates only the DLQ and bus.

Backend gets two new IAM permissions: `events:PutEvents` (publishing) and `sqs:CreateQueue`/`DeleteQueue`/`ReceiveMessage`/`DeleteMessage`/`GetQueueUrl` (per-agent queue lifecycle).

- [ ] **Step A2.1:** Create eventing-stack.ts
- [ ] **Step A2.2:** Wire in app.ts/goosetown-stage.ts
- [ ] **Step A2.3:** Add IAM permissions to backend + worldgm task roles
- [ ] **Step A2.4:** Synth + jest, commit

### Task A3: WorldGM ECS service

**Files:** Create `apps/infra/lib/stacks/worldgm-stack.ts`. Wire in stages.

```ts
const worldgmService = new ecs.FargateService(this, "WorldGmService", {
  serviceName: `goosetown-${env}-worldgm`,
  cluster: props.cluster,
  desiredCount: 1,  // singleton — Concordia is stateful
  taskDefinition: worldgmTaskDef,  // same image as backend, command override
  // No ALB — doesn't take HTTP traffic
});
```

The task def reuses the existing backend Docker image but overrides `command: ["python", "-m", "apps.backend.worldgm"]`. Same secrets, same VPC, DDB + Bedrock + EventBridge + SQS access.

Bedrock IAM: `bedrock:InvokeModel` on Nova Lite + Nova Micro model ARNs.

- [ ] **Step A3.1:** Create worldgm-stack.ts
- [ ] **Step A3.2:** Wire in stage with proper deps (Cluster, Eventing, Database, Auth)
- [ ] **Step A3.3:** Synth + jest, commit

---

## Phase B — Bedrock adapter + service skeleton

### Task B1: Bedrock LanguageModel adapter

**Files:** `apps/backend/worldgm/bedrock_model.py`, test

Concordia expects a `LanguageModel` subclass with a `sample_text(prompt, ...)` method. Implement a thin adapter:

```python
"""Bedrock language-model adapter for Concordia.

Concordia's LanguageModel is the abstraction GM components call to generate
narrative text. We back it with Amazon Bedrock's `converse` API. Two model
choices baked in:
  - Nova Lite (narrative generation, ~$0.06/Mtok input)
  - Nova Micro (structured/JSON, ~$0.035/Mtok input)
"""

from __future__ import annotations

import boto3
from concordia.language_model import language_model


_DEFAULT_LITE = "amazon.nova-lite-v1:0"
_DEFAULT_MICRO = "amazon.nova-micro-v1:0"


class BedrockLanguageModel(language_model.LanguageModel):
    def __init__(self, model_id: str = _DEFAULT_LITE, region: str = "us-east-1"):
        self._client = boto3.client("bedrock-runtime", region_name=region)
        self._model_id = model_id

    def sample_text(
        self,
        prompt: str,
        *,
        max_tokens: int = 1024,
        terminators: list[str] | None = None,
        temperature: float | None = 0.7,
        timeout: float | None = None,
        seed: int | None = None,
    ) -> str:
        resp = self._client.converse(
            modelId=self._model_id,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={
                "maxTokens": max_tokens,
                "temperature": temperature or 0.7,
                **({"stopSequences": terminators} if terminators else {}),
            },
        )
        return resp["output"]["message"]["content"][0]["text"]

    def sample_choice(self, prompt: str, responses: list[str], **kwargs) -> tuple[int, str, dict]:
        # Concordia sometimes asks for a multiple-choice answer. Cheap impl:
        # ask the LLM and string-match. Real implementations use logprobs;
        # for our use case structured-output is fine.
        text = self.sample_text(prompt, max_tokens=64)
        for i, r in enumerate(responses):
            if r.lower() in text.lower():
                return i, r, {}
        return 0, responses[0], {}
```

Test: mock `boto3.client('bedrock-runtime')`, verify `sample_text` formats the prompt correctly + returns the response text.

- [ ] **Step B1.1:** Add `gdm-concordia` to `pyproject.toml`, `uv lock`, commit
- [ ] **Step B1.2:** Write the adapter + failing test
- [ ] **Step B1.3:** Run test, commit

### Task B2: WorldGM module skeleton + entrypoint

**Files:** `apps/backend/worldgm/__init__.py`, `apps/backend/worldgm/__main__.py`

Minimal entrypoint:

```python
"""WorldGM service entrypoint.

Run: python -m apps.backend.worldgm

Builds a Concordia interrupt-driven GameMaster, runs an async tick loop:
  1. Sync agent locations + goals + stances from DDB
  2. Decide whether to kickoff a new arc
  3. Process pending arc beats
  4. Sleep until next tick

This is a long-lived single-replica service. Restart loses in-memory event
queue but durable state (arcs, locations, goals, stances) survives in DDB.
"""

import asyncio
import logging
import os
import signal
import sys

from .bedrock_model import BedrockLanguageModel
from .location_sync import sync_locations
from .arc_orchestrator import maybe_kickoff_arc, process_arc_beats
from .event_publisher import emit_world_event

TICK_INTERVAL_SECONDS = float(os.environ.get("WORLDGM_TICK_INTERVAL", "30"))

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

_should_exit = False


def _signal_handler(signum, frame):
    global _should_exit
    logger.info("Received signal %d, shutting down...", signum)
    _should_exit = True


signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)


async def main():
    logger.info("Starting WorldGM service (tick=%.1fs)", TICK_INTERVAL_SECONDS)
    model_lite = BedrockLanguageModel(model_id="amazon.nova-lite-v1:0")
    model_micro = BedrockLanguageModel(model_id="amazon.nova-micro-v1:0")

    # Build Concordia GM, location filter, narrative resolution components.
    # Detailed wiring in Task B3.
    gm_state = await _bootstrap_gm(model_lite, model_micro)

    while not _should_exit:
        try:
            await sync_locations(gm_state)
            await maybe_kickoff_arc(gm_state)
            await process_arc_beats(gm_state)
        except Exception:
            logger.exception("Tick failed; continuing")
        await asyncio.sleep(TICK_INTERVAL_SECONDS)

    logger.info("WorldGM service exiting cleanly")


if __name__ == "__main__":
    asyncio.run(main())
```

Skeleton imports the modules from later tasks. Initial implementation: `_bootstrap_gm` returns a stub; `sync_locations` is a no-op; `maybe_kickoff_arc` is a no-op. Subsequent tasks fill them in.

- [ ] **Step B2.1:** Add `__init__.py` (empty)
- [ ] **Step B2.2:** Add `__main__.py` with skeleton
- [ ] **Step B2.3:** Verify `python -m apps.backend.worldgm --help`-style works (or that the module parses)
- [ ] **Step B2.4:** Commit `feat(worldgm): service entrypoint skeleton`

---

## Phase C — Concordia integration

### Task C1: Location sync

**Files:** `apps/backend/worldgm/location_sync.py`, test

Periodic DDB scan of active agents, populate `LocationBasedFilter._entity_locations`. The filter is Concordia's, but its location dict is a plain dict — we update it directly.

```python
async def sync_locations(gm_state: GmState) -> None:
    """Pull active agents' currentLocation from DDB and update the filter."""
    agents = gm_state.town_service.get_active_agents()
    new_locations = {a["id"]: a.get("currentLocation", "unknown") for a in agents}
    location_filter = gm_state.components["location_filter"]
    location_filter._entity_locations = new_locations
    location_filter._location_to_entities = _invert(new_locations)
    if new_locations != gm_state.last_known_locations:
        logger.info("Locations updated: %d agents tracked", len(new_locations))
        gm_state.last_known_locations = new_locations
```

(Adapter idiom from Concordia's contrib `LocationBasedFilter` — its internal dict is mutable.)

Test: feed mock DDB rows, verify the filter dict is updated.

- [ ] **Step C1.1:** Implement + tests
- [ ] **Step C1.2:** Commit

### Task C2: Concordia GM bootstrap

**Files:** `apps/backend/worldgm/__main__.py` (replace stub with real wiring)

Wire `interrupt_driven.GameMaster` + `LocationBasedFilter` + `NarrativeEventResolution`:

```python
async def _bootstrap_gm(model_lite, model_micro):
    from concordia.prefabs.game_master import interrupt_driven
    from concordia.contrib.components.game_master import (
        location_based_filter,
        narrative_event_resolution,
    )

    location_filter = location_based_filter.LocationBasedFilter(
        model=model_micro,
        entity_names=[],  # populated by location_sync; LocationBasedFilter
                          # tolerates entries it didn't know at construction
    )
    narrative_resolver = narrative_event_resolution.NarrativeEventResolution(
        model=model_lite,
    )
    gm = interrupt_driven.GameMaster(entities=[]).build(
        model=model_lite,
        components=[location_filter, narrative_resolver],
    )
    return GmState(
        gm=gm,
        components={"location_filter": location_filter, "narrative_resolver": narrative_resolver},
        model_lite=model_lite,
        model_micro=model_micro,
        town_service=TownService(),
        last_known_locations={},
    )
```

(Adapt to actual Concordia API surface. Concordia v0.2.5 entry points may differ slightly.)

Test: bootstrap with mock models, verify the GM object is constructed.

- [ ] **Step C2.1:** Replace skeleton with real wiring
- [ ] **Step C2.2:** Test with mocks
- [ ] **Step C2.3:** Commit

### Task C3: Arc templates + orchestrator

**Files:** `apps/backend/worldgm/arc_templates.py`, `apps/backend/worldgm/arc_orchestrator.py`, tests

Curated seed-prompt library — ~10-15 entries:

```python
ARC_TEMPLATES = [
    {
        "id": "traveling_merchant",
        "kickoff_prompt": "A traveling merchant has set up a colorful stall outside the library, selling unusual trinkets and exotic foods.",
        "duration_hours": 24,
        "preferred_location": "library",
    },
    {
        "id": "mysterious_note",
        "kickoff_prompt": "Someone left a mysterious handwritten note pinned to the bulletin board. The handwriting looks deliberate but the message is cryptic.",
        "duration_hours": 48,
        "preferred_location": "plaza",
    },
    # ... ~10 more
]
```

Orchestrator:

```python
async def maybe_kickoff_arc(gm_state) -> None:
    """Check arc-kickoff conditions; if met, pick a template and run NarrativeEventResolution."""
    state = await _read_state(gm_state)
    if state.active_arcs:
        return  # only one active arc at a time in v1
    if state.online_agent_count < 2:
        return
    if state.minutes_since_last_arc < 60:
        return

    if random.random() < 0.05 or state.has_goal_advanceable_template():
        template = _pick_template(state)
        await _kickoff_arc(gm_state, template)


async def process_arc_beats(gm_state) -> None:
    """Process any arc beats whose nextBeatAt has elapsed."""
    pending = gm_state.town_service.scan_arcs_due_now()
    for arc in pending:
        await _fire_beat(gm_state, arc)
```

`_kickoff_arc` calls Concordia's `NarrativeEventResolution` to expand the seed prompt into a narrative + per-witness observations. Per-witness events get published via `event_publisher`.

Goal-aware kickoff: when scanning candidate templates, increase the score of templates whose `kickoff_prompt` mentions concepts in the agent's `currentGoal`. Stance-aware: hostile-stance pairs at the same location bias toward `tension`-tagged templates; affectionate pairs bias toward `friendship`-tagged.

- [ ] **Step C3.1:** Define `arc_templates.py` with ~10 entries
- [ ] **Step C3.2:** Write `arc_orchestrator.py` with `maybe_kickoff_arc` + `process_arc_beats` + helpers
- [ ] **Step C3.3:** Tests with mocked Concordia + DDB
- [ ] **Step C3.4:** Commit each substep separately

### Task C4: Event publisher (EventBridge)

**Files:** `apps/backend/worldgm/event_publisher.py`, test

Tag taxonomy:

```python
class EventTag(str, Enum):
    # weather
    WEATHER_STORM = "weather.storm"
    WEATHER_FOG = "weather.fog"
    # ambient
    AMBIENT_CAT = "ambient.cat_spotted"
    AMBIENT_DELIVERY = "ambient.delivery_truck"
    # arc
    ARC_KICKOFF = "arc.kickoff"
    ARC_BEAT = "arc.beat"
    ARC_RESOLVED = "arc.resolved"
    # social
    SOCIAL_ANNOUNCEMENT = "social.announcement"

WAKE_BY_DEFAULT = {
    "personal.": True,   # personal.<agent_id>.* always WAKE
    "conversation.": True,
    "owner.": True,
    # rest LOG
}
```

Helper to publish:

```python
def emit_world_event(
    *,
    bus_name: str,
    tag: str,
    detail: dict,
    witnesses: list[str],
    wake: bool | None = None,
):
    """Publish an event to the world bus.

    Witnesses get per-witness `personal.<witness_id>.observed` events derived
    from the base event. The base event is also published once (un-witnessed)
    for archive + Plan-4 frontend feed.
    """
    eb = boto3.client("events")
    if wake is None:
        wake = _infer_wake_default(tag)
    # Publish base event for archive
    eb.put_events(Entries=[{
        "EventBusName": bus_name,
        "Source": "goosetown.worldgm",
        "DetailType": tag,
        "Detail": json.dumps({**detail, "witnesses": witnesses, "wake": wake}),
    }])
    # Per-witness events for fan-out to per-agent SQS
    for w in witnesses:
        eb.put_events(Entries=[{
            "EventBusName": bus_name,
            "Source": "goosetown.worldgm",
            "DetailType": f"personal.{w}.observed",
            "Detail": json.dumps({**detail, "witness": w, "base_tag": tag, "wake": wake}),
        }])
    # Also persist the base event to world_events table for Plan 4
    _persist_to_world_events_table(tag, detail, witnesses)
```

The per-witness event lets the per-agent SQS subscription routing pattern be just `DetailType: personal.<this_agent_id>.*`.

- [ ] **Step C4.1:** Implement + tests with mocked boto3
- [ ] **Step C4.2:** Commit

### Task C5: Goal relevance check (Nova Micro batched)

**Files:** `apps/backend/worldgm/goal_relevance.py`, test

```python
def goal_match(model_micro, event_text: str, witnesses_with_goals: list[tuple[str, str]]) -> dict[str, str]:
    """One LLM call per event; returns {agent_id: reason} for witnesses whose goal is advanced."""
    if not witnesses_with_goals:
        return {}
    prompt = _build_goal_match_prompt(event_text, witnesses_with_goals)
    response = model_micro.sample_text(prompt, max_tokens=512)
    return _parse_json_array(response)
```

Tests: with mocked model, verify prompt structure + JSON parsing.

When `arc_orchestrator._fire_beat` runs, it invokes `goal_match` to determine which witnesses get upgraded to `personal.<id>.opportunity wake=true`.

- [ ] **Step C5.1:** Implement + tests
- [ ] **Step C5.2:** Commit

### Task C6: Stance bias (pure function)

**Files:** `apps/backend/worldgm/stance_bias.py`, test

```python
def arc_template_bias(active_pairs_with_stances: list[tuple[str, str, str | None, str | None]],
                      templates: list[dict]) -> list[tuple[float, dict]]:
    """Score each template by stance-pair bias.
    Hostile pair at same location → +1 to 'tension' templates.
    Affectionate pair → +1 to 'friendship' templates.
    """
    scores = []
    for tpl in templates:
        score = _base_score(tpl)
        for a, b, stance_ab, stance_ba in active_pairs_with_stances:
            if _is_hostile(stance_ab, stance_ba) and "tension" in tpl.get("tags", []):
                score += 1.0
            if _is_affectionate(stance_ab, stance_ba) and "friendship" in tpl.get("tags", []):
                score += 1.0
        scores.append((score, tpl))
    return scores
```

`_is_hostile` / `_is_affectionate` use simple keyword match initially (e.g., "annoying", "hate", "wary" → hostile; "warm", "kind", "love" → affectionate). LLM-based stance classification is a future improvement.

- [ ] **Step C6.1:** Implement + tests
- [ ] **Step C6.2:** Commit

---

## Phase D — Backend SQS drainer + integration

### Task D1: SQS drainer service

**Files:** `apps/backend/core/services/sqs_drainer.py`, test

```python
async def drain_for_agent(agent_id: str) -> int:
    """Drain the agent's SQS queue, write events into TOWN_EVENTS.md.

    Returns number of events drained. WAKE events also set think=true on the
    agent's TOWN_STATUS.md (forces hook re-wake).

    Idempotent: messages have deduplication ids.
    """
    queue_url = _agent_queue_url(agent_id)
    sqs = boto3.client("sqs")
    drained = 0
    while True:
        msgs = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=0)
        if not msgs.get("Messages"):
            break
        for m in msgs["Messages"]:
            event = json.loads(m["Body"])
            await _apply_event_to_agent(agent_id, event)
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=m["ReceiptHandle"])
            drained += 1
    return drained


async def _apply_event_to_agent(agent_id: str, event: dict):
    """Append to TOWN_EVENTS.md; set think=true if wake."""
    daemon_mailbox = get_daemon_mailbox()
    mailbox_event = _format_for_daemon(event)
    daemon_mailbox.notify_connection(_get_connection_id(agent_id), mailbox_event)
    if event.get("wake"):
        # set think=true via existing daemon-bridge mechanism
        _set_think_true(agent_id)
```

Trigger paths:
1. On daemon connect (from `_handle_agent_connect`): drain immediately
2. Periodic poll (every 30s) for connected agents

For the "agent gets WAKE event" path, leverage the existing `think=true` mechanism that the wake-on-status hook reads.

- [ ] **Step D1.1:** Implement drainer + tests
- [ ] **Step D1.2:** Wire drain trigger into `_handle_agent_connect`
- [ ] **Step D1.3:** Commit

### Task D2: Per-agent queue lifecycle

**Files:** `apps/backend/routers/town.py`, `apps/backend/core/services/sqs_drainer.py`

Create the agent's SQS queue on `/town/agent/register` (or `opt_in`):

```python
# In opt_in, after agent created:
sqs_drainer.create_agent_queue(agent_id, bus_arn)
```

Delete on hard-leave (deactivate or delete):

```python
sqs_drainer.delete_agent_queue(agent_id)
```

EventBridge rule per agent (or one rule with `personal.*` pattern routing dynamically) — see CDK for the routing approach. Cheapest: one rule per agent, but creates lots of rules. Alternative: one rule pattern `personal.*` with a Lambda target that does fan-out by parsing the agent_id from the DetailType.

Decision for v1: one rule per agent, created/deleted with the queue. Rule lifecycle handled by `sqs_drainer.create_agent_queue`. Limit: ~300 EventBridge rules per region by default — fine for our scale.

- [ ] **Step D2.1:** Implement create_agent_queue / delete_agent_queue
- [ ] **Step D2.2:** Wire into opt_in + deactivate/delete paths
- [ ] **Step D2.3:** Tests + commit

---

## Phase E — Observability + final integration

### Task E1: CloudWatch metrics + alarms

**Files:** `apps/backend/worldgm/event_publisher.py` (emit metrics), `apps/infra/lib/stacks/observability-stack.ts` (alarms)

Metrics to publish from worldgm service:
- `EventsEmitted` (dimensioned by tag_prefix)
- `EventsWake` / `EventsLog`
- `LlmTokens` (dimensioned by model + purpose)
- `ArcsActive`, `ArcsKickoff`, `ArcsResolved`, `ArcsAbandoned`
- `GoalOpportunities`
- `WakesPerAgent` (dimensioned by agent_id)

Alarms (publish to existing `goosetown-{env}-alarms` SNS topic):
- `worldgm-task-missing` (using AWS/ECS:CPUUtilization SampleCount, same as gameserver fix from PR #318)
- `worldgm-llm-cost-daily` (>$5/day)
- `worldgm-wake-rate` (per-agent WAKE/hour > threshold)
- `worldgm-arc-rate` (>10 arcs/24h)

- [ ] **Step E1.1:** Add `worldgm-task-missing` alarm to ObservabilityStack
- [ ] **Step E1.2:** Add the three rate-limit alarms
- [ ] **Step E1.3:** Emit metrics from event_publisher
- [ ] **Step E1.4:** Commit per step

### Task E2: Daemon TOWN_EVENTS.md rendering of personal.opportunity events

**Files:** `apps/backend/data/goosetown-skill/skills/goosetown/goosetown_cli.py`

Existing TOWN_EVENTS.md handles raw events. Extend to render `personal.*.opportunity` events with the goal-nudge text:

```python
def _render_event_for_log(event: dict) -> str:
    base_tag = event.get("base_tag", "")
    if base_tag.startswith("personal.") and "opportunity" in base_tag:
        return f"💡 _Opportunity_: {event.get('why_relevant', event.get('summary', ''))}"
    # ... fall through to existing rendering
```

- [ ] **Step E2.1:** Implement + test
- [ ] **Step E2.2:** Commit

### Task E3: Final integration + push

- [ ] **Step E3.1:** Run full backend test suite. Should still be 595+ passing (Plan 2 baseline) plus the new worldgm tests.
- [ ] **Step E3.2:** CDK synth (with .venv stash). Verify no `deploy.yml` drift, all stacks synthesize.
- [ ] **Step E3.3:** Run jest infra tests. Pre-existing `compute-stack.test.ts:322` failure is the only allowed failure.
- [ ] **Step E3.4:** Push branch + open PR with detailed summary.
- [ ] **Step E3.5:** Watch CI. Merge per user direction (don't wait for codex).
- [ ] **Step E3.6:** Watch deploy. New `worldgm` ECS service should land. Verify `worldgm-task-missing` alarm transitions to OK.

---

## Self-review checklist

- [ ] No memory / AssociativeMemoryBank — explicitly out of scope
- [ ] Per-agent SQS queues created/deleted via SDK, not CDK
- [ ] Bedrock IAM scoped to specific Nova model ARNs
- [ ] WAKE policy declared at emit time per event; per-agent budget cap (50/day soft limit) enforced in drainer
- [ ] Concordia LanguageModel adapter is ~50 lines, not a fork
- [ ] Arc orchestrator handles `process_arc_beats` durability — restart resumes from `nextBeatAt`
- [ ] Stance-aware logic is pure function (testable without LLM)
- [ ] Goal-relevance is a single batched LLM call per event (not per witness)
- [ ] CDK synth passes without deploy.yml drift
- [ ] Test suite passes (the inevitable pre-existing `compute-stack.test.ts:322` failure stays out of scope)

## Effort estimate

- Phase A (infra): ~2 days
- Phase B (Bedrock + skeleton): ~1 day
- Phase C (Concordia integration): ~3-4 days
- Phase D (SQS drainer + queue lifecycle): ~2 days
- Phase E (observability + final): ~1 day

Total: ~9-10 days for a single human; subagent-driven parallelism may reduce significantly.
