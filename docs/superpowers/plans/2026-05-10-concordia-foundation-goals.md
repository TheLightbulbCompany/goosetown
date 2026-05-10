# Concordia Foundation — Goals Subsystem Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a persistent `currentGoal` per agent. Agents set goals via daemon (`town_act set_goal "..."`), complete them (`town_act complete_goal "<reflection>"`), and the goal surfaces at the top of TOWN_STATUS.md so it stays salient. Other agents see each other's goals when proximate. Frontend agent panel shows the goal under the agent name.

**Architecture:** New columns on the existing `agents` DDB table (`currentGoal: string | null`, `goalSetAt: timestamp | null`). New `completed_goals` history table. New WS-message actions `set_goal` / `complete_goal` validated through the existing `ws_models.py` discriminated union. Service-layer logic in a new `goals_service.py`. Daemon CLI gets two new subcommands. TOWN_STATUS.md rendering surfaces own goal at the top + nearby-agent goals proximate-only.

**Tech Stack:** FastAPI + pytest + uv + boto3 + DynamoDB on backend; CDK TypeScript for infra; Python `goosetown_cli.py` daemon; React 18 + TypeScript + vitest on frontend.

**Spec:** `docs/superpowers/specs/2026-05-07-concordia-foundation-design.md` (Section 3: Goals)

**Branch:** `plan/concordia-foundation-goals` (off `origin/main`).

**Out of scope (deferred to Plan 3):** GM goal-awareness — per-event goal-relevance check, opportunity events, goal-aware arc kickoff. Plan 1 ships only the goal *field + action surface*; the GM consumes it later.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `apps/infra/lib/stacks/database-stack.ts` | Modify | Add `CompletedGoalsTable` (PK `agentId`, SK `goalId` ULID, GSI on `completedAt`). PITR for prod. |
| `apps/backend/core/dynamo.py` | Modify | Add `completed_goals_table()` accessor. |
| `apps/backend/core/ws_models.py` | Modify | Add `set_goal` and `complete_goal` to the `TownAgentAct` action validator. |
| `apps/backend/core/services/goals_service.py` | Create | Business logic: `set_goal(agent_id, text)`, `complete_goal(agent_id, reflection)`, `get_goal(agent_id)`. |
| `apps/backend/tests/unit/services/test_goals_service.py` | Create | Unit tests for the service. |
| `apps/backend/routers/town.py` | Modify | Dispatch `action=set_goal` / `complete_goal` from the existing `town_agent_act` handler to `goals_service`. |
| `apps/backend/tests/unit/test_goals_actions.py` | Create | Endpoint/dispatch tests. |
| `apps/backend/data/goosetown-skill/skills/goosetown/goosetown_cli.py` | Modify | New `set_goal` / `complete_goal` subcommands. Render `currentGoal` at top of TOWN_STATUS.md + stale-goal hint + nearby-agent goals (proximate only). |
| `apps/backend/data/goosetown-skill/skills/goosetown/SKILL.md` | Modify | Document new actions. |
| `apps/frontend/src/components/ApartmentCard.tsx` | Modify | Display `currentGoal` under agent name. |
| `apps/frontend/src/components/ApartmentCard.test.tsx` | Modify | Add test for goal rendering. |

Net source LOC: ~+450 / -10. Net test LOC: ~+250.

---

## Task 1: DDB schema — `completed_goals` table

**Files:**
- Modify: `apps/infra/lib/stacks/database-stack.ts`
- Modify: `apps/backend/core/dynamo.py`

**Why two columns on `agents` instead of a separate `current_goals` table:** the goal is mutated via simple atomic `UpdateItem` on the existing row. No new lookup needed; no GSI needed. History (completed goals) gets its own table so it can grow without bloating the agents row.

