# Ship-Ready Sweep + Make Agents Feel Alive Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix critical backend bugs, add router test coverage, make agents visually expressive through activity animations and speech bubble polish, and upgrade conversations to support N participants via proximity-based joining.

**Architecture:** Three sequential phases. Phase D (ship-ready sweep) hardens the backend. Phase B (alive) adds visual life to the Godot viewer. Phase B+ (group conversations) migrates the conversation data model from 2-party to N-party with additive migration. Each phase is independently shippable.

**Tech Stack:** Python 3.12 / FastAPI / pytest / boto3 / GDScript 4.6 / Godot shader language / React 18 / TypeScript

---

## Phase D: Ship-Ready Sweep

### Task D1: Replace debug print with logger

**Files:**
- Modify: `apps/backend/routers/town.py:1216`

- [ ] **Step 1: Replace the print statement**

Change line 1216 from:
```python
print(f"[WS_MSG] town_agent_act: action={action}, agent={agent_conn.agent_name}, ctx={agent_conn.location_context}", flush=True)
```
To:
```python
logger.debug("town_agent_act: action=%s agent=%s ctx=%s", action, agent_conn.agent_name, agent_conn.location_context)
```

- [ ] **Step 2: Verify no other print() in routers/town.py**

Run: `grep -n 'print(' apps/backend/routers/town.py`
Expected: No output.

- [ ] **Step 3: Commit**

```bash
git add apps/backend/routers/town.py
git commit -m "fix: replace debug print with logger in town_agent_act handler"
```

---

### Task D2: Game server WebSocket connection timeout

**Files:**
- Modify: `apps/backend/core/services/game_server_client.py:92,356`
- Test: `apps/backend/tests/test_game_server_client.py`

- [ ] **Step 1: Write failing test**

Add to `apps/backend/tests/test_game_server_client.py`:
```python
import asyncio
import pytest
from unittest.mock import AsyncMock, patch
from core.services.game_server_client import GameServerClient


@pytest.mark.asyncio
async def test_connect_agent_times_out_if_unreachable(monkeypatch):
    """connect_agent should raise TimeoutError within 10s if websocket never completes."""
    async def _hang(*args, **kwargs):
        await asyncio.sleep(60)  # Simulate unresponsive server

    with patch("core.services.game_server_client.websockets.connect", new=AsyncMock(side_effect=_hang)):
        client = GameServerClient("ws://localhost:8080")
        with pytest.raises((asyncio.TimeoutError, TimeoutError)):
            await asyncio.wait_for(
                client.connect_agent(
                    agent_id="test",
                    display_name="Test",
                    sprite_url="",
                    location="plaza",
                    context="town",
                    user_id="user1",
                ),
                timeout=12.0,
            )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/test_game_server_client.py::test_connect_agent_times_out_if_unreachable -v`
Expected: FAIL (test times out at 12s because connect has no open_timeout).

- [ ] **Step 3: Add open_timeout to websockets.connect calls**

In `game_server_client.py`, change line 92 from:
```python
ws = await websockets.connect(self._base_url)
```
To:
```python
ws = await websockets.connect(self._base_url, open_timeout=10)
```

And line 356 (in `_reconnect_agent`) from:
```python
ws = await websockets.connect(self._base_url)
```
To:
```python
ws = await websockets.connect(self._base_url, open_timeout=10)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/test_game_server_client.py::test_connect_agent_times_out_if_unreachable -v`
Expected: PASS within 11 seconds.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/game_server_client.py apps/backend/tests/test_game_server_client.py
git commit -m "fix: add 10s open_timeout to game server WebSocket connections"
```

---

### Task D3: Auth on /ws-message endpoint

**Files:**
- Modify: `apps/backend/core/config.py`
- Modify: `apps/backend/routers/town.py` (around the `/ws-message` endpoint)
- Test: `apps/backend/tests/test_routers_town.py` (new file)

- [ ] **Step 1: Add WS_LAMBDA_SECRET to Settings**

In `apps/backend/core/config.py`, inside the `Settings` class, add after line 21:
```python
    WS_LAMBDA_SECRET: str = os.getenv("WS_LAMBDA_SECRET", "")
```

- [ ] **Step 2: Write failing test**

Create `apps/backend/tests/test_routers_town.py`:
```python
"""Integration tests for /ws-message endpoint auth."""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("WS_LAMBDA_SECRET", "test-secret")
    from importlib import reload
    from core import config
    reload(config)
    from main import app
    return TestClient(app)


def test_ws_message_rejects_missing_secret(client):
    resp = client.post(
        "/api/v1/town/ws-message",
        json={"connectionId": "c1", "type": "town_agent_connect", "body": {}},
    )
    assert resp.status_code == 403


def test_ws_message_rejects_wrong_secret(client):
    resp = client.post(
        "/api/v1/town/ws-message",
        headers={"X-WS-Lambda-Secret": "nope"},
        json={"connectionId": "c1", "type": "town_agent_connect", "body": {}},
    )
    assert resp.status_code == 403


def test_ws_message_accepts_correct_secret(client):
    with patch("routers.town.TownService"), patch("routers.town.get_town_agent_ws_manager"):
        resp = client.post(
            "/api/v1/town/ws-message",
            headers={"X-WS-Lambda-Secret": "test-secret"},
            json={"connectionId": "c1", "type": "unknown_type", "body": {}},
        )
    # 200 with unknown type is fine — we just want to confirm auth passed
    assert resp.status_code == 200
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/test_routers_town.py -v`
Expected: FAIL (all three tests fail because no auth exists yet).

- [ ] **Step 4: Add auth check to /ws-message handler**

In `apps/backend/routers/town.py`, find the `/ws-message` endpoint handler (search for `@router.post("/ws-message")`). At the start of the function body (before any existing logic), add:

```python
    from fastapi import Header, HTTPException
    from core.config import settings

    # In non-dev environments, require a shared secret header from the API Gateway Lambda.
    # In dev, skip the check if WS_LAMBDA_SECRET is unset (local dev ergonomics).
    if settings.ENVIRONMENT != "dev" or settings.WS_LAMBDA_SECRET:
        provided = request.headers.get("x-ws-lambda-secret", "")
        if provided != settings.WS_LAMBDA_SECRET:
            raise HTTPException(status_code=403, detail="Invalid or missing WS lambda secret")
```

You'll need to ensure the endpoint signature accepts `request: Request` from FastAPI. If it already takes a Pydantic body model, add `request: Request` as an additional parameter:
```python
from fastapi import Request

@router.post("/ws-message")
async def ws_message(request: Request, body: WsMessageRequest = Body(...)):
    # ... auth check above ...
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/test_routers_town.py -v`
Expected: PASS all three tests.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/routers/town.py apps/backend/core/config.py apps/backend/tests/test_routers_town.py
git commit -m "feat: require shared secret header on /ws-message endpoint"
```

---

### Task D4: Paginate get_all_relationships scan

**Files:**
- Modify: `apps/backend/core/services/town_service.py:558-593`
- Test: `apps/backend/tests/test_town_service.py`

- [ ] **Step 1: Write failing test**

Add to `apps/backend/tests/test_town_service.py`:
```python
def test_get_all_relationships_paginates(mock_dynamo):
    """Should fetch all records across multiple scan pages."""
    from core.services.town_service import TownService

    # Seed 250 relationships (exceeds single scan limit of 100)
    rels_table = mock_dynamo.relationships_table()
    for i in range(250):
        rels_table.put_item(Item={
            "agentAId": f"a-{i:03d}",
            "agentBId": f"b-{i:03d}",
            "relationshipType": "friend",
            "interactionCount": 1,
        })

    service = TownService()
    result = service.get_all_relationships()
    assert len(result) == 250
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/test_town_service.py::test_get_all_relationships_paginates -v`
Expected: FAIL (returns 100, expected 250).

- [ ] **Step 3: Replace scan with pagination loop**

In `apps/backend/core/services/town_service.py`, replace the body of `get_all_relationships` (lines 558-593) with:

