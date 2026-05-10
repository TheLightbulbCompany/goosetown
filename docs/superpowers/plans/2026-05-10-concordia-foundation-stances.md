# Concordia Foundation — Stances Subsystem Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add agent-authored emotional stances to relationships. Each pair of agents who've interacted has two directional stances — what A thinks of B, and what B thinks of A — as freeform strings. Surfaces in TOWN_STATUS.md when proximate; bidirectional visibility (both agents see both stances). Replaces the inert numeric `affinity` score as the narrative-layer relationship state. No automated stance generation; agent's LLM owns the words.

**Architecture:** Three new columns on the existing `relationships` DDB table (`stanceAtoB: string | null`, `stanceBtoA: string | null`, `stanceUpdatedAt: timestamp | null`). New WS-message action `describe_stance` validated through `ws_models.py`. Service-layer logic in a new `stances_service.py`. Daemon CLI gets one new subcommand. TOWN_STATUS.md rendering surfaces both directions of stance per nearby-agent. Post-conversation prompt fires (LOG-only) suggesting the agent consider their stance.

**Tech Stack:** FastAPI + pytest + uv + boto3 + DynamoDB on backend; CDK TypeScript for infra; Python `goosetown_cli.py` daemon; React 18 + TypeScript + vitest on frontend.

**Spec:** `docs/superpowers/specs/2026-05-07-concordia-foundation-design.md` (Section 4: Stances)

**Branch:** `plan/concordia-foundation-stances` (off `origin/main` after Plan 1 merges).

**Out of scope (deferred to Plan 3):** GM stance-awareness — arc kickoff considers stance pairs, opportunity events factor stance into framing.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `apps/infra/lib/stacks/database-stack.ts` | Modify | Add three columns to relationships (no schema change in DDB; just documentation if any). DDB doesn't enforce schemas — adding fields is implicit. **No CDK change required.** |
| `apps/backend/core/ws_models.py` | Modify | Add `describe_stance` action (requires `target_agent_id` + `text`). |
| `apps/backend/core/services/stances_service.py` | Create | Business logic: `describe_stance(agent_id, target_agent_id, text)`, `get_stance(agent_id, target_agent_id)`, `get_pair_stances(agent_a, agent_b)`. |
| `apps/backend/tests/unit/services/test_stances_service.py` | Create | Unit tests. |
| `apps/backend/core/services/town_service.py` | Modify | Extend relationship CRUD to read/write stance fields. |
| `apps/backend/routers/town.py` | Modify | Dispatch `action=describe_stance` from `town_agent_act` handler. |
| `apps/backend/tests/unit/test_stances_actions.py` | Create | Endpoint tests. |
| `apps/backend/core/services/conversation_service.py` | Modify | After a conversation ends, fire a `system.consider_stance` LOG-only event (no WAKE) into TOWN_EVENTS.md for both participants. |
| `apps/backend/core/services/daemon_bridge.py` | Modify | Include `stanceFromMe` and `stanceFromThem` per nearby agent in the world_update payload. |
| `apps/backend/data/goosetown-skill/skills/goosetown/goosetown_cli.py` | Modify | New `describe_stance` subcommand. Render stances on nearby agents in TOWN_STATUS.md. |
| `apps/backend/data/goosetown-skill/skills/goosetown/SKILL.md` + `references/actions.md` | Modify | Document new action. Keep SKILL.md ≤100 lines. |
| `apps/frontend/src/hooks/useApartment.ts` | Modify | Add stance fields to `ApartmentAgent` type if `/apartment` exposes them (decision: skip for now — stances are proximate-only data; owner sidebar shows them later). |

Net source LOC: ~+450. Net test LOC: ~+250.

---

## Task 1: WS model validation for `describe_stance`

**Files:**
- Modify: `apps/backend/core/ws_models.py`
- Test: `apps/backend/tests/unit/test_ws_models.py`

- [ ] **Step 1.1:** Add `target_agent_id` field to `TownAgentAct`:

```python
    target_agent_id: str | None = Field(default=None, max_length=128)
```

- [ ] **Step 1.2:** Add `describe_stance` to the `AgentAction` literal (alongside the other actions).

- [ ] **Step 1.3:** Write failing tests in `test_ws_models.py`:

```python
def test_describe_stance_requires_target_and_text():
    body_no_target = {
        "type": "town_agent_act", "connectionId": "abc", "agent_id": "alice",
        "action": "describe_stance", "text": "x",
    }
    body_no_text = {
        "type": "town_agent_act", "connectionId": "abc", "agent_id": "alice",
        "action": "describe_stance", "target_agent_id": "bob",
    }
    for body in (body_no_target, body_no_text):
        with pytest.raises(ValidationError):
            TypeAdapter(WsMessage).validate_python(body)


def test_describe_stance_validates_with_target_and_text():
    body = {
        "type": "town_agent_act", "connectionId": "abc", "agent_id": "alice",
        "action": "describe_stance", "target_agent_id": "bob",
        "text": "kind colleague but lately distant",
    }
    msg = TypeAdapter(WsMessage).validate_python(body)
    assert msg.action == "describe_stance"
    assert msg.target_agent_id == "bob"
    assert msg.text == "kind colleague but lately distant"


def test_describe_stance_rejects_text_over_500_chars():
    body = {
        "type": "town_agent_act", "connectionId": "abc", "agent_id": "alice",
        "action": "describe_stance", "target_agent_id": "bob",
        "text": "x" * 501,
    }
    with pytest.raises(ValidationError):
        TypeAdapter(WsMessage).validate_python(body)
```

- [ ] **Step 1.4:** Run tests; should fail (no `describe_stance` action yet).

- [ ] **Step 1.5:** Add validator branches to `_check_action_fields`:

```python
        if self.action == "describe_stance":
            if not self.target_agent_id:
                raise ValueError("'describe_stance' requires 'target_agent_id'")
            if not self.text:
                raise ValueError("'describe_stance' requires 'text'")
            if len(self.text) > 500:
                raise ValueError("'describe_stance' text must be <= 500 chars")
```

- [ ] **Step 1.6:** Run tests; should pass.

- [ ] **Step 1.7:** Commit.

```bash
git add apps/backend/core/ws_models.py apps/backend/tests/unit/test_ws_models.py
git commit -m "feat(backend): validate describe_stance WS action"
```

---

## Task 2: `stances_service.py` — business logic + tests

**Files:**
- Create: `apps/backend/core/services/stances_service.py`
- Create: `apps/backend/tests/unit/services/test_stances_service.py`

The relationships table uses `agentAId` (PK) + `agentBId` (SK). Stances are directional, so each row stores BOTH stances:
- `stanceAtoB` — what agent A thinks of B
- `stanceBtoA` — what agent B thinks of A
- `stanceUpdatedAt` — last update timestamp

When agent A describes their stance toward B, we update `stanceAtoB` on the row where (agentAId, agentBId) is canonically ordered (e.g., always lex-smaller first). When agent B describes toward A, same row but `stanceBtoA`.

- [ ] **Step 2.1:** Write failing tests:

```python
"""Unit tests for stances_service."""

from unittest.mock import patch
import pytest
from core.services import stances_service


@patch("core.services.stances_service.TownService")
def test_describe_stance_writes_stance_AtoB_when_caller_is_A(mock_town):
    """When the caller's id sorts lex-smaller than target, write stanceAtoB."""
    mock_town.return_value.upsert_relationship_stance.return_value = None
    stances_service.describe_stance(
        agent_id="alice",
        target_agent_id="bob",
        text="kind colleague",
    )
    args = mock_town.return_value.upsert_relationship_stance.call_args.kwargs
    assert args["agent_a_id"] == "alice"
    assert args["agent_b_id"] == "bob"
    assert args["stance_a_to_b"] == "kind colleague"
    assert "stance_b_to_a" not in args or args["stance_b_to_a"] is None


@patch("core.services.stances_service.TownService")
def test_describe_stance_writes_stance_BtoA_when_caller_is_B(mock_town):
    """When the caller's id sorts lex-larger, the order swaps internally."""
    stances_service.describe_stance(
        agent_id="bob",
        target_agent_id="alice",
        text="annoying but means well",
    )
    args = mock_town.return_value.upsert_relationship_stance.call_args.kwargs
    # canonical order: alice (a-id) < bob (b-id)
    assert args["agent_a_id"] == "alice"
    assert args["agent_b_id"] == "bob"
    assert args["stance_b_to_a"] == "annoying but means well"


@patch("core.services.stances_service.TownService")
def test_describe_stance_rejects_self_target(mock_town):
    with pytest.raises(ValueError, match="self"):
        stances_service.describe_stance(
            agent_id="alice", target_agent_id="alice", text="x",
        )
    mock_town.return_value.upsert_relationship_stance.assert_not_called()


@patch("core.services.stances_service.TownService")
def test_describe_stance_rejects_empty_text(mock_town):
    with pytest.raises(ValueError, match="empty"):
        stances_service.describe_stance(
            agent_id="alice", target_agent_id="bob", text="   ",
        )


@patch("core.services.stances_service.TownService")
def test_get_pair_stances_returns_both_directions_and_updated_at(mock_town):
    mock_town.return_value.get_relationship.return_value = {
        "agentAId": "alice", "agentBId": "bob",
        "stanceAtoB": "trustworthy", "stanceBtoA": "warm but distracted",
        "stanceUpdatedAt": 12345,
    }
    result = stances_service.get_pair_stances("alice", "bob")
    assert result["stance_a_to_b"] == "trustworthy"
    assert result["stance_b_to_a"] == "warm but distracted"
    assert result["updated_at"] == 12345


@patch("core.services.stances_service.TownService")
def test_get_pair_stances_returns_None_when_no_relationship(mock_town):
    mock_town.return_value.get_relationship.return_value = None
    result = stances_service.get_pair_stances("alice", "bob")
    assert result is None


@patch("core.services.stances_service.TownService")
def test_get_stance_for_caller_returns_caller_perspective(mock_town):
    """get_stance(viewer, subject) returns what `viewer` thinks of `subject`."""
    mock_town.return_value.get_relationship.return_value = {
        "agentAId": "alice", "agentBId": "bob",
        "stanceAtoB": "kind colleague",  # what alice thinks of bob
        "stanceBtoA": "good company",    # what bob thinks of alice
    }
    # alice (viewer) toward bob (subject): use stanceAtoB
    assert stances_service.get_stance("alice", "bob") == "kind colleague"
    # bob (viewer) toward alice (subject): use stanceBtoA
    assert stances_service.get_stance("bob", "alice") == "good company"
```