- [ ] **Step 1.1:** Add `CompletedGoalsTable` in `database-stack.ts`. Find the `OwnerChatsTable` definition (added by PR #248) and add this just after it:

```ts
const completedGoals = new dynamodb.Table(this, "CompletedGoalsTable", {
  tableName: `goosetown-${env}-completed-goals`,
  partitionKey: { name: "agentId", type: dynamodb.AttributeType.STRING },
  sortKey: { name: "goalId", type: dynamodb.AttributeType.STRING },
  billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
  pointInTimeRecovery: isProd,
  removalPolicy: isProd ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
});
completedGoals.addGlobalSecondaryIndex({
  indexName: "agentId-completedAt-index",
  partitionKey: { name: "agentId", type: dynamodb.AttributeType.STRING },
  sortKey: { name: "completedAt", type: dynamodb.AttributeType.STRING },
});
```

- [ ] **Step 1.2:** Add accessor in `apps/backend/core/dynamo.py`. Match the existing `owner_chats_table()` pattern.

```python
def completed_goals_table():
    """DynamoDB table handle for completed_goals."""
    return _resource().Table(f"{TABLE_PREFIX}-completed-goals")
```

- [ ] **Step 1.3:** Run `cdk synth` to verify the change. From the repo root after stashing the `.venv`:

```bash
mv apps/backend/.venv /tmp/_venv-stash || true
cd apps/infra && npx cdk synth --quiet 2>&1 | tail -5
cd ../.. && mv /tmp/_venv-stash apps/backend/.venv
```

Expected: synth succeeds with `Successfully synthesized to .../cdk.out`. Any error: fix before continuing.

- [ ] **Step 1.4:** Commit.

```bash
git add apps/infra/lib/stacks/database-stack.ts apps/backend/core/dynamo.py
git commit -m "feat(infra): add completed_goals DDB table for goal history"
```

---

## Task 2: WS model validation for new actions

**Files:**
- Modify: `apps/backend/core/ws_models.py`
- Test: `apps/backend/tests/unit/test_ws_models.py`

The existing `TownAgentAct` discriminated union has per-action `model_validator` rules ensuring required fields are present. Extend it for `set_goal` (requires `text`) and `complete_goal` (requires `reflection`).

- [ ] **Step 2.1:** Read the existing `TownAgentAct` definition in `ws_models.py` (~lines 45-75) to see the current validation pattern.

- [ ] **Step 2.2:** Write the failing test first.

In `apps/backend/tests/unit/test_ws_models.py` add:

```python
def test_set_goal_requires_text():
    body = {
        "type": "town_agent_act",
        "connectionId": "abc",
        "agent_id": "alice-123",
        "action": "set_goal",
    }
    with pytest.raises(ValidationError):
        TypeAdapter(WsMessage).validate_python(body)


def test_set_goal_with_text_validates():
    body = {
        "type": "town_agent_act",
        "connectionId": "abc",
        "agent_id": "alice-123",
        "action": "set_goal",
        "text": "find out why Bob has been distant",
    }
    msg = TypeAdapter(WsMessage).validate_python(body)
    assert msg.action == "set_goal"
    assert msg.text == "find out why Bob has been distant"


def test_complete_goal_requires_reflection():
    body = {
        "type": "town_agent_act",
        "connectionId": "abc",
        "agent_id": "alice-123",
        "action": "complete_goal",
    }
    with pytest.raises(ValidationError):
        TypeAdapter(WsMessage).validate_python(body)
```

- [ ] **Step 2.3:** Run tests; they should fail.

```bash
cd apps/backend && uv run pytest tests/unit/test_ws_models.py -v -k "goal"
```

Expected: FAIL with the new tests because the validator doesn't know about `set_goal` / `complete_goal` yet.

- [ ] **Step 2.4:** Update `TownAgentAct` validator in `ws_models.py`. Locate the `_validate_action_args` `model_validator` and add (in the same style as existing `if self.action == "X" and not self.Y` lines):

```python
        if self.action == "set_goal" and not self.text:
            raise ValueError("'set_goal' requires 'text'")
        if self.action == "complete_goal" and not self.reflection:
            raise ValueError("'complete_goal' requires 'reflection'")
```

Plus add the new fields to `TownAgentAct` class definition (alongside `text`, `message`, `destination`, etc.):

```python
    reflection: str | None = Field(default=None, max_length=2000)
```

`text` field already exists for `reply_owner` per PR #248; reuse it. Add a length cap if not present: `max_length=500` for goal text.

- [ ] **Step 2.5:** Run tests; they should pass.

```bash
cd apps/backend && uv run pytest tests/unit/test_ws_models.py -v -k "goal"
```

Expected: PASS for the 3 new tests, no regressions on existing tests.

- [ ] **Step 2.6:** Commit.

```bash
git add apps/backend/core/ws_models.py apps/backend/tests/unit/test_ws_models.py
git commit -m "feat(backend): validate set_goal / complete_goal WS actions"
```

---

## Task 3: `goals_service.py` — business logic + tests

**Files:**
- Create: `apps/backend/core/services/goals_service.py`
- Create: `apps/backend/tests/unit/services/test_goals_service.py`

Service module owns the DDB calls. Backend handler delegates to it.

- [ ] **Step 3.1:** Write failing tests first. In `apps/backend/tests/unit/services/test_goals_service.py`:

```python
"""Unit tests for goals_service."""

from unittest.mock import patch
import time
import pytest
from core.services import goals_service


def _now_ms() -> int:
    return int(time.time() * 1000)


@patch("core.services.goals_service.TownService")
def test_set_goal_writes_currentGoal_and_goalSetAt(mock_town):
    mock_town.return_value.update_agent_state.return_value = None
    goals_service.set_goal(agent_id="alice-123", text="find out why Bob has been distant")
    args = mock_town.return_value.update_agent_state.call_args.kwargs
    assert args["current_goal"] == "find out why Bob has been distant"
    assert "goal_set_at" in args
    assert args["goal_set_at"] >= _now_ms() - 5000


@patch("core.services.goals_service.dynamo.completed_goals_table")
@patch("core.services.goals_service.TownService")
def test_complete_goal_archives_then_clears_currentGoal(mock_town, mock_table):
    mock_town.return_value.get_town_agent_by_id.return_value = {
        "id": "alice-123",
        "currentGoal": "find out why Bob has been distant",
        "goalSetAt": _now_ms() - 60_000,
    }
    mock_table.return_value.put_item.return_value = None
    mock_town.return_value.update_agent_state.return_value = None

    goals_service.complete_goal(agent_id="alice-123", reflection="we talked, all good")

    # archive write
    put_args = mock_table.return_value.put_item.call_args.kwargs["Item"]
    assert put_args["agentId"] == "alice-123"
    assert put_args["goalText"] == "find out why Bob has been distant"
    assert put_args["reflection"] == "we talked, all good"
    assert "completedAt" in put_args

    # clear current
    update_args = mock_town.return_value.update_agent_state.call_args.kwargs
    assert update_args["current_goal"] is None
    assert update_args["goal_set_at"] is None


@patch("core.services.goals_service.TownService")
def test_complete_goal_noop_when_no_current_goal(mock_town):
    mock_town.return_value.get_town_agent_by_id.return_value = {
        "id": "alice-123",
        "currentGoal": None,
    }
    result = goals_service.complete_goal(agent_id="alice-123", reflection="...")
    assert result["status"] == "no_active_goal"
    mock_town.return_value.update_agent_state.assert_not_called()
```

- [ ] **Step 3.2:** Run tests; they should fail (module doesn't exist).

```bash
cd apps/backend && uv run pytest tests/unit/services/test_goals_service.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'core.services.goals_service'`.

- [ ] **Step 3.3:** Implement `goals_service.py`:

```python
"""Goals subsystem — business logic.

Agents set/clear/complete persistent goals. The currentGoal field is a single
string that surfaces at the top of TOWN_STATUS.md so the agent's LLM keeps it
salient. Completed goals move to a history table; current_goal is cleared.

GM goal-awareness (per-event opportunity events, goal-aware arc kickoff) is
deferred to Plan 3 (World GM).
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from core import dynamo
from core.services.town_service import TownService

logger = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


def set_goal(agent_id: str, text: str) -> dict[str, Any]:
    """Set or replace the agent's currentGoal.

    Replaces any existing goal without archiving — only ``complete_goal``
    archives. This matches the spec: agents either complete a goal (worth
    remembering) or change their mind (not worth remembering).
    """
    text = text.strip()
    if not text:
        raise ValueError("goal text must not be empty")
    if len(text) > 500:
        raise ValueError("goal text must be <= 500 chars")

    service = TownService()
    service.update_agent_state(
        agent_id,
        current_goal=text,
        goal_set_at=_now_ms(),
    )
    logger.info("[agent:%s] currentGoal set: %s", agent_id[:8], text[:60])
    return {"status": "ok", "currentGoal": text}


def complete_goal(agent_id: str, reflection: str) -> dict[str, Any]:
    """Archive the current goal to history and clear it.

    No-op (returns ``no_active_goal``) if the agent has no currentGoal —
    that's a no-op rather than an error so daemons don't have to pre-check.
    """
    service = TownService()
    agent = service.get_town_agent_by_id(agent_id)
    if not agent:
        raise ValueError(f"agent not found: {agent_id}")

    goal_text = agent.get("currentGoal")
    if not goal_text:
        return {"status": "no_active_goal"}

    set_at = agent.get("goalSetAt")
    completed_at = _now_ms()
    goal_id = str(uuid.uuid4())

    table = dynamo.completed_goals_table()
    table.put_item(
        Item={
            "agentId": agent_id,        # camelCase key, snake_case Python var
            "goalId": goal_id,
            "goalText": goal_text,
            "reflection": reflection.strip()[:2000],
            "setAt": set_at,
            "completedAt": completed_at,
        }
    )

    service.update_agent_state(
        agent_id,
        current_goal=None,
        goal_set_at=None,
    )
    logger.info(
        "[agent:%s] currentGoal completed: %s (reflection: %s)",
        agent_id[:8], goal_text[:40], reflection[:40],
    )
    return {
        "status": "ok",
        "completed_goal_id": goal_id,
        "goal_text": goal_text,
    }


def get_goal(agent_id: str) -> str | None:
    """Read the agent's currentGoal. Returns None if unset."""
    service = TownService()
    agent = service.get_town_agent_by_id(agent_id)
    return agent.get("currentGoal") if agent else None
```

- [ ] **Step 3.4:** Update `TownService.update_agent_state` field map. In `apps/backend/core/services/town_service.py`, find `update_agent_state` (the kwarg-passthrough helper); add to its `field_map`:

```python
            "current_goal": "currentGoal",
            "goal_set_at": "goalSetAt",
```

If `update_agent_state` accepts `None` to clear a field, no further work. Otherwise add a special-case to write `None` as a real DDB null (not skip).

- [ ] **Step 3.5:** Run tests; they should pass.

```bash
cd apps/backend && uv run pytest tests/unit/services/test_goals_service.py -v
```

Expected: PASS for all 3.

- [ ] **Step 3.6:** Commit.

```bash
git add apps/backend/core/services/goals_service.py apps/backend/core/services/town_service.py apps/backend/tests/unit/services/test_goals_service.py
git commit -m "feat(backend): goals_service — set_goal, complete_goal, get_goal"
```

---

## Task 4: Backend dispatcher — route `set_goal` / `complete_goal` actions

**Files:**
- Modify: `apps/backend/routers/town.py`
- Create: `apps/backend/tests/unit/test_goals_actions.py`

Existing `town_agent_act` WS handler dispatches by `action`. Add cases for the new actions.

- [ ] **Step 4.1:** Find the action dispatcher. In `apps/backend/routers/town.py`, search for `if action == "move"` or `_handle_act` or similar. The existing `reply_owner` handler is a good template — match its pattern.

- [ ] **Step 4.2:** Write failing tests in `apps/backend/tests/unit/test_goals_actions.py`:

```python
"""Tests for set_goal / complete_goal action dispatch in town_agent_act handler."""

from unittest.mock import patch, MagicMock
import pytest
from fastapi.testclient import TestClient

# (Use the same TestClient + auth fixtures as test_owner_chat_endpoints.py.)


@patch("routers.town.goals_service")
def test_set_goal_dispatches_to_service(mock_goals, authed_ws_request):
    mock_goals.set_goal.return_value = {"status": "ok", "currentGoal": "test goal"}
    body = {
        "type": "town_agent_act",
        "connectionId": "conn-abc",
        "agent_id": "alice-123",
        "action": "set_goal",
        "text": "test goal",
    }
    resp = authed_ws_request(body)
    assert resp.status_code == 200
    mock_goals.set_goal.assert_called_once_with(
        agent_id="alice-123", text="test goal"
    )


@patch("routers.town.goals_service")
def test_complete_goal_dispatches_to_service(mock_goals, authed_ws_request):
    mock_goals.complete_goal.return_value = {
        "status": "ok",
        "completed_goal_id": "g1",
        "goal_text": "test goal",
    }
    body = {
        "type": "town_agent_act",
        "connectionId": "conn-abc",
        "agent_id": "alice-123",
        "action": "complete_goal",
        "reflection": "did it",
    }
    resp = authed_ws_request(body)
    assert resp.status_code == 200
    mock_goals.complete_goal.assert_called_once_with(
        agent_id="alice-123", reflection="did it"
    )
```

- [ ] **Step 4.3:** Run; should fail.

```bash
cd apps/backend && uv run pytest tests/unit/test_goals_actions.py -v
```

- [ ] **Step 4.4:** Wire dispatch in `routers/town.py`. Add import at top:

```python
from core.services import goals_service
```

In the `town_agent_act` handler, find the `reply_owner` branch and add the two new cases nearby:

```python
    elif action == "set_goal":
        text = body.get("text", "")
        result = goals_service.set_goal(agent_id=agent_id, text=text)
        return {"statusCode": 200, "body": result}

    elif action == "complete_goal":
        reflection = body.get("reflection", "")
        result = goals_service.complete_goal(agent_id=agent_id, reflection=reflection)
        return {"statusCode": 200, "body": result}
```

- [ ] **Step 4.5:** Run tests; should pass.

- [ ] **Step 4.6:** Commit.

```bash
git add apps/backend/routers/town.py apps/backend/tests/unit/test_goals_actions.py
git commit -m "feat(backend): dispatch set_goal/complete_goal in town_agent_act"
```

---

## Task 5: Daemon CLI — `set_goal` and `complete_goal` subcommands

**Files:**
- Modify: `apps/backend/data/goosetown-skill/skills/goosetown/goosetown_cli.py`

The CLI's `cmd_act` function (around line 705) dispatches by action name. Add the two new actions.

- [ ] **Step 5.1:** Find the `cmd_act` function (around `def cmd_act(args: argparse.Namespace) -> None:`).

Read its existing pattern for `reply_owner` (around line 792) — that's the closest analog (single-text-arg action).

- [ ] **Step 5.2:** Add the new action branches. Below the `elif action == "reply_owner":` block:

```python
    elif action == "set_goal":
        if not action_args:
            _die(
                "Usage: act set_goal <text>",
                "Example: act set_goal \"find out why Bob has been distant\"",
            )
        text = " ".join(action_args).strip()
        if not text:
            _die("Goal text must not be empty.")
        if len(text) > 500:
            _die("Goal text must be <= 500 chars.")
        payload = {
            "type": "town_agent_act",
            "agent_id": agent_id,
            "action": "set_goal",
            "text": text,
        }
        result = _send_to_daemon({"cmd": "ws_send", "payload": payload})
        if result.get("status") == "ok":
            print(f"Goal set: {text}")
        else:
            print(json.dumps(result))

    elif action == "complete_goal":
        if not action_args:
            _die(
                "Usage: act complete_goal <reflection>",
                "Example: act complete_goal \"we talked, it's resolved\"",
            )
        reflection = " ".join(action_args).strip()
        if not reflection:
            _die("Reflection must not be empty.")
        if len(reflection) > 2000:
            _die("Reflection must be <= 2000 chars.")
        payload = {
            "type": "town_agent_act",
            "agent_id": agent_id,
            "action": "complete_goal",
            "reflection": reflection,
        }
        result = _send_to_daemon({"cmd": "ws_send", "payload": payload})
        if result.get("status") == "ok":
            print(f"Goal completed: {reflection}")
        else:
            print(json.dumps(result))
```

- [ ] **Step 5.3:** Verify the daemon's `ws_send` handler accepts these payloads (it should — it's a generic forwarder).

- [ ] **Step 5.4:** Commit.

```bash
git add apps/backend/data/goosetown-skill/skills/goosetown/goosetown_cli.py
git commit -m "feat(skill): add set_goal / complete_goal CLI actions"
```

---

## Task 6: TOWN_STATUS.md rendering — surface currentGoal + nearby agents' goals

**Files:**
- Modify: `apps/backend/data/goosetown-skill/skills/goosetown/goosetown_cli.py`

The daemon writes TOWN_STATUS.md on every world_update. It already includes "your" agent's state and nearby agents. Extend it to surface goals.

- [ ] **Step 6.1:** Find the `_write_status` or `_render_status` function in `goosetown_cli.py` (search for `TOWN_STATUS.md` and `def _write`). Read it to understand the section structure.

- [ ] **Step 6.2:** Add a "Your current goal" block at the top of the rendered status, immediately after the header. New helper:

```python
def _render_goal_block(agent_state: dict, now_ms: int) -> str:
    """Render the agent's currentGoal + staleness hint at the top of TOWN_STATUS."""
    goal = agent_state.get("currentGoal") or ""
    if not goal:
        return ""
    set_at = agent_state.get("goalSetAt") or 0
    age_days = max(0, (now_ms - set_at) // 86_400_000) if set_at else 0
    stale_hint = ""
    if age_days >= 5:
        stale_hint = (
            f"\n_(this goal has been on your mind for {age_days} days. "
            "Are you still pursuing it? `act complete_goal \"<reflection>\"` "
            "to mark it done, or `act set_goal \"<new text>\"` to replace.)_"
        )
    return (
        f"\n## Your current goal\n"
        f"_{goal}_{stale_hint}\n"
        f"\nUse `act complete_goal \"<reflection>\"` when you've achieved it.\n"
    )
