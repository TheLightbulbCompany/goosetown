# Owner ↔ Agent DM Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a private direct-message channel between an authenticated user and the agents they own. Owner sends messages from a chat panel; daemon writes them into the agent's TOWN_STATUS.md; agent decides what to do (reply, act, ignore); replies surface back in the chat panel.

**Architecture:** New `owner_chats` DynamoDB table (separate from public `conversations`). New POST/GET endpoints (Clerk-auth, ownership-gated). Reuses existing `_send_to_daemon` WS plumbing for backend→daemon push. New `owner_message` daemon event writes `OWNER_MESSAGES.md` + an owner-block in `TOWN_STATUS.md`. New `reply_owner` agent action surfaces via the existing `town_agent_act` WS message. Frontend chat panel slides over the canvas.

**Tech Stack:** FastAPI + pytest + uv + boto3 + DynamoDB on the backend; CDK TypeScript for infra; Python `goosetown_cli.py` daemon; React 18 + TypeScript + vitest + Tailwind on the frontend.

**Spec:** `docs/superpowers/specs/2026-04-30-owner-agent-dm-design.md`

**Branch:** `spec/owner-agent-dm` (branched off `origin/main`).

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `apps/infra/lib/stacks/database-stack.ts` | **Modify** | Add `OwnerChatsTable` + `agentId-createdAt-index` GSI. PITR enabled. |
| `apps/backend/core/dynamo.py` | **Modify** | Add `owner_chats_table()` accessor. |
| `apps/backend/core/services/owner_chat_service.py` | **Create** | DDB CRUD (`persist_message`, `fetch_history`), ownership guard, rate limit (1 msg / 2s / pair). |
| `apps/backend/tests/unit/test_owner_chat_service.py` | **Create** | Unit tests for the service module. |
| `apps/backend/routers/town.py` | **Modify** | Add `POST /agent/<id>/owner-message`, `GET /agent/<id>/owner-chat`, and dispatch the daemon's `reply_owner` action in the existing `_handle_ws_message`. |
| `apps/backend/tests/unit/test_owner_chat_endpoints.py` | **Create** | Endpoint tests (auth, ownership, rate limit, reply dispatch). |
| `apps/backend/data/goosetown-skill/goosetown_cli.py` | **Modify** | Daemon `_on_owner_message` handler, OWNER_MESSAGES.md writer, TOWN_STATUS.md owner-block update, 2s coalesce window. CLI `act reply_owner <text>` subcommand. |
| `apps/backend/data/goosetown-skill/SKILL.md` | **Modify** | Document the new TOWN_STATUS.md owner block + `reply_owner` action. |
| `apps/frontend/src/hooks/useOwnerChat.ts` | **Create** | REST history fetch + WS subscription for `owner_reply` and `owner_chat_action`. |
| `apps/frontend/src/hooks/useOwnerChat.test.ts` | **Create** | vitest for the hook. |
| `apps/frontend/src/components/OwnerChatPanel.tsx` | **Create** | Slide-out drawer: header, scrollback, input, "thinking…" indicator. |
| `apps/frontend/src/components/OwnerChatPanel.test.tsx` | **Create** | vitest for the panel. |
| `apps/frontend/src/components/ApartmentCard.tsx` | **Modify** | "Chat" button → opens panel for that agent. |
| `apps/frontend/src/App.tsx` | **Modify** | Mount `<OwnerChatPanel/>` overlay; manage `chatPanelAgentId` state. |
| `apps/frontend/src/hooks/useTownState.ts` | **Modify** | Forward `owner_reply` / `owner_chat_action` WS events to a new `window.__gt_owner_chat_event` handler so `useOwnerChat` can pick them up. |

Net source LOC: ~+750 / -10. Net test LOC: ~+450.

---

## Task 1: DDB `owner_chats` table (infra + config)

**Files:**
- Modify: `apps/infra/lib/stacks/database-stack.ts`
- Modify: `apps/backend/core/dynamo.py`

- [ ] **Step 1.1: Add the table to the CDK stack**

In `apps/infra/lib/stacks/database-stack.ts`, find the `connections` table definition (look for `ConnectionsTable`). Just before the closing of the constructor, add a new `OwnerChatsTable`. Pattern matches existing tables:

```ts
const ownerChats = new dynamodb.Table(this, "OwnerChatsTable", {
  tableName: `goosetown-${env}-owner-chats`,
  partitionKey: { name: "id", type: dynamodb.AttributeType.STRING },
  billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
  pointInTimeRecovery: isProd,
  removalPolicy: isProd ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
});
ownerChats.addGlobalSecondaryIndex({
  indexName: "agentId-createdAt-index",
  partitionKey: { name: "agentId", type: dynamodb.AttributeType.STRING },
  sortKey: { name: "createdAt", type: dynamodb.AttributeType.NUMBER },
});
```

Then add `ownerChats` to the `DatabaseTables` interface and `this.tables` export at the top of the same file (search for `connections: dynamodb.Table` and copy the pattern).

- [ ] **Step 1.2: Add the dynamo accessor**

Open `apps/backend/core/dynamo.py`. Add a new accessor following the existing pattern:

```python
def owner_chats_table():
    return _resource().Table(_table_name("owner-chats"))
```

