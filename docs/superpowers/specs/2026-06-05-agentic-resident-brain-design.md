# Agentic Resident Brain — Design

**Status:** Approved (foundation approved interactively 2026-06-05; remaining sections decided autonomously per user delegation). Subsystem #1 of 3.

**Goal:** Replace the deterministic movement FSM that drives GooseTown residents with an LLM decision loop. Each tick, an awake resident asks Amazon Nova Lite — given its persona, location, who's nearby, and any active conversation — what to do, choosing from `move / chat / say / end / activity / idle`. This makes residents reason about the world, converse in character, and do activities, instead of silently pathing between locations.

## Scope

This is **subsystem #1** of a three-part decomposition agreed during brainstorming:

| # | Subsystem | This spec? |
|---|---|---|
| **1** | **Agentic decision brain** (LLM per tick chooses the action) | ✅ yes |
| 2 | GM storyline injection (wire the dormant `worldgm/arc_orchestrator.py`) | ❌ next spec |
| 3 | Memory & reflection (Smallville-style memory stream + daily diary reflection) | ❌ later phase |

Explicitly **out of scope here:** storyline/arc injection, persistent memory, daily reflection, goal-planning depth. The brain reacts to the *current* scene only. A clean seam is left where memory (#3) and storylines (#2) plug in.

## Decisions locked

- **Architecture:** LLM decides **every tick** (full Smallville model), not trigger-based. User accepted ~$65–95/mo Nova Lite cost.
- **Decision mechanism:** **single JSON-returning LLM call per tick** (approach A), with a robust parser and idle-fallback. Two-step `sample_choice`+`sample_text` is the documented escape hatch if parse-failure rates are high in testing.
- **Cost guards:** deterministic pre-gates skip the LLM on non-decision ticks (asleep, mid-move, waiting for the other speaker's turn). These remove pure waste without reducing aliveness.
- **Graceful degradation:** if no model is supplied (Bedrock outage / `model=None`), the brain falls back to the existing deterministic FSM (`behavior.next_action`) so residents still sleep and move sanely.

## Architecture

```
worldgm tick (every 30s)
  └─ asyncio.gather over residents                         [keep — PR #336]
       └─ step_resident_brain(daemon, runtime, status, *, model, persona, now_utc_hour=None)
            ├─ 1. Deterministic pre-gates (NO LLM):
            │     • model is None?           → FSM fallback (next_action), dispatch, return
            │     • in sleep window?         → move to bed (or noop if already at bed), return
            │     • runtime.state=="moving"? → return (the move await gates re-entry)
            │     • in conversation, no pending messages? → return (wait for the other speaker)
            ├─ 2. ctx = build_decision_context(daemon, runtime, persona, status)
            ├─ 3. action = await decide(model, ctx)         # the one LLM call (in a thread)
            ├─ 4. cmd = to_command(action)                   # validated → handle_tool_command payload
            └─ 5. if cmd: await daemon.handle_tool_command(cmd)
```

**New module — `apps/backend/worldgm/residents/agentic_brain.py`** owns all of the new logic: context-building, prompt rendering, the LLM call, parsing, validation, and command translation. It has no daemon mutation side effects — it reads daemon state and returns a command for `step_resident_brain` to dispatch.

**`behavior.py` (the FSM) is kept, demoted to the fallback path** (`next_action` + `is_in_sleep_window` + `_BED`). Nothing is deleted; the FSM becomes the safety net.

**`brain.py:step_resident_brain` is modified** to add the pre-gates, call `decide`, and dispatch. Its signature gains keyword-only `model` and `persona`.

**`__main__.py:_tick_one_resident` is modified** to pass `state.model_lite` and `handle.persona` into `step_resident_brain`.

## Components (agentic_brain.py)

### `DecisionContext` (dataclass)
The complete, daemon-decoupled view a decision needs:
```python
@dataclass
class DecisionContext:
    name: str                       # slug
    display_name: str
    personality: str
    traits: tuple[str, ...]
    location: str                   # e.g. "cafe"
    location_context: str           # "town" | "apartment" | "library"
    nearby: list[dict]              # [{"agent_id": "...", "display_name": "..."}]
    active_conversation: dict | None  # {conv_id, participants, turns:[{speaker,text}]}
    pending_messages: list[dict]    # unread incoming turns (own turns already filtered by daemon)
    available_activities: list[str] # activity_id names valid at this location+context
    legal_actions: frozenset[str]   # computed subset of {move,chat,say,end,activity,idle}
```

### `build_decision_context(runtime, persona, parsed_status) -> DecisionContext`
Reads only `parsed_status` (the extended `read_status` output) + `persona` — `read_status` is the single seam that touches `daemon.state`, keeping this function pure and trivially testable. Computes:
- `nearby` from `parsed_status["nearby"]`, normalizing each to `{agent_id, display_name}` (display falls back to `name` then a short agent_id).
- `available_activities` from `core.services.activity_registry.LOCATION_ACTIVITIES[location]`, filtered to those whose `location_context` matches the resident's current context (uses the registry's existing context field; mismatches excluded).
- `legal_actions`:
  - if `active_conversation` set → `{say, end}` (move is rejected by the daemon mid-conversation anyway; not offered)
  - else → `{move, idle}` ∪ (`{chat}` if `nearby` non-empty) ∪ (`{activity}` if `available_activities` non-empty)

### `build_prompt(ctx) -> str`
Renders an in-character decision prompt. Skeleton:
```
You are {display_name}, a resident of the small town of GooseTown.
Personality: {personality}
Your traits: {", ".join(traits)}

Current moment:
- You are at: {location}{" ("+location_context+")" if context interesting}
- Nearby: {display names joined, or "no one is nearby"}
{if active_conversation:}
- You are talking with {participants}. Conversation so far:
{render turns as "  {Speaker}: {text}"}
{if available_activities:}
- Things you can do here: {activities joined}

Choose ONE action. Reply with a single JSON object and nothing else.
Allowed actions right now:
{render only the legal-action schemas, e.g.:}
  {"action": "chat", "target": "<one nearby name>", "message": "<your opening line>"}
  {"action": "say", "message": "<your next line>"}
  {"action": "end"}
  {"action": "move", "destination": "<plaza|library|cafe|activity_center|bank>"}
  {"action": "activity", "name": "<one of the activities listed above>"}
  {"action": "idle"}

Stay fully in character as {display_name}. Keep any spoken line to 1–2 sentences.
```
The LLM names targets/activities by the human-readable name it was shown; mapping to wire IDs happens in `to_command`. Only legal-action schemas are rendered, which both shrinks the prompt and steers the model.

### `parse_action(raw: str) -> dict`
Robust extraction: find the first `{...}` span, `json.loads` it, require a string `action` key. On any failure (no JSON, bad JSON, missing/unknown action) return `{"action": "idle"}`. Never raises.

### `validate_action(action, ctx) -> dict`
Legality gate; illegal → `{"action": "idle"}`:
- `chat`: only if `chat ∈ legal_actions` **and** `action["target"]` matches a `ctx.nearby` display_name (case-insensitive). Resolves and stashes the matched `agent_id`.
- `say` / `end`: only if `active_conversation` set.
- `move`: only if `move ∈ legal_actions` and `destination ∈ {plaza, library, cafe, activity_center, bank}` (the town set; `residence`/`bed` are reached via the sleep gate, not the LLM).
- `activity`: only if `action["name"] ∈ ctx.available_activities`.
- `idle`: always legal.

### `to_command(action) -> dict | None`
Translates a validated action into the `handle_tool_command` payload (the `{"action":"act","payload":{...}}` envelope), matching the verified wire schema in `routers/town.py:1414-1490` + `core/ws_models.py:TownAgentAct`:
| Action | Payload `payload` dict |
|---|---|
| chat | `{"action":"chat","target":<agent_id>,"message":<opening or "">}` |
| say | `{"action":"say","message":<text>}` |
| end | `{"action":"end_conversation","conv_id":<active conv_id>}` |
| move | `{"action":"move","destination":<dest>}` |
| activity | `{"action":"activity","activity_id":<name>}` |
| idle | returns `None` (no dispatch) |

### `async decide(model, ctx) -> dict`
`prompt = build_prompt(ctx)`; `raw = await asyncio.to_thread(model.sample_text, prompt, max_tokens=200, temperature=0.8)`; `return validate_action(parse_action(raw), ctx)`.
**Concurrency note:** `BedrockLanguageModel.sample_text` is a *synchronous* boto3 call. It MUST be wrapped in `asyncio.to_thread` so a ~0.5–2s Bedrock round-trip for one resident doesn't block the other 19 brains running under `asyncio.gather` (PR #336). This is load-bearing, not optional.

## "Whose turn is it" (conversation gating)

The daemon already filters the agent's own turns out of `pending_messages` (PR for Vera-finding U). So:
- `active_conversation` set **and** `pending_messages` non-empty → it's my turn → LLM decides `say` or `end`.
- `active_conversation` set **and** `pending_messages` empty → I just spoke / it's quiet → **noop** (pre-gate, no LLM). The backend's 60s inactivity timer ends abandoned conversations.

This keeps two residents from talking over each other and avoids paying for "waiting" ticks.

## Error handling

- **LLM call raises** (Bedrock throttle, network): `decide` catches, logs once, returns `{"action":"idle"}`. The tick is a no-op; next tick retries. Never propagates (the gather's `_tick_one_resident` also swallows, but `decide` is defensive in depth).
- **Unparseable / illegal output:** `parse_action` / `validate_action` return idle. Harmless missed tick.
- **No model:** FSM fallback (deterministic movement + sleep) — residents still behave sanely with zero Bedrock.
- **`handle_tool_command` rejection** (e.g. chat target went offline, move-while-in-conversation): already returns a structured rejection; the brain logs and moves on. Next tick re-decides against fresh state.

## Cost

Tick = 30s; ~⅓ of the cast asleep at any time; pre-gates skip asleep/moving/waiting ticks. Effective ~13–15 awake-settled residents × 2 LLM calls/min ≈ ~$65–95/mo on Nova Lite (input $0.06/Mtok, output $0.24/Mtok; prompt ~600 in / ~40 out). Accepted by the user. The pre-gates are what keep this from doubling.

## Testing strategy

All tests mock the model with a tiny fake (`FakeModel(scripted_text)`) — **no real Bedrock, S3, or DynamoDB**. The brain's purity (context in → action out) makes this clean.

**`test_agentic_brain.py`** (new) — unit tests on the pure functions:
- `build_decision_context`: legal_actions correct in/out of conversation; nearby normalization; activities filtered by location+context.
- `parse_action`: clean JSON; JSON embedded in prose; malformed → idle; unknown action → idle; non-JSON → idle.
- `validate_action`: chat to nearby (ok) vs chat to absent name (→ idle); say/end only in conversation; move to invalid destination → idle; activity not-here → idle.
- `to_command`: each action → exact wire payload; idle → None; chat resolves display_name→agent_id.
- `decide`: FakeModel returning each action shape → expected validated action; FakeModel raising → idle; `decide` wraps the call via `to_thread` (assert it's awaitable and non-blocking).

**`test_brain_agentic.py`** (new, or extend `test_brain.py`) — `step_resident_brain` integration with a fake daemon + FakeModel:
- sleep-window pre-gate → moves to bed, model never called.
- mid-move (`runtime.state=="moving"`) → noop, model never called.
- in-conversation-no-pending → noop, model never called.
- in-conversation-with-pending → model called, `say` dispatched.
- awake+settled+nearby → model called, `chat` dispatched via `handle_tool_command`.
- `model=None` → FSM fallback path (existing `next_action` behavior preserved).

**Regression:** existing `test_brain.py` / `test_behavior.py` continue to pass — the FSM is unchanged and still exercised via the fallback path.

## Files

- **Create:** `apps/backend/worldgm/residents/agentic_brain.py`
- **Create:** `apps/backend/tests/unit/worldgm/test_agentic_brain.py`
- **Create:** `apps/backend/tests/unit/worldgm/test_brain_agentic.py`
- **Modify:** `apps/backend/worldgm/residents/brain.py` — pre-gates, `decide` call, dispatch; `read_status` extended to surface `nearby` + `pending_messages`; `step_resident_brain` signature gains `*, model, persona`.
- **Modify:** `apps/backend/worldgm/__main__.py:_tick_one_resident` — pass `state.model_lite` + `handle.persona`.
- **Unchanged:** `behavior.py` (now the fallback), `personas.py`, `manager.py`.

## Rollout

- Single PR. The change is gated behind `model` being non-None: if `state.model_lite` is wired (it already is, bootstrapped in `_bootstrap_gm`), residents go agentic; if Bedrock is unreachable the FSM fallback keeps them moving.
- No DDB schema change. No infra change. The worldgm image rebuilds and redeploys via the normal pipeline on merge.
- **Fallback if it misbehaves in dev:** set `model=None` wiring (one-line revert in `_tick_one_resident`) to drop back to pure FSM, or revert the PR. The live town is unaffected until merge — this PR is opened, not merged, per the build instruction.

## Open seams for later subsystems

- **#2 (storylines):** `build_decision_context` is where an `active_storylines` field will be added; `build_prompt` gets a "what's happening in town" block. The GM (`arc_orchestrator`) will write storyline state that the context reader picks up.
- **#3 (memory):** `DecisionContext` gains a `recent_memory` field sourced from a memory store + retrieval; `build_prompt` gets a "you remember…" block. The diary (`TOWN_LIFE.md`) becomes the persistence substrate.