```

- [ ] **Step 6.3:** Add nearby-agent goal rendering. In the section that lists nearby agents, augment each line:

```python
def _render_nearby_agent_line(near: dict) -> str:
    name = near.get("display_name") or near.get("name") or near.get("agent_id", "?")
    agent_id = near.get("agent_id", "")
    goal = (near.get("currentGoal") or "").strip()
    line = f"- **{name}** (`{agent_id}`)"
    if goal:
        line += f"\n  - Goal: _{goal}_"
    return line
```

- [ ] **Step 6.4:** Wire both into `_write_status`. The goal block goes near the top after the agent identity; the nearby-line uses the new helper for each nearby agent.

The backend needs to include `currentGoal` in the world_update payload's nearby array. Verify this in `daemon_bridge.py` — the existing serializer for nearby agents may already pick up `currentGoal` if it pulls all DDB fields. If not, add it explicitly.

- [ ] **Step 6.5:** Commit.

```bash
git add apps/backend/data/goosetown-skill/skills/goosetown/goosetown_cli.py
git commit -m "feat(skill): render currentGoal + nearby goals in TOWN_STATUS.md"
```

---

## Task 7: Backend serializer — include `currentGoal` in nearby-agent payload

**Files:**
- Modify: `apps/backend/core/services/daemon_bridge.py` (or wherever `nearby` is serialized)

For nearby-agent goals to render, the world_update payload must include them.

- [ ] **Step 7.1:** Find the nearby-agent serialization. Likely in `daemon_bridge.py` or `state_translator.py`. Look for a function that builds the dict containing `display_name` / `agent_id` / `currentLocation` for each nearby agent.

- [ ] **Step 7.2:** Add `currentGoal` to the dict. Pull from the `nearby_agent_record` (already fetched from DDB):

```python
        "currentGoal": rec.get("currentGoal") or None,