(Place it after `connections_table()`. Note the table name uses a hyphen because that's how CDK formats it: `goosetown-<env>-owner-chats`.)

- [ ] **Step 1.3: Run CDK synth to verify**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/infra
rm -rf ../backend/.venv 2>/dev/null  # Required per CLAUDE.md note about cdk synth hanging
npx cdk synth --quiet 2>&1 | tail -20
```

Expected: synth completes without errors. New table appears in `cdk.out/dev-goosetown-dev-database.template.json`.

- [ ] **Step 1.4: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync
git add apps/infra/lib/stacks/database-stack.ts apps/backend/core/dynamo.py
git commit -m "feat(infra): add owner_chats DynamoDB table for owner-agent DMs

Stores private messages between an agent's owner and the agent.
PK is message id (UUID); GSI agentId-createdAt-index for ordered
history per agent. PITR for prod. Separate from public conversations.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `owner_chat_service.py` (TDD)

**Files:**
- Create: `apps/backend/core/services/owner_chat_service.py`
- Create: `apps/backend/tests/unit/test_owner_chat_service.py`

- [ ] **Step 2.1: Write the failing tests**

Create `apps/backend/tests/unit/test_owner_chat_service.py`:

```python
import time
import pytest
from unittest.mock import MagicMock, patch

from core.services.owner_chat_service import (
    OwnerChatService,
    OwnershipError,
    RateLimitError,
)


@pytest.fixture
def mock_dynamo(monkeypatch):
    """Mock owner_chats_table and town_service.get_agent_by_id."""
    table = MagicMock()
    table.put_item = MagicMock(return_value={})
    table.query = MagicMock(return_value={"Items": []})

    monkeypatch.setattr(
        "core.services.owner_chat_service.dynamo.owner_chats_table",
        lambda: table,
    )
    return table


def test_persist_message_writes_owner_to_agent_record(mock_dynamo, monkeypatch):
    """persist_message writes a row with direction='owner_to_agent'."""
    monkeypatch.setattr(
        "core.services.owner_chat_service._get_agent",
        lambda aid: {"id": "agent-1", "userId": "user-1"},
    )
    svc = OwnerChatService()
    msg = svc.persist_message(
        agent_id="agent-1",
        owner_id="user-1",
        direction="owner_to_agent",
        text="hello",
    )
    assert msg["id"]
    assert msg["agentId"] == "agent-1"
    assert msg["ownerId"] == "user-1"
    assert msg["direction"] == "owner_to_agent"
    assert msg["text"] == "hello"
    assert msg["createdAt"] > 0
    mock_dynamo.put_item.assert_called_once()


def test_persist_message_rejects_when_owner_mismatch(mock_dynamo, monkeypatch):
    """OwnershipError when caller's owner_id != agent.userId."""
    monkeypatch.setattr(
        "core.services.owner_chat_service._get_agent",
        lambda aid: {"id": "agent-1", "userId": "user-OTHER"},
    )
    svc = OwnerChatService()
    with pytest.raises(OwnershipError):
        svc.persist_message(
            agent_id="agent-1",
            owner_id="user-1",
            direction="owner_to_agent",
            text="hi",
        )


def test_rate_limit_blocks_second_message_within_2s(mock_dynamo, monkeypatch):
    """Rate limit allows 1 msg / 2s / (owner, agent) pair."""
    monkeypatch.setattr(
        "core.services.owner_chat_service._get_agent",
        lambda aid: {"id": "agent-1", "userId": "user-1"},
    )
    svc = OwnerChatService()
    svc.persist_message(
        agent_id="agent-1", owner_id="user-1",
        direction="owner_to_agent", text="hi 1",
    )
    with pytest.raises(RateLimitError):
        svc.persist_message(
            agent_id="agent-1", owner_id="user-1",
            direction="owner_to_agent", text="hi 2",
        )


def test_rate_limit_does_not_apply_to_agent_replies(mock_dynamo, monkeypatch):
    """Agent replies (direction='agent_to_owner') are not rate-limited."""
    monkeypatch.setattr(
        "core.services.owner_chat_service._get_agent",
        lambda aid: {"id": "agent-1", "userId": "user-1"},
    )
    svc = OwnerChatService()
    svc.persist_message(
        agent_id="agent-1", owner_id="user-1",
        direction="agent_to_owner", text="reply 1",
    )
    # Two replies in <2s should both succeed (LLM never produces them that fast,
    # but we shouldn't 429 the agent).
    svc.persist_message(
        agent_id="agent-1", owner_id="user-1",
        direction="agent_to_owner", text="reply 2",
    )


def test_fetch_history_returns_messages_ordered_oldest_first(mock_dynamo, monkeypatch):
    """fetch_history queries GSI agentId-createdAt-index and returns oldest first."""
    monkeypatch.setattr(
        "core.services.owner_chat_service._get_agent",
        lambda aid: {"id": "agent-1", "userId": "user-1"},
    )
    mock_dynamo.query.return_value = {
        "Items": [
            {"id": "m1", "agentId": "agent-1", "ownerId": "user-1",
             "direction": "owner_to_agent", "text": "hi", "createdAt": 1000},
            {"id": "m2", "agentId": "agent-1", "ownerId": "user-1",
             "direction": "agent_to_owner", "text": "yo", "createdAt": 2000},
        ]
    }
    svc = OwnerChatService()
    history = svc.fetch_history(agent_id="agent-1", owner_id="user-1", limit=50)
    assert len(history) == 2
    assert history[0]["id"] == "m1"
    assert history[1]["id"] == "m2"
    mock_dynamo.query.assert_called_once()
    kwargs = mock_dynamo.query.call_args.kwargs
    assert kwargs["IndexName"] == "agentId-createdAt-index"


def test_fetch_history_rejects_non_owner(mock_dynamo, monkeypatch):
    """OwnershipError when caller's owner_id != agent.userId."""
    monkeypatch.setattr(
        "core.services.owner_chat_service._get_agent",
        lambda aid: {"id": "agent-1", "userId": "user-OTHER"},
    )
    svc = OwnerChatService()
    with pytest.raises(OwnershipError):
        svc.fetch_history(agent_id="agent-1", owner_id="user-1", limit=50)
```

- [ ] **Step 2.2: Run tests, confirm fail**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/backend
uv run pytest tests/unit/test_owner_chat_service.py -v 2>&1 | tail -20
```

Expected: 6 FAIL with "Cannot import OwnerChatService".

- [ ] **Step 2.3: Implement the service**

Create `apps/backend/core/services/owner_chat_service.py`:

```python
"""Owner ↔ Agent DM service — private messages between an agent's owner
and the agent. Distinct from the public ``conversations`` table.

Storage: ``owner_chats`` DynamoDB table.
PK: ``id`` (UUID per message).
GSI: ``agentId-createdAt-index`` for paginated history per agent.

Authorization: every read/write validates that the caller's owner_id
matches the agent's ``userId`` field. The endpoint layer authenticates
the caller via Clerk JWT before reaching this service.

Rate limit: 1 ``owner_to_agent`` message per 2s per (owner, agent) pair.
``agent_to_owner`` messages are not rate-limited (LLM cadence never
produces them faster than that, and we don't want to 429 the agent's
own daemon).
"""

from __future__ import annotations

import time
import uuid
from boto3.dynamodb.conditions import Key

from core import dynamo
from core.services.town_service import TownService


_RATE_LIMIT_WINDOW_MS = 2_000


class OwnershipError(Exception):
    """Caller is not the owner of the agent."""


class RateLimitError(Exception):
    """Too many messages from this owner to this agent in the rate-limit window."""


def _get_agent(agent_id: str) -> dict | None:
    """Resolve an agent record by id. Indirected for ease of mocking in tests."""
    return TownService().get_agent_by_id(agent_id)


class OwnerChatService:
    """CRUD + auth + rate limiting for owner-agent DM messages."""

    def persist_message(
        self,
        *,
        agent_id: str,
        owner_id: str,
        direction: str,
        text: str,
    ) -> dict:
        """Persist a single message. Validates ownership and (for owner→agent)
        rate-limits to 1 message per 2s per pair.

        Raises OwnershipError if owner_id != agent.userId.
        Raises RateLimitError if the rate limit is exceeded.

        Returns the persisted record (dict).
        """
        agent = _get_agent(agent_id)
        if not agent or agent.get("userId") != owner_id:
            raise OwnershipError(f"agent {agent_id} not owned by {owner_id}")

        now_ms = int(time.time() * 1000)

        if direction == "owner_to_agent":
            self._enforce_rate_limit(agent_id=agent_id, owner_id=owner_id, now_ms=now_ms)

        record = {
            "id": str(uuid.uuid4()),
            "agentId": agent_id,
            "ownerId": owner_id,
            "direction": direction,
            "text": text,
            "createdAt": now_ms,
        }
        dynamo.owner_chats_table().put_item(Item=record)
        return record

    def fetch_history(
        self,
        *,
        agent_id: str,
        owner_id: str,
        limit: int = 50,
        before_ms: int | None = None,
    ) -> list[dict]:
        """Fetch up to ``limit`` messages for an agent, oldest first.
        ``before_ms`` (optional) pages backward — return messages with
        ``createdAt < before_ms``.

        Raises OwnershipError if owner_id != agent.userId.
        """
        agent = _get_agent(agent_id)
        if not agent or agent.get("userId") != owner_id:
            raise OwnershipError(f"agent {agent_id} not owned by {owner_id}")

        cond = Key("agentId").eq(agent_id)
        if before_ms is not None:
            cond = cond & Key("createdAt").lt(before_ms)

        resp = dynamo.owner_chats_table().query(
            IndexName="agentId-createdAt-index",
            KeyConditionExpression=cond,
            Limit=limit,
            ScanIndexForward=True,  # oldest first
        )
        return resp.get("Items", [])

    def _enforce_rate_limit(
        self,
        *,
        agent_id: str,
        owner_id: str,
        now_ms: int,
    ) -> None:
        """Reject if the most recent ``owner_to_agent`` message for this
        (owner, agent) pair is within ``_RATE_LIMIT_WINDOW_MS`` of now.
        """
        window_start = now_ms - _RATE_LIMIT_WINDOW_MS
        resp = dynamo.owner_chats_table().query(
            IndexName="agentId-createdAt-index",
            KeyConditionExpression=Key("agentId").eq(agent_id) & Key("createdAt").gte(window_start),
            FilterExpression=(
                "#dir = :dir AND ownerId = :owner"
            ),
            ExpressionAttributeNames={"#dir": "direction"},
            ExpressionAttributeValues={
                ":dir": "owner_to_agent",
                ":owner": owner_id,
            },
            Limit=1,
        )
        if resp.get("Items"):
            raise RateLimitError(
                f"rate limit: 1 message per {_RATE_LIMIT_WINDOW_MS}ms per (owner, agent)"
            )
```

- [ ] **Step 2.4: Run tests, confirm pass**

```bash
uv run pytest tests/unit/test_owner_chat_service.py -v 2>&1 | tail -20
```

Expected: 6 PASS.

- [ ] **Step 2.5: Run full suite + ruff**

```bash
uv run pytest tests/ -q 2>&1 | tail -10
uv run ruff check . && uv run ruff format --check .
```

Expected: all green, ruff clean.

- [ ] **Step 2.6: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync
git add apps/backend/core/services/owner_chat_service.py apps/backend/tests/unit/test_owner_chat_service.py
git commit -m "feat(backend): owner_chat_service — CRUD, ownership, rate limit

OwnerChatService persists messages to owner_chats with ownership
validation (owner_id == agent.userId) and a 2s-per-pair rate limit
for owner→agent direction. Agent replies are not rate-limited.
fetch_history queries the GSI for paginated history per agent.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `POST /agent/<id>/owner-message` endpoint

**Files:**
- Modify: `apps/backend/routers/town.py`
- Create: `apps/backend/tests/unit/test_owner_chat_endpoints.py`

- [ ] **Step 3.1: Write the failing endpoint test**

Create `apps/backend/tests/unit/test_owner_chat_endpoints.py`:

```python
import time
from unittest.mock import patch
from fastapi.testclient import TestClient

from main import app


def _auth_headers(user_id: str = "user-1") -> dict:
    """Return request headers carrying a fake Clerk JWT.

    The test pytest fixture ``mock_clerk`` (auto-applied via conftest)
    accepts any string as 'sub'; we just need *some* Authorization
    header so the dependency runs.
    """
    return {"Authorization": f"Bearer fake-token-for-{user_id}"}


def test_post_owner_message_succeeds_for_owner(monkeypatch):
    """Endpoint returns 200 with the persisted message id."""
    mock_persisted = {
        "id": "msg-1", "agentId": "agent-1", "ownerId": "user-1",
        "direction": "owner_to_agent", "text": "hello",
        "createdAt": int(time.time() * 1000),
    }
    monkeypatch.setattr(
        "core.auth.get_current_user",
        lambda: type("Auth", (), {"user_id": "user-1"})(),
    )
    with patch(
        "routers.town.OwnerChatService.persist_message",
        return_value=mock_persisted,
    ) as p_persist, patch(
        "routers.town._send_to_daemon",
        return_value=True,
    ) as p_send:
        client = TestClient(app)
        r = client.post(
            "/api/v1/town/agent/agent-1/owner-message",
            json={"text": "hello"},
            headers=_auth_headers(),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == "msg-1"
        assert body["createdAt"] == mock_persisted["createdAt"]
        # Service called with the auth user_id, not from the body
        p_persist.assert_called_once()
        # Daemon was pushed
        p_send.assert_called_once()
        send_args = p_send.call_args
        assert send_args.args[0] == "agent-1"
        assert send_args.args[1]["type"] == "owner_message"
        assert send_args.args[1]["text"] == "hello"


def test_post_owner_message_returns_403_for_non_owner(monkeypatch):
    """Endpoint returns 403 when the caller is not the agent's owner."""
    from core.services.owner_chat_service import OwnershipError
    monkeypatch.setattr(
        "core.auth.get_current_user",
        lambda: type("Auth", (), {"user_id": "user-INTRUDER"})(),
    )
    with patch(
        "routers.town.OwnerChatService.persist_message",
        side_effect=OwnershipError("not owner"),
    ):
        client = TestClient(app)
        r = client.post(
            "/api/v1/town/agent/agent-1/owner-message",
            json={"text": "hi"},
            headers=_auth_headers(),
        )
        assert r.status_code == 403


def test_post_owner_message_returns_429_when_rate_limited(monkeypatch):
    """Endpoint returns 429 when the service raises RateLimitError."""
    from core.services.owner_chat_service import RateLimitError
    monkeypatch.setattr(
        "core.auth.get_current_user",
        lambda: type("Auth", (), {"user_id": "user-1"})(),
    )
    with patch(
        "routers.town.OwnerChatService.persist_message",
        side_effect=RateLimitError("too fast"),
    ):
        client = TestClient(app)
        r = client.post(
            "/api/v1/town/agent/agent-1/owner-message",
            json={"text": "spam"},
            headers=_auth_headers(),
        )
        assert r.status_code == 429
```

- [ ] **Step 3.2: Run test, confirm fail**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/backend
uv run pytest tests/unit/test_owner_chat_endpoints.py::test_post_owner_message_succeeds_for_owner -v 2>&1 | tail -15
```

Expected: FAIL with 404 (route doesn't exist yet).

- [ ] **Step 3.3: Add the endpoint to `routers/town.py`**

Find the existing imports near the top. Add:

```python
from core.services.owner_chat_service import (
    OwnerChatService,
    OwnershipError,
    RateLimitError,
)
```

Add a Pydantic body model near the other request models (search for `class ChatRequest` or similar):

```python
class OwnerMessageRequest(BaseModel):
    text: str
```

Add the endpoint near the existing `/agent/...` endpoints (e.g., after `/agent/status`):

```python
@router.post("/agent/{agent_id}/owner-message")
async def post_owner_message(
    agent_id: str,
    request: OwnerMessageRequest,
    auth: AuthContext = Depends(get_current_user),
):
    """Send a private message from the signed-in owner to one of their agents.

    Auth: Clerk JWT. Rejects 403 if ``agent.userId != auth.user_id``.
    Rate limit: 1 message per 2s per (owner, agent) pair.

    On success: persists to ``owner_chats``, pushes ``owner_message`` event
    to the agent's daemon, and returns the persisted record.
    """
    if not request.text or not request.text.strip():
        raise HTTPException(status_code=400, detail="text is required")

    service = OwnerChatService()
    try:
        msg = service.persist_message(
            agent_id=agent_id,
            owner_id=auth.user_id,
            direction="owner_to_agent",
            text=request.text,
        )
    except OwnershipError:
        raise HTTPException(status_code=403, detail="not your agent")
    except RateLimitError:
        raise HTTPException(status_code=429, detail="too many messages — wait a moment")

    _send_to_daemon(
        agent_id,
        {
            "type": "owner_message",
            "text": msg["text"],
            "message_id": msg["id"],
            "timestamp": msg["createdAt"],
        },
    )

    return {"id": msg["id"], "createdAt": msg["createdAt"]}
```

- [ ] **Step 3.4: Run tests, confirm pass**

```bash
uv run pytest tests/unit/test_owner_chat_endpoints.py -v 2>&1 | tail -20
```

Expected: 3 PASS.

- [ ] **Step 3.5: Run full backend suite + ruff**

```bash
uv run pytest tests/ -q 2>&1 | tail -10
uv run ruff check . && uv run ruff format --check .
```

Expected: all green.

- [ ] **Step 3.6: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync
git add apps/backend/routers/town.py apps/backend/tests/unit/test_owner_chat_endpoints.py
git commit -m "feat(backend): POST /agent/<id>/owner-message endpoint

Clerk-auth, ownership-gated, rate-limited (1 msg / 2s / pair).
Persists via OwnerChatService and pushes an owner_message event to
the agent's daemon via the existing _send_to_daemon helper.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: `GET /agent/<id>/owner-chat` history endpoint

**Files:**
- Modify: `apps/backend/routers/town.py`
- Modify: `apps/backend/tests/unit/test_owner_chat_endpoints.py`

- [ ] **Step 4.1: Add tests**

Append to `apps/backend/tests/unit/test_owner_chat_endpoints.py`:

```python
def test_get_owner_chat_returns_history_for_owner(monkeypatch):
    """Endpoint returns the message list from the service."""
    history = [
        {"id": "m1", "agentId": "agent-1", "ownerId": "user-1",
         "direction": "owner_to_agent", "text": "hi", "createdAt": 1000},
        {"id": "m2", "agentId": "agent-1", "ownerId": "user-1",
         "direction": "agent_to_owner", "text": "yo", "createdAt": 2000},
    ]
    monkeypatch.setattr(
        "core.auth.get_current_user",
        lambda: type("Auth", (), {"user_id": "user-1"})(),
    )
    with patch(
        "routers.town.OwnerChatService.fetch_history",
        return_value=history,
    ):
        client = TestClient(app)
        r = client.get(
            "/api/v1/town/agent/agent-1/owner-chat",
            headers=_auth_headers(),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["messages"] == history


def test_get_owner_chat_returns_403_for_non_owner(monkeypatch):
    """Endpoint returns 403 when caller is not the owner."""
    from core.services.owner_chat_service import OwnershipError
    monkeypatch.setattr(
        "core.auth.get_current_user",
        lambda: type("Auth", (), {"user_id": "user-INTRUDER"})(),
    )
    with patch(
        "routers.town.OwnerChatService.fetch_history",
        side_effect=OwnershipError("not owner"),
    ):
        client = TestClient(app)
        r = client.get(
            "/api/v1/town/agent/agent-1/owner-chat",
            headers=_auth_headers(),
        )
        assert r.status_code == 403
```

- [ ] **Step 4.2: Run tests, confirm fail**

```bash
uv run pytest tests/unit/test_owner_chat_endpoints.py::test_get_owner_chat_returns_history_for_owner -v 2>&1 | tail -10
```

Expected: FAIL with 404.

- [ ] **Step 4.3: Add the endpoint**

In `apps/backend/routers/town.py`, immediately after the `post_owner_message` endpoint:

```python
@router.get("/agent/{agent_id}/owner-chat")
async def get_owner_chat(
    agent_id: str,
    limit: int = 50,
    before: int | None = None,
    auth: AuthContext = Depends(get_current_user),
):
    """Return the owner's chat history with the agent, oldest first.

    Auth: Clerk JWT. Rejects 403 if ``agent.userId != auth.user_id``.
    ``before`` (Unix ms) pages backward.
    """
    service = OwnerChatService()
    try:
        history = service.fetch_history(
            agent_id=agent_id,
            owner_id=auth.user_id,
            limit=min(limit, 200),
            before_ms=before,
        )
    except OwnershipError:
        raise HTTPException(status_code=403, detail="not your agent")
    return {"messages": history}
```

- [ ] **Step 4.4: Run tests, confirm pass**

```bash
uv run pytest tests/unit/test_owner_chat_endpoints.py -v 2>&1 | tail -20
```

Expected: 5 PASS (3 from Task 3 + 2 new).

- [ ] **Step 4.5: Commit**

```bash
git add apps/backend/routers/town.py apps/backend/tests/unit/test_owner_chat_endpoints.py
git commit -m "feat(backend): GET /agent/<id>/owner-chat history endpoint

Clerk-auth, ownership-gated. Returns paginated message history
(oldest first) via OwnerChatService.fetch_history. Capped at 200.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Backend WS handler for daemon's `reply_owner` action

**Files:**
- Modify: `apps/backend/routers/town.py` (existing `_handle_ws_message` and `town_agent_act` dispatch)
- Modify: `apps/backend/tests/unit/test_owner_chat_endpoints.py` (add a reply_owner test)

- [ ] **Step 5.1: Find the existing town_agent_act dispatcher**

```bash
grep -n "town_agent_act\|_handle_chat\|_handle_say\|_handle_activity" apps/backend/routers/town.py | head -20
```

Locate the section where `town_agent_act` is dispatched by action name (probably a series of `if action == "...":` branches). Add `reply_owner` handling there.

- [ ] **Step 5.2: Add the test**

Append to `apps/backend/tests/unit/test_owner_chat_endpoints.py`:

```python
def test_reply_owner_action_persists_and_pushes_to_owner(monkeypatch):
    """When the daemon emits town_agent_act/reply_owner, backend persists
    the agent's reply and pushes owner_reply WS event to the owner's
    viewer connections.
    """
    from core.services.owner_chat_service import OwnerChatService
    persisted = {
        "id": "msg-2", "agentId": "agent-1", "ownerId": "user-1",
        "direction": "agent_to_owner", "text": "OK heading there",
        "createdAt": 12345,
    }
    with patch.object(OwnerChatService, "persist_message", return_value=persisted) as p_persist, \
         patch("routers.town._push_to_owner_viewers", return_value=None) as p_push:
        # Simulate the daemon WS dispatch directly. Use the internal
        # handler so we don't have to fake an API Gateway invocation.
        from routers.town import _handle_reply_owner_action
        _handle_reply_owner_action(
            agent_id="agent-1",
            owner_id="user-1",
            text="OK heading there",
            ref_message_id=None,
        )
        p_persist.assert_called_once()
        p_push.assert_called_once()
        push_args = p_push.call_args
        assert push_args.kwargs["user_id"] == "user-1"
        assert push_args.kwargs["payload"]["event"] == "owner_reply"
        assert push_args.kwargs["payload"]["agent_id"] == "agent-1"
        assert push_args.kwargs["payload"]["text"] == "OK heading there"
```

- [ ] **Step 5.3: Run, confirm fail**

```bash
uv run pytest tests/unit/test_owner_chat_endpoints.py::test_reply_owner_action_persists_and_pushes_to_owner -v 2>&1 | tail -10
```

Expected: FAIL with `cannot import _handle_reply_owner_action`.

- [ ] **Step 5.4: Add the handler + dispatcher branch**

In `apps/backend/routers/town.py`, find the existing dispatch in the WS message handler — typically a function like `_handle_town_agent_act` or similar that branches on `action`. Add a new branch:

```python
elif action == "reply_owner":
    text = data.get("text", "")
    ref_message_id = data.get("ref_message_id")
    _handle_reply_owner_action(
        agent_id=agent_id,
        owner_id=owner_id,
        text=text,
        ref_message_id=ref_message_id,
    )
```

Where `agent_id` and `owner_id` are looked up from the daemon's connection context (the WS message arrives over the agent's authenticated connection — the daemon's `town_token` carries `uid` which is the owner). Use the existing connection-lookup helper that other branches already use; if `owner_id` isn't already pulled, grep for `payload.get("uid")` or `agent_record["userId"]` to see how peer dispatchers obtain it.

Add the helper function near the other WS handlers in `routers/town.py`:

```python
def _handle_reply_owner_action(
    *,
    agent_id: str,
    owner_id: str,
    text: str,
    ref_message_id: str | None,
) -> None:
    """Persist an agent's owner-reply and push it to the owner's viewer connections."""
    if not text or not text.strip():
        logger.warning("reply_owner: empty text from agent %s — dropping", agent_id)
        return

    service = OwnerChatService()
    try:
        msg = service.persist_message(
            agent_id=agent_id,
            owner_id=owner_id,
            direction="agent_to_owner",
            text=text,
        )
    except OwnershipError:
        # Should never happen: only the agent's daemon can produce reply_owner
        # over its authenticated connection. Log and drop.
        logger.error("reply_owner: ownership mismatch agent=%s owner=%s", agent_id, owner_id)
        return

    _push_to_owner_viewers(
        user_id=owner_id,
        payload={
            "event": "owner_reply",
            "agent_id": agent_id,
            "text": msg["text"],
            "message_id": msg["id"],
            "timestamp": msg["createdAt"],
            "ref_message_id": ref_message_id,
        },
    )
```

Add `_push_to_owner_viewers` helper. If a similar function already exists for other WS broadcasts to user connections, reuse it; otherwise add:

```python
def _push_to_owner_viewers(*, user_id: str, payload: dict) -> None:
    """Push a payload to every viewer connection associated with ``user_id``.

    Looks up the user's viewer connections in the ``connections`` table and
    sends through the API Gateway Management API. No-op if the user has no
    active viewer connections (their reply will be fetched via REST history
    when they reopen the panel).
    """
    from core.services.management_api_client import ManagementApiClient
    table = dynamo.connections_table()
    # Connections table is keyed by connectionId. Scan + filter by userId
    # is OK at current scale (single-digit users); revisit with a GSI when
    # we have many concurrent viewers.
    resp = table.scan(
        FilterExpression="userId = :uid AND connectionType = :ct",
        ExpressionAttributeValues={":uid": user_id, ":ct": "viewer"},
    )
    client = ManagementApiClient()
    for item in resp.get("Items", []):
        connection_id = item["connectionId"]
        try:
            client.post_to_connection(connection_id=connection_id, payload=payload)
        except Exception as e:
            logger.warning("push_to_owner_viewers: failed cid=%s: %s", connection_id, e)
```

- [ ] **Step 5.5: Run, confirm pass**

```bash
uv run pytest tests/unit/test_owner_chat_endpoints.py -v 2>&1 | tail -15
```

Expected: 6 PASS.

- [ ] **Step 5.6: Run full suite + ruff**

```bash
uv run pytest tests/ -q 2>&1 | tail -10
uv run ruff check . && uv run ruff format --check .
```

Expected: all green.

- [ ] **Step 5.7: Commit**

```bash
git add apps/backend/routers/town.py apps/backend/tests/unit/test_owner_chat_endpoints.py
git commit -m "feat(backend): handle reply_owner WS action from agent daemon

Persists agent reply to owner_chats and pushes owner_reply event
to all of the owner's active viewer connections via the API Gateway
Management API. Dispatched by the existing town_agent_act handler.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Daemon — handle `owner_message` event

**Files:**
- Modify: `apps/backend/data/goosetown-skill/goosetown_cli.py` (the `Daemon` class)

- [ ] **Step 6.1: Locate the event dispatcher**

```bash
grep -n 'elif event ==\|def _on_\|def _handle_event' apps/backend/data/goosetown-skill/goosetown_cli.py | head -30
```

The existing dispatcher is around line 949 (an if/elif chain on `event`). Add a new branch for `owner_message`.

- [ ] **Step 6.2: Add the dispatch branch and handler**

Find this block (currently around line 950-1010):

```python
        if event == "connected":
            self._on_connected(data)
        elif event == "sprite_ready":
            self._on_sprite_ready(data)
        ...
```

Add a new branch:

```python
        elif event == "owner_message":
            self._on_owner_message(data)
```

Add the handler method on the `Daemon` class. Look at how `_on_conversation_message` is structured for the pattern. Implement:

```python
    def _on_owner_message(self, data: dict) -> None:
        """Owner sent a private message to the agent.

        Writes to OWNER_MESSAGES.md (full scrollback, last 50) and
        updates TOWN_STATUS.md with an owner-message block + think:true
        so the agent acts on it at its next heartbeat.

        Coalesces multiple messages within a 2s window into a single
        TOWN_STATUS.md update so a burst of owner messages produces
        one think cycle, not N.
        """
        import time as _time

        text = str(data.get("text", "")).strip()
        message_id = str(data.get("message_id", ""))
        timestamp = int(data.get("timestamp", _time.time() * 1000))
        if not text:
            return

        # Append to OWNER_MESSAGES.md (last 50 messages — file is the
        # agent's persistent scrollback for the channel).
        self._append_owner_message(text=text, timestamp=timestamp, message_id=message_id)

        # Buffer messages within a 2s window so a burst doesn't produce
        # N think cycles. Flush on a deferred handle.
        self._owner_message_buffer.append({"text": text, "timestamp": timestamp})
        if self._owner_flush_handle is not None:
            return  # already scheduled; the existing handle will pick up the new entry
        self._owner_flush_handle = self._loop.call_later(2.0, self._flush_owner_messages)

    def _flush_owner_messages(self) -> None:
        """Flush buffered owner messages into TOWN_STATUS.md with think:true."""
        if not self._owner_message_buffer:
            self._owner_flush_handle = None
            return
        messages = list(self._owner_message_buffer)
        self._owner_message_buffer.clear()
        self._owner_flush_handle = None

        # Update TOWN_STATUS.md by injecting an owner-message block. The
        # status writer is `_write_status` — it expects a fully-formed
        # markdown body. We re-render the current cached state with the
        # owner block prepended and think:true forced.
        self._pending_owner_messages = messages
        self._think_pending = True
        # Re-render based on most recent state snapshot so the rest of
        # TOWN_STATUS.md (location, mood, action menu) stays accurate.
        if self._last_state is not None:
            self._handle_world_update(self._last_state)
```

Add init state in `Daemon.__init__`:

```python
        self._owner_message_buffer: list[dict] = []
        self._owner_flush_handle = None
        self._pending_owner_messages: list[dict] = []
        self._think_pending: bool = False
```

Add `_append_owner_message`:

```python
    def _append_owner_message(self, *, text: str, timestamp: int, message_id: str) -> None:
        """Append a single owner message to OWNER_MESSAGES.md, capped at last 50."""
        path = self._workspace_path / "OWNER_MESSAGES.md"
        line = f"- [{timestamp}] (msg:{message_id}) **owner:** {text}\n"
        existing = path.read_text() if path.exists() else "# Owner Messages\n\n"
        # Keep the header; trim oldest if >50 entries.
        lines = [ln for ln in existing.splitlines(keepends=True) if ln.startswith("- [")]
        lines.append(line)
        if len(lines) > 50:
            lines = lines[-50:]
        path.write_text("# Owner Messages\n\n" + "".join(lines))
```

Modify `_handle_world_update` (or wherever TOWN_STATUS.md is composed) to inject an owner block + force `think:true` when `self._pending_owner_messages` is non-empty. Pseudocode (find the actual composition site by searching for `Your turn — decide what to do next` in the file):

```python
        # Inject owner-message block at top of action prompt
        owner_block = ""
        if self._pending_owner_messages:
            quoted = "\n".join(
                f"> {m['text']}" for m in self._pending_owner_messages
            )
            owner_block = (
                "\n## Your owner just said:\n"
                f"{quoted}\n\n"
                "You can reply with `python3 goosetown_cli.py act reply_owner \"<text>\"`, "
                "act on the suggestion, ignore it, or any combination.\n"
            )
            think = True
            self._pending_owner_messages = []
            self._think_pending = False
        # ... compose status_md including owner_block if present, with think:true
```

- [ ] **Step 6.3: Test the daemon handler manually**

The daemon doesn't have a unit test harness today. Run the daemon against a fixture by writing a small ad-hoc invocation:

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/backend/data/goosetown-skill
python3 -c "
from goosetown_cli import Daemon
d = Daemon.__new__(Daemon)
d._workspace_path = __import__('pathlib').Path('/tmp/owner-msg-test')
d._workspace_path.mkdir(exist_ok=True, parents=True)
d._append_owner_message(text='hello', timestamp=12345, message_id='m1')
print((d._workspace_path / 'OWNER_MESSAGES.md').read_text())
"
```

Expected: prints a header + a single line containing 'hello'.

(For full event-handling tests we'd need to mock the WebSocket connection — punted to live smoke testing.)

- [ ] **Step 6.4: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync
git add apps/backend/data/goosetown-skill/goosetown_cli.py
git commit -m "feat(daemon): handle owner_message event

Daemon writes incoming owner messages to OWNER_MESSAGES.md (last 50)
and injects an owner-message block into TOWN_STATUS.md with
think:true so the agent acts at its next heartbeat tick. Coalesces
messages within a 2s window into a single status update so a burst
produces one think cycle.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Daemon CLI — `act reply_owner` subcommand

**Files:**
- Modify: `apps/backend/data/goosetown-skill/goosetown_cli.py`

- [ ] **Step 7.1: Locate the `act` command parser**

```bash
grep -n 'def cmd_act\|sub.add_parser..act\|"reply_owner"' apps/backend/data/goosetown-skill/goosetown_cli.py | head
```

`cmd_act` is at line 625. Find the `if action == "..."` chain inside that function.

- [ ] **Step 7.2: Add `reply_owner` to the action chain**

In `cmd_act`, after the existing `elif action == "end":` branch (around line 703), add:

```python
    elif action == "reply_owner":
        if not rest:
            _die("Usage: act reply_owner <text>")
        # Args after `reply_owner` join with spaces; quotes around the
        # whole message are the user's responsibility. Optionally preserve
        # ref_message_id with --ref=<id> as the first arg.
        ref = None
        if rest and rest[0].startswith("--ref="):
            ref = rest[0][len("--ref="):]
            rest = rest[1:]
        message_text = " ".join(rest)
        payload = {
            "action": "reply_owner",
            "text": message_text,
        }
        if ref:
            payload["ref_message_id"] = ref
```

Then update the `_VALID_ACTIONS` set (or wherever the local validator is) to include `"reply_owner"`. Search for `"end"` in the function body — it's likely in a hardcoded validation set.

- [ ] **Step 7.3: Smoke-test the CLI shape**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/backend/data/goosetown-skill
python3 goosetown_cli.py act reply_owner "hello owner" 2>&1 | head -3
```

Expected: an error about no daemon (since we're not in an agent workspace), but NOT "Unknown action: reply_owner" — the action validator passed.

- [ ] **Step 7.4: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync
git add apps/backend/data/goosetown-skill/goosetown_cli.py
git commit -m "feat(daemon-cli): add 'act reply_owner <text>' subcommand

Sends {action: reply_owner, text} via the daemon WS to the backend,
where _handle_reply_owner_action persists and pushes to the owner.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Update SKILL.md to document owner DMs

**Files:**
- Modify: `apps/backend/data/goosetown-skill/SKILL.md`

- [ ] **Step 8.1: Read the current SKILL.md to find the right section**

```bash
head -100 apps/backend/data/goosetown-skill/SKILL.md
```

Find the section that documents available actions / TOWN files.

- [ ] **Step 8.2: Add owner-message documentation**

Add a new section under "Files in your workspace":

```markdown
- `OWNER_MESSAGES.md` — Private DM channel between you and your owner.
  Read this when TOWN_STATUS.md mentions "Your owner just said." Your
  owner is the human user who registered you. They can suggest things;
  you decide whether to act on them. Reply with `act reply_owner "<text>"`.
```

Add a new action under "Actions":

```markdown
- `act reply_owner "<text>"` — Send a private reply to your owner. Not
  visible to other agents or viewers; appears only in their chat panel.
```

- [ ] **Step 8.3: Commit**

```bash
git add apps/backend/data/goosetown-skill/SKILL.md
git commit -m "docs(skill): document OWNER_MESSAGES.md + reply_owner action

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Frontend `useOwnerChat` hook (TDD)

**Files:**
- Create: `apps/frontend/src/hooks/useOwnerChat.ts`
- Create: `apps/frontend/src/hooks/useOwnerChat.test.ts`
- Modify: `apps/frontend/src/hooks/useTownState.ts` (add a `window.__gt_owner_chat_event` dispatch when `owner_reply` arrives over WS)

- [ ] **Step 9.1: Write the failing tests**

Create `apps/frontend/src/hooks/useOwnerChat.test.ts`:

```ts
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';

import { useOwnerChat } from './useOwnerChat';

describe('useOwnerChat', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it('fetches history on mount', async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url.includes('/owner-chat')) {
        return new Response(JSON.stringify({
          messages: [
            { id: 'm1', text: 'hi', direction: 'owner_to_agent', createdAt: 1 },
            { id: 'm2', text: 'yo', direction: 'agent_to_owner', createdAt: 2 },
          ],
        }), { status: 200 });
      }
      return new Response('{}', { status: 200 });
    });
    vi.stubGlobal('fetch', fetchMock);

    const { result } = renderHook(() =>
      useOwnerChat({ agentId: 'agent-1', getToken: async () => 'fake' }),
    );
    await waitFor(() => {
      expect(result.current.messages.length).toBe(2);
    });
    expect(result.current.messages[0].id).toBe('m1');
    expect(result.current.messages[1].id).toBe('m2');
  });

  it('appends an owner message optimistically when send is called', async () => {
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.includes('/owner-chat') && (init?.method ?? 'GET') === 'GET') {
        return new Response(JSON.stringify({ messages: [] }), { status: 200 });
      }
      // POST /owner-message
      if (url.includes('/owner-message')) {
        return new Response(JSON.stringify({ id: 'persisted-1', createdAt: 999 }), { status: 200 });
      }
      return new Response('{}', { status: 200 });
    });
    vi.stubGlobal('fetch', fetchMock);

    const { result } = renderHook(() =>
      useOwnerChat({ agentId: 'agent-1', getToken: async () => 'fake' }),
    );
    await waitFor(() => expect(result.current.messages.length).toBe(0));

    await act(async () => {
      await result.current.send('hello');
    });

    expect(result.current.messages.length).toBe(1);
    expect(result.current.messages[0].text).toBe('hello');
    expect(result.current.messages[0].direction).toBe('owner_to_agent');
  });

  it('appends an agent reply when the WS bridge dispatches owner_reply', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ messages: [] }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    const { result } = renderHook(() =>
      useOwnerChat({ agentId: 'agent-1', getToken: async () => 'fake' }),
    );
    await waitFor(() => expect(result.current.messages.length).toBe(0));

    // Simulate the WS bridge dispatching an owner_reply event for agent-1.
    act(() => {
      window.dispatchEvent(new CustomEvent('gt_owner_chat_event', {
        detail: {
          event: 'owner_reply',
          agent_id: 'agent-1',
          text: 'OK!',
          message_id: 'reply-1',
          timestamp: 9999,
        },
      }));
    });

    expect(result.current.messages.length).toBe(1);
    expect(result.current.messages[0].text).toBe('OK!');
    expect(result.current.messages[0].direction).toBe('agent_to_owner');
  });

  it('ignores owner_reply events for a different agent', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ messages: [] }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    const { result } = renderHook(() =>
      useOwnerChat({ agentId: 'agent-1', getToken: async () => 'fake' }),
    );
    await waitFor(() => expect(result.current.messages.length).toBe(0));

    act(() => {
      window.dispatchEvent(new CustomEvent('gt_owner_chat_event', {
        detail: {
          event: 'owner_reply',
          agent_id: 'OTHER-agent',
          text: 'wrong',
          message_id: 'reply-X',
          timestamp: 9999,
        },
      }));
    });

    expect(result.current.messages.length).toBe(0);
  });
});
```

- [ ] **Step 9.2: Run, confirm fail**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/frontend
pnpm test -- useOwnerChat.test.ts 2>&1 | tail -10
```

Expected: FAIL with "Cannot find module './useOwnerChat'".

- [ ] **Step 9.3: Implement the hook**

Create `apps/frontend/src/hooks/useOwnerChat.ts`:

```ts
import { useCallback, useEffect, useRef, useState } from 'react';

const API_URL = import.meta.env.VITE_BACKEND_URL ?? 'http://localhost:8000/api/v1';

export interface OwnerChatMessage {
  id: string;
  agentId?: string;
  ownerId?: string;
  direction: 'owner_to_agent' | 'agent_to_owner';
  text: string;
  createdAt: number;
}

interface UseOwnerChatArgs {
  agentId: string;
  getToken?: () => Promise<string | null>;
}

interface UseOwnerChatResult {
  messages: OwnerChatMessage[];
  loading: boolean;
  pending: boolean;
  send: (text: string) => Promise<void>;
}

/**
 * Hook for the owner-agent DM chat panel.
 *
 * - Fetches history on mount via REST.
 * - Subscribes to WS-bridged ``owner_reply`` and ``owner_chat_action`` events
 *   via the ``gt_owner_chat_event`` CustomEvent that ``useTownState`` dispatches
 *   when the WS forwards them. Filters by ``agentId``.
 * - ``send(text)`` posts to /owner-message and appends an optimistic local
 *   message before the response arrives.
 */
export function useOwnerChat({ agentId, getToken }: UseOwnerChatArgs): UseOwnerChatResult {
  const [messages, setMessages] = useState<OwnerChatMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState(false);
  const lastSentAtRef = useRef<number>(0);

  // History fetch on mount (or agentId change).
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const token = getToken ? await getToken() : null;
      const headers: Record<string, string> = { 'Content-Type': 'application/json' };
      if (token) headers.Authorization = `Bearer ${token}`;
      try {
        const r = await fetch(`${API_URL}/town/agent/${agentId}/owner-chat`, { headers });
        if (!r.ok) {
          if (!cancelled) setLoading(false);
          return;
        }
        const body = await r.json() as { messages: OwnerChatMessage[] };
        if (!cancelled) {
          setMessages(body.messages);
          setLoading(false);
        }
      } catch {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [agentId, getToken]);

  // WS bridge listener.
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail as {
        event: string;
        agent_id: string;
        text?: string;
        message_id?: string;
        timestamp?: number;
        summary?: string;
      };
      if (detail.agent_id !== agentId) return;

      if (detail.event === 'owner_reply') {
        const reply: OwnerChatMessage = {
          id: detail.message_id ?? `reply-${Date.now()}`,
          direction: 'agent_to_owner',
          text: detail.text ?? '',
          createdAt: detail.timestamp ?? Date.now(),
        };
        setMessages((prev) => [...prev, reply]);
        setPending(false);
      }
      // owner_chat_action event renders an inline breadcrumb — same shape
      // as a message but a separate visual treatment in the panel.
      if (detail.event === 'owner_chat_action') {
        const breadcrumb: OwnerChatMessage = {
          id: `breadcrumb-${Date.now()}`,
          direction: 'agent_to_owner',
          text: `→ ${detail.summary ?? 'agent acted'}`,
          createdAt: detail.timestamp ?? Date.now(),
        };
        setMessages((prev) => [...prev, breadcrumb]);
      }
    };
    window.addEventListener('gt_owner_chat_event', handler);
    return () => {
      window.removeEventListener('gt_owner_chat_event', handler);
    };
  }, [agentId]);

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed) return;

      // Optimistic append.
      const optimistic: OwnerChatMessage = {
        id: `optimistic-${Date.now()}`,
        direction: 'owner_to_agent',
        text: trimmed,
        createdAt: Date.now(),
      };
      setMessages((prev) => [...prev, optimistic]);
      setPending(true);
      lastSentAtRef.current = Date.now();

      const token = getToken ? await getToken() : null;
      const headers: Record<string, string> = { 'Content-Type': 'application/json' };
      if (token) headers.Authorization = `Bearer ${token}`;

      try {
        const r = await fetch(`${API_URL}/town/agent/${agentId}/owner-message`, {
          method: 'POST',
          headers,
          body: JSON.stringify({ text: trimmed }),
        });
        if (!r.ok) {
          // Revert optimistic on failure.
          setMessages((prev) => prev.filter((m) => m.id !== optimistic.id));
          setPending(false);
          return;
        }
        const body = await r.json() as { id: string; createdAt: number };
        // Replace optimistic id with persisted id/timestamp.
        setMessages((prev) =>
          prev.map((m) =>
            m.id === optimistic.id
              ? { ...m, id: body.id, createdAt: body.createdAt }
              : m,
          ),
        );
        // Patience timer: clear pending if no reply arrives within 60s.
        setTimeout(() => {
          if (Date.now() - lastSentAtRef.current >= 60_000) {
            setPending(false);
          }
        }, 60_000);
      } catch {
        setMessages((prev) => prev.filter((m) => m.id !== optimistic.id));
        setPending(false);
      }
    },
    [agentId, getToken],
  );

  return { messages, loading, pending, send };
}
```

- [ ] **Step 9.4: Wire WS forwarding in `useTownState.ts`**

In `apps/frontend/src/hooks/useTownState.ts`, find the `ws.onmessage` handler. Add a branch for `owner_reply` and `owner_chat_action`:

```ts
if (msg.event === 'owner_reply' || msg.event === 'owner_chat_action') {
  window.dispatchEvent(new CustomEvent('gt_owner_chat_event', { detail: msg }));
  return;
}
```

Place this near the other `msg.event ===` branches (e.g., after `world_state` handling).

- [ ] **Step 9.5: Run tests, confirm pass**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/frontend
pnpm test -- useOwnerChat 2>&1 | tail -15
pnpm typecheck && pnpm lint
```