```python
    def get_all_relationships(self) -> list[dict]:
        """Get all relationships with latest conversation topics. Paginated."""
        table = dynamo.relationships_table()
        rels: list[dict] = []
        scan_kwargs: dict = {}
        max_total = 500  # Safety cap
        while True:
            resp = table.scan(**scan_kwargs)
            rels.extend(resp.get("Items", []))
            if len(rels) >= max_total:
                rels = rels[:max_total]
                break
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break
            scan_kwargs["ExclusiveStartKey"] = last_key

        conv_table = dynamo.conversations_table()
        out = []
        for r in rels:
            r = _decimal_to_native(r)
            a_id = r["agentAId"]
            b_id = r["agentBId"]

            last_topic = None
            conv_resp = conv_table.query(
                IndexName="participantA-endedAt-index",
                KeyConditionExpression=Key("participantAId").eq(a_id),
                FilterExpression=Attr("participantBId").eq(b_id) & Attr("status").eq("ended"),
                ScanIndexForward=False,
                Limit=1,
            )
            items = conv_resp.get("Items", [])
            if items:
                last_topic = items[0].get("topicSummary")

            out.append(
                {
                    "agent_a_id": a_id,
                    "agent_b_id": b_id,
                    "relationship_type": r.get("relationshipType"),
                    "interaction_count": r.get("interactionCount", 0),
                    "last_topic": last_topic,
                }
            )
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/test_town_service.py::test_get_all_relationships_paginates -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/town_service.py apps/backend/tests/test_town_service.py
git commit -m "fix: paginate get_all_relationships scan with 500-record cap"
```

---

### Task D5: Paginate viewer relay seed_from_dynamodb

**Files:**
- Modify: `apps/backend/core/services/viewer_relay.py:40-59`

- [ ] **Step 1: Replace scan with pagination loop**

In `apps/backend/core/services/viewer_relay.py`, replace the body of `seed_from_dynamodb` (lines 40-59) with:

```python
    def seed_from_dynamodb(self) -> None:
        """Read all viewer connections from DynamoDB to seed the in-memory set.

        Paginated — walks the table until all viewer-type connections are loaded.
        """
        try:
            import core.dynamo as dynamo

            table = dynamo.connections_table()
            scan_kwargs: dict = {
                "FilterExpression": "connectionType = :t",
                "ExpressionAttributeValues": {":t": "viewer"},
            }
            total = 0
            while True:
                resp = table.scan(**scan_kwargs)
                for item in resp.get("Items", []):
                    conn_id = item.get("connectionId")
                    if conn_id:
                        self._viewer_connections.add(conn_id)
                        total += 1
                last_key = resp.get("LastEvaluatedKey")
                if not last_key:
                    break
                scan_kwargs["ExclusiveStartKey"] = last_key
            logger.info("Seeded %d viewer connections from DynamoDB", total)
        except Exception as e:
            logger.warning("Failed to seed viewer connections from DynamoDB: %s", e)
```

- [ ] **Step 2: Verify import and run existing tests**

Run: `cd apps/backend && uv run pytest tests/test_viewer_relay.py -v`
Expected: PASS (existing tests still pass).

- [ ] **Step 3: Commit**

```bash
git add apps/backend/core/services/viewer_relay.py
git commit -m "fix: paginate viewer relay seed_from_dynamodb scan"
```

---

### Task D6: Config validation — fail fast

**Files:**
- Modify: `apps/backend/core/config.py:23-38`
- Test: `apps/backend/tests/test_config.py` (new)

- [ ] **Step 1: Write failing test**

Create `apps/backend/tests/test_config.py`:
```python
"""Configuration validation tests."""
import os
import pytest
from importlib import reload


def _fresh_settings():
    from core import config
    reload(config)
    return config.Settings()


def test_dev_mode_skips_validation(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    s = _fresh_settings()
    # Should not raise even with nothing set
    s.validate()


def test_prod_raises_on_missing_vars(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.delenv("TOWN_TOKEN_SECRET", raising=False)
    monkeypatch.delenv("WS_MANAGEMENT_API_URL", raising=False)
    monkeypatch.delenv("WS_LAMBDA_SECRET", raising=False)
    monkeypatch.delenv("GAME_SERVER_URL", raising=False)
    monkeypatch.delenv("SPRITE_S3_BUCKET", raising=False)
    monkeypatch.delenv("SPRITE_CDN_URL", raising=False)
    s = _fresh_settings()
    with pytest.raises(RuntimeError, match="Missing required environment variables"):
        s.validate()


def test_prod_passes_with_all_vars_set(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("TOWN_TOKEN_SECRET", "x")
    monkeypatch.setenv("WS_MANAGEMENT_API_URL", "https://mgmt")
    monkeypatch.setenv("WS_LAMBDA_SECRET", "x")
    monkeypatch.setenv("GAME_SERVER_URL", "ws://gs")
    monkeypatch.setenv("SPRITE_S3_BUCKET", "b")
    monkeypatch.setenv("SPRITE_CDN_URL", "https://cdn")
    monkeypatch.setenv("CLERK_ISSUER", "https://clerk")
    s = _fresh_settings()
    s.validate()
```

- [ ] **Step 2: Run test to verify 2 of 3 fail**

Run: `cd apps/backend && uv run pytest tests/test_config.py -v`
Expected: `test_dev_mode_skips_validation` passes; the other two fail because validation currently only checks 3 vars.

- [ ] **Step 3: Expand validate() method**

In `apps/backend/core/config.py`, replace lines 23-38 with:

```python
    def validate(self) -> None:
        """Validate required env vars. Raises RuntimeError in non-dev environments if critical vars are missing."""
        if self.ENVIRONMENT == "dev":
            return

        required = {
            "TOWN_TOKEN_SECRET": self.TOWN_TOKEN_SECRET,
            "WS_MANAGEMENT_API_URL": self.WS_MANAGEMENT_API_URL,
            "WS_LAMBDA_SECRET": self.WS_LAMBDA_SECRET,
            "GAME_SERVER_URL": self.GAME_SERVER_URL,
            "SPRITE_S3_BUCKET": self.SPRITE_S3_BUCKET,
            "SPRITE_CDN_URL": self.SPRITE_CDN_URL,
            "CLERK_ISSUER": self.CLERK_ISSUER,
        }
        missing = [name for name, val in required.items() if not val]
        if missing:
            raise RuntimeError(
                f"Missing required environment variables for {self.ENVIRONMENT}: {', '.join(missing)}"
            )
        logger.info("Configuration validated for environment: %s", self.ENVIRONMENT)
```

Note: `GAME_SERVER_URL` has a default of `"ws://localhost:8080"` — in prod we want to require an explicit value. Change line 21 of `config.py` to:
```python
    GAME_SERVER_URL: str = os.getenv("GAME_SERVER_URL", "")
```
And if you need a local dev fallback, handle it at the call site in `main.py` lifespan startup instead.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/test_config.py -v`
Expected: PASS all three tests.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/config.py apps/backend/tests/test_config.py
git commit -m "feat: expand config validation to cover all required prod env vars"
```

---

### Task D7: Verify sprite generation race condition check

**Files:**
- Read-only: `apps/backend/routers/town.py:745-751` (already implemented)
- Test: `apps/backend/tests/test_routers_town.py`

**Context:** Reading the current code shows the race condition check already exists at lines 745-751. We just need a test to lock in the behavior.

- [ ] **Step 1: Write test for race condition**

Add to `apps/backend/tests/test_routers_town.py`:
```python
def test_register_skips_sprite_pipeline_if_already_in_progress(client, monkeypatch):
    """If an agent already has spriteStatus=creating, re-registration must not start a second pipeline."""
    from unittest.mock import MagicMock, AsyncMock
    import routers.town as town_module

    mock_service = MagicMock()
    mock_service.get_active_instance.return_value = {"id": "i1", "userId": "u1"}
    mock_service.get_agent_by_name.return_value = None
    mock_service.opt_in.return_value = {"id": "agent-1", "agentName": "a1", "displayName": "A"}
    mock_service.get_town_agent_by_id.return_value = {"id": "agent-1", "spriteStatus": "creating"}

    pipeline_started = False
    async def fake_pipeline(*args, **kwargs):
        nonlocal pipeline_started
        pipeline_started = True

    monkeypatch.setattr(town_module, "TownService", lambda: mock_service)
    monkeypatch.setattr(town_module, "_sprite_pipeline_task", fake_pipeline)

    with patch("routers.town.get_town_token_user", return_value=("u1", "tok")):
        resp = client.post(
            "/api/v1/town/agent/register",
            headers={"Authorization": "Bearer tok"},
            json={
                "agent_name": "a1",
                "display_name": "A",
                "personality": "",
                "appearance": "a wizard",
                "traits": "",
            },
        )

    # In-progress detection should short-circuit: pipeline should NOT be re-started
    assert pipeline_started is False
```

- [ ] **Step 2: Run test — it should pass because the check already exists**

Run: `cd apps/backend && uv run pytest tests/test_routers_town.py::test_register_skips_sprite_pipeline_if_already_in_progress -v`
Expected: PASS. If it fails, the race-condition check at `routers/town.py:745-751` has regressed — investigate why.