```

- [ ] **Step 7.3:** Add a unit test that verifies the serialized payload includes `currentGoal`.

- [ ] **Step 7.4:** Commit.

```bash
git add apps/backend/core/services/daemon_bridge.py apps/backend/tests/unit/services/test_daemon_bridge.py
git commit -m "feat(backend): include currentGoal in nearby-agent payload"
```

---

## Task 8: SKILL.md documentation

**Files:**
- Modify: `apps/backend/data/goosetown-skill/skills/goosetown/SKILL.md`

- [ ] **Step 8.1:** Add the two actions to the existing actions list:

```markdown
- `set_goal "<text>"` — set or replace your persistent goal. Surfaces at the top of TOWN_STATUS.md so it stays salient. Other agents see it when they're proximate to you. Max 500 chars. Replace by calling again; archive by calling `complete_goal`.
- `complete_goal "<reflection>"` — mark your current goal complete. Archives goal + reflection to history; clears `currentGoal`. Max 2000 chars on the reflection.
```

- [ ] **Step 8.2:** Commit.

```bash
git add apps/backend/data/goosetown-skill/skills/goosetown/SKILL.md
git commit -m "docs(skill): document set_goal / complete_goal actions"
```

---

## Task 9: Frontend — display agent's `currentGoal` in `ApartmentCard`

**Files:**
- Modify: `apps/frontend/src/components/ApartmentCard.tsx`
- Modify: `apps/frontend/src/components/ApartmentCard.test.tsx`

- [ ] **Step 9.1:** Read `ApartmentCard.tsx` to find where agent metadata is rendered.

- [ ] **Step 9.2:** Add `currentGoal` to the `Agent` interface (likely in `useApartment.ts` or the shared types):

```ts
interface Agent {
  // ...existing fields...
  currentGoal?: string | null;
}
```

- [ ] **Step 9.3:** Render the goal under the agent's name:

```tsx
{agent.currentGoal && (
  <div className="text-xs text-gray-500 italic mt-1 truncate">
    Goal: {agent.currentGoal}
  </div>
)}
```

- [ ] **Step 9.4:** Add a test:

```tsx
it("renders currentGoal when present", () => {
  render(<ApartmentCard agent={{...mockAgent, currentGoal: "make a friend"}} />);
  expect(screen.getByText(/make a friend/)).toBeInTheDocument();
});