Expected: 4 PASS, typecheck clean, lint clean.

- [ ] **Step 9.6: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync
git add apps/frontend/src/hooks/useOwnerChat.ts apps/frontend/src/hooks/useOwnerChat.test.ts apps/frontend/src/hooks/useTownState.ts
git commit -m "feat(frontend): useOwnerChat hook + WS event bridge

Hook fetches /owner-chat history on mount, listens for
gt_owner_chat_event CustomEvents (dispatched by useTownState when the
WS forwards owner_reply/owner_chat_action), and exposes send(text) for
optimistic posting. 60s patience timer for pending state.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: `OwnerChatPanel` component

**Files:**
- Create: `apps/frontend/src/components/OwnerChatPanel.tsx`
- Create: `apps/frontend/src/components/OwnerChatPanel.test.tsx`

- [ ] **Step 10.1: Write the failing tests**

Create `apps/frontend/src/components/OwnerChatPanel.test.tsx`:

```tsx
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import OwnerChatPanel from './OwnerChatPanel';

vi.mock('../hooks/useOwnerChat', () => ({
  useOwnerChat: vi.fn(),
}));
import { useOwnerChat } from '../hooks/useOwnerChat';

const mockUseOwnerChat = useOwnerChat as ReturnType<typeof vi.fn>;

function renderPanel(props: { agentId: string; agentName: string; onClose: () => void }) {
  return render(
    <MemoryRouter>
      <OwnerChatPanel {...props} />
    </MemoryRouter>,
  );
}

describe('OwnerChatPanel', () => {
  it('renders messages', () => {
    mockUseOwnerChat.mockReturnValue({
      messages: [
        { id: 'm1', direction: 'owner_to_agent', text: 'hi', createdAt: 1 },
        { id: 'm2', direction: 'agent_to_owner', text: 'yo', createdAt: 2 },
      ],
      loading: false,
      pending: false,
      send: vi.fn(),
    });
    renderPanel({ agentId: 'a1', agentName: 'Dwight', onClose: vi.fn() });
    expect(screen.getByText('hi')).toBeInTheDocument();
    expect(screen.getByText('yo')).toBeInTheDocument();
  });

  it('shows "thinking" indicator when pending', () => {
    mockUseOwnerChat.mockReturnValue({
      messages: [],
      loading: false,
      pending: true,
      send: vi.fn(),
    });
    renderPanel({ agentId: 'a1', agentName: 'Dwight', onClose: vi.fn() });
    expect(screen.getByText(/dwight is thinking/i)).toBeInTheDocument();
  });

  it('calls send when user submits the input', () => {
    const send = vi.fn();
    mockUseOwnerChat.mockReturnValue({
      messages: [],
      loading: false,
      pending: false,
      send,
    });
    renderPanel({ agentId: 'a1', agentName: 'Dwight', onClose: vi.fn() });
    const input = screen.getByPlaceholderText(/message dwight/i);
    fireEvent.change(input, { target: { value: 'go to library' } });
    fireEvent.click(screen.getByText(/^send$/i));
    expect(send).toHaveBeenCalledWith('go to library');
  });

  it('calls onClose when close button clicked', () => {
    const onClose = vi.fn();
    mockUseOwnerChat.mockReturnValue({
      messages: [], loading: false, pending: false, send: vi.fn(),
    });
    renderPanel({ agentId: 'a1', agentName: 'Dwight', onClose });
    fireEvent.click(screen.getByRole('button', { name: /close/i }));
    expect(onClose).toHaveBeenCalled();
  });
});
```