- [ ] **Step 3: Commit**

```bash
git add apps/backend/tests/test_routers_town.py
git commit -m "test: lock in sprite pipeline race condition check"
```

---

### Task D8: Verify game server reconnect max retries

**Files:**
- Read-only: `apps/backend/core/services/game_server_client.py:341` (already has `max_retries=5`)
- Test: `apps/backend/tests/test_game_server_client.py`

**Context:** The reconnect loop at line 341 already has `max_retries: int = 5`. We just need a test to lock this in and make sure it doesn't regress to infinite.

- [ ] **Step 1: Write test for bounded reconnect**

Add to `apps/backend/tests/test_game_server_client.py`:
```python
@pytest.mark.asyncio
async def test_reconnect_stops_after_max_retries(monkeypatch):
    """Reconnect should give up after max_retries and clean up connect params."""
    from core.services.game_server_client import GameServerClient

    client = GameServerClient("ws://localhost:8080")
    client._agent_connect_params["a1"] = {"type": "agent", "agent_id": "a1"}

    attempt_count = 0
    async def always_fail(*args, **kwargs):
        nonlocal attempt_count
        attempt_count += 1
        raise ConnectionRefusedError("server down")

    monkeypatch.setattr("core.services.game_server_client.websockets.connect", always_fail)

    await client._reconnect_agent("a1", max_retries=3)

    assert attempt_count == 3
    assert "a1" not in client._agent_connect_params
    assert "a1" not in client._agent_connections
```

- [ ] **Step 2: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/test_game_server_client.py::test_reconnect_stops_after_max_retries -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add apps/backend/tests/test_game_server_client.py
git commit -m "test: lock in game server reconnect max retries bound"
```

---

### Task D9: Router integration tests

**Files:**
- Modify: `apps/backend/tests/test_routers_town.py`

- [ ] **Step 1: Add chat handler test**

Add to `apps/backend/tests/test_routers_town.py`:
```python
@pytest.mark.asyncio
async def test_ws_message_chat_rejects_target_busy(monkeypatch):
    """_handle_chat should reject when target already in a conversation."""
    from unittest.mock import MagicMock, AsyncMock
    import routers.town as town_module

    mock_service = MagicMock()
    mock_service.get_town_agent_by_id.return_value = {
        "id": "a1", "agentName": "alice", "displayName": "Alice", "currentLocation": "plaza",
    }
    mock_service.get_agent_by_agent_name_active.return_value = {
        "id": "b1", "agentName": "bob", "displayName": "Bob", "currentConversationId": "existing",
    }

    sent_messages = []
    mock_mgmt = MagicMock()
    mock_mgmt.send_message.side_effect = lambda cid, msg: sent_messages.append(msg)

    monkeypatch.setattr(town_module, "TownService", lambda: mock_service)
    monkeypatch.setattr(
        "core.services.management_api_client.ManagementApiClient",
        lambda: mock_mgmt,
    )

    agent_conn = MagicMock(agent_id="a1", agent_name="alice")
    await town_module._handle_chat(
        "conn1",
        {"target": "bob", "message": "hi"},
        agent_conn,
    )

    assert any(m.get("event") == "chat_rejected" and m.get("reason") == "target_busy" for m in sent_messages)
```

- [ ] **Step 2: Add say handler test**

```python
@pytest.mark.asyncio
async def test_ws_message_say_rejects_wrong_conversation(monkeypatch):
    """_handle_say should reject when conv_id doesn't match speaker's current conversation."""
    from unittest.mock import MagicMock
    import routers.town as town_module

    mock_service = MagicMock()
    mock_service.get_town_agent_by_id.return_value = {
        "id": "a1", "currentConversationId": "conv-A",
    }
    mock_service.get_conversation_by_id.return_value = None  # conv-B not found

    sent_messages = []
    mock_mgmt = MagicMock()
    mock_mgmt.send_message.side_effect = lambda cid, msg: sent_messages.append(msg)

    monkeypatch.setattr(town_module, "TownService", lambda: mock_service)
    monkeypatch.setattr(
        "core.services.management_api_client.ManagementApiClient",
        lambda: mock_mgmt,
    )

    agent_conn = MagicMock(agent_id="a1", agent_name="alice")
    await town_module._handle_say(
        "conn1",
        {"conv_id": "conv-B", "message": "hi"},
        agent_conn,
    )

    assert any(m.get("event") == "say_rejected" for m in sent_messages)
```

- [ ] **Step 3: Add register duplicate name test**

```python
def test_register_duplicate_name_rejected(client, monkeypatch):
    from unittest.mock import MagicMock
    import routers.town as town_module

    mock_service = MagicMock()
    mock_service.get_active_instance.return_value = {"id": "i1", "userId": "u1"}
    mock_service.get_agent_by_name.return_value = {"id": "existing", "agentName": "a1"}

    monkeypatch.setattr(town_module, "TownService", lambda: mock_service)
    with patch("routers.town.get_town_token_user", return_value=("u1", "tok")):
        resp = client.post(
            "/api/v1/town/agent/register",
            headers={"Authorization": "Bearer tok"},
            json={"agent_name": "a1", "display_name": "A", "personality": "", "appearance": "w", "traits": ""},
        )

    assert resp.status_code == 400
    assert "already registered" in resp.json()["detail"]
```

- [ ] **Step 4: Run all router tests**

Run: `cd apps/backend && uv run pytest tests/test_routers_town.py -v`
Expected: PASS all tests.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/tests/test_routers_town.py
git commit -m "test: add router integration tests for chat/say/register handlers"
```

---

### Task D10: Update infra for WS_LAMBDA_SECRET

**Files:**
- Modify: `apps/infra/lib/stacks/auth-stack.ts`
- Modify: `apps/infra/lib/stacks/compute-stack.ts`
- Modify: `apps/infra/lib/stacks/api-stack.ts`

- [ ] **Step 1: Add WS_LAMBDA_SECRET to AuthStack**

In `apps/infra/lib/stacks/auth-stack.ts`, find the block where other secrets are created (`new secretsmanager.Secret(...)`). Add a new secret:

```typescript
this.wsLambdaSecret = new secretsmanager.Secret(this, 'WsLambdaSecret', {
  secretName: `goosetown/${props.environment}/ws_lambda_secret`,
  description: 'Shared secret for WebSocket Lambda → backend /ws-message auth',
  generateSecretString: {
    passwordLength: 64,
    excludePunctuation: true,
  },
  removalPolicy: props.environment === 'prod' ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
});
```

Add `wsLambdaSecret: secretsmanager.Secret` to the class as a public property (follow the pattern used for existing secrets).

- [ ] **Step 2: Inject into backend container env**

In `apps/infra/lib/stacks/compute-stack.ts`, find where secrets are injected into the task definition. Add:

```typescript
WS_LAMBDA_SECRET: ecs.Secret.fromSecretsManager(props.authStack.wsLambdaSecret),
```

- [ ] **Step 3: Inject into WebSocket $default Lambda env**

In `apps/infra/lib/stacks/api-stack.ts`, find where the `$default` Lambda is defined and where its environment vars are set. Add:

```typescript
WS_LAMBDA_SECRET_ARN: props.authStack.wsLambdaSecret.secretArn,
```

Also grant the Lambda read permission:
```typescript
props.authStack.wsLambdaSecret.grantRead(defaultLambda);
```

The Lambda's Python code should read the secret via `secretsmanager.get_secret_value()` at cold start and include it in the `X-WS-Lambda-Secret` header when POSTing to `/ws-message`. Find the existing Lambda handler code in `api-stack.ts` (inline) and add:

```python
# At module-level (cold start)
_ws_lambda_secret = None
def _get_ws_secret():
    global _ws_lambda_secret
    if _ws_lambda_secret is None:
        import boto3
        sm = boto3.client('secretsmanager')
        arn = os.environ.get('WS_LAMBDA_SECRET_ARN', '')
        if arn:
            _ws_lambda_secret = sm.get_secret_value(SecretId=arn)['SecretString']
    return _ws_lambda_secret or ''

# In the handler, when POSTing to /ws-message:
headers['X-WS-Lambda-Secret'] = _get_ws_secret()
```

- [ ] **Step 4: Synth to verify it compiles**

Run: `cd apps/infra && rm -rf ../backend/.venv && npx cdk synth 2>&1 | tail -20`
Expected: No TypeScript errors. CloudFormation template generates.

- [ ] **Step 5: Commit**