it("does not render goal section when goal is null", () => {
  render(<ApartmentCard agent={{...mockAgent, currentGoal: null}} />);
  expect(screen.queryByText(/Goal:/)).not.toBeInTheDocument();
});
```

- [ ] **Step 9.5:** Run tests:

```bash
cd apps/frontend && pnpm test ApartmentCard
```

(If `pnpm test` hangs, run `pnpm vitest run ApartmentCard` instead.)

- [ ] **Step 9.6:** Update `useApartment.ts` to ensure `currentGoal` is passed through from the `/town/apartment` REST response (verify backend sends it; add to response shape if not).

- [ ] **Step 9.7:** Commit.

```bash
git add apps/frontend/src/components/ApartmentCard.tsx apps/frontend/src/components/ApartmentCard.test.tsx apps/frontend/src/hooks/useApartment.ts
git commit -m "feat(frontend): show agent's currentGoal in ApartmentCard"
```

---

## Task 10: Backend `/apartment` endpoint — include `currentGoal`

**Files:**
- Modify: `apps/backend/routers/town.py` (the `/apartment` handler)

The frontend needs `currentGoal` from the apartment endpoint.

- [ ] **Step 10.1:** Find the `/apartment` handler. Look for `/town/apartment` or `apartment_endpoint`. The handler builds a per-agent response dict.

- [ ] **Step 10.2:** Add `currentGoal` to the response per agent:

```python
        "currentGoal": agent.get("currentGoal"),