- [ ] **Step 10.2: Run, confirm fail**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/frontend
pnpm test -- OwnerChatPanel 2>&1 | tail -10
```

Expected: FAIL with "Cannot find module './OwnerChatPanel'".

- [ ] **Step 10.3: Implement the panel**

Create `apps/frontend/src/components/OwnerChatPanel.tsx`:

```tsx
import { useState, useEffect, useRef, FormEvent } from 'react';
import { useAuth } from '@clerk/clerk-react';
import clsx from 'clsx';

import { useOwnerChat } from '../hooks/useOwnerChat';

interface OwnerChatPanelProps {
  agentId: string;
  agentName: string;
  onClose: () => void;
}

export default function OwnerChatPanel({ agentId, agentName, onClose }: OwnerChatPanelProps) {
  const { getToken } = useAuth();
  const getTokenFn = async () => await getToken();
  const { messages, loading, pending, send } = useOwnerChat({ agentId, getToken: getTokenFn });

  const [input, setInput] = useState('');
  const scrollRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to bottom on new messages.
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages.length, pending]);

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    const text = input.trim();
    if (!text) return;
    setInput('');
    void send(text);
  };

  return (
    <div className="fixed top-0 right-0 h-full w-[360px] bg-clay-900 border-l border-clay-700 flex flex-col z-30 shadow-2xl">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-clay-700 bg-clay-800">
        <h3 className="font-display text-lg text-brown-100 tracking-wider">
          {agentName}
        </h3>
        <button
          aria-label="Close"
          onClick={onClose}
          className="text-clay-300 hover:text-brown-200 text-2xl leading-none"
        >
          ×
        </button>
      </div>

      {/* Messages */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-3 space-y-2">
        {loading && <p className="font-body text-xs text-clay-300">Loading…</p>}
        {!loading && messages.length === 0 && (
          <p className="font-body text-xs text-clay-300">
            No messages yet. Say something to {agentName}.
          </p>
        )}
        {messages.map((m) => (
          <div
            key={m.id}
            className={clsx(
              'font-body text-sm rounded p-2 max-w-[280px]',
              m.direction === 'owner_to_agent'
                ? 'bg-brown-700 text-brown-100 ml-auto'
                : m.text.startsWith('→ ')
                  ? 'bg-clay-800 text-clay-300 italic'
                  : 'bg-clay-700 text-brown-200',
            )}
          >
            {m.text}
          </div>
        ))}
        {pending && (
          <p className="font-body text-xs text-clay-300 italic">
            {agentName} is thinking…
          </p>
        )}
      </div>

      {/* Footer: input */}
      <form onSubmit={onSubmit} className="border-t border-clay-700 bg-clay-800 px-4 py-3 flex gap-2">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={`Message ${agentName}…`}
          className="flex-1 bg-clay-900 text-brown-100 font-body text-sm px-3 py-2 rounded border border-clay-700 focus:outline-none focus:border-brown-500"
        />
        <button
          type="submit"
          className="px-3 py-2 bg-brown-600 hover:bg-brown-500 text-brown-100 rounded font-body text-sm transition-colors"
        >
          Send
        </button>
      </form>
    </div>
  );
}
```

- [ ] **Step 10.4: Run tests, confirm pass**

```bash
pnpm test -- OwnerChatPanel 2>&1 | tail -15
pnpm typecheck && pnpm lint
```

Expected: 4 PASS, typecheck clean, lint clean.

- [ ] **Step 10.5: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync
git add apps/frontend/src/components/OwnerChatPanel.tsx apps/frontend/src/components/OwnerChatPanel.test.tsx
git commit -m "feat(frontend): OwnerChatPanel — slide-out drawer for owner-agent DMs

360px right-side drawer with header, scrollable message log, and
input. Owner messages right-aligned brown bubbles; agent replies
left-aligned clay bubbles; action breadcrumbs italicized. 'X is
thinking…' indicator while pending. Auto-scrolls on new messages.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 11: ApartmentCard "Chat" button + App.tsx panel mount

**Files:**
- Modify: `apps/frontend/src/components/ApartmentCard.tsx`
- Modify: `apps/frontend/src/App.tsx`

- [ ] **Step 11.1: Add a Chat button to `ApartmentCard.tsx`**

Read the current file (already familiar from earlier work). Find the existing "View in X" link block. Add an onClick prop and a Chat button.

Update the component signature:

```tsx
interface ApartmentCardProps {
  agent: ApartmentAgent;
  onChatClick?: (agentId: string, agentName: string) => void;
}

