# Durable, Globally-Throttled Sprite Pipeline — Design

**Date:** 2026-06-11
**Status:** approved approach (B — dedicated worker), implementation pending
**Origin:** 2026-06-11 prod incident — 19 residents seeded at once, stalled at 4/19 sprites.
**Builds on:** `2026-06-09-sprite-pipeline-reliability-design.md` (PR #377: pooled client,
batched directions, process-global animation semaphore). This spec is the next layer:
that work made one process reliable; this work makes the *fleet* reliable.

## Incident (root cause already established — verified against code, not re-derived)

19 prod residents enqueued in one burst at the exact minute a backend rolling deploy
drained the task holding the queue:

- 4 completed before the drain.
- 4 froze mid-flight (`spriteStatus` creating/downloading, `pixellabCharacterId` set) —
  their coroutines died with the task.
- 11 never started (`spriteStatus` None, no char_id) — they existed only in the drained
  task's in-memory `asyncio.Queue`. **Lost.**
- The replacement task's startup recovery (`main.py:_reconnect_active_agents` →
  `resume_sprite_generation`) ran **before the residents existed**, so nothing re-enqueued
  them. Startup-scan recovery is a race, not a guarantee.

Aggravator: PixelLab 429 "too many concurrent jobs" (account-level cap). The animation
semaphore from PR #377 (`pixellab_service._get_animation_semaphore`) is **per-process**;
prod autoscales 2→6 backend replicas, so combined fleet concurrency is
`replicas × PIXELLAB_MAX_CONCURRENT_ANIMATIONS` — it blows the account cap by design.

### The two fatal faults

1. **In-memory, per-process job queue** (`core/services/sprite_queue.py`). Any deploy,
   scale-in, or crash loses every queued job in that task.
2. **Per-process concurrency limiter** (`pixellab_service.py` semaphore + per-replica
   `SpriteQueue(max_concurrent=1)`). No global view of in-flight PixelLab jobs.

## Goals

1. A deploy/scale/crash event must never lose a sprite job (durable queue).
2. Fleet-wide PixelLab concurrency stays under the account cap regardless of replica
   count (global throttle).
3. Keep the never-block-the-user property: Phase B static sprite appears fast; likeness
   fills in async. (Unchanged; agents also connect with shadow placeholder pre-sprite.)