```bash
git add apps/infra/lib/stacks/auth-stack.ts apps/infra/lib/stacks/compute-stack.ts apps/infra/lib/stacks/api-stack.ts
git commit -m "feat: provision WS_LAMBDA_SECRET for /ws-message auth"
```

---

## Phase B: Make Agents Feel Alive

### Task B1: Activity animation state machine

**Files:**
- Modify: `apps/godot/scripts/agent.gd:154-185`
- Modify: `apps/godot/scripts/agent_manager.gd`

**Context:** Reading `agent.gd:154-185` shows the animation logic is already in `_physics_process`. It reads `current_activity`, maps via `ACTIVITY_ANIMATION`, and switches spritesheets. What's missing: `agent_manager.gd` is already passing `current_activity` (line 100-102 of agent_manager.gd), but the activity string coming from the backend uses the activity *action name* (e.g., `people_watch`), not `walking` or `idle`. The skip logic `if initial_activity != "walking" and initial_activity != "idle"` at line 135 is correct, but the existing check isn't doing anything new — the mechanism works. The remaining problem is that when activity clears (back to ""), the agent stays stuck. Let me verify and add an explicit test script.

- [ ] **Step 1: Read agent.gd:154-185 to confirm current behavior**

Run: `grep -n 'current_activity' apps/godot/scripts/agent.gd`
Expected: See references at the physics_process animation block.

- [ ] **Step 2: Fix idle fallback when activity clears**

In `apps/godot/scripts/agent.gd`, find the animation selection block (around line 156-165). Replace with:

```gdscript
		# Determine target animation from activity or movement
		var target_anim: String = "walk"
		if current_activity != "" and current_activity in ACTIVITY_ANIMATION:
			var desired: String = ACTIVITY_ANIMATION[current_activity]
			if _spritesheets.has(desired):
				target_anim = desired
			elif _spritesheets.has("idle"):
				target_anim = "idle"
		elif _is_moving:
			# Moving with no activity — walk
			target_anim = "walk"
		else:
			# Stationary with no activity — prefer idle if loaded
			target_anim = "idle" if _spritesheets.has("idle") else "walk"
```

- [ ] **Step 3: Update agent_manager.gd to clear current_activity on empty string**

In `apps/godot/scripts/agent_manager.gd`, find the activity update block (around lines 97-102). Replace with:

```gdscript
			# Update activity animation state
			var activity: String = str(agent_data.get("activity", ""))
			if activity != "" and activity != "walking" and activity != "idle":
				agents[agent_id].current_activity = activity
			else:
				agents[agent_id].current_activity = ""
```

(This ensures that when the backend clears activity at conversation-end, the agent returns to idle.)

- [ ] **Step 4: Export Godot .pck**

Run: `cd apps/godot && ./export.sh 2>&1 | tail -10 || echo "Run Godot editor to export manually"`
Expected: `apps/frontend/public/godot/goosetown.pck` updated.

If export.sh doesn't exist, open the project in Godot editor and run File → Export → HTML5 to `apps/frontend/public/godot/`.

- [ ] **Step 5: Manual verify in browser**

Start frontend: `cd apps/frontend && pnpm dev`
Navigate to localhost:5173. Wait for an agent to do an activity. Confirm the sprite switches to an activity-appropriate animation.

- [ ] **Step 6: Commit**

```bash
git add apps/godot/scripts/agent.gd apps/godot/scripts/agent_manager.gd apps/frontend/public/godot/
git commit -m "feat: agent activity animation state machine with idle fallback"
```

---

### Task B2: Smooth activity transitions

**Files:**
- Modify: `apps/godot/scripts/agent.gd`

- [ ] **Step 1: Add transition buffer to play_animation**

In `apps/godot/scripts/agent.gd`, find the `play_animation` function (around line 257). Replace with:

```gdscript
func play_animation(animation_name: String) -> void:
	if animation_name == _current_animation:
		return
	if not _spritesheets.has(animation_name):
		return
	# If switching to an activity animation from walk, insert a brief idle pause
	# (2 frames at idle fps) to prevent a popping transition.
	var was_walk: bool = _current_animation == "walk"
	var going_to_activity: bool = animation_name != "walk" and animation_name != "idle"
	_current_animation = animation_name
	_anim_frame = 0
	if was_walk and going_to_activity and _spritesheets.has("idle"):
		# Force a brief idle render before the activity starts
		_anim_timer = -0.2  # Negative timer delays first frame advance
	else:
		_anim_timer = 0.0
	_update_sprite_frame()
```

- [ ] **Step 2: Export and verify**

Run: `cd apps/godot && ./export.sh 2>&1 | tail -10 || echo "Run Godot editor"`
Then: `cd apps/frontend && pnpm dev` and confirm transitions feel less abrupt.

- [ ] **Step 3: Commit**

```bash
git add apps/godot/scripts/agent.gd apps/frontend/public/godot/
git commit -m "feat: smooth activity transition with 200ms idle buffer"
```

---

### Task B3: Speech bubble polish

**Files:**
- Modify: `apps/godot/scenes/agent.tscn`
- Modify: `apps/godot/scripts/agent.gd`

- [ ] **Step 1: Add word-wrap and autosize in scene**

Open `apps/godot/scenes/agent.tscn` in Godot editor. Select `SpeechBubbleContainer/SpeechText` node. Set:
- `autowrap_mode` = `AUTOWRAP_WORD_SMART`
- `custom_minimum_size.x` = `150`

Select `SpeechBubbleContainer` node. Set:
- `z_index` = `10`

Save the scene.

Alternatively, edit the .tscn text directly — find the `[node name="SpeechText"` block and add:
```
autowrap_mode = 3
custom_minimum_size = Vector2(150, 0)
```

And in the `[node name="SpeechBubbleContainer"` block add:
```
z_index = 10
```

- [ ] **Step 2: Add fade tween to speech bubble in agent.gd**

In `apps/godot/scripts/agent.gd`, replace `update_state` (around line 216) with:

```gdscript
func update_state(data: Dictionary) -> void:
	var text: String = str(data.get("speechText", ""))
	if text != "" and text != speech_text:
		speech_text = text
		speech_text_label.text = speech_text
		_show_speech_bubble()
		if nameplate:
			nameplate.visible = false
	elif text == "":
		speech_text = ""
		_hide_speech_bubble()


func _show_speech_bubble() -> void:
	if speech_container == null:
		return
	speech_container.modulate.a = 0.0
	speech_container.visible = true
	var tween: Tween = create_tween()
	tween.tween_property(speech_container, "modulate:a", 1.0, 0.3)


func _hide_speech_bubble() -> void:
	if speech_container == null or not speech_container.visible:
		return
	var tween: Tween = create_tween()
	tween.tween_property(speech_container, "modulate:a", 0.0, 0.3)
	tween.tween_callback(func(): speech_container.visible = false)
```

- [ ] **Step 3: Export and verify**

Run the frontend and confirm:
- Long speech text wraps cleanly
- Bubble fades in/out smoothly
- Bubble renders above other agents when they overlap

- [ ] **Step 4: Commit**

```bash
git add apps/godot/scenes/agent.tscn apps/godot/scripts/agent.gd apps/frontend/public/godot/
git commit -m "feat: speech bubble word-wrap, fade, and z-order polish"
```

---

### Task B5: Idle variety

**Files:**
- Modify: `apps/godot/scripts/agent.gd`

- [ ] **Step 1: Add idle bob to agent**

In `apps/godot/scripts/agent.gd`, add these member variables near the other private vars (around line 88):

```gdscript
var _idle_bob_timer: float = 0.0
var _idle_bob_offset: float = 0.0
```

Then in `_physics_process` (after the animation block, around line 185), add:

```gdscript
	# Idle bob — subtle vertical sway when agent is stationary and idle
	if not _is_moving and (_current_animation == "idle" or _current_animation == "walk"):
		_idle_bob_timer += delta
		_idle_bob_offset = sin(_idle_bob_timer * 2.0) * 1.0
		sprite.position.y = -16 + _idle_bob_offset
	else:
		_idle_bob_offset = 0.0
		sprite.position.y = -16
```

- [ ] **Step 2: Export and verify**

Run the frontend and confirm idle agents have a subtle bob. It should be barely noticeable — 1 pixel up/down every ~3 seconds.

- [ ] **Step 3: Commit**

```bash
git add apps/godot/scripts/agent.gd apps/frontend/public/godot/
git commit -m "feat: subtle idle bob animation for stationary agents"
```

---

### Task B6: Day/night cycle shader

**Files:**
- Create: `apps/godot/shaders/daynight.gdshader`
- Modify: `apps/godot/maps/isol88/Isol88Map.tscn`
- Modify: `apps/godot/maps/isol88/Isol88Map.gd`