export default function ApartmentCard({ agent, onChatClick }: ApartmentCardProps) {
  // ... existing body
```

Add a button next to the "View in X" link, before the closing `</div>`:

```tsx
{onChatClick && (
  <button
    onClick={() => onChatClick(agent.agent_id, agent.display_name)}
    className="font-body text-xs text-clay-300 hover:text-brown-200 transition-colors mr-3"
  >
    Chat &rarr;
  </button>
)}
```

Place this in the same row as the "View in X" link. Update the surrounding markup so the row has both buttons aligned to the right.

- [ ] **Step 11.2: Mount the panel in `App.tsx`**

In `apps/frontend/src/App.tsx`, near the other useState calls:

```tsx
const [chatAgent, setChatAgent] = useState<{ id: string; name: string } | null>(null);
```

Pass `onChatClick={(id, name) => setChatAgent({ id, name })}` down to ApartmentCard. The current sidebar component is `AgentsSidebar` — find it and thread the prop down.

Render the panel as an overlay alongside the JoinTownModal:

```tsx
{chatAgent && (
  <OwnerChatPanel
    agentId={chatAgent.id}
    agentName={chatAgent.name}
    onClose={() => setChatAgent(null)}
  />
)}
```

Add the import: `import OwnerChatPanel from './components/OwnerChatPanel.tsx';`.

- [ ] **Step 11.3: Run tests + typecheck + lint**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/frontend
pnpm test && pnpm typecheck && pnpm lint
```

Expected: all green. Existing ApartmentCard tests should still pass (the Chat button only renders when onChatClick is provided, and existing tests don't pass it).

- [ ] **Step 11.4: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync
git add apps/frontend/src/components/ApartmentCard.tsx apps/frontend/src/App.tsx
git commit -m "feat(frontend): wire ApartmentCard Chat button → OwnerChatPanel

ApartmentCard accepts an optional onChatClick prop; when provided,
renders a 'Chat →' button alongside the existing 'View in X →' link.
App.tsx mounts <OwnerChatPanel/> as an overlay, opened from the
sidebar Chat button and closed via the X in the panel header.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 12: Open PR + manual smoke test

**Files:** none

- [ ] **Step 12.1: Push and open PR**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync
git push -u origin HEAD
gh pr create --title "feat(owner-dm): private DMs between owners and their own agents" --body "$(cat <<'EOF'
## Summary

Adds a private direct-message channel between an authenticated user and the agents they own.

- **Backend**: new \`owner_chats\` DDB table; \`POST /agent/<id>/owner-message\` and \`GET /agent/<id>/owner-chat\` (Clerk-auth, ownership-gated, 1 msg / 2s rate limit); WS dispatcher for \`reply_owner\` from daemon → push \`owner_reply\` to owner's viewer connections.
- **Daemon**: \`owner_message\` event handler writes \`OWNER_MESSAGES.md\` + \`TOWN_STATUS.md\` owner block + \`think:true\`. 2s coalesce window for bursts. New \`act reply_owner <text>\` CLI subcommand.
- **Frontend**: \`useOwnerChat\` hook (REST history + WS bridge); \`OwnerChatPanel\` slide-out drawer; \`ApartmentCard\` Chat button.
- **Latency**: harness-bounded — daemon writes immediately, agent reads on its 15-30s heartbeat. Total round-trip 20-60s. UI shows a 60s patience indicator. Documented in spec.

Spec: \`docs/superpowers/specs/2026-04-30-owner-agent-dm-design.md\`
Plan: \`docs/superpowers/plans/2026-04-30-owner-agent-dm.md\`

## Test plan
- [x] Backend: \`uv run pytest\` (new owner_chat_service + endpoint tests)
- [x] Frontend: \`pnpm test\` + typecheck + lint (new useOwnerChat + OwnerChatPanel tests)
- [ ] Dev deploy → sign in → click Chat on an agent → send 'go to the library' → agent walks within 60s and replies
- [ ] Send second message within 2s → 429
- [ ] Sign out → /apartment redirects (no regression)

## Out of scope (deferred)

- Click-on-agent-in-canvas as a chat entry point (sidebar Chat button is enough for v1).
- Public visibility toggle on owner chats.
- Cross-user DMs.
- Harness-aware wake hooks to drop the 15-30s heartbeat tick from round-trip.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 12.2: Watch CI + smoke test on dev**

After CI passes and codex review approves (or user fast-merges), and dev deploys:

1. Sign in to https://dev.goosetown.isol8.co.
2. Open the apartment view sidebar — see your agents.
3. Click the new "Chat →" button on one of your agent cards.
4. Type "go to the library" and Send.
5. Within 60s, expect to see the agent walk to the library in the canvas AND a reply in the chat panel ("OK, heading there now" or similar).
6. Send another message within 2s — UI should show an error indicator (429 returned).
7. Close panel via X — verify state cleanly resets.
8. Reopen panel — history reloads from REST.

If steps 4-5 fail, debug via:
- Daemon log: `cat /tmp/goosetown/<agent>/daemon.log`
- Backend ECS logs: `aws logs filter-log-events --log-group-name "/ecs/goosetown-dev" --filter-pattern "owner_message"`
- DynamoDB: `aws dynamodb scan --table-name goosetown-dev-owner-chats`

---

## Self-Review Notes

**Spec coverage:**
- DDB table: Task 1 ✅
- Service module: Task 2 ✅
- POST endpoint: Task 3 ✅
- GET endpoint: Task 4 ✅
- reply_owner WS dispatch: Task 5 ✅
- Daemon owner_message handler: Task 6 ✅
- Daemon CLI reply_owner: Task 7 ✅
- SKILL.md docs: Task 8 ✅
- Frontend hook: Task 9 ✅
- Frontend panel: Task 10 ✅
- Sidebar wiring: Task 11 ✅
- PR + smoke: Task 12 ✅

**Out-of-spec scope check:**
- No click-on-agent-in-canvas. Spec listed it as a secondary entry point; deferring to follow-up so v1 ships with one clean entry (sidebar Chat button).
- No public-visibility toggle (matches non-goal).
- No cross-user DMs (matches non-goal).
- No harness wake hooks (matches "out of scope" follow-up).

**Type consistency:**
- `OwnerChatService` methods: `persist_message(agent_id, owner_id, direction, text)` and `fetch_history(agent_id, owner_id, limit, before_ms)`. Used consistently across Tasks 2, 3, 4, 5.
- `OwnershipError` and `RateLimitError` used in Tasks 2, 3, 5.
- `direction` literal: `'owner_to_agent'` and `'agent_to_owner'`. Used consistently.
- `OwnerChatMessage` TS type: `id`, `direction`, `text`, `createdAt`. Used consistently across hook and panel.
- WS event names: `owner_message` (backend → daemon), `owner_reply` (backend → owner viewer), `owner_chat_action` (backend → owner viewer). Used consistently.
- daemon WS action: `reply_owner`. Used consistently.

No placeholders, no TODOs.
