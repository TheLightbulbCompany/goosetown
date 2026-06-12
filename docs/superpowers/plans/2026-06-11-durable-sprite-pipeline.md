# Durable Sprite Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the in-memory, per-process sprite queue with an SQS-backed durable queue drained by a dedicated single-replica sprite worker, so deploys/scale events never lose jobs and PixelLab concurrency is globally capped; split signup to walk-only with lazy idle/talking.

**Architecture:** Backend replicas become SQS *producers* only (with a DynamoDB conditional gate for idempotency). A new `sprite_worker` Fargate service (backend image, command override — the WorldgmStack pattern) is the only process that calls PixelLab generation, making PR #377's process-global semaphore globally true. The worker's backend-state side effects (game-server pushes, ws_manager cache, daemon messages) flow through one internal HTTP endpoint.

**Tech Stack:** Python 3.12 + FastAPI + boto3 (SQS/DDB/CloudWatch) + httpx; AWS CDK (TypeScript) for infra; pytest + unittest.mock.

**Spec:** `docs/superpowers/specs/2026-06-11-durable-sprite-pipeline-design.md` — read it first.

**Working rules (from CLAUDE.md — non-negotiable):**
- All work on branch `feat/durable-sprite-pipeline` in this worktree.
- Run tests: `cd apps/backend && uv run pytest tests/ -q` and `uv run ruff check . && uv run ruff format --check .`
- `cdk synth` before any push that touches `apps/infra/` (delete `apps/backend/.venv` first if present).
- Never hardcode a PixelLab key. Never touch deployed infra directly.

## File structure

| File | Responsibility |
|---|---|
| `apps/backend/core/config.py` (modify) | New settings: `ACTIVITY_ANIMATION_GENERATION`, `SPRITE_JOBS_QUEUE_NAME`, `SPRITE_WORKER_MAX_CONCURRENT_PIPELINES`, `BACKEND_INTERNAL_URL` |
| `apps/backend/core/services/pixellab_service.py` (modify) | Flip `idle`/`talking` to `auto_generate=False` (signup = walk only) |
| `apps/backend/core/services/town_service.py` (modify) | `claim_sprite_job` conditional-update gate |
| `apps/backend/core/services/sprite_jobs.py` (create) | SQS producer: `enqueue_signup`, `enqueue_animation` |
| `apps/backend/core/services/sprite_update_apply.py` (create) | `apply_sprite_update` — backend-local effects of a sprite change (gs push, cache refresh, daemon notify) |
| `apps/backend/routers/town.py` (modify) | `POST /town/internal/sprite-event`; register + admin call-site swaps |
| `apps/backend/core/services/sprite_pipeline.py` (modify) | Sink protocol; resume-aware `run_signup_pipeline` + `run_animation_job`; `_sprite_pipeline_task` deleted; `resume_sprite_generation` kept ONLY for the recomposite ("downloading") path |
| `apps/backend/core/services/agent_connect_service.py` (modify) | Auto-enqueue swaps to `sprite_jobs.enqueue_signup` |
| `apps/backend/core/services/activity_service.py` (modify) | Lazy-gen becomes flag-gated `enqueue_animation` |
| `apps/backend/core/services/conversation_service.py` (modify) | Talking lazy hook on conversation start |
| `apps/backend/core/services/sprite_queue.py` (delete) | Replaced by SQS |
| `apps/backend/main.py` (modify) | Drop queue start/stop + startup resume block |
| `apps/backend/sprite_worker/__init__.py`, `__main__.py`, `processor.py`, `backend_sink.py` (create) | The worker service |
| `apps/infra/lib/stacks/sprite-worker-stack.ts` (create) | Queue + DLQ + Fargate service + alarms |
| `apps/infra/lib/goosetown-stage.ts`, `apps/infra/lib/stacks/compute-stack.ts` (modify) | Stack wiring; backend env + SQS send grant |

Shared constant used throughout (define once in `sprite_jobs.py`, import elsewhere):

```python
IN_PROGRESS_STATUSES = (
    "queued", "creating", "building_static", "static_ready", "animating", "downloading",
)
```

---

### Task 1: Config settings

**Files:**
- Modify: `apps/backend/core/config.py`
- Test: `apps/backend/tests/unit/test_config_sprite_worker.py` (create)

- [ ] **Step 1: Write the failing test**

```python
"""New settings for the durable sprite pipeline (spec 2026-06-11)."""

import importlib


def _reload_settings(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import core.config

    importlib.reload(core.config)
    return core.config.settings


def test_defaults(monkeypatch):
    for var in (
        "ACTIVITY_ANIMATION_GENERATION",
        "SPRITE_JOBS_QUEUE_NAME",
        "SPRITE_WORKER_MAX_CONCURRENT_PIPELINES",
        "BACKEND_INTERNAL_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ENVIRONMENT", "dev")
    s = _reload_settings(monkeypatch)
    assert s.ACTIVITY_ANIMATION_GENERATION is True
    assert s.SPRITE_JOBS_QUEUE_NAME == "goosetown-dev-sprite-jobs"
    assert s.SPRITE_WORKER_MAX_CONCURRENT_PIPELINES == 2
    assert s.BACKEND_INTERNAL_URL == ""


def test_overrides(monkeypatch):
    s = _reload_settings(
        monkeypatch,
        ACTIVITY_ANIMATION_GENERATION="false",
        SPRITE_JOBS_QUEUE_NAME="custom-queue",
        SPRITE_WORKER_MAX_CONCURRENT_PIPELINES="3",
        BACKEND_INTERNAL_URL="https://api-dev.goosetown.isol8.co",
    )
    assert s.ACTIVITY_ANIMATION_GENERATION is False
    assert s.SPRITE_JOBS_QUEUE_NAME == "custom-queue"
    assert s.SPRITE_WORKER_MAX_CONCURRENT_PIPELINES == 3
    assert s.BACKEND_INTERNAL_URL == "https://api-dev.goosetown.isol8.co"
```