- [ ] **Step 1: Create the shader**

Create `apps/godot/shaders/daynight.gdshader`:

```glsl
shader_type canvas_item;

uniform float time_of_day : hint_range(0.0, 1.0) = 0.5;

void fragment() {
	// Map time_of_day (0-1) to a color overlay:
	// 0.0-0.2: deep night (blue, 0.4 alpha)
	// 0.2-0.3: dawn (amber, fading to clear)
	// 0.3-0.7: day (clear)
	// 0.7-0.8: dusk (amber, building from clear)
	// 0.8-1.0: night (blue, building to 0.4)

	vec3 night_color = vec3(0.1, 0.15, 0.35);
	vec3 dusk_color = vec3(0.9, 0.5, 0.2);
	float alpha = 0.0;
	vec3 tint = vec3(1.0);

	if (time_of_day < 0.2) {
		tint = night_color;
		alpha = 0.4;
	} else if (time_of_day < 0.3) {
		float t = (time_of_day - 0.2) / 0.1;
		tint = mix(night_color, dusk_color, t);
		alpha = mix(0.4, 0.15, t);
	} else if (time_of_day < 0.7) {
		alpha = 0.0;
	} else if (time_of_day < 0.8) {
		float t = (time_of_day - 0.7) / 0.1;
		tint = dusk_color;
		alpha = mix(0.0, 0.15, t);
	} else {
		float t = (time_of_day - 0.8) / 0.2;
		tint = mix(dusk_color, night_color, t);
		alpha = mix(0.15, 0.4, t);
	}

	COLOR = vec4(tint, alpha);
}
```

- [ ] **Step 2: Add CanvasLayer + ColorRect to Isol88Map.tscn**

Open `apps/godot/maps/isol88/Isol88Map.tscn` in Godot editor. Add a new `CanvasLayer` as a child of the root node, name it `DayNightLayer`. Set `layer` to `10` (above most content, below UI).

Add a `ColorRect` child named `DayNightOverlay`:
- `anchor_right` = 1.0, `anchor_bottom` = 1.0 (full screen)
- Create a new ShaderMaterial with `daynight.gdshader`

Save the scene.

- [ ] **Step 3: Update Isol88Map.gd to cycle time_of_day**

In `apps/godot/maps/isol88/Isol88Map.gd`, add:

```gdscript
@onready var day_night_overlay: ColorRect = $DayNightLayer/DayNightOverlay

const CYCLE_SECONDS: float = 1440.0  # 24 minutes real = 24 hours game

func _process(_delta: float) -> void:
	if day_night_overlay and day_night_overlay.material is ShaderMaterial:
		var t: float = fmod(Time.get_unix_time_from_system(), CYCLE_SECONDS) / CYCLE_SECONDS
		(day_night_overlay.material as ShaderMaterial).set_shader_parameter("time_of_day", t)
```

- [ ] **Step 4: Export and verify**

Run: `cd apps/godot && ./export.sh 2>&1 | tail -10`
Then: `cd apps/frontend && pnpm dev` and observe the town over ~5 minutes. You should see the lighting shift.

To test quickly without waiting 24 minutes, temporarily set `CYCLE_SECONDS = 120.0` (2 minutes), verify, then change back.

- [ ] **Step 5: Commit**

```bash
git add apps/godot/shaders/ apps/godot/maps/isol88/Isol88Map.tscn apps/godot/maps/isol88/Isol88Map.gd apps/frontend/public/godot/
git commit -m "feat: day/night cycle shader with 24-minute real-time cycle"
```

---

## Phase B+: Group Conversations

### Task BP1: Add participants list to create_active_conversation

**Files:**
- Modify: `apps/backend/core/services/town_service.py:652-709`
- Test: `apps/backend/tests/test_conversation_service.py`

- [ ] **Step 1: Write failing test**

Add to `apps/backend/tests/test_conversation_service.py`:
```python
def test_create_active_conversation_stores_participants_list(mock_dynamo):
    from core.services.town_service import TownService
    service = TownService()

    conv = service.create_active_conversation(
        initiator_id="a1",
        target_id="b1",
        location="plaza",
        opening_message="hi",
        initiator_display_name="Alice",
    )

    assert conv["participants"] == sorted(["a1", "b1"])
    # Legacy fields should still be present for backward compat
    assert conv["participantAId"] in ("a1", "b1")
    assert conv["participantBId"] in ("a1", "b1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/test_conversation_service.py::test_create_active_conversation_stores_participants_list -v`
Expected: FAIL (KeyError on "participants").

- [ ] **Step 3: Add participants field to create_active_conversation**

In `apps/backend/core/services/town_service.py`, in `create_active_conversation` (around line 680), change the item dict to include both legacy fields AND the new `participants` list:

```python
        item = {
            "id": conv_id,
            "participantAId": a_id,
            "participantBId": b_id,
            "participants": [a_id, b_id],  # NEW: canonical list representation
            "lastSpeakerId": initiator_id,  # NEW: who spoke last
            "location": location,
            "turnCount": 1,
            "topicSummary": "_none",
            "publicLog": [{"speaker": initiator_display_name, "text": opening_message}],
            "status": "active",
            "startedAt": now,
            "endedAt": "_active",
            "updatedAt": now,
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/test_conversation_service.py::test_create_active_conversation_stores_participants_list -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/town_service.py apps/backend/tests/test_conversation_service.py
git commit -m "feat: store participants list and lastSpeakerId on new conversations"
```

---

### Task BP2: join_conversation and leave_conversation service methods

**Files:**
- Modify: `apps/backend/core/services/town_service.py`
- Test: `apps/backend/tests/test_conversation_service.py`

- [ ] **Step 1: Write failing tests**

Add to `apps/backend/tests/test_conversation_service.py`:
```python
def test_join_conversation_adds_participant(mock_dynamo):
    from core.services.town_service import TownService
    service = TownService()

    conv = service.create_active_conversation(
        initiator_id="a1", target_id="b1", location="plaza",
        opening_message="hi", initiator_display_name="Alice",
    )

    updated = service.join_conversation(conv["id"], "c1")
    assert updated is not None
    assert "c1" in updated["participants"]
    assert len(updated["participants"]) == 3


def test_leave_conversation_removes_participant(mock_dynamo):
    from core.services.town_service import TownService
    service = TownService()

    conv = service.create_active_conversation(
        initiator_id="a1", target_id="b1", location="plaza",
        opening_message="hi", initiator_display_name="Alice",
    )
    service.join_conversation(conv["id"], "c1")

    remaining = service.leave_conversation(conv["id"], "b1")
    assert remaining == 2
    fresh = service.get_conversation_by_id(conv["id"])
    assert "b1" not in fresh["participants"]
    assert fresh["status"] == "active"


def test_leave_conversation_with_one_left_returns_count(mock_dynamo):
    from core.services.town_service import TownService
    service = TownService()
    conv = service.create_active_conversation(
        initiator_id="a1", target_id="b1", location="plaza",
        opening_message="hi", initiator_display_name="Alice",
    )
    remaining = service.leave_conversation(conv["id"], "b1")
    assert remaining == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/test_conversation_service.py -k "join_conversation or leave_conversation" -v`