4. Fast/slow split (task #40): signup generates **create + walk only**; idle/talking/
   activity animations generate lazily. Cuts signup PixelLab jobs roughly in half.
5. Prevention flags: dev never runs lazy animation generation against the shared
   PixelLab account (`ACTIVITY_ANIMATION_GENERATION=false` on dev);
   `PIXELLAB_MAX_CONCURRENT_ANIMATIONS` explicitly tuned where generation runs.
6. Per-env PixelLab API keys (Secrets Manager plumbing already per-env; the second key
   itself is a human dashboard action — flagged, steps drafted, no fake key).

## Approaches considered

**A. SQS durable queue + DynamoDB lease/token limiter; pipelines keep running in backend
replicas.** Every replica long-polls SQS; before each PixelLab call it acquires a lease
row/counter token in DynamoDB (TTL'd, heartbeat-extended). 
*Pros:* no new ECS service (~$0 infra delta); pipeline keeps in-process access to the
game-server client and ws_manager cache.
*Cons:* distributed-lease correctness is genuinely hard — a task killed mid-job holds its
token until TTL expiry (cap underutilized) or the TTL is short and the cap breaches; the
fleet's effective concurrency depends on autoscale timing; heavy PIL compositing stays on
API-serving tasks (the event-loop starvation class from PR #365); a new failure mode per
replica. More code, more failure modes, same dollar cost in engineering.

**B. SQS durable queue + ONE dedicated single-replica sprite worker (desiredCount=1)
draining it. (CHOSEN)** All generation moves off the API tasks into a small Fargate
service that reuses the backend image with a command override — exactly the `WorldgmStack`
pattern (and the single-replica game server ADR precedent).
*Pros:* global concurrency is correct **by construction** — one process, so PR #377's
semaphore becomes globally true with zero new machinery; PixelLab + PIL workload isolated
from API serving; deploys of the backend no longer touch generation at all; worker deploys
are safe because SQS redelivers and processing is resume-aware. Matches two existing
architectural precedents.
*Cons:* one more Fargate service (~$10–15/mo/env at 0.5 vCPU / 1 GB); generation
throughput capped at one task (fine: PixelLab's account cap is the real ceiling, and it's
far below what one task can drive); two cross-process seams (below).

**C. Tune/keep the status quo** (bigger semaphore, more retries). Rejected — fixes
neither fault; explicitly the class of symptom-patch the project forbids.

**Decision: B.** The lease limiter (A) spends its complexity budget simulating what a
single process gets for free, and history here (memory: fix root causes, not retries)
favors the structural fix. Single-task throughput is not a constraint because PixelLab's
account-level cap — the very thing we're respecting — is the bottleneck either way.

## Design

```
register / agent-connect / activity / conversation   (backend, any replica)
        │  conditional DDB gate (spriteStatus=queued) → SQS SendMessage
        ▼
goosetown-{env}-sprite-jobs  ──(maxReceiveCount 3)──▶  goosetown-{env}-sprite-jobs-dlq + alarm
        │ long-poll, visibility 300s + heartbeat
        ▼
sprite-worker (Fargate, desiredCount=1, backend image, `python -m sprite_worker`)
   ├── resume-aware pipeline (PixelLab + PIL + S3 + DDB writes)   [global semaphore here]
   └── POST /town/internal/sprite-event ──▶ backend (any replica)
            └── _send_to_daemon progress / gs_client connect+update_agent_sprites /
                phase-lattice cache refresh
```

### 1. Durable queue: `goosetown-{env}-sprite-jobs` (SQS standard + DLQ)

- Created in the new `SpriteWorkerStack`. DLQ `goosetown-{env}-sprite-jobs-dlq`,
  `maxReceiveCount: 3`. Message retention 4 days (default). Long polling 20 s.
- **Visibility timeout 300 s with heartbeat**: the worker extends visibility
  (`ChangeMessageVisibility`, +300 s every 120 s) while a job is in flight. A worker
  death surfaces the message again within ≤5 min; a full signup pipeline (~5–20 min)
  never exceeds visibility while alive.
- Message body (JSON):
  `{schema: 1, kind: "signup" | "animation", agent_id, display_name?, appearance?, anim_name?, enqueued_at}`.
- Producers resolve the queue URL at runtime by name (`get_queue_url`) — same pattern as
  `sqs_drainer.py`. IAM for the backend producer is granted in `ComputeStack` by ARN
  string (`arn:aws:sqs:…:goosetown-${env}-sprite-jobs`), avoiding any cross-stack object
  dependency — the established worldgm `agent-*` pattern.

### 2. Producers (backend) — enqueue with a DDB idempotency gate

A new thin module `core/services/sprite_jobs.py` (producer only — boto3 `send_message`):

- `enqueue_signup(agent_id, display_name, appearance)` — first does a **conditional**
  DynamoDB update: set `spriteStatus="queued"` only if `spriteStatus` is absent/None/
  not already in (`queued`, `creating`, `building_static`, `animating`, `downloading`).
  On `ConditionalCheckFailed` → skip (someone already enqueued). Then SQS send. If the
  send fails after the DDB write, the worker boot sweep (§3) re-enqueues `queued` rows —
  the write order makes failures self-healing rather than job-losing.
- `enqueue_animation(agent_id, anim_name)` — for lazy animations; dedup via the existing
  `spriteAnimations` membership check at the call sites plus worker-side idempotency
  (skip if the animation already exists on the agent row).

Call-site changes:
- `routers/town.py` register: replace `get_sprite_queue().enqueue(...)` with
  `enqueue_signup(...)` (the existing `_in_progress` pre-check stays as a fast path).
- `agent_connect_service.py:~284` auto-enqueue-on-connect: same replacement; existing
  gating (`appearance and not spriteReady and spriteStatus is None and no char_id`)
  stays.
- `activity_service.py` lazy-gen: `_generate_activity_animation`'s body becomes
  `enqueue_animation(agent_id, anim_name)`; the in-process `_custom_anim_in_progress`
  set remains as a cheap local dedup. Gated by `settings.ACTIVITY_ANIMATION_GENERATION`.
- **New**: conversation start (`conversation_service.py`) — if `"talking"` not in the
  agent's `spriteAnimations`, `enqueue_animation(agent_id, "talking")`. Same flag gate.
- Admin regenerate endpoint: enqueues `kind=signup` instead of spawning a local task.
- `core/services/sprite_queue.py` is **deleted**; `main.py` drops the queue start/stop
  and the startup `resume_sprite_generation` block (recovery moves to the worker, §4).
  `_reconnect_active_agents`'s game-server reconnect part is unchanged.

### 3. The sprite worker (`apps/backend/sprite_worker/`, new module)

Mirrors `worldgm/` packaging: lives in the backend directory (same Docker image), runs
via command override `["uv", "run", "python", "-m", "sprite_worker"]`.

- **Main loop:** long-poll SQS; process up to `SPRITE_WORKER_MAX_CONCURRENT_PIPELINES`
  (default 2) messages concurrently as asyncio tasks. Animation-level concurrency inside
  is PR #377's existing semaphore (`PIXELLAB_MAX_CONCURRENT_ANIMATIONS`, default 2) —
  now **globally** correct because this is the only process that generates.
- **Resume-aware processing** (replaces both `_sprite_pipeline_task`'s linear flow and
  `resume_sprite_generation`): processing always starts by reading the agent row and
  branching on (`spriteStatus`, `pixellabCharacterId`, `spriteAnimations`):
  - no char_id → full run: create → poll → Phase B static → walk.
  - char_id but no static/walk → skip creation, resume from the missing phase.
  - everything present (`complete`) → delete message, done (duplicate delivery).
  This makes SQS redelivery, standard-queue duplicates, and the boot sweep all converge
  on the same idempotent code path. The existing per-agent dedup from PR #380 carries
  over as an in-process active-set check.
- **Fast/slow split:** the signup pipeline generates **walk only**
  (`ANIMATION_REGISTRY`: flip `idle` and `talking` to `auto_generate=False`, so
  `SIGNUP_ANIMATIONS == {walk}`). `advance_phase(agent_id, "alive")` after walk lands.
  Idle/talking generate lazily through `kind=animation` messages (idle already rides the
  activity layer — `activity_registry` maps idle-ish activities to `animation="idle"`;
  talking comes from the new conversation-start hook). Missing animations are already a
  supported state — agents operate on the static sheet through all of Phase B today, and
  the Godot client falls back to the static/walk sheet when an animation key is absent.
- **`kind=animation` processing:** `generate_animation(char_id, anim_name)` → composite →
  S3 upload → merge into the agent row's `spriteAnimations` → notify backend (§4).
- **Failure handling:** transient failure → release the message (visibility lapse) for
  redelivery; 3 strikes → DLQ. `PixelLabCreditError` (402) → emit CloudWatch metric
  `GooseTown/SpriteWorker:CreditExhausted`, release the message with a 15-min visibility
  so the queue pauses instead of churning to DLQ — this plus the DLQ alarm closes the
  "PixelLab credit-exhaustion alarm missing" launch blocker.
- **Boot sweep (backstop, not primary recovery):** on startup, scan active agents for
  `spriteStatus` in (`queued`, `creating`, `building_static`, `animating`,
  `downloading`) and re-enqueue `kind=signup` for each. Primary recovery is SQS
  redelivery; the sweep covers rows whose SQS send failed (§2) and pre-migration stuck
  rows. Idempotent by §3 resume-awareness.

### 4. Worker → backend seam: one internal endpoint

The pipeline's non-PixelLab side effects need backend-process state the worker doesn't
have: per-agent game-server WebSocket connections (`game_server_client`), the in-memory
`ws_manager` registry, and the phase-lattice cache mirror. One seam handles all of it:

- **`POST /town/internal/sprite-event`** on the backend, authed with the existing
  `x-ws-lambda-secret` header (same secret + check as `/town/ws-message`; in-VPC call to
  the `api-{env}` ALB URL). Body:
  `{agent_id, type: "progress" | "static_ready" | "animations_updated", status?, …}`.
  - `progress` → `_send_to_daemon(agent_id, {event: "sprite_progress", status})` —
    keepalive messages the daemon writes into TOWN_STATUS.md.
  - `static_ready` / `animations_updated` → re-read the agent row,
    `restore_phase_from_db` (cache mirror), then `gs_client.connect_agent` (if the
    daemon is connected and the agent isn't in the game server yet) or
    `update_agent_sprites`, with the same versioned-URL treatment the connect path uses;
    `sprite_ready` push to the daemon.
- Worker calls are best-effort with small retry; a missed `progress` only stales a
  status line, and a missed `static_ready` self-heals on the daemon's next
  reconnect/poll (the `/apartment` poll and versioned sprite URLs already tolerate this).
- The worker does **all** DDB writes itself (spriteStatus, spriteUrl, spriteAnimations,
  `advance_phase` DDB half); the endpoint only refreshes backend-local state and pushes
  to game server/daemon. Phase truth stays in DynamoDB (per the phase-lattice design).

### 5. Infra: `SpriteWorkerStack` (new), `ComputeStack` (edits)

`SpriteWorkerStack` (clones the WorldgmStack shape; wired in `goosetown-stage.ts` with
`cluster` from `ClusterStack`, so no dependency cycles):
- SQS queue + DLQ (§1) + alarms: DLQ depth ≥ 1; oldest-message-age > 30 min;
  task-missing (CPUUtilization SampleCount pattern); CreditExhausted metric alarm.
- Fargate service: backend `DockerImageAsset` (CDK dedupes the build), command override,
  `desiredCount: 1`, 0.5 vCPU / 1024 MiB, circuit breaker rollback, private subnets.
- Env: `ENVIRONMENT`, `DYNAMODB_TABLE_PREFIX`, `SPRITE_S3_BUCKET`, `SPRITE_CDN_URL`,
  `BACKEND_INTERNAL_URL` (`https://api-{env}.goosetown.isol8.co` — the inline-string
  pattern from the CDK deploy-ordering fix), `SPRITE_JOBS_QUEUE_NAME`,
  `PIXELLAB_MAX_CONCURRENT_ANIMATIONS` (prod: 2 until PixelLab confirms the cap; §8),
  `SPRITE_WORKER_MAX_CONCURRENT_PIPELINES` (2).
- Secrets: `PIXELLAB_API_KEY` (the per-env secret that already exists:
  `goosetown/{env}/pixellab_api_key`), `WS_LAMBDA_SECRET` (for the internal endpoint).
- IAM: consume + send on the sprite-jobs queue; agents-table RW; sprite-bucket write;
  `cloudwatch:PutMetricData`.

`ComputeStack`:
- Backend env adds `ACTIVITY_ANIMATION_GENERATION` (`"false"` on dev, `"true"` on prod)
  and `SPRITE_JOBS_QUEUE_NAME`.
- Task-role grant: `sqs:SendMessage` + `sqs:GetQueueUrl` on the sprite-jobs queue ARN
  string.
- The backend keeps `PIXELLAB_API_KEY` (sprite-quota check and `/town/agent/status`
  PixelLab polling still live there) but no longer *generates*.

Per the critical rules: `cdk synth` must pass before push (delete `apps/backend/.venv`
first); all infra lands via the pipeline, nothing touched directly.

### 6. Config flags (prevention)

| Flag | Where | Value | Purpose |
|---|---|---|---|
| `ACTIVITY_ANIMATION_GENERATION` | backend (both envs) | dev `false`, prod `true` | Gates *all* lazy-animation enqueues (activity + talking). Dev must never burn the shared PixelLab account on activity anims. Signup walk generation still works in dev (dev needs sprites). |
| `PIXELLAB_MAX_CONCURRENT_ANIMATIONS` | sprite worker only | 2 (tune after PixelLab reply) | The now-truly-global animation gate. |
| `SPRITE_WORKER_MAX_CONCURRENT_PIPELINES` | sprite worker | 2 | Bounds concurrent signup pipelines (each ≤1 animation in flight at a time anyway). |

### 7. Per-env PixelLab API keys (human action required — flagged)

Secrets Manager already has per-env secrets (`goosetown/dev/pixellab_api_key`,
`goosetown/prod/pixellab_api_key`) — both currently hold the **same** account's key, so
dev and prod contend for one account cap. No infra code change needed. Human steps:

1. Create a second PixelLab account (or workspace key, if PixelLab supports multiple
   keys per account — ask in the email, §8) for **dev**.
2. `aws secretsmanager put-secret-value --secret-id goosetown/dev/pixellab_api_key \
   --secret-string '<new dev key>' --profile isol8-admin --region us-east-1`
3. Force a new deployment of the dev backend + dev sprite worker so containers pick up
   the new secret value.
4. Verify: register a throwaway dev agent; confirm generation succeeds and the prod
   account's generation count does not move.

This spec does **not** fabricate a key; until the human step happens, dev continues on
the shared key but with `ACTIVITY_ANIMATION_GENERATION=false` already cutting its load.

### 8. Email to PixelLab (draft — to send manually)

> **Subject:** Concurrent-job limit + higher-concurrency options for our account
>
> Hi PixelLab team,
>
> We run GooseTown (goosetown.isol8.co), a multi-agent AI town whose character sprites
> and animations are generated entirely through your v2 API
> (create-character-with-4-directions + characters/animations). We're on **Tier 2:
> Pixel Artisan** (currently ~3,200 of 5,000 monthly generations).
>
> Two questions:
>
> 1. What is our account's **concurrent background-job limit**? We see HTTP 429
>    "too many concurrent jobs" under burst load and currently self-throttle to ~8
>    concurrent jobs by guesswork; a concrete number would let us tune precisely.
>    (A `Retry-After` header on those 429s would also help, if that's on your roadmap.)
> 2. Is there a **higher-concurrency or enterprise tier**? Our signups arrive in
>    bursts (e.g. 19 characters at once) and we'd happily pay for more parallel
>    capacity. If multiple API keys per account (e.g. separate dev/prod) are
>    supported, that would be useful too.
>
> Thanks — happy to share more about our usage pattern if helpful.

### 9. Testing

- **Unit (backend):** producer conditional-gate semantics (enqueue once, skip when
  in-progress, DDB-write-then-send ordering); flag gating of lazy enqueues; talking
  hook fires once; register/connect call-site swaps; internal endpoint auth + the three
  event types' effects (mocked gs client / ws manager).
- **Unit (worker):** resume-aware branch selection from row state (no char_id / char_id
  no walk / complete); duplicate-delivery idempotency; heartbeat extender extends and
  stops; credit-error pause path emits the metric and does not DLQ; boot sweep enqueues
  exactly the stuck statuses.
- **Infra:** `SpriteWorkerStack` asserts (queue, DLQ wiring + maxReceiveCount, command
  override, desiredCount=1, env/IAM); `cdk synth` clean.
- **Dev validation after deploy:** seed N≥5 agents in one burst, then `aws ecs
  update-service --force-new-deployment` the worker mid-run; assert all N reach
  `spriteStatus=complete` with no manual action (the incident scenario, reproduced).

### 10. Rollout & rollback

- Single PR: backend + worker + infra ship together (same image). During the deploy
  window old backend tasks still run in-memory queues — at most one overlapping
  generation, acceptable.
- Auto-deploys to dev first (pipeline), prod after. Run the §9 dev validation before
  letting prod take a seed batch.
- Rollback: previous task definition via the per-stack runbook. Queued SQS messages are
  inert to an old backend (nothing consumes them) — re-rolling forward drains them; the
  worker boot sweep covers anything that expired.

## Non-goals (YAGNI)

- No DynamoDB job-state table — the agent row (`spriteStatus`, `pixellabCharacterId`,
  `spriteAnimations`) already is the durable job state; SQS is the work signal.
- No FIFO queue / priority lanes — resume-aware idempotency makes standard-queue
  duplicates safe; PixelLab's cap, not ordering, is the bottleneck.
- No webhook receiver, no multi-worker sharding (single worker is the point), no
  speculative pre-generation of activity animations.