(Note: after the reload-based tests run, re-reload `core.config` with a clean env in a fixture or at module teardown so other tests see pristine settings — follow the pattern used in `tests/unit/test_config_pixellab.py`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/test_config_sprite_worker.py -q`
Expected: FAIL with `AttributeError: ... no attribute 'ACTIVITY_ANIMATION_GENERATION'`

- [ ] **Step 3: Implement** — in `core/config.py`, after the `PIXELLAB_POOL_*` lines add:

```python
    # ── Durable sprite pipeline (spec 2026-06-11) ──────────────────────────
    # Gates ALL lazy-animation enqueues (activity anims + the talking hook).
    # Dev runs with "false" so it never burns the shared PixelLab account on
    # lazy generation; signup walk generation is unaffected.
    ACTIVITY_ANIMATION_GENERATION: bool = os.environ.get("ACTIVITY_ANIMATION_GENERATION", "true").lower() != "false"
    SPRITE_JOBS_QUEUE_NAME: str = os.environ.get(
        "SPRITE_JOBS_QUEUE_NAME", f"goosetown-{os.getenv('ENVIRONMENT', 'dev')}-sprite-jobs"
    )
    SPRITE_WORKER_MAX_CONCURRENT_PIPELINES: int = int(os.environ.get("SPRITE_WORKER_MAX_CONCURRENT_PIPELINES", "2"))
    # Base URL of the backend ALB, used by the sprite worker to POST
    # /town/internal/sprite-event. Empty in the backend itself.
    BACKEND_INTERNAL_URL: str = os.environ.get("BACKEND_INTERNAL_URL", "")
```

- [ ] **Step 4: Run test to verify it passes** — same command, expected PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/config.py apps/backend/tests/unit/test_config_sprite_worker.py
git commit -m "feat(config): settings for durable sprite pipeline"
```

---

### Task 2: Signup = walk only (fast/slow split, task #40)

**Files:**
- Modify: `apps/backend/core/services/pixellab_service.py` (ANIMATION_REGISTRY: `idle`, `talking`)
- Modify: any existing tests that assert the signup set (`tests/unit/services/test_signup_animations.py`, `tests/unit/services/test_animation_registry.py` — grep for `auto_generate`, `SIGNUP_ANIMATIONS`, `idle`, `talking`)
- Test: extend `tests/unit/services/test_animation_registry.py`

- [ ] **Step 1: Write the failing test** (add to `test_animation_registry.py`):

```python
def test_signup_animations_is_walk_only():
    """Fast/slow split (task #40): signup generates create + walk ONLY.
    idle/talking generate lazily (activity layer / conversation hook)."""
    from core.services.pixellab_service import SIGNUP_ANIMATIONS

    assert set(SIGNUP_ANIMATIONS.keys()) == {"walk"}
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/unit/services/test_animation_registry.py -q` → FAIL (set is `{walk, idle, talking}`).

- [ ] **Step 3: Implement** — in `ANIMATION_REGISTRY`, change `idle`'s entry to `"auto_generate": False` and `talking`'s entry to `"auto_generate": False`. Add a comment on each: `# lazily generated (fast/slow split, spec 2026-06-11) — idle via the activity layer, talking via the conversation-start hook`.

- [ ] **Step 4: Fix collateral tests.** Run `uv run pytest tests/unit/services/ -q`. Any test asserting idle/talking are in the signup set (e.g. in `test_signup_animations.py`) gets updated to walk-only expectations — update the *expectations*, do not weaken assertions. Also update the Phase C progress string in `core/services/sprite_pipeline.py` from `"Bringing you to life (animating walk, idle, talking)..."` to `"Bringing you to life (animating walk)..."` if a test references it (and even if not — the text now lies).

- [ ] **Step 5: Run full backend suite** — `uv run pytest tests/ -q` → PASS.

- [ ] **Step 6: Commit** — `git commit -m "feat(sprites): signup generates walk only — idle/talking go lazy (task #40)"`

---

### Task 3: `TownService.claim_sprite_job` (idempotency gate)

**Files:**
- Modify: `apps/backend/core/services/town_service.py`
- Test: `apps/backend/tests/unit/services/test_claim_sprite_job.py` (create)

- [ ] **Step 1: Write the failing test.** Look at how existing `town_service` tests fake the agents table (see `tests/unit/test_dynamo.py` / `tests/unit/services/` conftest for the established mock pattern — follow it). The behaviors to assert:

```python
"""claim_sprite_job: conditional gate that makes sprite-job enqueue idempotent."""

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from core.services.town_service import TownService


def _conditional_failure():
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "nope"}},
        "UpdateItem",
    )


@pytest.fixture()
def service():
    svc = TownService()
    svc.agents_table = MagicMock()  # adjust attribute name to the real one
    return svc


def test_claim_succeeds_sets_queued(service):
    assert service.claim_sprite_job("agent-1") is True
    kwargs = service.agents_table.update_item.call_args.kwargs
    assert kwargs["Key"] == {"id": "agent-1"}
    assert ":queued" in kwargs["ExpressionAttributeValues"]
    assert "ConditionExpression" in kwargs


def test_claim_returns_false_when_already_in_flight(service):
    service.agents_table.update_item.side_effect = _conditional_failure()
    assert service.claim_sprite_job("agent-1") is False


def test_claim_reraises_other_client_errors(service):
    service.agents_table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}},
        "UpdateItem",
    )
    with pytest.raises(ClientError):
        service.claim_sprite_job("agent-1")
```

(Adapt `svc.agents_table` to the actual table attribute used by `update_agent_state` — read the class first.)

- [ ] **Step 2: Run to verify it fails** — `AttributeError: claim_sprite_job`.

- [ ] **Step 3: Implement** — add to `TownService` next to `update_agent_state`:

```python
    def claim_sprite_job(self, agent_id: str) -> bool:
        """Atomically claim the right to enqueue a sprite job for this agent.

        Sets ``spriteStatus="queued"`` ONLY if the agent exists and no job is
        already queued/in-flight. Returns False when another producer (or a
        previous enqueue) already claimed it — the caller skips the SQS send.
        Write-then-send ordering: if the SQS send fails after this write, the
        worker boot sweep re-enqueues every ``queued`` row, so the failure is
        self-healing rather than job-losing.
        """
        from botocore.exceptions import ClientError

        try:
            self.agents_table.update_item(
                Key={"id": agent_id},
                UpdateExpression="SET spriteStatus = :queued",
                ConditionExpression=(
                    "attribute_exists(id) AND ("
                    "attribute_not_exists(spriteStatus) OR "
                    "attribute_type(spriteStatus, :null_type) OR "
                    "NOT (spriteStatus IN (:s0, :s1, :s2, :s3, :s4, :s5)))"
                ),
                ExpressionAttributeValues={
                    ":queued": "queued",
                    ":null_type": "NULL",
                    ":s0": "queued",
                    ":s1": "creating",
                    ":s2": "building_static",
                    ":s3": "static_ready",
                    ":s4": "animating",
                    ":s5": "downloading",
                },
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
```

Note: a `spriteStatus == "complete"` agent CAN be re-claimed (the IN list excludes
"complete") — but the worker skips complete agents unless their state was cleared
first (Task 6), so a stray re-claim costs nothing. Admin regenerate clears state
explicitly before enqueueing (Task 7).

- [ ] **Step 4: Run test → PASS.**

- [ ] **Step 5: Commit** — `git commit -m "feat(town-service): conditional claim_sprite_job gate"`

---

### Task 4: SQS producer `core/services/sprite_jobs.py`

**Files:**
- Create: `apps/backend/core/services/sprite_jobs.py`
- Test: `apps/backend/tests/unit/services/test_sprite_jobs.py` (create)

- [ ] **Step 1: Write the failing test**

```python
"""sprite_jobs producer: durable SQS enqueue with DDB idempotency gate."""

import json
from unittest.mock import MagicMock, patch

import core.services.sprite_jobs as sprite_jobs


def _patched(send_ok=True, claim=True):
    sqs = MagicMock()
    sqs.get_queue_url.return_value = {"QueueUrl": "https://sqs/q"}
    if not send_ok:
        sqs.send_message.side_effect = RuntimeError("boom")
    svc = MagicMock()
    svc.claim_sprite_job.return_value = claim
    return sqs, svc


def test_enqueue_signup_sends_message():
    sqs, svc = _patched()
    with patch.object(sprite_jobs, "_sqs", return_value=sqs), \
         patch("core.services.town_service.TownService", return_value=svc):
        sprite_jobs._queue_url_cache = None
        assert sprite_jobs.enqueue_signup("a-1", "Ada", "a goose in a suit") is True
    body = json.loads(sqs.send_message.call_args.kwargs["MessageBody"])
    assert body["kind"] == "signup"
    assert body["agent_id"] == "a-1"
    assert body["appearance"] == "a goose in a suit"
    assert body["schema"] == 1


def test_enqueue_signup_skips_when_claim_fails():
    sqs, svc = _patched(claim=False)
    with patch.object(sprite_jobs, "_sqs", return_value=sqs), \
         patch("core.services.town_service.TownService", return_value=svc):
        sprite_jobs._queue_url_cache = None
        assert sprite_jobs.enqueue_signup("a-1", "Ada", "desc") is False
    sqs.send_message.assert_not_called()


def test_enqueue_signup_force_bypasses_claim():
    sqs, svc = _patched(claim=False)
    with patch.object(sprite_jobs, "_sqs", return_value=sqs), \
         patch("core.services.town_service.TownService", return_value=svc):
        sprite_jobs._queue_url_cache = None
        assert sprite_jobs.enqueue_signup("a-1", "Ada", "desc", force=True) is True
    svc.claim_sprite_job.assert_not_called()
    sqs.send_message.assert_called_once()


def test_enqueue_animation_sends_message():
    sqs, _ = _patched()
    with patch.object(sprite_jobs, "_sqs", return_value=sqs):
        sprite_jobs._queue_url_cache = None
        assert sprite_jobs.enqueue_animation("a-1", "talking") is True
    body = json.loads(sqs.send_message.call_args.kwargs["MessageBody"])
    assert body == {
        "schema": 1,
        "kind": "animation",
        "agent_id": "a-1",
        "anim_name": "talking",
        "enqueued_at": body["enqueued_at"],
    }


def test_send_failure_returns_false_not_raise():
    sqs, svc = _patched(send_ok=False)
    with patch.object(sprite_jobs, "_sqs", return_value=sqs), \
         patch("core.services.town_service.TownService", return_value=svc):
        sprite_jobs._queue_url_cache = None
        assert sprite_jobs.enqueue_signup("a-1", "Ada", "desc") is False
```

- [ ] **Step 2: Run to verify it fails** — `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
"""SQS producer for durable sprite-generation jobs.

Replaces the in-memory ``SpriteQueue`` (deleted — see spec 2026-06-11). The
queue ``goosetown-{env}-sprite-jobs`` is drained by the dedicated single-replica
sprite worker (``apps/backend/sprite_worker/``); backend replicas only produce.

Idempotency: ``enqueue_signup`` first claims the job via a conditional
DynamoDB write (``TownService.claim_sprite_job`` sets spriteStatus="queued"),
so concurrent producers (register endpoint + connect auto-enqueue racing) send
at most one message. Write-then-send ordering means a failed send leaves a
``queued`` row that the worker's boot sweep re-enqueues — self-healing, never
job-losing.
"""

from __future__ import annotations

import json
import logging
import time

import boto3

from core.config import settings

logger = logging.getLogger(__name__)

# spriteStatus values that mean "a sprite job is queued or in flight".
# Shared with the producer gate, the worker boot sweep, and the register
# endpoint's fast-path check.
IN_PROGRESS_STATUSES = (
    "queued",
    "creating",
    "building_static",
    "static_ready",
    "animating",
    "downloading",
)

_sqs_client = None
_queue_url_cache: str | None = None


def _sqs():
    global _sqs_client
    if _sqs_client is None:
        _sqs_client = boto3.client("sqs", region_name=settings.AWS_REGION)
    return _sqs_client


def _queue_url() -> str:
    global _queue_url_cache
    if _queue_url_cache is None:
        _queue_url_cache = _sqs().get_queue_url(QueueName=settings.SPRITE_JOBS_QUEUE_NAME)["QueueUrl"]
    return _queue_url_cache


def _send(body: dict) -> bool:
    try:
        _sqs().send_message(QueueUrl=_queue_url(), MessageBody=json.dumps(body))
        return True
    except Exception:
        # Non-fatal by design: the DDB "queued" claim (already written) makes
        # the worker boot sweep re-enqueue this agent. Log loudly.
        logger.exception("sprite_jobs: SQS send failed for %s", body.get("agent_id"))
        return False


def enqueue_signup(agent_id: str, display_name: str, appearance: str, *, force: bool = False) -> bool:
    """Enqueue a full signup pipeline (create → static → walk) for an agent.

    Returns True if a message was sent. ``force=True`` skips the conditional
    claim — used by the worker boot sweep (re-enqueueing rows that are already
    in an in-progress status) and by admin regenerate (which clears state first).
    """
    if not force:
        from core.services.town_service import TownService

        if not TownService().claim_sprite_job(agent_id):
            logger.info("sprite_jobs: %s already queued/in-flight — skipping", agent_id[:8])
            return False
    return _send(
        {
            "schema": 1,
            "kind": "signup",
            "agent_id": agent_id,
            "display_name": display_name,
            "appearance": appearance,
            "enqueued_at": int(time.time()),
        }
    )


def enqueue_animation(agent_id: str, anim_name: str) -> bool:
    """Enqueue lazy generation of ONE animation (all 4 directions).

    Dedup is best-effort at call sites (skip if already in spriteAnimations)
    plus authoritative in the worker (skips if the animation exists by the
    time the message is processed). Duplicate messages are cheap and safe.
    """
    return _send(
        {
            "schema": 1,
            "kind": "animation",
            "agent_id": agent_id,
            "anim_name": anim_name,
            "enqueued_at": int(time.time()),
        }
    )
```

- [ ] **Step 4: Run test → PASS.** Also `uv run ruff check core/services/sprite_jobs.py`.

- [ ] **Step 5: Commit** — `git commit -m "feat(sprites): SQS producer for durable sprite jobs"`

---

### Task 5: `apply_sprite_update` + internal endpoint

**Files:**
- Create: `apps/backend/core/services/sprite_update_apply.py`
- Modify: `apps/backend/routers/town.py` (new endpoint; place it next to `/ws-message` which holds the secret-check pattern at ~line 1400)
- Test: `apps/backend/tests/unit/services/test_sprite_update_apply.py`, `apps/backend/tests/unit/test_internal_sprite_event.py` (create both)

- [ ] **Step 1: Write the failing tests.**

`test_sprite_update_apply.py` — assert the three behaviors (mock `TownService`, `get_town_agent_ws_manager`, `get_game_server_client`, `restore_phase_from_db`):

```python
"""apply_sprite_update: backend-local effects after the worker changes sprites in DDB."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services.sprite_update_apply import apply_sprite_update

AGENT = {
    "id": "a-1",
    "displayName": "Ada",
    "spriteUrl": "https://cdn/sprites/a-1/walk.png",
    "spriteAnimations": {"walk": {"url": "https://cdn/sprites/a-1/walk.png", "frameCount": 6, "fps": 8}},
    "spriteUpdatedAt": 123,
    "currentLocation": "plaza",
    "onboardingPhase": "static",
    "userId": "u-1",
}


def _mocks(agent=AGENT, gs_connected=False, daemon_connected=True):
    svc = MagicMock()
    svc.get_town_agent_by_id.return_value = agent
    gs = MagicMock()
    gs._agent_connections = {"a-1": object()} if gs_connected else {}
    gs.update_agent_sprites = AsyncMock()
    gs.connect_agent = AsyncMock()
    ws = MagicMock()
    conn = MagicMock(location_context="town", user_id="u-1", connection_id="c-1")
    ws.get_by_agent_id.return_value = conn if daemon_connected else None
    return svc, gs, ws


@pytest.mark.asyncio
async def test_updates_sprites_when_agent_in_game_server():
    svc, gs, ws = _mocks(gs_connected=True)
    with (
        patch("core.services.sprite_update_apply.TownService", return_value=svc),
        patch("core.services.sprite_update_apply.get_game_server_client", return_value=gs),
        patch("core.services.sprite_update_apply.get_town_agent_ws_manager", return_value=ws),
        patch("core.services.sprite_update_apply.restore_phase_from_db") as restore,
    ):
        await apply_sprite_update("a-1", "animations_updated")
    restore.assert_called_once_with("a-1", "static")
    gs.update_agent_sprites.assert_awaited_once()
    gs.connect_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_connects_when_daemon_online_but_not_in_game_server():
    svc, gs, ws = _mocks(gs_connected=False, daemon_connected=True)
    with (
        patch("core.services.sprite_update_apply.TownService", return_value=svc),
        patch("core.services.sprite_update_apply.get_game_server_client", return_value=gs),
        patch("core.services.sprite_update_apply.get_town_agent_ws_manager", return_value=ws),
        patch("core.services.sprite_update_apply.restore_phase_from_db"),
    ):
        await apply_sprite_update("a-1", "static_ready")
    gs.connect_agent.assert_awaited_once()


@pytest.mark.asyncio
async def test_noop_when_agent_missing():
    svc, gs, ws = _mocks()
    svc.get_town_agent_by_id.return_value = None
    with (
        patch("core.services.sprite_update_apply.TownService", return_value=svc),
        patch("core.services.sprite_update_apply.get_game_server_client", return_value=gs),
        patch("core.services.sprite_update_apply.get_town_agent_ws_manager", return_value=ws),
    ):
        await apply_sprite_update("missing", "static_ready")
    gs.update_agent_sprites.assert_not_awaited()
    gs.connect_agent.assert_not_awaited()
```

`test_internal_sprite_event.py` — FastAPI TestClient (copy fixture style from `tests/unit/test_town_endpoints.py`): progress type calls `_send_to_daemon` with a `sprite_progress` event; `static_ready` awaits `apply_sprite_update`; wrong secret in non-dev returns 403 (mirror the assertions in whichever test covers `/ws-message` auth — grep `x-ws-lambda-secret` under `tests/`).

- [ ] **Step 2: Run to verify both fail** (module/route missing).

- [ ] **Step 3: Implement `sprite_update_apply.py`**

```python
"""Backend-local effects of a sprite change made by the sprite worker.

The worker owns all DynamoDB writes (spriteStatus / spriteUrl /
spriteAnimations / onboardingPhase). What it CANNOT touch is backend-process
state: the per-agent game-server WebSocket connections, the in-memory
ws_manager registry, and the phase-lattice cache mirror. This module is the
single place those effects happen — called by the internal
``/town/internal/sprite-event`` endpoint (worker → backend) and usable
in-process by the admin recomposite path.
"""

from __future__ import annotations

import logging

from core.services.daemon_bridge import _make_agent_event_callback, _send_to_daemon
from core.services.game_server_client import get_game_server_client
from core.services.phase_lattice import restore_phase_from_db
from core.services.town_agent_ws import get_town_agent_ws_manager
from core.services.town_service import TownService

logger = logging.getLogger(__name__)


async def apply_sprite_update(agent_id: str, kind: str) -> None:
    """Refresh local caches and push the agent's current sprites to the game server.

    kind: "static_ready" (Phase B done) or "animations_updated" (walk or a lazy
    animation landed). Both re-read DDB as the source of truth.
    """
    # Import here: these helpers live beside the connect path. Use the SAME
    # module agent_connect_service imports them from (grep `versioned_sprite_url`).
    from core.services.agent_connect_service import _version_animation_urls, versioned_sprite_url

    service = TownService()
    agent = service.get_town_agent_by_id(agent_id)
    if not agent:
        logger.warning("apply_sprite_update: agent %s not found", agent_id[:8])
        return

    restore_phase_from_db(agent_id, agent.get("onboardingPhase") or "alive")

    ws_manager = get_town_agent_ws_manager()
    conn = ws_manager.get_by_agent_id(agent_id)
    gs_client = get_game_server_client()
    sprite_url = versioned_sprite_url(agent.get("spriteUrl") or "", agent)
    animations = _version_animation_urls(agent.get("spriteAnimations", {}) or {}, agent)

    try:
        if agent_id in gs_client._agent_connections:
            await gs_client.update_agent_sprites(agent_id, animations)
        elif conn:
            await gs_client.connect_agent(
                agent_id=agent_id,
                display_name=agent.get("displayName", ""),
                sprite_url=sprite_url,
                location=agent.get("currentLocation", "plaza"),
                context=conn.location_context or "town",
                user_id=conn.user_id,
                sprite_animations=animations,
                on_event=_make_agent_event_callback(),
            )
    except Exception:
        logger.exception("apply_sprite_update: game-server push failed for %s", agent_id[:8])

    if kind == "animations_updated" and conn:
        _send_to_daemon(
            agent_id,
            {"event": "sprite_ready", "agent_id": agent_id, "sprite_url": agent.get("spriteUrl", "")},
        )
```

(If `versioned_sprite_url` / `_version_animation_urls` actually live in another
module — `routers/town.py` or a helpers module — import from there instead;
verify with grep before writing. If importing from `agent_connect_service`
creates a cycle, move the two helpers into `sprite_update_apply.py`'s module or
a shared location and re-export.)

- [ ] **Step 4: Implement the endpoint** in `routers/town.py`, adjacent to `/ws-message`:

```python
class SpriteEventRequest(BaseModel):
    agent_id: str
    type: Literal["progress", "static_ready", "animations_updated"]
    status: str | None = None


@router.post("/internal/sprite-event")
async def internal_sprite_event(request: SpriteEventRequest, raw_request: Request):
    """Sprite-worker → backend seam (spec 2026-06-11 §4).

    The worker owns PixelLab + DDB; this endpoint owns the backend-process
    side effects. Same shared-secret gate as /ws-message (and the same known
    caveat: the secret is not yet wired end-to-end through CDK — see
    core/config.py's WS_LAMBDA_SECRET note).
    """
    if settings.ENVIRONMENT != "dev" or settings.WS_LAMBDA_SECRET:
        provided = raw_request.headers.get("x-ws-lambda-secret", "")
        if provided != settings.WS_LAMBDA_SECRET:
            raise HTTPException(status_code=403, detail="forbidden")

    if request.type == "progress":
        _send_to_daemon(request.agent_id, {"event": "sprite_progress", "status": request.status or ""})
    else:
        from core.services.sprite_update_apply import apply_sprite_update

        await apply_sprite_update(request.agent_id, request.type)
    return {"ok": True}
```

(Reuse the exact secret-check block from `/ws-message` — copy its current form rather than this sketch if they differ.)

- [ ] **Step 5: Run both test files → PASS. Run full suite + ruff.**

- [ ] **Step 6: Commit** — `git commit -m "feat(sprites): internal sprite-event seam (worker -> backend effects)"`

---

### Task 6: Pipeline refactor — sink protocol + resume-aware `run_signup_pipeline` / `run_animation_job`

This is the core refactor. `core/services/sprite_pipeline.py` becomes worker-runnable: no direct game-server/ws_manager access; all backend effects go through a sink.

**Files:**
- Modify: `apps/backend/core/services/sprite_pipeline.py`
- Test: rewrite `apps/backend/tests/unit/services/` pipeline tests (grep for `_sprite_pipeline_task` and `resume_sprite_generation` under `tests/` and update each)

- [ ] **Step 1: Define the sink protocol** at the top of `sprite_pipeline.py`:

```python
class SpriteEventSink(Protocol):
    """Where pipeline side effects go. The worker passes an HTTP sink that
    POSTs /town/internal/sprite-event; in-backend callers (admin recomposite)
    pass LocalSpriteEventSink."""

    async def progress(self, agent_id: str, status: str) -> None: ...

    async def sprite_updated(self, agent_id: str, kind: str) -> None: ...


class LocalSpriteEventSink:
    """In-process sink for pipeline code that still runs inside the backend."""

    async def progress(self, agent_id: str, status: str) -> None:
        _send_to_daemon(agent_id, {"event": "sprite_progress", "status": status})

    async def sprite_updated(self, agent_id: str, kind: str) -> None:
        from core.services.sprite_update_apply import apply_sprite_update

        await apply_sprite_update(agent_id, kind)
```

- [ ] **Step 2: Write the failing tests.** New `tests/unit/services/test_run_signup_pipeline.py` covering, with `PixelLabService` fully mocked (AsyncMock methods) and `TownService` mocked:

1. **Fresh agent** (row: no char_id, status "queued") → `create_character` called; Phase B builds static (mock `fetch_all_rotations` → non-empty, `build_static_spritesheet_from_rotations`/`upload_sprite_to_s3` patched); `advance_phase` called with "static"; sink receives `sprite_updated("a-1", "static_ready")`; Phase C calls `queue_signup_animations`; final state write has `sprite_status="complete"`; `advance_phase(..., "alive", ...)`; sink receives `sprite_updated("a-1", "animations_updated")`.
2. **Resume with char_id, status "animating"** → `create_character` NOT called; goes straight to Phase C.
3. **Already complete** (status "complete", spriteReady True) → returns immediately; no PixelLab calls.
4. **create_character raises** → exception propagates (the worker releases the message) and `spriteStatus` is NOT cleared to None (resume must see "creating").
5. **`run_animation_job`**: agent with char_id, missing "talking" → `generate_animation` called; `spriteAnimations` merged (existing keys preserved); sink `sprite_updated(..., "animations_updated")`. Agent already has "talking" → no PixelLab call. Agent without char_id → no call, no raise (warn + return).

Write each as a real test with mocks; model the mock style on the existing pipeline tests you're replacing (look at how they patch `core.services.sprite_pipeline.PixelLabService` etc. — note the imports are function-local, so patch at the source module `core.services.pixellab_service.PixelLabService`).

- [ ] **Step 3: Run to verify they fail.**

- [ ] **Step 4: Implement.** Replace `_sprite_pipeline_task` with:

```python
async def run_signup_pipeline(agent_id: str, *, sink: SpriteEventSink) -> None:
    """Resume-aware signup pipeline: (A) create → (B) static → (C) walk.

    Reads the agent row and starts from whichever phase is incomplete, keyed
    on (pixellabCharacterId, spriteStatus). On transient failure it RAISES —
    the worker releases the SQS message and the redelivery resumes from the
    phase recorded in spriteStatus. It deliberately does NOT clear
    spriteStatus on failure (the old behavior) — that would erase the resume
    pointer the durability story depends on.
    """
```

Body — port the existing `_sprite_pipeline_task` code with these mechanical transformations:
- Read the row first: `agent = service.get_town_agent_by_id(agent_id)`; bail (return, success) if missing, or if `spriteStatus == "complete" and spriteReady`. Pull `display_name = agent.get("displayName", "")`, `appearance = agent.get("appearance", "")`, `char_id = agent.get("pixellabCharacterId")`, `status = agent.get("spriteStatus")`.
- Keep the `_resume_in_flight` per-agent guard exactly as-is (worker-side belt to the processor's braces).
- **Phase A** only when `not char_id` (requires `appearance`; if both missing, log + return). No try/except-return around `create_character` — let it raise.
- **Phase B** when `not agent.get("spriteUrl")` or `status in (None, "queued", "creating", "building_static")`. Same code as today except: every `_send_progress(...)` becomes `await sink.progress(agent_id, ...)`, and the entire "Connect to game server immediately" block (gs_client + ws_manager) is replaced by `await sink.sprite_updated(agent_id, "static_ready")` right after `advance_phase(..., "static", ...)`.
- **Phase C** as today (walk-only now via Task 2): progress via sink; after the final `update_agent_state(..., sprite_status="complete")` + `advance_phase(agent_id, "alive", service=service)`, replace the `update_agent_sprites` push, the "connect now" block, and the ManagementApiClient `sprite_ready` block with a single `await sink.sprite_updated(agent_id, "animations_updated")`.
- The outer `except Exception: ... sprite_status=None` handler is REMOVED (exceptions propagate). Keep `except PixelLabCreditError: raise` explicit for readability.

Add:

```python
async def run_animation_job(agent_id: str, anim_name: str, *, sink: SpriteEventSink) -> None:
    """Generate ONE lazy animation end-to-end (used for activity anims,
    idle, talking). Idempotent: skips if the animation already exists on the
    agent row. Raises on transient PixelLab failure (worker releases)."""
    from core.config import settings
    from core.services.pixellab_service import ANIMATION_REGISTRY, PixelLabService
    from core.services.sprite_storage import composite_animation_spritesheet, upload_sprite_to_s3

    service = TownService()
    agent = service.get_town_agent_by_id(agent_id)
    if not agent:
        return
    char_id = agent.get("pixellabCharacterId")
    if not char_id:
        logger.warning("[anim:%s] no pixellabCharacterId for %s — dropping", anim_name, agent_id[:8])
        return
    existing = agent.get("spriteAnimations") or {}
    if anim_name in existing:
        return
    if anim_name not in ANIMATION_REGISTRY:
        logger.warning("[anim:%s] unknown animation — dropping", anim_name)
        return

    pxl = PixelLabService()
    direction_frames = await pxl.generate_animation(char_id, anim_name)
    if not direction_frames:
        logger.warning("[anim:%s] no frames for %s", anim_name, agent_id[:8])
        return
    sheet = await asyncio.to_thread(composite_animation_spritesheet, direction_frames)
    if not sheet:
        return
    png_bytes, frame_count = sheet
    s3_key = await asyncio.to_thread(
        upload_sprite_to_s3, png_bytes, str(agent_id), settings.SPRITE_S3_BUCKET, f"{anim_name}.png"
    )
    registry_entry = ANIMATION_REGISTRY.get(anim_name, {})
    merged = dict(existing)
    merged[anim_name] = {
        "url": f"{settings.SPRITE_CDN_URL}/{s3_key}",
        "frameCount": frame_count,
        "fps": int(registry_entry.get("fps", 8)),
    }
    service.update_agent_state(agent_id, sprite_animations=merged)
    await sink.sprite_updated(agent_id, "animations_updated")
```

(Existing in-backend lazy-gen code in `activity_service._generate_activity_animation` does roughly this — port its composite/upload/merge details if they differ, then Task 7 deletes that copy. Note the read-merge-write on `spriteAnimations` is safe because the worker serializes per-agent via its active-set.)

`resume_sprite_generation` is now used ONLY by the admin recomposite endpoint with `current_status="downloading"`: delete the `"creating"` and `"animating"` branches (and their tests), keep the "downloading" re-download/recomposite branch, and have it call `LocalSpriteEventSink` semantics for its tail (it currently calls gs_client directly — acceptable to leave that tail as-is since it runs in the backend; just verify it still compiles and its tests pass). Update its docstring to say "recomposite-only".

- [ ] **Step 5: Update collateral.** `main.py` still imports `resume_sprite_generation` (removed in Task 7 — for now the import remains valid). `routers/town.py` may re-export `_sprite_pipeline_task` (grep line ~415) — update the re-export to `run_signup_pipeline` or delete it if nothing imports it.

- [ ] **Step 6: Run the new tests + full suite + ruff → PASS.**

- [ ] **Step 7: Commit** — `git commit -m "refactor(sprites): resume-aware pipeline behind a sink (worker-runnable)"`

---

### Task 7: Producer call-site swaps + delete SpriteQueue

**Files:**
- Modify: `apps/backend/routers/town.py` (register block ~line 458–490; admin regenerate endpoint from PR #364)
- Modify: `apps/backend/core/services/agent_connect_service.py` (~line 284 block)
- Modify: `apps/backend/core/services/activity_service.py` (lazy-gen block ~line 148–178, `_generate_activity_animation`)
- Modify: `apps/backend/core/services/conversation_service.py` (`_handle_chat`, after `create_active_conversation` ~line 280)
- Modify: `apps/backend/main.py` (drop lines 36–45 queue start/stop and the "Resume interrupted sprite generation" block ~143–167)
- Delete: `apps/backend/core/services/sprite_queue.py`, `apps/backend/tests/unit/services/test_sprite_queue.py`
- Tests: update `tests/unit/test_handle_agent_connect.py`, `tests/unit/test_quota_enforcement.py` (they reference sprite_queue — grep first); add talking-hook + activity-flag tests

- [ ] **Step 1: Write the failing tests.**

Add to the conversation tests (new file `tests/unit/services/test_talking_lazy_hook.py`, fixture style copied from `tests/unit/test_conversation_handlers.py`):

```python
@pytest.mark.asyncio
async def test_chat_enqueues_talking_for_agents_missing_it(...):
    # initiator + target rows WITHOUT "talking" in spriteAnimations, with char ids
    # → sprite_jobs.enqueue_animation called for both with "talking"

@pytest.mark.asyncio
async def test_chat_skips_talking_when_flag_off(monkeypatch, ...):
    # settings.ACTIVITY_ANIMATION_GENERATION patched False → enqueue not called

@pytest.mark.asyncio
async def test_chat_skips_talking_when_already_present(...):
    # "talking" in spriteAnimations → enqueue not called
```

Add to activity tests (`tests/unit/test_agent_activity_handler.py` or a new file): activity whose animation is missing → `sprite_jobs.enqueue_animation(agent_id, anim)` called when flag on; not called when flag off; not called when animation already present.

Update register-path and connect-path tests to assert `sprite_jobs.enqueue_signup` is called instead of `SpriteQueue.enqueue` (patch `core.services.sprite_jobs.enqueue_signup`).

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement the swaps.**

`routers/town.py` register block — replace the `get_sprite_queue()` usage:

```python
                from core.services import sprite_jobs

                queued = sprite_jobs.enqueue_signup(_agent_id, _display_name, _appearance)
                if queued:
                    _send_to_daemon(
                        _agent_id,
                        {"event": "sprite_progress", "status": "Queued for sprite generation"},
                    )
```

(The `_in_progress` pre-check above it stays — change its tuple to `sprite_jobs.IN_PROGRESS_STATUSES` so "queued" is included.)

`agent_connect_service.py` — replace the SpriteQueue import + enqueue with:

```python
        from core.services import sprite_jobs

        queued = sprite_jobs.enqueue_signup(agent_id, agent.get("displayName", agent_name), appearance)
        logger.info("[agent:%s] auto-enqueued sprite pipeline (queued=%s)", agent_id[:8], queued)
```

(keep the surrounding `if appearance and not spriteReady and spriteStatus is None and not char_id:` gating exactly as-is, and update the comment: durability + dedup now come from SQS + the DDB claim).

`activity_service.py` — replace the `asyncio.create_task(_generate_activity_animation(...))` block with:

```python
        if activity_anim not in agent_animations and anim_key not in _custom_anim_in_progress:
            if not settings.ACTIVITY_ANIMATION_GENERATION:
                logger.info("Activity animation generation disabled — skipping %s", activity_anim)
            elif char_id:
                _custom_anim_in_progress.add(anim_key)
                from core.services import sprite_jobs

                sprite_jobs.enqueue_animation(agent_id, activity_anim)
                logger.info("Enqueued lazy animation %s for agent %s", activity_anim, agent_id)
            else:
                logger.warning(...)
```

Delete `_generate_activity_animation` entirely (its logic now lives in `run_animation_job`, Task 6) and any imports it held. Keep `_custom_anim_in_progress` as the cheap local dedup.

`conversation_service.py` — add a module-level helper and call it in `_handle_chat` right after `create_active_conversation` (it has `target` row loaded; fetch the initiator row via `service.get_town_agent_by_id(initiator_id)` if no row is already in scope):

```python
def _maybe_enqueue_talking(agent_row: dict | None) -> None:
    """Lazy 'talking' generation on first conversation (fast/slow split)."""
    from core.config import settings
    from core.services import sprite_jobs

    if not settings.ACTIVITY_ANIMATION_GENERATION or not agent_row:
        return
    if "talking" in (agent_row.get("spriteAnimations") or {}):
        return
    if not agent_row.get("pixellabCharacterId"):
        return
    sprite_jobs.enqueue_animation(agent_row["id"], "talking")
```

`main.py` — remove the sprite-queue start/stop and the whole "Resume interrupted sprite generation" loop (worker boot sweep owns recovery now); remove now-unused imports.

Admin regenerate endpoint (PR #364 — grep `regenerate-sprite` in `routers/town.py`): replace its `resume_sprite_generation(...)`/task-spawn with explicit clear + force enqueue:

```python
    service.update_agent_state(agent_id, sprite_status="queued", pixellab_character_id=None)
    from core.services import sprite_jobs

    sent = sprite_jobs.enqueue_signup(agent_id, agent.get("displayName", ""), agent.get("appearance", ""), force=True)
    return {"ok": sent, "agent_id": agent_id, "mode": "queued"}
```

(Adapt the return shape to whatever the endpoint returns today; keep its dev-only guard.)

Then `git rm apps/backend/core/services/sprite_queue.py apps/backend/tests/unit/services/test_sprite_queue.py`.

- [ ] **Step 4: Run the full backend suite + ruff; fix every fallout** (grep `sprite_queue` repo-wide — zero references must remain in `apps/backend/`).

- [ ] **Step 5: Commit** — `git commit -m "feat(sprites): backend becomes SQS producer; SpriteQueue deleted; talking lazy hook"`

---

### Task 8: The sprite worker service

**Files:**
- Create: `apps/backend/sprite_worker/__init__.py` (empty), `apps/backend/sprite_worker/backend_sink.py`, `apps/backend/sprite_worker/processor.py`, `apps/backend/sprite_worker/__main__.py`
- Test: `apps/backend/tests/unit/sprite_worker/__init__.py`, `test_processor.py`, `test_backend_sink.py`, `test_worker_loop.py` (create)

- [ ] **Step 1: Write the failing processor tests** (`test_processor.py`):

```python
"""Worker message processing: parsing, idempotency, outcome mapping."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from core.services.pixellab_service import PixelLabCreditError
from sprite_worker import processor
from sprite_worker.processor import Outcome


def _msg(**over):
    body = {"schema": 1, "kind": "signup", "agent_id": "a-1", "display_name": "Ada", "appearance": "x"}
    body.update(over)
    return json.dumps(body)


@pytest.fixture(autouse=True)
def _clear_active():
    processor._active.clear()


@pytest.mark.asyncio
async def test_malformed_json_is_deleted():
    assert await processor.process_message("{not json", sink=AsyncMock()) == Outcome.DELETE


@pytest.mark.asyncio
async def test_unknown_kind_is_deleted():
    assert await processor.process_message(_msg(kind="dance"), sink=AsyncMock()) == Outcome.DELETE


@pytest.mark.asyncio
async def test_signup_success_deletes():
    with patch("sprite_worker.processor.run_signup_pipeline", new=AsyncMock()) as run:
        assert await processor.process_message(_msg(), sink=AsyncMock()) == Outcome.DELETE
    run.assert_awaited_once()


@pytest.mark.asyncio
async def test_transient_failure_releases():
    with patch("sprite_worker.processor.run_signup_pipeline", new=AsyncMock(side_effect=RuntimeError)):
        assert await processor.process_message(_msg(), sink=AsyncMock()) == Outcome.RELEASE


@pytest.mark.asyncio
async def test_credit_error_pauses():
    with patch("sprite_worker.processor.run_signup_pipeline", new=AsyncMock(side_effect=PixelLabCreditError)):
        assert await processor.process_message(_msg(), sink=AsyncMock()) == Outcome.CREDIT_PAUSE


@pytest.mark.asyncio
async def test_duplicate_for_active_agent_releases_without_processing():
    processor._active.add("a-1")
    with patch("sprite_worker.processor.run_signup_pipeline", new=AsyncMock()) as run:
        assert await processor.process_message(_msg(), sink=AsyncMock()) == Outcome.RELEASE
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_animation_kind_routes_to_run_animation_job():
    with patch("sprite_worker.processor.run_animation_job", new=AsyncMock()) as run:
        out = await processor.process_message(_msg(kind="animation", anim_name="talking"), sink=AsyncMock())
    assert out == Outcome.DELETE
    run.assert_awaited_once_with("a-1", "talking", sink=run.await_args.kwargs["sink"])
```

- [ ] **Step 2: Run → fails (module missing).**

- [ ] **Step 3: Implement `processor.py`**

```python
"""Resume-aware processing of sprite-job SQS messages."""

from __future__ import annotations

import enum
import json
import logging

from core.services.pixellab_service import PixelLabCreditError
from core.services.sprite_pipeline import run_animation_job, run_signup_pipeline

logger = logging.getLogger(__name__)


class Outcome(enum.Enum):
    DELETE = "delete"  # done (or permanently un-processable) — remove from queue
    RELEASE = "release"  # transient failure / duplicate-in-flight — let visibility lapse
    CREDIT_PAUSE = "credit_pause"  # 402 — long visibility + metric, queue effectively pauses


# Per-process serialization: one job per agent at a time. SQS standard
# queues can deliver duplicates; a duplicate for an in-flight agent is
# RELEASEd (it will re-poll later and find the work complete → DELETE).
_active: set[str] = set()


async def process_message(raw_body: str, *, sink) -> Outcome:
    try:
        body = json.loads(raw_body)
    except (json.JSONDecodeError, TypeError):
        logger.error("sprite-worker: malformed message dropped: %.200s", raw_body)
        return Outcome.DELETE

    kind = body.get("kind")
    agent_id = body.get("agent_id")
    if not agent_id or kind not in ("signup", "animation"):
        logger.error("sprite-worker: unknown message dropped: %.200s", raw_body)
        return Outcome.DELETE

    if agent_id in _active:
        logger.info("sprite-worker: %s already in flight — releasing duplicate", agent_id[:8])
        return Outcome.RELEASE

    _active.add(agent_id)
    try:
        if kind == "signup":
            await run_signup_pipeline(agent_id, sink=sink)
        else:
            await run_animation_job(agent_id, body.get("anim_name", ""), sink=sink)
        return Outcome.DELETE
    except PixelLabCreditError:
        logger.error("sprite-worker: PixelLab credits exhausted (agent %s)", agent_id[:8])
        return Outcome.CREDIT_PAUSE
    except Exception:
        logger.exception("sprite-worker: job failed for %s — releasing for retry", agent_id[:8])
        return Outcome.RELEASE
    finally:
        _active.discard(agent_id)
```

- [ ] **Step 4: Implement `backend_sink.py`** (+ `test_backend_sink.py`: posts correct payloads with the secret header; retries on 5xx/transport error up to 3 times; never raises — use `httpx.MockTransport`):

```python
"""HTTP sink: worker-side sprite events → backend internal endpoint."""

from __future__ import annotations

import asyncio
import logging

import httpx

from core.config import settings

logger = logging.getLogger(__name__)


class HttpSpriteEventSink:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._url = f"{settings.BACKEND_INTERNAL_URL}/api/v1/town/internal/sprite-event"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, payload: dict) -> None:
        headers = {}
        if settings.WS_LAMBDA_SECRET:
            headers["x-ws-lambda-secret"] = settings.WS_LAMBDA_SECRET
        for attempt in range(3):
            try:
                resp = await self._client.post(self._url, json=payload, headers=headers)
                if resp.status_code < 500:
                    return
                logger.warning("sprite-event POST -> %d (attempt %d/3)", resp.status_code, attempt + 1)
            except httpx.HTTPError as exc:
                logger.warning("sprite-event POST failed: %s (attempt %d/3)", exc, attempt + 1)
            await asyncio.sleep(1.5 * (attempt + 1))
        logger.error("sprite-event POST gave up: %s", payload.get("type"))

    async def progress(self, agent_id: str, status: str) -> None:
        await self._post({"agent_id": agent_id, "type": "progress", "status": status})

    async def sprite_updated(self, agent_id: str, kind: str) -> None:
        await self._post({"agent_id": agent_id, "type": kind})
```

(Confirm the router mount prefix — `TOWN_API_URL` ends in `/api/v1`, and the town router serves `/town/...` under it; adjust `self._url` to match reality by checking `main.py`'s `include_router` prefix.)

- [ ] **Step 5: Implement `__main__.py`** (+ `test_worker_loop.py` for `_handle_outcome` mapping and `_boot_sweep` filtering — keep the loop itself thin enough that those two pure-ish functions carry the logic):

```python
"""Sprite worker entrypoint — single-replica drainer of goosetown-{env}-sprite-jobs.

Mirrors worldgm/__main__.py packaging: same backend Docker image, command
override ``uv run python -m sprite_worker``. This is the ONLY process that
calls PixelLab generation, which is what makes the animation semaphore in
pixellab_service a true GLOBAL cap (spec 2026-06-11).
"""

from __future__ import annotations

import asyncio
import logging
import signal

import boto3

from core.config import settings
from sprite_worker.backend_sink import HttpSpriteEventSink
from sprite_worker.processor import Outcome, process_message

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

VISIBILITY_SECONDS = 300
HEARTBEAT_SECONDS = 120
CREDIT_PAUSE_SECONDS = 900

_should_exit = False


def _signal_handler(signum: int, frame: object) -> None:  # noqa: ARG001
    global _should_exit  # noqa: PLW0603
    logger.info("Received signal %d, shutting down...", signum)
    _should_exit = True


signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)


def _sqs():
    return boto3.client("sqs", region_name=settings.AWS_REGION)


def _emit_credit_metric() -> None:
    try:
        boto3.client("cloudwatch", region_name=settings.AWS_REGION).put_metric_data(
            Namespace="GooseTown/SpriteWorker",
            MetricData=[{"MetricName": "CreditExhausted", "Value": 1, "Unit": "Count"}],
        )
    except Exception:
        logger.exception("Failed to emit CreditExhausted metric")


def _boot_sweep() -> int:
    """Re-enqueue agents stuck in an in-progress spriteStatus (backstop —
    primary recovery is SQS redelivery; this covers rows whose SQS send
    failed and pre-migration stuck rows). Idempotent: processing is
    resume-aware and skips completed work."""
    from core.services import sprite_jobs
    from core.services.town_service import TownService

    swept = 0
    try:
        agents = TownService().get_all_active_agents()
    except Exception:
        logger.exception("Boot sweep: agent scan failed — continuing without sweep")
        return 0
    for agent in agents:
        if agent.get("spriteStatus") in sprite_jobs.IN_PROGRESS_STATUSES:
            if sprite_jobs.enqueue_signup(
                agent["id"], agent.get("displayName", ""), agent.get("appearance", ""), force=True
            ):
                swept += 1
    logger.info("Boot sweep: re-enqueued %d stuck agent(s)", swept)
    return swept


async def _heartbeat(sqs, queue_url: str, receipt: str, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_SECONDS)
        except asyncio.TimeoutError:
            try:
                await asyncio.to_thread(
                    sqs.change_message_visibility,
                    QueueUrl=queue_url,
                    ReceiptHandle=receipt,
                    VisibilityTimeout=VISIBILITY_SECONDS,
                )
            except Exception:
                logger.warning("Heartbeat visibility extension failed", exc_info=True)


def _apply_outcome(sqs, queue_url: str, receipt: str, outcome: Outcome) -> None:
    if outcome is Outcome.DELETE:
        sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
    elif outcome is Outcome.CREDIT_PAUSE:
        _emit_credit_metric()
        sqs.change_message_visibility(
            QueueUrl=queue_url, ReceiptHandle=receipt, VisibilityTimeout=CREDIT_PAUSE_SECONDS
        )
    # RELEASE: do nothing — visibility lapses naturally (~5 min), 3 receives → DLQ.


async def _handle(sqs, queue_url: str, msg: dict, sink) -> None:
    receipt = msg["ReceiptHandle"]
    stop = asyncio.Event()
    hb = asyncio.create_task(_heartbeat(sqs, queue_url, receipt, stop))
    try:
        outcome = await process_message(msg["Body"], sink=sink)
    finally:
        stop.set()
        await hb
    try:
        await asyncio.to_thread(_apply_outcome, sqs, queue_url, receipt, outcome)
    except Exception:
        logger.exception("Failed to apply outcome %s", outcome)


async def main() -> None:
    logger.info(
        "Sprite worker starting (queue=%s, pipelines=%d, anims=%d)",
        settings.SPRITE_JOBS_QUEUE_NAME,
        settings.SPRITE_WORKER_MAX_CONCURRENT_PIPELINES,
        settings.PIXELLAB_MAX_CONCURRENT_ANIMATIONS,
    )
    for var in ("PIXELLAB_API_KEY", "SPRITE_S3_BUCKET", "SPRITE_CDN_URL", "BACKEND_INTERNAL_URL"):
        if not getattr(settings, var):
            raise RuntimeError(f"sprite_worker: missing required env {var}")

    sqs = _sqs()
    queue_url = sqs.get_queue_url(QueueName=settings.SPRITE_JOBS_QUEUE_NAME)["QueueUrl"]
    sink = HttpSpriteEventSink()
    tasks: set[asyncio.Task] = set()

    await asyncio.to_thread(_boot_sweep)

    while not _should_exit:
        free = settings.SPRITE_WORKER_MAX_CONCURRENT_PIPELINES - len(tasks)
        if free <= 0:
            await asyncio.sleep(1)
            continue
        try:
            resp = await asyncio.to_thread(
                sqs.receive_message,
                QueueUrl=queue_url,
                MaxNumberOfMessages=min(10, free),
                WaitTimeSeconds=20,
            )
        except Exception:
            logger.exception("receive_message failed — backing off")
            await asyncio.sleep(5)
            continue
        for msg in resp.get("Messages", []):
            task = asyncio.create_task(_handle(sqs, queue_url, msg, sink))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

    if tasks:
        logger.info("Draining %d in-flight job(s) before exit (25s budget)...", len(tasks))
        await asyncio.wait(tasks, timeout=25)

    from core.services.pixellab_service import close_pixellab_client

    await close_pixellab_client()
    await sink.aclose()
    logger.info("Sprite worker exiting cleanly")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 6: Run all sprite_worker tests + full suite + ruff → PASS.**

- [ ] **Step 7: Commit** — `git commit -m "feat(sprites): single-replica sprite worker draining the durable queue"`

---

### Task 9: Infra — SpriteWorkerStack + wiring

**Files:**
- Create: `apps/infra/lib/stacks/sprite-worker-stack.ts`
- Modify: `apps/infra/lib/goosetown-stage.ts`, `apps/infra/lib/stacks/compute-stack.ts`
- Test: `apps/infra/test/sprite-worker-stack.test.ts` (create; note `apps/infra/test` is not in CI — still write it)

- [ ] **Step 1: Write `sprite-worker-stack.ts`** — clone the WorldgmStack shape:

```typescript
import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as sqs from "aws-cdk-lib/aws-sqs";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cw_actions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as sns from "aws-cdk-lib/aws-sns";
import { DockerImageAsset, Platform } from "aws-cdk-lib/aws-ecr-assets";
import * as path from "path";
import { Construct } from "constructs";
import { AuthSecrets } from "./auth-stack";
import { DatabaseTables } from "./database-stack";

export interface SpriteWorkerStackProps extends cdk.StackProps {
  environment: string;
  cluster: ecs.ICluster;
  vpc: ec2.IVpc;
  secrets: AuthSecrets;
  tables: DatabaseTables;
  spriteBucket: s3.IBucket;
  alarmTopic?: sns.ITopic;
}

/**
 * Sprite worker — single-replica Fargate service that drains the durable
 * sprite-jobs SQS queue and is the ONLY process that calls PixelLab
 * generation (global concurrency cap by construction). Spec 2026-06-11.
 * Same packaging pattern as WorldgmStack: backend image, command override.
 */
export class SpriteWorkerStack extends cdk.Stack {
  public readonly service: ecs.FargateService;
  public readonly queue: sqs.Queue;

  constructor(scope: Construct, id: string, props: SpriteWorkerStackProps) {
    super(scope, id, props);
    const env = props.environment;

    const dlq = new sqs.Queue(this, "SpriteJobsDlq", {
      queueName: `goosetown-${env}-sprite-jobs-dlq`,
      retentionPeriod: cdk.Duration.days(14),
    });

    this.queue = new sqs.Queue(this, "SpriteJobsQueue", {
      // Physical name is load-bearing: producers (backend) and the worker
      // resolve it at runtime via GetQueueUrl by name — no cross-stack object.
      queueName: `goosetown-${env}-sprite-jobs`,
      visibilityTimeout: cdk.Duration.seconds(300),
      retentionPeriod: cdk.Duration.days(4),
      receiveMessageWaitTime: cdk.Duration.seconds(20),
      deadLetterQueue: { queue: dlq, maxReceiveCount: 3 },
    });

    const backendImage = new DockerImageAsset(this, "SpriteWorkerImage", {
      directory: path.join(__dirname, "..", "..", "..", "backend"),
      platform: Platform.LINUX_AMD64,
    });

    const logGroup = new logs.LogGroup(this, "LogGroup", {
      logGroupName: `/ecs/goosetown-${env}-sprite-worker`,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const taskDef = new ecs.FargateTaskDefinition(this, "TaskDef", {
      cpu: 512,
      memoryLimitMiB: 1024,
    });

    taskDef.addContainer("SpriteWorker", {
      image: ecs.ContainerImage.fromDockerImageAsset(backendImage),
      command: ["uv", "run", "python", "-m", "sprite_worker"],
      logging: ecs.LogDrivers.awsLogs({ logGroup, streamPrefix: "sprite-worker" }),
      environment: {
        AWS_REGION: cdk.Stack.of(this).region,
        ENVIRONMENT: env,
        DYNAMODB_TABLE_PREFIX: `goosetown-${env}`,
        SPRITE_S3_BUCKET: props.spriteBucket.bucketName,
        SPRITE_CDN_URL: `https://assets-${env}.goosetown.isol8.co`,
        SPRITE_JOBS_QUEUE_NAME: `goosetown-${env}-sprite-jobs`,
        BACKEND_INTERNAL_URL: `https://api-${env}.goosetown.isol8.co`,
        // Global PixelLab animation concurrency (each animation = up to 4
        // jobs). Tune after PixelLab confirms the account cap.
        PIXELLAB_MAX_CONCURRENT_ANIMATIONS: "2",
        SPRITE_WORKER_MAX_CONCURRENT_PIPELINES: "2",
      },
      secrets: {
        PIXELLAB_API_KEY: ecs.Secret.fromSecretsManager(props.secrets.pixelLabApiKey),
      },
    });

    this.queue.grantConsumeMessages(taskDef.taskRole);
    this.queue.grantSendMessages(taskDef.taskRole); // boot sweep re-enqueue
    props.tables.agents.grantReadWriteData(taskDef.taskRole);
    props.spriteBucket.grantReadWrite(taskDef.taskRole);
    taskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["cloudwatch:PutMetricData"],
        resources: ["*"],
      }),
    );

    this.service = new ecs.FargateService(this, "Service", {
      serviceName: `goosetown-${env}-sprite-worker`,
      cluster: props.cluster,
      taskDefinition: taskDef,
      desiredCount: 1,
      assignPublicIp: false,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      circuitBreaker: { rollback: true },
    });

    if (props.alarmTopic) {
      const dlqAlarm = new cloudwatch.Alarm(this, "SpriteJobsDlqNotEmpty", {
        alarmName: `goosetown-${env}-sprite-jobs-dlq-not-empty`,
        metric: dlq.metricApproximateNumberOfMessagesVisible({ period: cdk.Duration.minutes(5) }),
        threshold: 1,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        evaluationPeriods: 1,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        alarmDescription:
          "Sprite jobs are dead-lettering (3 failed attempts each). Check PixelLab credits/status and sprite-worker logs. See RUNBOOK.md.",
      });
      dlqAlarm.addAlarmAction(new cw_actions.SnsAction(props.alarmTopic));

      const ageAlarm = new cloudwatch.Alarm(this, "SpriteJobsBacklogAge", {
        alarmName: `goosetown-${env}-sprite-jobs-backlog-age`,
        metric: this.queue.metricApproximateAgeOfOldestMessage({ period: cdk.Duration.minutes(5) }),
        threshold: 1800,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        evaluationPeriods: 3,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        alarmDescription: "Oldest sprite job > 30 min — worker stuck, dead, or PixelLab degraded.",
      });
      ageAlarm.addAlarmAction(new cw_actions.SnsAction(props.alarmTopic));

      const taskMissingAlarm = new cloudwatch.Alarm(this, "SpriteWorkerTaskMissing", {
        alarmName: `goosetown-${env}-sprite-worker-task-missing`,
        metric: new cloudwatch.Metric({
          namespace: "AWS/ECS",
          metricName: "CPUUtilization",
          dimensionsMap: {
            ClusterName: props.cluster.clusterName,
            ServiceName: this.service.serviceName,
          },
          statistic: "SampleCount",
          period: cdk.Duration.minutes(1),
        }),
        threshold: 1,
        comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
        evaluationPeriods: 5,
        treatMissingData: cloudwatch.TreatMissingData.BREACHING,
        alarmDescription:
          "Sprite worker has no running task. New signups keep their static placeholder until it returns (jobs are durable in SQS).",
      });
      taskMissingAlarm.addAlarmAction(new cw_actions.SnsAction(props.alarmTopic));

      const creditAlarm = new cloudwatch.Alarm(this, "PixelLabCreditExhausted", {
        alarmName: `goosetown-${env}-pixellab-credit-exhausted`,
        metric: new cloudwatch.Metric({
          namespace: "GooseTown/SpriteWorker",
          metricName: "CreditExhausted",
          statistic: "Sum",
          period: cdk.Duration.minutes(5),
        }),
        threshold: 1,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        evaluationPeriods: 1,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        alarmDescription:
          "PixelLab returned 402 (out of generations). Top up credits; queued jobs auto-resume.",
      });
      creditAlarm.addAlarmAction(new cw_actions.SnsAction(props.alarmTopic));
    }

    new cdk.CfnOutput(this, "QueueName", { value: this.queue.queueName });
    new cdk.CfnOutput(this, "ServiceName", { value: this.service.serviceName });
  }
}
```

(Check `AuthSecrets`' actual property name for the PixelLab secret — compute-stack uses `props.secrets.pixelLabApiKey`.)

- [ ] **Step 2: Wire into `goosetown-stage.ts`** after the WorldgmStack block:

```typescript
    new SpriteWorkerStack(this, `goosetown-${env}-sprite-worker`, {
      stackName: `goosetown-${env}-sprite-worker`,
      environment: env,
      cluster: clusterStack.cluster,
      vpc: network.vpc,
      secrets: auth.secrets,
      tables: database.tables,
      spriteBucket: storage.spriteBucket,
      alarmTopic: observability.snsTopic,
    });
```

(plus the import at the top.)

- [ ] **Step 3: `compute-stack.ts`** — in the backend container `environment` block add:

```typescript
        // Durable sprite pipeline (spec 2026-06-11): backend is producer-only.
        SPRITE_JOBS_QUEUE_NAME: `goosetown-${env}-sprite-jobs`,
        // Lazy animation generation (activity anims + talking). Dev MUST stay
        // false so it never burns the shared PixelLab account on lazy gen.
        ACTIVITY_ANIMATION_GENERATION: env === "prod" ? "true" : "false",
```

and grant the backend task role producer rights (ARN-string pattern, same as worldgm's `agent-*` grant — find where `taskRole` policies are added in compute-stack and append):

```typescript
    taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["sqs:SendMessage", "sqs:GetQueueUrl"],
        resources: [
          `arn:aws:sqs:${cdk.Stack.of(this).region}:${cdk.Stack.of(this).account}:goosetown-${env}-sprite-jobs`,
        ],
      }),
    );
```

- [ ] **Step 4: Write `apps/infra/test/sprite-worker-stack.test.ts`** asserting: queue named `goosetown-dev-sprite-jobs` with `VisibilityTimeout: 300` and a RedrivePolicy with `maxReceiveCount: 3`; service `DesiredCount: 1`; container command `["uv","run","python","-m","sprite_worker"]`. Follow the assertion style of `apps/infra/test/storage-stack.test.ts`.

- [ ] **Step 5: Synth.**

```bash
rm -rf apps/backend/.venv   # critical rule — synth hangs otherwise
cd apps/infra && npx cdk synth --quiet
```

Expected: synth completes with no errors. (Do NOT deploy anything.)

- [ ] **Step 6: Commit** — `git commit -m "feat(infra): SpriteWorkerStack — durable sprite-jobs queue + single-replica worker"`

---

### Task 10: Full verification + docs

**Files:**
- Modify: `CLAUDE.md` (architecture/sprite-pipeline sections), `apps/infra/RUNBOOK.md` (sprite worker rollback + DLQ replay + per-env PixelLab key steps)

- [ ] **Step 1: Full backend suite + lint**

```bash
cd apps/backend && uv run pytest tests/ -q && uv run ruff check . && uv run ruff format --check .
```

Expected: all pass, no lint errors.

- [ ] **Step 2: CLAUDE.md updates** — in the Sprite Pipeline section: describe the producer → SQS → worker flow, walk-only signup + lazy idle/talking, the internal sprite-event seam, the new env flags, and mark the "PixelLab credit-exhaustion alarm missing" launch blocker resolved (DLQ + CreditExhausted alarms). Keep edits surgical.

- [ ] **Step 3: RUNBOOK.md** — add a SpriteWorkerStack section: rollback = previous task def; DLQ replay = `aws sqs start-message-move-task --source-arn <dlq-arn>` (or manual receive/send); the per-env PixelLab key steps from spec §7 verbatim.

- [ ] **Step 4: Commit** — `git commit -m "docs: durable sprite pipeline (CLAUDE.md + runbook)"`

- [ ] **Step 5: Push + PR.** Push `feat/durable-sprite-pipeline`, open a PR titled `feat(sprites): durable SQS sprite pipeline + single-replica worker (global PixelLab throttle)`. Body: link spec; the two faults; behavior changes (walk-only signup, lazy idle/talking, dev lazy-gen off); flag the human actions (per-env PixelLab key; PixelLab email). Wait for codex + claude-review; E2E Smoke failure is known/pre-existing — ignore it.

## Plan self-review notes

- Spec §1–§6, §9 map to Tasks 1–10; §7 (key split) lands in RUNBOOK (Task 10) + PR body; §8 (email) is delivered in the session summary, not the repo.
- `IN_PROGRESS_STATUSES` lives in `sprite_jobs.py` and is imported everywhere else (Task 4 defines; Tasks 3/7/8 consume — Task 3's DDB condition repeats the values inline because DynamoDB expressions can't import constants; keep the two lists in sync, there's a comment for it).
- `run_signup_pipeline` raises on failure (Task 6) ↔ processor maps exceptions to RELEASE (Task 8) ↔ queue maxReceiveCount 3 → DLQ (Task 9). Consistent.
- Sink protocol: `progress(agent_id, status)` / `sprite_updated(agent_id, kind)` used identically in Tasks 5, 6, 8.