Expected: FAIL (methods don't exist).

- [ ] **Step 3: Implement join_conversation and leave_conversation**

In `apps/backend/core/services/town_service.py`, add these methods after `append_message_to_conversation`:

```python
    def join_conversation(self, conv_id: str, agent_id: str) -> Optional[dict]:
        """Add an agent to an active conversation's participants list.

        Returns the updated conversation, or None if conversation doesn't exist
        or is not active.
        """
        table = dynamo.conversations_table()
        now = _now_iso()
        try:
            resp = table.update_item(
                Key={"id": conv_id},
                UpdateExpression=(
                    "SET participants = list_append(participants, :new), updatedAt = :now"
                ),
                ConditionExpression="attribute_exists(id) AND #s = :active AND NOT contains(participants, :aid)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":new": [agent_id],
                    ":aid": agent_id,
                    ":now": now,
                    ":active": "active",
                },
                ReturnValues="ALL_NEW",
            )
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            return None

        # Mark the joiner's currentConversationId
        agents_table = dynamo.agents_table()
        try:
            agents_table.update_item(
                Key={"id": agent_id},
                UpdateExpression="SET currentConversationId = :cid",
                ExpressionAttributeValues={":cid": conv_id},
                ConditionExpression="attribute_exists(id)",
            )
        except agents_table.meta.client.exceptions.ConditionalCheckFailedException:
            logger.warning("Agent %s not found when joining conversation", agent_id)

        return _decimal_to_native(resp.get("Attributes", {}))

    def leave_conversation(self, conv_id: str, agent_id: str) -> int:
        """Remove an agent from an active conversation's participants list.

        Returns the number of remaining participants (0 means conversation
        should be ended by caller).
        """
        conv = self.get_conversation_by_id(conv_id)
        if not conv or conv.get("status") != "active":
            return 0

        participants: list[str] = conv.get("participants") or [
            conv.get("participantAId"),
            conv.get("participantBId"),
        ]
        participants = [p for p in participants if p and p != agent_id]

        table = dynamo.conversations_table()
        now = _now_iso()
        try:
            table.update_item(
                Key={"id": conv_id},
                UpdateExpression="SET participants = :p, updatedAt = :now",
                ExpressionAttributeValues={":p": participants, ":now": now},
                ConditionExpression="attribute_exists(id)",
            )
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            return 0

        # Clear the leaver's currentConversationId
        agents_table = dynamo.agents_table()
        try:
            agents_table.update_item(
                Key={"id": agent_id},
                UpdateExpression="REMOVE currentConversationId SET lastConversationAt = :now",
                ExpressionAttributeValues={":now": now},
                ConditionExpression="attribute_exists(id)",
            )
        except agents_table.meta.client.exceptions.ConditionalCheckFailedException:
            pass

        return len(participants)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/test_conversation_service.py -k "join_conversation or leave_conversation" -v`
Expected: PASS all three tests.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/town_service.py apps/backend/tests/test_conversation_service.py
git commit -m "feat: join_conversation and leave_conversation service methods"
```

---

### Task BP3: _handle_join_conversation router

**Files:**
- Modify: `apps/backend/routers/town.py` (in the `town_agent_act` dispatcher and new handler)

- [ ] **Step 1: Add join_conversation to the action dispatcher**

In `apps/backend/routers/town.py`, in the `town_agent_act` handler block (near line 1238 where `say` and `end_conversation` are dispatched), add:

```python
        elif action == "join_conversation":
            await _handle_join_conversation(request.connectionId, body, agent_conn)
```

- [ ] **Step 2: Implement _handle_join_conversation**

In `apps/backend/routers/town.py`, add the handler function near `_handle_end_conversation`:

```python
async def _handle_join_conversation(connection_id: str, body: dict, agent_conn) -> None:
    """Handle a join_conversation action — agent joins an existing active conversation.

    Validates the joiner is not already in a conversation and the target conversation
    is active. Adds the agent to participants and broadcasts participant_joined to all.
    """
    from core.services.management_api_client import ManagementApiClient

    mgmt = ManagementApiClient()
    service = TownService()
    ws_manager = get_town_agent_ws_manager()
    joiner_id = agent_conn.agent_id

    def _reject(reason: str) -> None:
        try:
            mgmt.send_message(
                connection_id,
                {"event": "join_rejected", "reason": reason},
            )
        except Exception as exc:
            logger.warning("Failed to send join_rejected to %s: %s", joiner_id, exc)

    conv_id = body.get("conv_id", "")
    if not conv_id:
        _reject("missing_conv_id")
        return

    joiner = service.get_town_agent_by_id(joiner_id)
    if not joiner:
        _reject("joiner_not_found")
        return

    if joiner.get("currentConversationId"):
        _reject("already_in_conversation")
        return

    updated = service.join_conversation(conv_id, joiner_id)
    if updated is None:
        _reject("conversation_not_joinable")
        return

    joiner_display = joiner.get("displayName", agent_conn.agent_name)
    participants: list[str] = updated.get("participants", [])

    # Notify all other participants
    for pid in participants:
        if pid == joiner_id:
            continue
        conn = ws_manager.get_by_agent_id(pid)
        if not conn:
            continue
        try:
            mgmt.send_message(
                conn.connection_id,
                {
                    "event": "participant_joined",
                    "conv_id": conv_id,
                    "agent_id": joiner_id,
                    "display_name": joiner_display,
                },
            )
        except Exception as exc:
            logger.warning("Failed to send participant_joined to %s: %s", pid, exc)

    # Acknowledge to the joiner
    try:
        mgmt.send_message(
            connection_id,
            {"event": "act_ok", "action": "join_conversation", "conv_id": conv_id},
        )
    except Exception as exc:
        logger.warning("Failed to send join act_ok to %s: %s", joiner_id, exc)
```

- [ ] **Step 3: Commit**

```bash
git add apps/backend/routers/town.py
git commit -m "feat: _handle_join_conversation router for group conversations"
```

---

### Task BP4: Broadcast conversation_message to all participants + lastSpeakerId check

**Files:**
- Modify: `apps/backend/routers/town.py:1407-1510` (_handle_say)
- Modify: `apps/backend/core/services/town_service.py:727-769` (append_message_to_conversation)

- [ ] **Step 1: Update append_message_to_conversation to set lastSpeakerId**

In `apps/backend/core/services/town_service.py`, replace the `UpdateExpression` and `ExpressionAttributeValues` inside `append_message_to_conversation` (around line 752):

```python
            resp = table.update_item(
                Key={"id": conv_id},
                UpdateExpression=(
                    "SET publicLog = list_append(publicLog, :entry), "
                    "turnCount = turnCount + :one, "
                    "lastSpeakerId = :sid, "
                    "updatedAt = :now"
                ),
                ConditionExpression="attribute_exists(id) AND #s = :active",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":entry": [new_entry],
                    ":one": 1,
                    ":now": now,
                    ":active": "active",
                    ":sid": speaker_id,
                },
                ReturnValues="ALL_NEW",
            )
```

Update the function signature to take `speaker_id`:
```python
    def append_message_to_conversation(
        self,
        conv_id: str,
        speaker_id: str,
        speaker_display_name: str,
        text: str,
    ) -> Optional[dict]:
```

- [ ] **Step 2: Update _handle_say to broadcast to all participants + enforce lastSpeakerId**

In `apps/backend/routers/town.py`, in `_handle_say` (around line 1461), before the `append_message_to_conversation` call add:

```python
    # Enforce "not twice in a row" for group conversations
    last_speaker = conv.get("lastSpeakerId")
    participants: list[str] = conv.get("participants") or [
        conv.get("participantAId"), conv.get("participantBId"),
    ]
    if len(participants) > 2 and last_speaker == speaker_id:
        _reject("must_let_others_speak")
        return
```

Then change the `append_message_to_conversation` call to pass `speaker_id`:
```python
    updated = service.append_message_to_conversation(conv_id, speaker_id, speaker_display, message)
```

Then in the broadcast block (around line 1472-1490 where the message is sent to the other participant), replace the two-party loop with a participant loop. Find where the other participant is notified (the block with `conversation_message` event) and replace with:

```python
    # Broadcast conversation_message to ALL participants except the speaker
    for pid in participants:
        if pid == speaker_id:
            continue
        conn = ws_manager.get_by_agent_id(pid)
        if not conn:
            continue
        try:
            mgmt.send_message(
                conn.connection_id,
                {
                    "event": "conversation_message",
                    "conv_id": conv_id,
                    "from": speaker_display,
                    "from_agent_name": agent_conn.agent_name,
                    "text": message,
                    "turn": updated.get("turnCount", 0),
                },
            )
        except Exception as exc:
            logger.warning("Failed to send conversation_message to %s: %s", pid, exc)