```

- [ ] **Step 10.3:** Add or update an endpoint test to assert `currentGoal` is present.

- [ ] **Step 10.4:** Commit.

```bash
git add apps/backend/routers/town.py apps/backend/tests/integration/test_apartment_endpoint.py
git commit -m "feat(backend): /apartment includes currentGoal per agent"
```

---

## Task 11: Final integration test + push

- [ ] **Step 11.1:** Run the full backend test suite:

```bash
cd apps/backend && uv run pytest tests/ -v 2>&1 | tail -30
```

Expected: all tests pass (no regressions). Note any pre-existing failures separately.

- [ ] **Step 11.2:** Run frontend tests:

```bash
cd apps/frontend && pnpm test --run 2>&1 | tail -20
```

- [ ] **Step 11.3:** Run `cdk synth` one more time after the venv stash dance (per session memory):

```bash
mv apps/backend/.venv /tmp/_venv-stash || true
cd apps/infra && npx cdk synth --quiet 2>&1 | tail -5 && npm test -- --ci 2>&1 | tail -8
cd ../.. && mv /tmp/_venv-stash apps/backend/.venv
```

- [ ] **Step 11.4:** Push branch + open PR.

```bash
git push -u origin plan/concordia-foundation-goals
gh pr create --title "feat: concordia foundation — goals subsystem (Plan 1)" --body "..."
```

- [ ] **Step 11.5:** Watch CI. Per project memory, CI failures are blockers (8-job suite). Pre-existing `compute-stack.test.ts:322` failure stays out of scope.

- [ ] **Step 11.6:** Merge when CI green. Watch the deploy pipeline; verify `DeployVercelDev` succeeds and the prod alias step lands cleanly.

- [ ] **Step 11.7:** Smoke verify on dev:

```bash
# Existing dev test agent: register a goal via daemon, observe TOWN_STATUS.md shows it
curl -sI https://goosetown.isol8.co/  # should still be 200
```

---

## Self-review checklist (run before pushing the plan PR)

- [ ] Each task has actual code, not placeholders
- [ ] Type names consistent across tasks (`currentGoal` everywhere, not `current_goal` mixed in)
- [ ] No reference to GM goal-awareness — that's Plan 3
- [ ] No `pnpm install` commands in steps (per memory, hangs in Bash)
- [ ] CDK synth includes the venv-stash dance (per memory, hangs without it)
- [ ] The `set_goal`/`complete_goal` action validators in Task 2 use field names that match `goals_service.py` calls in Task 3
- [ ] All tests are runnable (no `# add tests` placeholders)
