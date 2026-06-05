# Agentic Resident Brain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the deterministic resident movement FSM with an LLM-per-tick decision brain so GooseTown residents reason about the world, converse in character, and do activities.

**Architecture:** A new pure module `agentic_brain.py` builds a `DecisionContext` from daemon state + persona, renders an in-character prompt, calls Amazon Nova Lite for ONE JSON action, parses + validates it, and translates it to a `handle_tool_command` payload. `brain.py:step_resident_brain` gains deterministic pre-gates (sleep/moving/waiting → no LLM) and dispatches the chosen command; it falls back to the existing FSM when no model is supplied. `__main__.py` wires `state.model_lite` + `handle.persona` into the tick.

**Tech Stack:** Python 3.12, asyncio, Amazon Bedrock (Nova Lite via `BedrockLanguageModel.sample_text`), pytest + pytest-asyncio. Run tests with `uv run pytest` from `apps/backend/`.

**Spec:** `docs/superpowers/specs/2026-06-05-agentic-resident-brain-design.md`

---

## Context the engineer needs

- **Run tests:** `cd apps/backend && uv run pytest tests/unit/worldgm/ -v`
- **Lint:** `cd apps/backend && uv run ruff check . && uv run ruff format --check .`
- **The verified wire schema** (from `core/ws_models.py:TownAgentAct` + `routers/town.py:1414-1490`) — the daemon's `handle_tool_command` expects `{"action":"act","payload":{...}}` where `payload` is:
  - chat: `{"action":"chat","target":<agent_id>,"message":<opening|"">}`
  - say: `{"action":"say","message":<text>}`
  - end: `{"action":"end_conversation","conv_id":<id>}`
  - move: `{"action":"move","destination":<loc>}`
  - activity: `{"action":"activity","activity_id":<name>}`
- **`BedrockLanguageModel.sample_text(prompt, *, max_tokens, temperature, ...)` is SYNCHRONOUS** (boto3). It MUST be wrapped in `asyncio.to_thread` so one resident's Bedrock latency doesn't stall the other 19 under `asyncio.gather`.
- **Activity registry reuse** (`core/services/activity_registry.py`): `canonical_location(loc)` normalizes a raw location to the registry key; `LOCATION_ACTIVITIES[key]` is a `list[Activity]`; `get_context_mismatch_message(activity, context)` returns `None` when the activity is allowed in that context. Each `Activity` has `.name`, `.location`, `.location_context`.
- **Daemon state keys** (read in `read_status`): `state["agent"]` (has `location`, `location_context`, `activity`), `state["nearby"]` (`list[dict]` with `agent_id` + `name`/`display_name`), `state["active_conversation"]` (`{conv_id, participants, turns:[{speaker,text}]}` or absent), `state["pending_messages"]` (`list[dict]`, own turns already filtered out by the daemon).
- **TDD discipline:** every task is failing-test-first. Mock the model with a tiny local `FakeModel`; never touch real Bedrock/S3/DynamoDB.

## File Structure

| File | Responsibility |
|---|---|
| `apps/backend/worldgm/residents/agentic_brain.py` (new) | All new logic: `DecisionContext`, `parse_action`, `validate_action`, `to_command`, `build_decision_context`, `build_prompt`, `decide`. Pure except `decide` (which awaits the model in a thread). No daemon mutation. |
| `apps/backend/worldgm/residents/brain.py` (modify) | `read_status` extended with `nearby`+`pending_messages`; `step_resident_brain` gains pre-gates + `*, model, persona`, calls `decide`, dispatches, falls back to FSM when `model is None`. |
| `apps/backend/worldgm/__main__.py` (modify) | `_tick_one_resident` passes `state.model_lite` + `handle.persona`. |
| `apps/backend/worldgm/residents/behavior.py` (unchanged) | The FSM — now the fallback path. |
| `apps/backend/tests/unit/worldgm/test_agentic_brain.py` (new) | Unit tests for the pure functions + `decide`. |
| `apps/backend/tests/unit/worldgm/test_brain_agentic.py` (new) | Integration tests for `step_resident_brain` with a fake daemon + `FakeModel`. |

---

### Task 1: DecisionContext + parse_action

**Files:**
- Create: `apps/backend/worldgm/residents/agentic_brain.py`
- Test: `apps/backend/tests/unit/worldgm/test_agentic_brain.py`

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/worldgm/test_agentic_brain.py
"""Unit tests for the agentic resident brain (pure functions + decide)."""
from __future__ import annotations

from worldgm.residents.agentic_brain import DecisionContext, parse_action