```

- [ ] **Step 3: Run conversation tests**

Run: `cd apps/backend && uv run pytest tests/test_conversation_service.py tests/test_routers_town.py -v`
Expected: PASS all tests. (Any test calling `append_message_to_conversation` with old signature will need `speaker_id` added — update them.)

- [ ] **Step 4: Commit**

```bash
git add apps/backend/core/services/town_service.py apps/backend/routers/town.py apps/backend/tests/
git commit -m "feat: broadcast conversation_message to all participants with lastSpeakerId enforcement"
```

---

### Task BP5: _finalize_conversation uses participants list

**Files:**
- Modify: `apps/backend/routers/town.py:1571-1680` (_finalize_conversation)
- Modify: `apps/backend/routers/town.py:1509-1568` (_handle_end_conversation)

- [ ] **Step 1: Change _handle_end_conversation to leave semantics**

In `apps/backend/routers/town.py`, replace the body of `_handle_end_conversation` (around line 1509-1568):

```python
async def _handle_end_conversation(connection_id: str, body: dict, agent_conn) -> None:
    """Handle an end_conversation action — the requesting agent LEAVES the conversation.

    If the conversation has ≥2 remaining participants, it continues without them.
    If <2 remain, the conversation is finalized.
    """
    from core.services.management_api_client import ManagementApiClient

    mgmt = ManagementApiClient()
    service = TownService()
    ws_manager = get_town_agent_ws_manager()
    leaver_id = agent_conn.agent_id

    def _reject(reason: str) -> None:
        try:
            mgmt.send_message(connection_id, {"event": "end_rejected", "reason": reason})
        except Exception as exc:
            logger.warning("Failed to send end_rejected to %s: %s", leaver_id, exc)

    conv_id = body.get("conv_id", "")
    if not conv_id:
        _reject("missing_conv_id")
        return

    conv = service.get_conversation_by_id(conv_id)
    if not conv:
        _reject("conversation_not_found")
        return

    participants: list[str] = conv.get("participants") or [
        conv.get("participantAId"), conv.get("participantBId"),
    ]
    if leaver_id not in participants:
        _reject("not_a_participant")
        return

    if conv.get("status") != "active":
        _reject("conversation_not_active")
        return

    remaining = service.leave_conversation(conv_id, leaver_id)

    leaver_display = agent_conn.agent_name
    # Notify remaining participants that someone left
    for pid in participants:
        if pid == leaver_id or pid not in [p for p in participants if p != leaver_id]:
            continue
        conn = ws_manager.get_by_agent_id(pid)
        if not conn:
            continue
        try:
            mgmt.send_message(
                conn.connection_id,
                {
                    "event": "participant_left",
                    "conv_id": conv_id,
                    "agent_id": leaver_id,
                    "display_name": leaver_display,
                    "remaining": remaining,
                },
            )
        except Exception as exc:
            logger.warning("Failed to send participant_left to %s: %s", pid, exc)

    # Acknowledge leaver
    try:
        mgmt.send_message(connection_id, {"event": "act_ok", "action": "end_conversation", "conv_id": conv_id})
    except Exception:
        pass

    # If fewer than 2 remain, finalize the conversation entirely
    if remaining < 2:
        await _finalize_conversation(conv_id, service, mgmt, ws_manager, auto_ended=False)
```

- [ ] **Step 2: Update _finalize_conversation to loop over participants**

In `apps/backend/routers/town.py`, in `_finalize_conversation` (around line 1599-1670), replace the hard-coded `(a_id, b_id)` loops with a participant-list loop.

Replace the section starting at:
```python
    a_id = conv.get("participantAId")
    b_id = conv.get("participantBId")
```
through the end of the function with:

```python
    participants: list[str] = conv.get("participants") or [
        conv.get("participantAId"), conv.get("participantBId"),
    ]
    participants = [p for p in participants if p]
    turn_count = conv.get("turnCount", 0)
    location = conv.get("location", "unknown")

    if end_reason_override:
        end_reason = end_reason_override
    else:
        end_reason = "turn_limit_reached" if auto_ended else "ended_by_participant"

    # Apply mood/energy effects for each participant
    for agent_id in participants:
        agent = service.get_town_agent_by_id(agent_id)
        if not agent:
            continue

        current_energy = int(agent.get("energy", 100))
        current_mood_raw = agent.get("mood", 0)
        current_mood = town_mood_engine.parse_mood(current_mood_raw)
        traits = agent.get("traits", "")

        new_energy, new_mood = town_mood_engine.apply_event(
            "conversation_completed", current_energy, current_mood, traits
        )
        service.update_agent_mood_energy(agent_id, new_mood, new_energy)

    # Update relationships for all pairs of participants (only if exactly 2, or bump all pairs)
    if len(participants) >= 2:
        for i in range(len(participants)):
            for j in range(i + 1, len(participants)):
                try:
                    service.get_or_create_relationship(participants[i], participants[j])
                    service.update_relationship(participants[i], participants[j], affinity_delta=5)
                except Exception as exc:
                    logger.warning("Failed to update relationship: %s", exc)

    # Clear talking activity and speech bubbles for all participants
    gs_client = get_game_server_client()
    for agent_id in participants:
        try:
            await gs_client.send_command(agent_id, {"cmd": "set_activity", "activity": ""})
            await gs_client.send_command(agent_id, {"cmd": "set_speech", "text": ""})
        except Exception:
            pass

    # Notify all connected participants
    public_log = conv.get("publicLog") or []
    log_summary = _build_log_summary(public_log)

    for agent_id in participants:
        conn = ws_manager.get_by_agent_id(agent_id)
        if not conn:
            continue
        try:
            mgmt.send_message(
                conn.connection_id,
                {
                    "event": "conversation_ended",
                    "conv_id": conv_id,
                    "turn_count": turn_count,
                    "location": location,
                    "reason": end_reason,
                    "log_summary": log_summary,
                },
            )
        except Exception as exc:
            logger.warning("Failed to send conversation_ended to %s: %s", agent_id, exc)

    logger.info(
        "Conversation %s finalised (%s, %d turns, location=%s, %d participants)",
        conv_id, end_reason, turn_count, location, len(participants),
    )
```

- [ ] **Step 3: Also update end_conversation service method to clear all participants**

In `apps/backend/core/services/town_service.py`, in `end_conversation` (around line 798), replace the participant clearing loop:

```python
        # Clear currentConversationId on all participants
        agents_table = dynamo.agents_table()
        participants_list: list[str] = conv.get("participants") or [
            conv.get("participantAId"), conv.get("participantBId"),
        ]
        for agent_id in participants_list:
            if not agent_id:
                continue
            try:
                agents_table.update_item(
                    Key={"id": agent_id},
                    UpdateExpression="REMOVE currentConversationId SET lastConversationAt = :now",
                    ExpressionAttributeValues={":now": now},
                    ConditionExpression="attribute_exists(id)",
                )
            except agents_table.meta.client.exceptions.ConditionalCheckFailedException:
                logger.warning("Agent %s not found when clearing currentConversationId", agent_id)
```

- [ ] **Step 4: Run tests**

Run: `cd apps/backend && uv run pytest tests/test_conversation_service.py tests/test_routers_town.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/routers/town.py apps/backend/core/services/town_service.py
git commit -m "feat: end_conversation is now leave, _finalize loops over participants list"
```

---

### Task BP6: Surface nearby_conversations in world_update for daemon

**Files:**
- Modify: `apps/backend/core/services/state_translator.py` (or wherever daemon state is built)
- Modify: `apps/backend/routers/town.py` (where `agent_state` events build daemon payload)

- [ ] **Step 1: Find where daemon state is constructed**

Run: `grep -rn "nearby_agents\|world_update\|agent_state" apps/backend/core/services/state_translator.py apps/backend/routers/town.py | head -20`

- [ ] **Step 2: Add nearby_conversations computation**

In the function that builds the daemon payload (likely in `state_translator.py` or in the `_handle_game_server_event` callback for `agent_state` events in `routers/town.py`), after nearby agents are computed, add:

```python
    # Find active conversations where at least one nearby agent is a participant
    nearby_conv_payload: list[dict] = []
    try:
        nearby_ids = {a.get("agent_id") for a in nearby_agents if a.get("agent_id")}
        if nearby_ids:
            # Fetch any conversation the nearby agents are in
            for nid in nearby_ids:
                neighbor = service.get_town_agent_by_id(nid)
                if not neighbor:
                    continue
                conv_id = neighbor.get("currentConversationId")
                if not conv_id:
                    continue
                # Skip if this agent is already in that conversation
                if own_conv_id and own_conv_id == conv_id:
                    continue
                conv = service.get_conversation_by_id(conv_id)
                if not conv or conv.get("status") != "active":
                    continue
                participants: list[str] = conv.get("participants") or []
                participant_names = []
                for pid in participants:
                    pagent = service.get_town_agent_by_id(pid)
                    if pagent:
                        participant_names.append(pagent.get("displayName", pagent.get("agentName", pid[:8])))
                last_msg = {}
                pub = conv.get("publicLog") or []
                if pub:
                    last_msg = pub[-1]
                nearby_conv_payload.append({
                    "conv_id": conv_id,
                    "participants": participant_names,
                    "last_message": last_msg,
                    "location": conv.get("location", ""),
                })
    except Exception as exc:
        logger.warning("Failed to build nearby_conversations: %s", exc)

    # Include in the daemon payload
    payload["nearby_conversations"] = nearby_conv_payload