- [ ] **Step 2.2:** Run tests; should fail (module doesn't exist).

- [ ] **Step 2.3:** Implement `stances_service.py`:

```python
"""Stances subsystem — business logic.

Each agent describes a freeform stance toward each agent they've encountered.
Stances are directional: alice's stance toward bob ≠ bob's stance toward alice.
Stored on the existing relationships table as `stanceAtoB` + `stanceBtoA`.

The relationships table uses canonical (agentA, agentB) ordering — always the
lex-smaller id is `agentAId`. When the caller is agent A, we update stanceAtoB;
when the caller is agent B, we update stanceBtoA.

GM stance-awareness (arc kickoff biased by stance pairs, opportunity events
factor stance into framing) is deferred to Plan 3 (World GM).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from core.services.town_service import TownService

logger = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _canonical_pair(agent_a: str, agent_b: str) -> tuple[str, str, bool]:
    """Return (canonical_a, canonical_b, caller_is_a) where canonical order is
    lex-sorted. ``caller_is_a`` indicates whether the FIRST argument was the A
    side. Used to decide whether to write stance_a_to_b or stance_b_to_a."""
    if agent_a < agent_b:
        return agent_a, agent_b, True
    return agent_b, agent_a, False


def describe_stance(agent_id: str, target_agent_id: str, text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        raise ValueError("stance text must not be empty")
    if len(text) > 500:
        raise ValueError("stance text must be <= 500 chars")
    if agent_id == target_agent_id:
        raise ValueError("cannot describe stance toward self")

    canonical_a, canonical_b, caller_is_a = _canonical_pair(agent_id, target_agent_id)

    service = TownService()
    if caller_is_a:
        service.upsert_relationship_stance(
            agent_a_id=canonical_a,
            agent_b_id=canonical_b,
            stance_a_to_b=text,
            stance_updated_at=_now_ms(),
        )
    else:
        service.upsert_relationship_stance(
            agent_a_id=canonical_a,
            agent_b_id=canonical_b,
            stance_b_to_a=text,
            stance_updated_at=_now_ms(),
        )

    logger.info(
        "[agent:%s] stance updated toward %s: %s",
        agent_id[:8], target_agent_id[:8], text[:60],
    )
    return {"status": "ok", "stance": text}


def get_pair_stances(agent_a: str, agent_b: str) -> dict[str, Any] | None:
    canonical_a, canonical_b, _ = _canonical_pair(agent_a, agent_b)
    service = TownService()
    rel = service.get_relationship(canonical_a, canonical_b)
    if not rel:
        return None
    return {
        "stance_a_to_b": rel.get("stanceAtoB"),
        "stance_b_to_a": rel.get("stanceBtoA"),
        "updated_at": rel.get("stanceUpdatedAt"),
    }


def get_stance(viewer_agent_id: str, subject_agent_id: str) -> str | None:
    """Return what ``viewer`` thinks of ``subject``."""
    canonical_a, canonical_b, viewer_is_a = _canonical_pair(viewer_agent_id, subject_agent_id)
    service = TownService()
    rel = service.get_relationship(canonical_a, canonical_b)
    if not rel:
        return None
    return rel.get("stanceAtoB") if viewer_is_a else rel.get("stanceBtoA")
```

- [ ] **Step 2.4:** Add `upsert_relationship_stance` and `get_relationship` to `TownService` if not already present. Check existing relationship methods and match the pattern. Likely an `update_relationship_state` method already exists; if so, extend its `field_map`:

```python
            "stance_a_to_b": "stanceAtoB",
            "stance_b_to_a": "stanceBtoA",
            "stance_updated_at": "stanceUpdatedAt",
```

- [ ] **Step 2.5:** Run tests; should pass.

- [ ] **Step 2.6:** Commit.

```bash
git add apps/backend/core/services/stances_service.py apps/backend/core/services/town_service.py apps/backend/tests/unit/services/test_stances_service.py
git commit -m "feat(backend): stances_service — describe_stance, get_pair_stances, get_stance"
```

---

## Task 3: Backend dispatcher — route `describe_stance` action

**Files:**
- Modify: `apps/backend/routers/town.py`
- Create: `apps/backend/tests/unit/test_stances_actions.py`

- [ ] **Step 3.1:** Write failing tests in `test_stances_actions.py`. Match the style of `test_goals_actions.py` (Plan 1 reference).

- [ ] **Step 3.2:** Add dispatcher case in `town_agent_act` handler:

```python
    elif action == "describe_stance":
        target_agent_id = body.get("target_agent_id", "")
        text = body.get("text", "")
        result = stances_service.describe_stance(
            agent_id=agent_id,
            target_agent_id=target_agent_id,
            text=text,
        )
        return {"statusCode": 200, "body": result}
```

Add the import:

```python
from core.services import stances_service
```

- [ ] **Step 3.3:** Run tests + full suite. Commit.

```bash
git commit -m "feat(backend): dispatch describe_stance in town_agent_act"
```

---

## Task 4: Daemon CLI — `describe_stance` subcommand

**Files:**
- Modify: `apps/backend/data/goosetown-skill/skills/goosetown/goosetown_cli.py`

Add a subcommand alongside `set_goal` from Plan 1:

```python
    elif action == "describe_stance":
        if len(action_args) < 2:
            _die(
                "Usage: act describe_stance <target_agent_id> <text...>",
                "Example: act describe_stance bob-uuid \"kind but lately distant\"",
            )
        target_agent_id = action_args[0]
        text = " ".join(action_args[1:]).strip()
        if not text:
            _die("Stance text must not be empty.")
        if len(text) > 500:
            _die("Stance text must be <= 500 chars.")
        payload = {
            "action": "describe_stance",
            "target_agent_id": target_agent_id,
            "text": text,
        }
        result = _ipc_send(sock_path, {"action": "act", "payload": payload})
        _out(result)
```

Add `"describe_stance"` to `_VALID_COMMANDS`. Match exact patterns from Plan 1's CLI changes.

- [ ] **Step 4.1:** Implement.
- [ ] **Step 4.2:** Syntax check + commit.

---

## Task 5: TOWN_STATUS.md rendering — surface stances on nearby agents

**Files:**
- Modify: `apps/backend/data/goosetown-skill/skills/goosetown/goosetown_cli.py`

Extend the `nearby_chat_options` rendering (where Plan 1 already adds the `Goal:` sub-bullet) with stance lines:

```
- **Bob** (bob-uuid) — at cafe
  - Goal: _make peace with Alice over the cafe incident_
  - Your stance toward him: _kind colleague but lately distant_
  - His stance toward you: _Alice means well but her questions wear me out_
```

The data comes from the world_update payload. Task 6 ensures the backend includes it.

- [ ] **Step 5.1:** Add a helper:

```python
def _render_stance_lines(near: dict) -> list[str]:
    """Return 0, 1, or 2 stance sub-bullet lines for a nearby agent."""
    lines = []
    your_stance = (near.get("stanceFromMe") or "").strip()
    their_stance = (near.get("stanceFromThem") or "").strip()
    if your_stance:
        lines.append(f"  - Your stance toward them: _{your_stance}_")
    if their_stance:
        lines.append(f"  - Their stance toward you: _{their_stance}_")
    return lines
```

- [ ] **Step 5.2:** Wire into the existing nearby loop, after the goal sub-bullet from Plan 1.

- [ ] **Step 5.3:** Tests in `test_goosetown_cli.py` — add cases for both directions present, only one present, neither present.

- [ ] **Step 5.4:** Commit.

---

## Task 6: Backend serializer — include stances in nearby-agent payload

**Files:**
- Modify: `apps/backend/core/services/daemon_bridge.py`

In the per-nearby-agent enrichment loop (where Plan 1 added `currentGoal`), also fetch stances:

```python
    pair_stances = stances_service.get_pair_stances(sender_id, near_id)
    if pair_stances:
        # Determine which direction is "from sender's perspective" (stanceFromMe)
        # vs "from nearby agent's perspective" (stanceFromThem).
        # stances_service.get_stance(viewer, subject) handles the canonical-pair logic.
        near["stanceFromMe"] = stances_service.get_stance(sender_id, near_id)
        near["stanceFromThem"] = stances_service.get_stance(near_id, sender_id)
    else:
        near["stanceFromMe"] = None
        near["stanceFromThem"] = None
```

(Use `get_pair_stances` first to skip per-direction lookups when no relationship exists, then `get_stance` is a one-row lookup so the math is fine.)

Note on hot-path cost: this adds two more relationship reads per nearby agent per `agent_state` event. Same pattern as Plan 1's `currentGoal` lookup. Acceptable for current scale (≤5 nearby agents typical).

- [ ] **Step 6.1:** Implement + tests in `test_daemon_bridge.py`.
- [ ] **Step 6.2:** Run full suite; should be no regressions. Commit.

---

## Task 7: Post-conversation `system.consider_stance` event (LOG-only)

**Files:**
- Modify: `apps/backend/core/services/conversation_service.py`

When a conversation ends (auto-end or `end` action), emit a `system.consider_stance` event into both participants' TOWN_EVENTS.md. **LOG-only — does not set `think=true`.** Just a hint; the agent's LLM sees it on the next /loop tick and decides whether to call `describe_stance`.

- [ ] **Step 7.1:** Find `_finalize_conversation` (auto-end + manual-end paths).

- [ ] **Step 7.2:** After the existing finalization, for each participant, emit:

```python
    # Suggest a stance update — LOG-only, no WAKE pressure.
    for participant_id in [conv.participant_a_id, conv.participant_b_id]:
        other_id = conv.participant_b_id if participant_id == conv.participant_a_id else conv.participant_a_id
        other_name = await _get_display_name(other_id)
        await _emit_system_event(
            agent_id=participant_id,
            event_type="system.consider_stance",
            payload={
                "summary": f"You just talked with {other_name}. Has your stance toward them changed?",
                "target_agent_id": other_id,
                "wake": False,
            },
        )
```

Adapt to existing event-emission helpers. If `_emit_system_event` doesn't exist, look at how `system.low_energy` events are emitted today and match the pattern.

- [ ] **Step 7.3:** Update the daemon's TOWN_EVENTS.md handling so `system.consider_stance` events render as readable hints (not raw JSON).

- [ ] **Step 7.4:** Tests in `test_conversation_service.py` and `test_goosetown_cli.py`.

- [ ] **Step 7.5:** Commit.

---

## Task 8: SKILL.md + references docs

**Files:**
- Modify: `apps/backend/data/goosetown-skill/skills/goosetown/SKILL.md`
- Modify: `apps/backend/data/goosetown-skill/skills/goosetown/references/actions.md`

Add `describe_stance` to the SKILL.md actions list (one tight sentence) and detailed docs in `references/actions.md`. Keep SKILL.md ≤100 lines (the test enforces).

- [ ] **Step 8.1:** Edit + commit.

---

## Task 9: Final integration + push

- [ ] **Step 9.1:** Run full backend test suite; expect zero new failures.

- [ ] **Step 9.2:** CDK synth (with `.venv` stash dance) — should be no-op since no infra change in this plan.

- [ ] **Step 9.3:** Push branch + open PR.

- [ ] **Step 9.4:** Watch CI, merge when green.

- [ ] **Step 9.5:** After deploy, smoke verify on dev.

---

## Self-review checklist

- [ ] No persona-as-product framing — stances are agent-authored, not server-generated
- [ ] Bidirectional visibility (both agents see both stances)
- [ ] Canonical (a < b) ordering used consistently for relationship rows
- [ ] Self-target rejected
- [ ] No GM logic — that's Plan 3
- [ ] CLI subcommand added to `_VALID_COMMANDS` for discoverability
- [ ] SKILL.md ≤100 lines after addition