def _ctx(**over):
    base = dict(
        name="sherlock_holmes",
        display_name="Sherlock Holmes",
        personality="Deductive savant.",
        traits=("brilliant", "aloof"),
        location="library",
        location_context="library",
        nearby=[{"agent_id": "uuid-ada", "display_name": "Ada Lovelace"}],
        active_conversation=None,
        pending_messages=[],
        available_activities=["read", "study"],
        legal_actions=frozenset({"move", "idle", "chat", "activity"}),
    )
    base.update(over)
    return DecisionContext(**base)


def test_parse_action_clean_json():
    assert parse_action('{"action": "idle"}') == {"action": "idle"}


def test_parse_action_json_embedded_in_prose():
    raw = 'Sure, here you go: {"action": "move", "destination": "cafe"} hope that helps'
    assert parse_action(raw) == {"action": "move", "destination": "cafe"}


def test_parse_action_malformed_returns_idle():
    assert parse_action("{not valid json") == {"action": "idle"}


def test_parse_action_no_json_returns_idle():
    assert parse_action("I think I will go to the cafe.") == {"action": "idle"}


def test_parse_action_missing_action_key_returns_idle():
    assert parse_action('{"destination": "cafe"}') == {"action": "idle"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/test_agentic_brain.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'worldgm.residents.agentic_brain'`

- [ ] **Step 3: Write minimal implementation**

```python
# apps/backend/worldgm/residents/agentic_brain.py
"""Agentic resident brain: LLM-per-tick decision making.

Pure functions (context-build, prompt, parse, validate, translate) plus the
single async ``decide`` entry point. No daemon mutation — reads a context,
returns a validated action / command. See
docs/superpowers/specs/2026-06-05-agentic-resident-brain-design.md.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Town locations the LLM may move to. Apartment/bed is sleep-gate-only.
_TOWN_DESTINATIONS = frozenset({"plaza", "library", "cafe", "activity_center", "bank"})

_IDLE: dict = {"action": "idle"}


@dataclass
class DecisionContext:
    """The complete, daemon-decoupled view a single decision needs."""

    name: str
    display_name: str
    personality: str
    traits: tuple[str, ...]
    location: str
    location_context: str
    nearby: list[dict]
    active_conversation: dict | None
    pending_messages: list[dict]
    available_activities: list[str]
    legal_actions: frozenset[str]


def parse_action(raw: str) -> dict:
    """Extract the first JSON object from an LLM response.

    Robust by design: any failure (no JSON, bad JSON, missing/non-string
    ``action``) returns a safe ``{"action": "idle"}``. Never raises.
    """
    if not raw:
        return dict(_IDLE)
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        return dict(_IDLE)
    try:
        obj = json.loads(raw[start : end + 1])
    except (ValueError, TypeError):
        return dict(_IDLE)
    if not isinstance(obj, dict) or not isinstance(obj.get("action"), str):
        return dict(_IDLE)
    return obj
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/test_agentic_brain.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/worldgm/residents/agentic_brain.py apps/backend/tests/unit/worldgm/test_agentic_brain.py
git commit -m "feat(worldgm): DecisionContext + robust parse_action for agentic brain"
```

---

### Task 2: validate_action + to_command

**Files:**
- Modify: `apps/backend/worldgm/residents/agentic_brain.py`
- Test: `apps/backend/tests/unit/worldgm/test_agentic_brain.py`

- [ ] **Step 1: Write the failing test**

```python
# append to test_agentic_brain.py
from worldgm.residents.agentic_brain import to_command, validate_action


def test_validate_chat_to_nearby_ok():
    ctx = _ctx()
    action = {"action": "chat", "target": "Ada Lovelace", "message": "A word, Miss Lovelace?"}
    out = validate_action(action, ctx)
    assert out["action"] == "chat"
    assert out["_agent_id"] == "uuid-ada"  # resolved display->id
    assert out["message"] == "A word, Miss Lovelace?"


def test_validate_chat_to_absent_name_becomes_idle():
    ctx = _ctx()
    out = validate_action({"action": "chat", "target": "Nikola Tesla"}, ctx)
    assert out == {"action": "idle"}


def test_validate_say_requires_conversation():
    ctx = _ctx(active_conversation=None, legal_actions=frozenset({"move", "idle"}))
    assert validate_action({"action": "say", "message": "hi"}, ctx) == {"action": "idle"}


def test_validate_say_in_conversation_ok():
    ctx = _ctx(
        active_conversation={"conv_id": "c1", "participants": ["Ada Lovelace"], "turns": []},
        pending_messages=[{"from": "Ada Lovelace", "text": "Hello?"}],
        legal_actions=frozenset({"say", "end"}),
    )
    out = validate_action({"action": "say", "message": "Indeed."}, ctx)
    assert out == {"action": "say", "message": "Indeed."}


def test_validate_move_invalid_destination_becomes_idle():
    ctx = _ctx()
    assert validate_action({"action": "move", "destination": "moon"}, ctx) == {"action": "idle"}


def test_validate_activity_not_here_becomes_idle():
    ctx = _ctx(available_activities=["read"])
    assert validate_action({"action": "activity", "name": "exercise"}, ctx) == {"action": "idle"}


def test_validate_idle_always_ok():
    assert validate_action({"action": "idle"}, _ctx()) == {"action": "idle"}


def test_to_command_chat():
    action = {"action": "chat", "_agent_id": "uuid-ada", "message": "Hello"}
    assert to_command(action) == {
        "action": "act",
        "payload": {"action": "chat", "target": "uuid-ada", "message": "Hello"},
    }


def test_to_command_say():
    assert to_command({"action": "say", "message": "Indeed."}) == {
        "action": "act",
        "payload": {"action": "say", "message": "Indeed."},
    }


def test_to_command_end():
    assert to_command({"action": "end", "_conv_id": "c1"}) == {
        "action": "act",
        "payload": {"action": "end_conversation", "conv_id": "c1"},
    }


def test_to_command_move():
    assert to_command({"action": "move", "destination": "cafe"}) == {
        "action": "act",
        "payload": {"action": "move", "destination": "cafe"},
    }


def test_to_command_activity():
    assert to_command({"action": "activity", "name": "read"}) == {
        "action": "act",
        "payload": {"action": "activity", "activity_id": "read"},
    }


def test_to_command_idle_returns_none():
    assert to_command({"action": "idle"}) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/test_agentic_brain.py -v`
Expected: FAIL — `ImportError: cannot import name 'to_command'`

- [ ] **Step 3: Write minimal implementation**

Append to `agentic_brain.py`:

```python
def _resolve_nearby_id(target: str, nearby: list[dict]) -> str | None:
    """Map an LLM-supplied display name to a nearby agent_id (case-insensitive)."""
    if not target:
        return None
    t = target.strip().lower()
    for person in nearby:
        if str(person.get("display_name", "")).strip().lower() == t:
            return person.get("agent_id")
    return None


def validate_action(action: dict, ctx: DecisionContext) -> dict:
    """Return the action if legal in ``ctx``, else ``{"action": "idle"}``.

    Resolves chat targets (display name -> agent_id, stashed under ``_agent_id``)
    and stamps the active conv_id onto ``end`` (under ``_conv_id``) so the
    downstream ``to_command`` needs no further context.
    """
    kind = action.get("action")
    if kind not in ctx.legal_actions:
        return dict(_IDLE)

    if kind == "idle":
        return dict(_IDLE)

    if kind == "chat":
        agent_id = _resolve_nearby_id(action.get("target", ""), ctx.nearby)
        if not agent_id:
            return dict(_IDLE)
        return {"action": "chat", "_agent_id": agent_id, "message": str(action.get("message", ""))}

    if kind == "say":
        if not ctx.active_conversation:
            return dict(_IDLE)
        msg = str(action.get("message", "")).strip()
        if not msg:
            return dict(_IDLE)
        return {"action": "say", "message": msg}

    if kind == "end":
        if not ctx.active_conversation:
            return dict(_IDLE)
        return {"action": "end", "_conv_id": ctx.active_conversation.get("conv_id", "")}

    if kind == "move":
        dest = action.get("destination", "")
        if dest not in _TOWN_DESTINATIONS:
            return dict(_IDLE)
        return {"action": "move", "destination": dest}

    if kind == "activity":
        name = action.get("name", "")
        if name not in ctx.available_activities:
            return dict(_IDLE)
        return {"action": "activity", "name": name}

    return dict(_IDLE)


def to_command(action: dict) -> dict | None:
    """Translate a validated action into a ``handle_tool_command`` payload.

    Returns ``None`` for idle (no dispatch). Wire shape verified against
    ``core/ws_models.py:TownAgentAct`` + ``routers/town.py:1414-1490``.
    """
    kind = action.get("action")
    if kind == "chat":
        return {
            "action": "act",
            "payload": {"action": "chat", "target": action["_agent_id"], "message": action.get("message", "")},
        }
    if kind == "say":
        return {"action": "act", "payload": {"action": "say", "message": action["message"]}}
    if kind == "end":
        return {"action": "act", "payload": {"action": "end_conversation", "conv_id": action["_conv_id"]}}
    if kind == "move":
        return {"action": "act", "payload": {"action": "move", "destination": action["destination"]}}
    if kind == "activity":
        return {"action": "act", "payload": {"action": "activity", "activity_id": action["name"]}}
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/test_agentic_brain.py -v`
Expected: PASS (all Task 1 + Task 2 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/worldgm/residents/agentic_brain.py apps/backend/tests/unit/worldgm/test_agentic_brain.py
git commit -m "feat(worldgm): validate_action + to_command (legality gate + wire translation)"
```

---

### Task 3: build_decision_context + build_prompt

**Files:**
- Modify: `apps/backend/worldgm/residents/agentic_brain.py`
- Test: `apps/backend/tests/unit/worldgm/test_agentic_brain.py`

- [ ] **Step 1: Write the failing test**

```python
# append to test_agentic_brain.py
from worldgm.residents.agentic_brain import build_decision_context, build_prompt
from worldgm.residents.personas import Persona

_PERSONA = Persona(
    name="sherlock_holmes",
    display_name="Sherlock Holmes",
    personality="Deductive savant.",
    appearance="Deerstalker cap.",
    traits=("brilliant", "aloof"),
    hangout="library",
    sleep_hour=3,
    wake_hour=11,
)


def _status(**over):
    base = dict(
        location="cafe",
        location_context="town",
        activity="idle",
        active_conversation=None,
        nearby=[{"agent_id": "uuid-ada", "name": "Ada Lovelace"}],
        pending_messages=[],
    )
    base.update(over)
    return base


def test_context_not_in_conversation_offers_chat_move_activity_idle():
    ctx = build_decision_context(_PERSONA, _status())
    assert "chat" in ctx.legal_actions  # someone nearby
    assert "move" in ctx.legal_actions
    assert "idle" in ctx.legal_actions
    assert "say" not in ctx.legal_actions
    # cafe has order_coffee -> activity offered
    assert "activity" in ctx.legal_actions
    assert "order_coffee" in ctx.available_activities


def test_context_in_conversation_offers_only_say_end():
    status = _status(active_conversation={"conv_id": "c1", "participants": ["Ada Lovelace"], "turns": []})
    ctx = build_decision_context(_PERSONA, status)
    assert ctx.legal_actions == frozenset({"say", "end"})


def test_context_no_nearby_no_chat():
    ctx = build_decision_context(_PERSONA, _status(nearby=[]))
    assert "chat" not in ctx.legal_actions


def test_context_nearby_normalized_to_display_name():
    ctx = build_decision_context(_PERSONA, _status())
    assert ctx.nearby[0]["display_name"] == "Ada Lovelace"
    assert ctx.nearby[0]["agent_id"] == "uuid-ada"


def test_build_prompt_includes_persona_and_legal_actions_only():
    status = _status(active_conversation={"conv_id": "c1", "participants": ["Ada Lovelace"],
                                          "turns": [{"speaker": "Ada Lovelace", "text": "A puzzle for you."}]})
    ctx = build_decision_context(_PERSONA, status)
    prompt = build_prompt(ctx)
    assert "Sherlock Holmes" in prompt
    assert "Deductive savant." in prompt
    assert '"action": "say"' in prompt   # legal
    assert '"action": "move"' not in prompt  # not legal mid-conversation
    assert "A puzzle for you." in prompt  # transcript rendered
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/test_agentic_brain.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_decision_context'`

- [ ] **Step 3: Write minimal implementation**

Append to `agentic_brain.py` (add imports at top of file):

```python
# add near the top imports
from core.services.activity_registry import (
    LOCATION_ACTIVITIES,
    canonical_location,
    get_context_mismatch_message,
)
from worldgm.residents.personas import Persona


def _normalize_nearby(raw_nearby: list[dict]) -> list[dict]:
    """Normalize each nearby entry to {agent_id, display_name}."""
    out: list[dict] = []
    for person in raw_nearby or []:
        agent_id = person.get("agent_id", "")
        if not agent_id:
            continue
        display = person.get("display_name") or person.get("name") or agent_id[:8]
        out.append({"agent_id": agent_id, "display_name": display})
    return out


def _activities_here(location: str, location_context: str) -> list[str]:
    """Activity names valid at this location+context (reuses the registry's
    own context-legality check)."""
    key = canonical_location(location)
    return [
        a.name
        for a in LOCATION_ACTIVITIES.get(key, [])
        if get_context_mismatch_message(a, location_context) is None
    ]


def build_decision_context(persona: Persona, parsed_status: dict) -> DecisionContext:
    """Assemble the decision view from a persona + the extended read_status dict."""
    location = parsed_status.get("location", "unknown")
    location_context = parsed_status.get("location_context", "town")
    nearby = _normalize_nearby(parsed_status.get("nearby", []))
    active_conversation = parsed_status.get("active_conversation")
    available_activities = _activities_here(location, location_context)

    if active_conversation:
        legal_actions = frozenset({"say", "end"})
    else:
        legal = {"move", "idle"}
        if nearby:
            legal.add("chat")
        if available_activities:
            legal.add("activity")
        legal_actions = frozenset(legal)

    return DecisionContext(
        name=persona.name,
        display_name=persona.display_name,
        personality=persona.personality,
        traits=persona.traits,
        location=location,
        location_context=location_context,
        nearby=nearby,
        active_conversation=active_conversation,
        pending_messages=parsed_status.get("pending_messages", []),
        available_activities=available_activities,
        legal_actions=legal_actions,
    )


# Per-action JSON schema lines shown to the model, keyed by action name.
_ACTION_SCHEMAS: dict[str, str] = {
    "chat": '  {"action": "chat", "target": "<one nearby name>", "message": "<your opening line>"}',
    "say": '  {"action": "say", "message": "<your next line>"}',
    "end": '  {"action": "end"}',
    "move": '  {"action": "move", "destination": "<plaza|library|cafe|activity_center|bank>"}',
    "activity": '  {"action": "activity", "name": "<one activity listed above>"}',
    "idle": '  {"action": "idle"}',
}


def build_prompt(ctx: DecisionContext) -> str:
    """Render the in-character decision prompt. Only legal-action schemas are
    shown, which both shrinks the prompt and steers the model."""
    lines = [
        f"You are {ctx.display_name}, a resident of the small town of GooseTown.",
        f"Personality: {ctx.personality}",
        f"Your traits: {', '.join(ctx.traits)}",
        "",
        "Current moment:",
        f"- You are at: {ctx.location}",
    ]
    if ctx.nearby:
        lines.append("- Nearby: " + ", ".join(p["display_name"] for p in ctx.nearby))
    else:
        lines.append("- Nearby: no one is nearby")

    if ctx.active_conversation:
        participants = ", ".join(ctx.active_conversation.get("participants", []) or ["someone"])
        lines.append(f"- You are talking with {participants}. Conversation so far:")
        for turn in ctx.active_conversation.get("turns", []):
            lines.append(f"    {turn.get('speaker', '?')}: {turn.get('text', '')}")

    if ctx.available_activities:
        lines.append("- Things you can do here: " + ", ".join(ctx.available_activities))

    lines += [
        "",
        "Choose ONE action. Reply with a single JSON object and nothing else.",
        "Allowed actions right now:",
    ]
    # Stable, legal-only ordering.
    for name in ("chat", "say", "end", "move", "activity", "idle"):
        if name in ctx.legal_actions:
            lines.append(_ACTION_SCHEMAS[name])

    lines += [
        "",
        f"Stay fully in character as {ctx.display_name}. Keep any spoken line to 1-2 sentences.",
    ]
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/test_agentic_brain.py -v`
Expected: PASS (all tests through Task 3)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/worldgm/residents/agentic_brain.py apps/backend/tests/unit/worldgm/test_agentic_brain.py
git commit -m "feat(worldgm): build_decision_context + in-character build_prompt"
```

---

### Task 4: decide (async, model in a thread)

**Files:**
- Modify: `apps/backend/worldgm/residents/agentic_brain.py`
- Test: `apps/backend/tests/unit/worldgm/test_agentic_brain.py`

- [ ] **Step 1: Write the failing test**

```python
# append to test_agentic_brain.py
import pytest
from worldgm.residents.agentic_brain import decide


class _FakeModel:
    """Stand-in for BedrockLanguageModel. sample_text is sync (like boto3)."""
    def __init__(self, text="", raises=False):
        self._text = text
        self._raises = raises
        self.calls = 0

    def sample_text(self, prompt, **kw):
        self.calls += 1
        if self._raises:
            raise RuntimeError("bedrock throttled")
        return self._text


@pytest.mark.asyncio
async def test_decide_returns_validated_chat():
    ctx = build_decision_context(_PERSONA, _status())
    model = _FakeModel('{"action": "chat", "target": "Ada Lovelace", "message": "A word?"}')
    out = await decide(model, ctx)
    assert out["action"] == "chat"
    assert out["_agent_id"] == "uuid-ada"
    assert model.calls == 1


@pytest.mark.asyncio
async def test_decide_model_error_returns_idle():
    ctx = build_decision_context(_PERSONA, _status())
    out = await decide(_FakeModel(raises=True), ctx)
    assert out == {"action": "idle"}


@pytest.mark.asyncio
async def test_decide_illegal_output_returns_idle():
    ctx = build_decision_context(_PERSONA, _status())  # not in conversation
    out = await decide(_FakeModel('{"action": "say", "message": "hi"}'), ctx)
    assert out == {"action": "idle"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/test_agentic_brain.py -v`
Expected: FAIL — `ImportError: cannot import name 'decide'`

- [ ] **Step 3: Write minimal implementation**

Append to `agentic_brain.py` (add `import asyncio` at top):

```python
# add to top imports
import asyncio

# decision sampling knobs
_MAX_TOKENS = 200
_TEMPERATURE = 0.8


async def decide(model, ctx: DecisionContext) -> dict:
    """Run one LLM decision for ``ctx`` and return a validated action.

    ``model.sample_text`` is a synchronous boto3 call, so it runs in a thread
    to avoid blocking the other residents' brains under ``asyncio.gather``.
    Any model error degrades to a safe idle.
    """
    prompt = build_prompt(ctx)
    try:
        raw = await asyncio.to_thread(
            model.sample_text, prompt, max_tokens=_MAX_TOKENS, temperature=_TEMPERATURE
        )
    except Exception:
        logger.warning("decide: model call failed for %s; idling this tick", ctx.name, exc_info=True)
        return dict(_IDLE)
    return validate_action(parse_action(raw), ctx)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/test_agentic_brain.py -v`
Expected: PASS (all `agentic_brain` tests)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/worldgm/residents/agentic_brain.py apps/backend/tests/unit/worldgm/test_agentic_brain.py
git commit -m "feat(worldgm): async decide() — LLM call in a thread with idle-fallback"
```

---

### Task 5: read_status extension + step_resident_brain rewrite

**Files:**
- Modify: `apps/backend/worldgm/residents/brain.py`
- Test: `apps/backend/tests/unit/worldgm/test_brain_agentic.py`

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/worldgm/test_brain_agentic.py
"""Integration tests for step_resident_brain with the agentic path."""
from __future__ import annotations

import pytest

from worldgm.residents.behavior import ResidentRuntime
from worldgm.residents.brain import read_status, step_resident_brain
from worldgm.residents.personas import Persona

_PERSONA = Persona(
    name="ada_lovelace", display_name="Ada Lovelace", personality="Pattern-lover.",
    appearance="Green gown.", traits=("curious",), hangout="library",
    sleep_hour=23, wake_hour=7,
)


class _FakeDaemon:
    def __init__(self, state):
        self.state = state
        self.commands: list[dict] = []

    async def handle_tool_command(self, cmd):
        self.commands.append(cmd)
        return {"status": "ok"}


class _FakeModel:
    def __init__(self, text="", raises=False):
        self._text, self._raises, self.calls = text, raises, 0

    def sample_text(self, prompt, **kw):
        self.calls += 1
        if self._raises:
            raise RuntimeError("x")
        return self._text


def _runtime(location="cafe"):
    return ResidentRuntime(
        name="ada_lovelace", location=location, state="idle", idle_ticks=2,
        hangout="library", location_context="town", sleep_hour=23, wake_hour=7,
    )


def _state(**over):
    agent = dict(location="cafe", location_context="town", activity="idle")
    agent.update(over.pop("agent", {}))
    base = dict(agent=agent, nearby=[{"agent_id": "uuid-tesla", "name": "Nikola Tesla"}],
                pending_messages=[])
    base.update(over)
    return base


def test_read_status_surfaces_nearby_and_pending():
    d = _FakeDaemon(_state())
    status = read_status(d)
    assert status["nearby"] == [{"agent_id": "uuid-tesla", "name": "Nikola Tesla"}]
    assert status["pending_messages"] == []


@pytest.mark.asyncio
async def test_sleep_window_goes_to_bed_without_llm():
    d = _FakeDaemon(_state())
    model = _FakeModel('{"action":"chat","target":"Nikola Tesla"}')
    rt = _runtime()
    # 02:00 UTC is inside Ada's [23,7) window.
    await step_resident_brain(d, rt, read_status(d), model=model, persona=_PERSONA, now_utc_hour=2)
    assert model.calls == 0
    assert d.commands == [{"action": "act", "payload": {"action": "move", "destination": "bed"}}]


@pytest.mark.asyncio
async def test_mid_move_noops_without_llm():
    d = _FakeDaemon(_state())
    model = _FakeModel()
    rt = _runtime()
    rt.state = "moving"
    await step_resident_brain(d, rt, read_status(d), model=model, persona=_PERSONA, now_utc_hour=12)
    assert model.calls == 0
    assert d.commands == []


@pytest.mark.asyncio
async def test_in_conversation_no_pending_noops_without_llm():
    d = _FakeDaemon(_state(active_conversation={"conv_id": "c1", "participants": ["Nikola Tesla"], "turns": []}))
    model = _FakeModel()
    await step_resident_brain(d, _runtime(), read_status(d), model=model, persona=_PERSONA, now_utc_hour=12)
    assert model.calls == 0
    assert d.commands == []


@pytest.mark.asyncio
async def test_in_conversation_with_pending_calls_llm_and_says():
    state = _state(active_conversation={"conv_id": "c1", "participants": ["Nikola Tesla"],
                                        "turns": [{"speaker": "Nikola Tesla", "text": "A pattern?"}]},
                   pending_messages=[{"from": "Nikola Tesla", "text": "A pattern?"}])
    d = _FakeDaemon(state)
    model = _FakeModel('{"action": "say", "message": "Indeed, look closer."}')
    await step_resident_brain(d, _runtime(), read_status(d), model=model, persona=_PERSONA, now_utc_hour=12)
    assert model.calls == 1
    assert d.commands == [{"action": "act", "payload": {"action": "say", "message": "Indeed, look closer."}}]


@pytest.mark.asyncio
async def test_awake_settled_nearby_calls_llm_and_chats():
    d = _FakeDaemon(_state())
    model = _FakeModel('{"action": "chat", "target": "Nikola Tesla", "message": "Hello!"}')
    await step_resident_brain(d, _runtime(), read_status(d), model=model, persona=_PERSONA, now_utc_hour=12)
    assert model.calls == 1
    assert d.commands == [
        {"action": "act", "payload": {"action": "chat", "target": "uuid-tesla", "message": "Hello!"}}
    ]


@pytest.mark.asyncio
async def test_model_none_falls_back_to_fsm():
    # Awake, settled, not at hangout -> FSM moves toward hangout (library). No LLM.
    d = _FakeDaemon(_state())
    await step_resident_brain(d, _runtime(location="cafe"), read_status(d), model=None,
                              persona=_PERSONA, now_utc_hour=12)
    assert d.commands == [{"action": "act", "payload": {"action": "move", "destination": "library"}}]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/test_brain_agentic.py -v`
Expected: FAIL — `TypeError: step_resident_brain() got an unexpected keyword argument 'model'`

- [ ] **Step 3: Write minimal implementation**

In `brain.py`, extend `read_status` to surface the two new keys:

```python
# inside read_status(), update the returned dict:
    state = getattr(daemon, "state", None) or {}
    agent = state.get("agent") or {}
    return {
        "location": agent.get("location", "unknown"),
        "location_context": agent.get("location_context", "town"),
        "activity": agent.get("activity", "idle"),
        "active_conversation": state.get("active_conversation"),
        "nearby": state.get("nearby", []),
        "pending_messages": state.get("pending_messages", []),
    }
```

Replace `step_resident_brain` with the agentic version (keep the existing module imports of `next_action`; add the new imports + `is_in_sleep_window`/`_BED`):

```python
# top-of-file imports to add:
from worldgm.residents.agentic_brain import build_decision_context, decide, to_command
from worldgm.residents.behavior import ResidentRuntime, is_in_sleep_window, next_action, _BED


async def step_resident_brain(
    daemon,
    runtime: ResidentRuntime,
    parsed_status: dict,
    *,
    model=None,
    persona=None,
    rng=None,
    now_utc_hour: int | None = None,
) -> None:
    """One brain tick. With ``model`` set, the LLM decides the action; without
    it (or on any failure) the deterministic FSM drives movement + sleep.

    Pre-gates skip the LLM on non-decision ticks (asleep / mid-move / waiting
    for the other speaker) — see the design spec.
    """
    import random

    hour = now_utc_hour if now_utc_hour is not None else _now_utc_hour()

    # --- Reconcile runtime with reported state (same as the FSM did) ---
    location = parsed_status.get("location", "unknown")
    if location != runtime.location:
        runtime.idle_ticks = 0
    runtime.location = location
    runtime.location_context = parsed_status.get("location_context", "town")

    # --- No model: deterministic FSM fallback (movement + sleep) ---
    if model is None or persona is None:
        # Mirror the legacy stub: don't act mid-conversation.
        if parsed_status.get("active_conversation"):
            return
        runtime.state = "idle"
        runtime.idle_ticks += 1
        cmd = next_action(runtime, (rng or random.random)(), now_utc_hour=hour)
        if cmd.get("action") == "noop":
            return
        runtime.state = "moving"
        runtime.idle_ticks = 0
        await daemon.handle_tool_command(cmd)
        return

    # --- Pre-gate 1: mid-move. The move await gates re-entry; do nothing. ---
    if runtime.state == "moving":
        return

    runtime.state = "idle"
    runtime.idle_ticks += 1

    # --- Pre-gate 2: sleep window (deterministic, no LLM) ---
    if is_in_sleep_window(hour, runtime.sleep_hour, runtime.wake_hour):
        if runtime.location == _BED:
            return
        runtime.state = "moving"
        runtime.idle_ticks = 0
        await daemon.handle_tool_command(
            {"action": "act", "payload": {"action": "move", "destination": _BED}}
        )
        return

    # --- Pre-gate 3: in a conversation but it isn't my turn → wait ---
    active_conv = parsed_status.get("active_conversation")
    if active_conv and not parsed_status.get("pending_messages"):
        return

    # --- Agentic decision ---
    ctx = build_decision_context(persona, parsed_status)
    action = await decide(model, ctx)
    cmd = to_command(action)
    if cmd is None:
        return  # idle
    if cmd["payload"]["action"] == "move":
        runtime.state = "moving"
        runtime.idle_ticks = 0
    await daemon.handle_tool_command(cmd)
```

Note: remove the now-unused top-of-file `from worldgm.residents.behavior import ResidentRuntime, next_action` line if it duplicates the new combined import; keep a single import line that includes `ResidentRuntime, is_in_sleep_window, next_action, _BED`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/test_brain_agentic.py tests/unit/worldgm/test_brain.py -v`
Expected: PASS — new integration tests pass AND the existing `test_brain.py` still passes (FSM fallback preserves legacy behavior).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/worldgm/residents/brain.py apps/backend/tests/unit/worldgm/test_brain_agentic.py
git commit -m "feat(worldgm): agentic step_resident_brain with pre-gates + FSM fallback"
```

---

### Task 6: Wire model + persona into the tick loop

**Files:**
- Modify: `apps/backend/worldgm/__main__.py:229-253` (`_tick_one_resident`)
- Test: `apps/backend/tests/unit/worldgm/test_tick_concurrency.py` (existing — extend)

- [ ] **Step 1: Write the failing test**

```python
# append to apps/backend/tests/unit/worldgm/test_tick_concurrency.py
@pytest.mark.asyncio
async def test_tick_passes_model_and_persona_to_brain(monkeypatch):
    """_tick_one_resident threads state.model_lite + handle.persona into the brain."""
    import worldgm.__main__ as wm

    captured = {}

    async def _fake_brain(daemon, runtime, status, *, model=None, persona=None, **kw):
        captured["model"] = model
        captured["persona"] = persona

    monkeypatch.setattr(wm, "step_resident_brain", _fake_brain)

    class _D:
        state = {"agent": {"location": "plaza", "location_context": "town"}}
        async def handle_tool_command(self, cmd):  # pragma: no cover
            return {}

    class _Handle:
        daemon = _D()
        persona = object()

    class _State:
        model_lite = object()
        resident_runtimes = {}

    state = _State()
    handle = _Handle()
    await wm._tick_one_resident(state, "ada_lovelace", handle)
    assert captured["model"] is state.model_lite
    assert captured["persona"] is handle.persona
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/test_tick_concurrency.py::test_tick_passes_model_and_persona_to_brain -v`
Expected: FAIL — `KeyError: 'model'` (brain called without `model`/`persona`).

- [ ] **Step 3: Write minimal implementation**

In `__main__.py`, update the `step_resident_brain` call inside `_tick_one_resident` (currently line ~251):

```python
    try:
        await step_resident_brain(
            daemon,
            runtime,
            status,
            model=getattr(state, "model_lite", None),
            persona=handle.persona,
        )
    except Exception:
        logger.exception("Resident brain tick failed for %s", name)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/test_tick_concurrency.py -v`
Expected: PASS (new test + existing concurrency tests).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/worldgm/__main__.py apps/backend/tests/unit/worldgm/test_tick_concurrency.py
git commit -m "feat(worldgm): wire model_lite + persona into resident tick"
```

---

### Task 7: Full suite + lint gate

**Files:** none (verification only)

- [ ] **Step 1: Run the full worldgm + brain suite**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/ -v`
Expected: PASS (all new + existing).

- [ ] **Step 2: Run the broader backend suite for regressions**

Run: `cd apps/backend && uv run pytest tests/ -q`
Expected: PASS (no regressions outside worldgm).

- [ ] **Step 3: Lint + format**

Run: `cd apps/backend && uv run ruff check . && uv run ruff format --check .`
Expected: `All checks passed!` and `... files already formatted`. If ruff reports unused `next_action`/`random` imports in `brain.py`, remove them.

- [ ] **Step 4: Commit any lint fixes**

```bash
git add -A && git commit -m "chore(worldgm): lint/format pass for agentic brain" || echo "nothing to fix"
```

---

## Self-Review (completed by plan author)

**1. Spec coverage:** Architecture diagram → Task 5. `DecisionContext`/parse → Task 1. validate/to_command → Task 2. build_decision_context/build_prompt → Task 3. `decide` + `to_thread` concurrency → Task 4. Pre-gates + FSM fallback + "whose turn" gating → Task 5. `__main__` wiring → Task 6. Error handling → covered by Task 4 (`decide` idle-fallback) + Task 5 (FSM fallback) tests. Testing strategy → Tasks 1-6 each TDD; Task 7 full gate. No gaps.

**2. Placeholder scan:** No TBD/“handle edge cases”/bare prose steps — every code step shows complete code. Prompt skeleton uses `{...}` f-string interpolation (real code), not placeholders.

**3. Type/name consistency:** `DecisionContext` fields identical across Tasks 1/2/3. `validate_action` stashes `_agent_id`/`_conv_id`; `to_command` reads exactly those. `step_resident_brain(..., *, model, persona, rng=None, now_utc_hour=None)` consistent between Task 5 impl and Task 6 caller (caller passes `model`/`persona` only — others default). Wire payloads in Task 2 match the verified schema in the Context section. Activity field `activity_id` consistent (Task 2 to_command ↔ spec ↔ ws_models).