```

(Adapt the local variable names — `nearby_agents`, `own_conv_id`, `payload` — to whatever names the existing code uses.)

- [ ] **Step 3: Commit**

```bash
git add apps/backend/core/services/state_translator.py apps/backend/routers/town.py
git commit -m "feat: surface nearby_conversations in daemon state payload"
```

---

### Task BP7: Daemon handles participant_joined/left, multi-participant state

**Files:**
- Modify: `apps/backend/data/goosetown-skill/goosetown_cli.py:975-1022` (_on_conversation_message, _on_conversation_ended)
- Modify: `apps/backend/data/goosetown-skill/goosetown_cli.py` (add handlers for participant_joined/left, nearby_conversations surface)

- [ ] **Step 1: Update active_conversation to track participants list**

In `apps/backend/data/goosetown-skill/goosetown_cli.py`, replace `_on_conversation_message` (lines 975-1006) with:

```python
    def _on_conversation_message(self, event: str, data: dict) -> None:
        """Handle conversation_invite and conversation_message events."""
        self.state.setdefault("pending_messages", []).append(data)
        conv_id = data.get("conv_id", "")

        if event == "conversation_invite":
            self.state["active_conversation"] = {
                "conv_id": conv_id,
                "participants": [data.get("from", "?")],  # Others will be added via world_update enrichment
                "turns": [{"speaker": data.get("from", "?"), "text": data.get("message", "")}],
            }
            self._append_event(
                f'CHAT_INVITE from {data.get("from", "?")} — "{data.get("message", "")}" (conv_id: {conv_id})'
            )
        else:
            active = self.state.get("active_conversation", {})
            if active.get("conv_id") == conv_id:
                active.setdefault("turns", []).append(
                    {"speaker": data.get("from", "?"), "text": data.get("text", "")}
                )
                # Track participants seen
                participants = active.setdefault("participants", [])
                if data.get("from") and data.get("from") not in participants:
                    participants.append(data.get("from"))
            else:
                self.state["active_conversation"] = {
                    "conv_id": conv_id,
                    "participants": [data.get("from", "?")],
                    "turns": [{"speaker": data.get("from", "?"), "text": data.get("text", "")}],
                }
            self._append_event(
                f'CHAT_MESSAGE {data.get("from", "?")} — "{data.get("text", "")}" '
                f'(conv_id: {conv_id}, turn {data.get("turn", "?")})'
            )
```

- [ ] **Step 2: Add handlers for participant_joined / participant_left**

Find the event dispatcher in `goosetown_cli.py` (around line 852) and add new branches:

```python
        elif event == "participant_joined":
            self._on_participant_joined(data)
        elif event == "participant_left":
            self._on_participant_left(data)
```

Then add the handler methods near `_on_conversation_message`:

```python
    def _on_participant_joined(self, data: dict) -> None:
        """Handle participant_joined event — someone new joined your conversation."""
        active = self.state.get("active_conversation", {})
        if active.get("conv_id") != data.get("conv_id"):
            return
        display = data.get("display_name", "?")
        participants = active.setdefault("participants", [])
        if display not in participants:
            participants.append(display)
        self._append_event(f'PARTICIPANT_JOINED {display} joined the conversation')

    def _on_participant_left(self, data: dict) -> None:
        """Handle participant_left event — someone left your conversation (but it continues)."""
        active = self.state.get("active_conversation", {})
        if active.get("conv_id") != data.get("conv_id"):
            return
        display = data.get("display_name", "?")
        participants = active.setdefault("participants", [])
        if display in participants:
            participants.remove(display)
        remaining = data.get("remaining", len(participants))
        self._append_event(f'PARTICIPANT_LEFT {display} left (still {remaining} remaining)')
```

- [ ] **Step 3: Add join_conversation action to the CLI parser**

Find the action dispatcher (around line 643, the `chat`/`say`/`end` block) and add:

```python
    elif action == "join_conversation":
        if len(rest) < 1:
            _die("Usage: act join_conversation <conv_id>")
        payload = {"action": "join_conversation", "conv_id": rest[0]}
```

And add `"join_conversation"` to `_VALID_COMMANDS` around line 660.

- [ ] **Step 4: Surface nearby_conversations in TOWN_STATUS.md**

Find the TOWN_STATUS.md renderer (around line 1285). In the "other actions" section, add before the "End conversation" block:

```python
            # Nearby conversations (other agents talking near you — you can join)
            nearby_convs: list[dict] = self.state.get("nearby_conversations", [])
            if nearby_convs and not active_conv:
                nearby_conv_block = "**Nearby conversations (you can join):**\n"
                for nc in nearby_convs[:3]:
                    names = ", ".join(nc.get("participants", [])[:5])
                    last = nc.get("last_message", {})
                    last_text = last.get("text", "") if isinstance(last, dict) else ""
                    nearby_conv_block += (
                        f'- {names} are chatting — last: "{last_text[:60]}" '
                        f'— `python3 goosetown_cli.py act join_conversation {nc.get("conv_id")}`\n'
                    )
                nearby_conv_block += "\n"
            else:
                nearby_conv_block = ""
```

Then include `{nearby_conv_block}` in the f-string that renders the status body.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/data/goosetown-skill/goosetown_cli.py
git commit -m "feat: daemon supports group conversations and nearby_conversations surface"
```

---

### Task BP8: Game server group facing

**Files:**
- Modify: `apps/godot/scripts/server/agent_controller.gd`

- [ ] **Step 1: Add group-facing logic**

In `apps/godot/scripts/server/agent_controller.gd`, find where facing is set during conversations (likely a `face_toward` helper or inline in a chat handler). If none exists, add:

```gdscript
func face_agents_toward_centroid(agent_ids: Array) -> void:
	## When 2+ agents are in a group conversation, face each toward the
	## centroid of the group (excluding themselves).
	if agent_ids.size() < 2:
		return
	var positions: Array = []
	for id in agent_ids:
		var a: Node = _agents.get(id)
		if a != null:
			positions.append(a.global_position)
	if positions.size() < 2:
		return
	for id in agent_ids:
		var a: Node = _agents.get(id)
		if a == null:
			continue
		var others: Array = []
		for p in positions:
			if p != a.global_position:
				others.append(p)
		if others.is_empty():
			continue
		var centroid: Vector2 = Vector2.ZERO
		for p in others:
			centroid += p
		centroid /= others.size()
		var delta: Vector2 = centroid - a.global_position
		if abs(delta.x) > abs(delta.y):
			a.facing = "east" if delta.x > 0 else "west"
		else:
			a.facing = "south" if delta.y > 0 else "north"
```

- [ ] **Step 2: Call face_agents_toward_centroid when conversation is updated**

This requires backend → game server signal. For now, add a server protocol command `set_conversation_group` that the backend sends with the list of participant IDs in a conversation. In `server_protocol.gd` or the command dispatch in `agent_controller.gd`, add:

```gdscript
		"set_conversation_group":
			var ids: Array = data.get("agent_ids", [])
			face_agents_toward_centroid(ids)
```

- [ ] **Step 3: Export and commit**

```bash
cd apps/godot && ./export.sh 2>&1 | tail -5
git add apps/godot/scripts/server/agent_controller.gd apps/frontend/public/godot/
git commit -m "feat: group conversation centroid facing in game server"
```

---

## Verification

### Phase D verification:
- [ ] `cd apps/backend && uv run pytest tests/ -v` — all tests pass
- [ ] `cd apps/backend && uv run ruff check . && uv run ruff format --check .` — clean lint
- [ ] `cd apps/infra && rm -rf ../backend/.venv && npx cdk synth` — CDK synth succeeds
- [ ] Manual: start backend locally with `ENVIRONMENT=prod` and no env vars → startup fails with clear error
- [ ] Manual: POST to `/ws-message` without `X-WS-Lambda-Secret` header → 403

### Phase B verification:
- [ ] Export Godot .pck
- [ ] Run `cd apps/frontend && pnpm dev`
- [ ] Agent doing an activity shows the correct animation (not just walk/idle)
- [ ] Speech bubble word-wraps for long text, fades in/out, renders above other agents
- [ ] Idle agents have subtle bob
- [ ] Day/night overlay cycles over 24 minutes (or 2 min in test mode)

### Phase B+ verification:
- [ ] `cd apps/backend && uv run pytest tests/test_conversation_service.py -v` — all pass
- [ ] Manual: Agent A chats Agent B. Agent C joins via `act join_conversation <conv_id>`. All three exchange messages.
- [ ] Manual: Agent B leaves via `act end <conv_id>`. A and C continue. Verify A and C see `participant_left` event.
- [ ] Manual: Agent C leaves. Only A remains → conversation auto-finalizes.
- [ ] Manual: Agent D (not in any conversation) near A+C's conversation sees `nearby_conversations` in TOWN_STATUS.md with the join command.
