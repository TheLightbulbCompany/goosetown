> **Migration warning:** This historical runbook contains retired `goosetown.isol8.co` routes and potentially destructive recovery procedures. Use the current domain-migration record and a reviewed restore plan; do not execute old domain or table-replacement commands.

# GooseTown Infrastructure Runbook

Target audience: on-call engineer (or a future Claude session). Assumes familiarity with AWS CLI but not this repo.

AWS account: `877352799272`, region: `us-east-1`, SSO profile: `isol8-admin`.

```bash
# Auth before any aws commands
aws sso login --profile isol8-admin
```

---

## 1. Alarm Index

| Alarm name | Threshold | SNS topic | What it means |
|---|---|---|---|
| `goosetown-{env}-gameserver-task-missing` | `ECS/ContainerInsights/RunningTaskCount` < 1 for 5 min (Container Insights enabled on gameserver cluster) | `goosetown-{env}-alarms` | Game server Fargate task has crashed or failed to start. All connected agents will drop after ~1 min (5-retry cap in daemon). |
| `goosetown-{env}-backend-task-missing` | `AWS/ApplicationELB/HealthyHostCount` < desired (1 dev / 2 prod) for 5 min | `goosetown-{env}-alarms` | Backend ALB has fewer healthy targets than desired. Either the Fargate task is down or it's failing `/health`. REST API and WebSocket forwarding are unavailable either way. |
| `goosetown-{env}-nat-port-errors` | ErrorPortAllocation >= 1 in 5 min | `goosetown-{env}-alarms` | NAT gateway port exhaustion or AZ-level fault. Private-subnet egress (backend → Clerk, PixelLab, DynamoDB endpoint) may be degraded. Awareness-only in dev — see issue #165. |
| `goosetown-{env}-sprite-dlq-depth` | DLQ `ApproximateNumberOfMessagesVisible` >= 1 | `goosetown-{env}-alarms` | Sprite job failed 3 times and landed in the DLQ. Inspect the message, fix the root cause, then redrive (section 19). |
| `goosetown-{env}-sprite-backlog-age` | `ApproximateAgeOfOldestMessage` > 1800s (30 min) | `goosetown-{env}-alarms` | Sprite jobs are not being processed fast enough — worker may be down, PixelLab may be rate-limiting, or a credit exhaustion is causing 15-min re-enqueue cycles. |
| `goosetown-{env}-sprite-worker-missing` | Sprite worker CPUUtilization SampleCount < 1 for 5 min | `goosetown-{env}-alarms` | Sprite worker Fargate task is missing. Restart via section 19. |
| `goosetown-{env}-sprite-credit-exhausted` | `GooseTown/SpriteWorker:CreditExhausted` sum >= 1 | `goosetown-{env}-alarms` | PixelLab returned 402. Top up credits; queued jobs are re-enqueued with a 15-minute delay and auto-resume. Receive count resets each cycle so credit outages never dead-letter jobs. See section 19 for recovery steps. |

SNS subscriptions are managed manually per environment:

```bash
aws sns subscribe \
  --topic-arn arn:aws:sns:us-east-1:877352799272:goosetown-dev-alarms \
  --protocol email \
  --notification-endpoint your@email.com \
  --profile isol8-admin
```

---

## 2. Game Server Restart Playbook

The game server is a single Godot headless Fargate task (`desiredCount: 1`). It runs in the private subnet of each env.

Relevant resources:
- Cluster: check CloudFormation output `goosetown-{env}-gameserver` stack, `ClusterName` output
- Service: `ServiceName` output from same stack
- Log group: `/ecs/goosetown-{env}-gameserver`
- Cloud Map: `gameserver.goosetown-{env}.local` (used by backend as `GAME_SERVER_URL`)

### Step 1 — Confirm the task is actually missing

```bash
aws ecs describe-services \
  --cluster $(aws cloudformation describe-stacks \
    --stack-name goosetown-dev-gameserver \
    --query "Stacks[0].Outputs[?OutputKey=='ClusterName'].OutputValue" \
    --output text --profile isol8-admin) \
  --services $(aws cloudformation describe-stacks \
    --stack-name goosetown-dev-gameserver \
    --query "Stacks[0].Outputs[?OutputKey=='ServiceName'].OutputValue" \
    --output text --profile isol8-admin) \
  --query "services[0].{running:runningCount,desired:desiredCount,pending:pendingCount}" \
  --profile isol8-admin
```

### Step 2 — Check the stopped task reason

```bash
# List the most-recently stopped task
aws ecs list-tasks \
  --cluster goosetown-dev \
  --desired-status STOPPED \
  --profile isol8-admin \
  --query "taskArns[0]" \
  --output text | xargs -I{} aws ecs describe-tasks \
    --cluster goosetown-dev \
    --tasks {} \
    --profile isol8-admin \
    --query "tasks[0].{stoppedReason:stoppedReason,exitCode:containers[0].exitCode,reason:containers[0].reason}"
```

### Step 3 — Force a new deployment

```bash
CLUSTER=$(aws cloudformation describe-stacks \
  --stack-name goosetown-dev-gameserver \
  --query "Stacks[0].Outputs[?OutputKey=='ClusterName'].OutputValue" \
  --output text --profile isol8-admin)

SERVICE=$(aws cloudformation describe-stacks \
  --stack-name goosetown-dev-gameserver \
  --query "Stacks[0].Outputs[?OutputKey=='ServiceName'].OutputValue" \
  --output text --profile isol8-admin)

aws ecs update-service \
  --cluster "$CLUSTER" \
  --service "$SERVICE" \
  --force-new-deployment \
  --profile isol8-admin
```

ECS circuit breaker is enabled with rollback. If the new task fails health checks 5 times, ECS rolls back to the previous task definition automatically.

### Step 4 — Tail logs while it starts

```bash
aws logs tail /ecs/goosetown-dev-gameserver \
  --follow \
  --profile isol8-admin
```

The Godot server prints `WebSocket server listening on port 8080` when ready. The health check (TCP connect on 8080) starts passing ~120s after container start (`startPeriod`).

Note: After a game server restart, all connected agents will reconnect automatically up to 5 times with exponential backoff (`game_server_client.py`). If an agent was mid-move it will re-spawn at plaza. Agents that exceeded the 5-retry cap during the outage will need to call `town_connect` again from their daemon.

---

## 3. Backend Task Missing (RunningTaskCount < 1)

The backend FastAPI service runs in cluster `goosetown-{env}` with service name `goosetown-{env}-backend`.

### Step 1 — Check service state

```bash
aws ecs describe-services \
  --cluster goosetown-dev \
  --services goosetown-dev-backend \
  --query "services[0].{running:runningCount,desired:desiredCount,deployments:deployments[*].{status:status,running:runningCount,failed:failedTasks}}" \
  --profile isol8-admin
```

### Step 2 — Check stopped task reason (same pattern as game server above)

```bash
aws ecs list-tasks \
  --cluster goosetown-dev \
  --service-name goosetown-dev-backend \
  --desired-status STOPPED \
  --profile isol8-admin \
  --query "taskArns[0]" \
  --output text | xargs -I{} aws ecs describe-tasks \
    --cluster goosetown-dev \
    --tasks {} \
    --profile isol8-admin \
    --query "tasks[0].{stoppedReason:stoppedReason,exitCode:containers[0].exitCode,reason:containers[0].reason}"
```

### Step 3 — Check ALB target group health

```bash
# Get target group ARN from CloudFormation output or the ALB console
# Then:
aws elbv2 describe-target-health \
  --target-group-arn <target-group-arn> \
  --profile isol8-admin \
  --query "TargetHealthDescriptions[*].{id:Target.Id,port:Target.Port,state:TargetHealth.State,reason:TargetHealth.Reason,description:TargetHealth.Description}"
```

Common causes:
- `/health` endpoint returns non-200 → check container logs for startup errors
- Secret not found in Secrets Manager → missing env var at container launch (check task definition)
- DynamoDB table missing → check `DYNAMODB_TABLE_PREFIX` env var and that tables exist

### Step 4 — Force new deployment

```bash
aws ecs update-service \
  --cluster goosetown-dev \
  --service goosetown-dev-backend \
  --force-new-deployment \
  --profile isol8-admin
```

### Step 5 — Tail backend logs

```bash
aws logs tail /ecs/goosetown-dev \
  --follow \
  --profile isol8-admin
```

---

## 4. NAT ErrorPortAllocation

This alarm fires when the NAT gateway reports port-allocation failures — a signal that either (a) private-subnet egress is saturated on the NAT's port table, or (b) the NAT gateway's AZ has a service event.

**Dev: this is awareness-only.** Dev runs a single NAT gateway across 2 AZs intentionally — $32/month per NAT is the tradeoff against the risk of a full AZ-level NAT failure. This is tracked in issue #165. The alarm tells us when that tradeoff bites us; it does not require immediate action beyond investigation.

### Diagnosis

```bash
# Check CloudWatch for ErrorPortAllocation metric in the last 30 minutes
aws cloudwatch get-metric-statistics \
  --namespace AWS/NATGateway \
  --metric-name ErrorPortAllocation \
  --start-time $(date -u -v-30M +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 60 \
  --statistics Sum \
  --profile isol8-admin

# Check for any concurrent unusual egress (e.g., runaway PixelLab polling)
aws logs filter-log-events \
  --log-group-name /ecs/goosetown-dev \
  --start-time $(python3 -c "import time; print(int((time.time()-1800)*1000))") \
  --filter-pattern "PixelLab" \
  --profile isol8-admin \
  --query "events[].message" \
  --output text
```

### If egress is genuinely blocked (AZ event)

Temporarily add a second NAT gateway per the VPC console (manual, not CDK — CDK change is tracked in issue #165). Remove after the incident. This takes ~3 minutes.

The private-subnet route tables in the VPC will need updating to point to the new NAT for the affected AZ. Do this via the VPC console Route Tables panel. Route table names follow the pattern the CDK creates: look for the private subnet route tables in `us-east-1`.

---

## 5. Authorizer Lambda Timeout

Alarm: the authorizer Lambda (`goosetown-{env}-ws-authorizer`) has a 15s timeout. A spike in duration means either cold-start overhead or Clerk JWKS endpoint latency.

### Step 1 — Check Lambda duration metrics

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/Lambda \
  --metric-name Duration \
  --dimensions Name=FunctionName,Value=goosetown-dev-ws-authorizer \
  --start-time $(date -u -v-1H +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 60 \
  --statistics Maximum,p99 \
  --profile isol8-admin
```

### Step 2 — Check Lambda logs for JWKS fetch failures

```bash
aws logs filter-log-events \
  --log-group-name /aws/lambda/goosetown-dev-ws-authorizer \
  --start-time $(python3 -c "import time; print(int((time.time()-3600)*1000))") \
  --filter-pattern "JWKS" \
  --profile isol8-admin \
  --query "events[].message" \
  --output text
```

### Step 3 — Check Clerk issuer reachability

The authorizer fetches `{CLERK_ISSUER}/.well-known/jwks.json` on cold start (before the in-memory cache warms). If Clerk has an incident, every cold start will hit the 5s `urlopen(timeout=5)` and add ~5s to Lambda duration.

Check https://status.clerk.com for active incidents.

### Step 4 — If duration spikes are sustained

The current mitigation path is provisioned concurrency on the authorizer function (keeps a warm execution context; eliminates cold starts entirely). This was explicitly deferred from sub-project #2 pending prod cold-start metrics. If p99 duration is regularly > 10s, raise with the technical director to approve provisioned concurrency on `goosetown-prod-ws-authorizer`.

---

## 6. Emergency Rollback

The CI/CD pipeline is `cdk-pipelines-github`. It deploys on every merge to `main`. Rollback = merge a revert commit.

### Step 1 — Identify the bad commit

```bash
git log --oneline -10
```

### Step 2 — Revert on the feature branch, then merge to main

```bash
git revert <bad-commit-sha>
git push origin main
```

The pipeline picks up the push automatically (GitHub Actions trigger on `main`).

### Step 3 — Monitor the pipeline

Pipeline stages: `Dev` (auto) → `Prod` (auto).

The manual approval gate was removed on 2026-04-19 (see the comment in
`apps/infra/lib/app.ts:107`). Prod auto-deploys as soon as every dev
stack is green. If you need to stop a bad commit from reaching prod,
the only tool is to revert on `main` before dev finishes — or cancel
the in-flight GitHub Actions run manually.

GitHub environment protection rules on the `Production` environment
are also empty (verified via `gh api /repos/.../environments`); no
reviewer gate lives there either.

### Step 4 — Emergency prod-only rollback (last resort)

If main is clean but a prod CloudFormation stack is stuck in `UPDATE_ROLLBACK_FAILED`:

```bash
aws cloudformation continue-update-rollback \
  --stack-name goosetown-prod-<stack-suffix> \
  --profile isol8-admin
```

If the stack is wedged on a specific resource (common with Lambda@Edge), you may need to skip resources:

```bash
aws cloudformation continue-update-rollback \
  --stack-name goosetown-prod-<stack-suffix> \
  --resources-to-skip <LogicalResourceId> \
  --profile isol8-admin
```

### ManagementApiUrl export rollback note

`api-stack.ts` still exports `goosetown-{env}-management-api-url` (marked TODO for removal in sub-project-3). If you need to roll back `compute-stack` to a revision that uses `Fn.importValue` for this key, the export is present and the rollback is safe. Remove the export only after at least one full deploy cycle where `compute-stack` no longer imports it.

---

## 7. Fast-Revert SLA (no approval gate)

The manual approval gate between dev and prod was removed on 2026-04-19 (`apps/infra/lib/app.ts:107`). The pipeline now reads:

```
Synth → DeployDev (8 stacks + Vercel dev) → DeployProd (8 stacks + Vercel prod)
```

There is no "wait for human" step between dev and prod. **A bad commit landing on `main` will hit prod ~15-25 minutes after dev finishes**, depending on stack drift size.

### SLA target

**Sev-1 (prod down or data risk): rollback initiated within 30 min of detection.**

The only ways to stop a bad commit from reaching prod once detected:

1. **Pre-flight kill** (cheapest): cancel the in-flight GitHub Actions run via `gh run cancel <run-id>` before `DeployProd` starts. Works only if you catch it during dev deploy.
2. **Revert-and-push** (default): `git revert <bad-sha> && git push origin main`. The revert flows through the same pipeline. Total time: ~25-40 min including dev → prod.
3. **Direct CFN rollback** (last resort): `aws cloudformation update-stack` with the previous template, OR `aws cloudformation cancel-update-stack` if mid-deploy. Use when the pipeline itself is broken (e.g., synth fails on the revert).

Per-stack rollback procedures in sections 9-17 below assume option 2 or 3.

### What "detection" means

- CloudWatch alarm fires (paged via SNS subscription) — section 1.
- User reports prod broken (Slack #incidents).
- Smoke test in pipeline catches it (rare — most issues are runtime).

If you don't have evidence the prod deploy is broken yet but suspect it will be, **err toward revert**. A revert commit is cheap; an outage that lands on prod is not.

---

## 8. Cross-Stack Dependency Map

Useful before rolling back: knowing which stacks read from which lets you predict blast radius. Source: `apps/infra/lib/goosetown-stage.ts`.

```
ObservabilityStack ────┐
                       ├──► (alarmTopic SNS) ──► NetworkStack
                       │                   ├──► GameServerStack
                       │                   └──► ComputeStack
                       │
NetworkStack ──────────┴──► (vpc) ──► GameServerStack
                                  ├──► ComputeStack
                                  └──► ApiStack (disconnect + default Lambdas)

DatabaseStack ──► (5 tables) ──► ComputeStack (read/write)
                              └──► ApiStack (connections table only)

AuthStack ────► (4 secrets) ──► ComputeStack (env injection)
                            └──► ApiStack (authorizer reads clerk_issuer + town_token_secret)

DnsStack ─────► (cert + hostedZone) ──► StorageStack
                                    ├──► ComputeStack (ALB HTTPS)
                                    └──► ApiStack (ws-{env} custom domain)

StorageStack ──► (spriteBucket) ──► ComputeStack (S3 read/write IAM grant)

GameServerStack ──► (serviceUrl + sg) ──► ComputeStack (env var + SG ingress)

ComputeStack ──► (fargateTaskRole) ──► ApiStack (grants ManageConnections IAM)

SpriteWorkerStack ──► (SQS queue ARN string) ──► ComputeStack (IAM SendMessage grant, inline ARN — no cross-stack object reference)
                  ──► (cluster from ClusterStack) ──► Fargate task (same backend image, command override)
                  ──► AuthStack (PIXELLAB_API_KEY + WS_LAMBDA_SECRET secrets)
                  ──► StorageStack (sprite bucket write)
```

### Inverted dependencies (rare but real)

- ApiStack depends on ComputeStack via `fargateTaskRole` — but the reverse used to exist via `Fn.importValue("management-api-url")`. That import was replaced with an inline custom-domain string on 2026-04-20 (sub-project #2). The export is still present in ApiStack for one rollback cycle (see section 6's "ManagementApiUrl export rollback note"). **There is currently no Compute → Api stack reference**.

### Blast radius rules of thumb

| If you roll back... | These break/restart |
|---|---|
| NetworkStack | Every stack with a Fargate task or VPC Lambda restarts (Compute, GameServer, Api disconnect/default). ~5 min downtime. |
| DatabaseStack | Compute backend will fail on the next request that hits the rolled-back GSI/table. Connections Lambda fails on every WS connect. |
| AuthStack | Compute backend's secret env vars stale until task restart. ApiStack authorizer caches secrets in-memory — also stale. |
| DnsStack | Cert ARN doesn't change unless re-issued. Pure record changes propagate per record TTL. |
| StorageStack | Sprite bucket stays. CloudFront cache change can take up to 24h to fully purge without explicit invalidation. |
| GameServerStack | All connected agents drop. Backend's `GAME_SERVER_URL` resolves via Cloud Map and re-resolves on next connect. |
| ComputeStack | Backend ALB targets churn. WebSocket connections (handled by ApiStack Lambdas, not backend) keep working briefly but `/ws-message` POSTs fail. |
| ApiStack | All WebSocket clients drop. New connects fail until authorizer redeploys. Backend itself unaffected — REST polling continues. |
| SpriteWorkerStack | Sprite generation pauses. Jobs are durable in SQS (4-day retention); no sprites are lost. New registrations succeed with shadow-placeholder sprite until worker restarts and drains the queue. Safe to restart at any time — in-flight job resumes via SQS redelivery. |

---

## 9. NetworkStack Rollback

**Contains:** VPC, 2 AZs (us-east-1a, us-east-1b), 1 NAT gateway, 4 subnets (2 public + 2 private-with-egress), a NAT port-exhaustion alarm. The VPC ID is exported as `goosetown-{env}-vpc-id`.

### Typical failure modes

NetworkStack rarely changes — VPC topology is stable. The dangerous failure modes are **CDK drift on subnet CIDRs** (causes new private subnets, redistributes Fargate tasks across AZs and triggers brief task replacement) and **NAT replacement** (causes route-table churn and ~30s of egress outage as routes update). The single-NAT-across-2-AZs design is intentional (`network-stack.ts:18`, issue #165): if NAT's AZ has a service event, all private-subnet egress dies until you manually add a second NAT.

The NAT port-exhaustion alarm (section 4) is the canonical warning that NetworkStack-related egress is degraded. NetworkStack itself doesn't typically need rollback — but if a CDK change accidentally widened/changed CIDRs, you must roll back fast or downstream stacks will get re-placed Fargate tasks.

### Rollback procedure

1. **Confirm the diff is actually in NetworkStack.** `aws cloudformation describe-stack-events --stack-name goosetown-prod-network --max-items 30 --profile isol8-admin` — look for `UPDATE_IN_PROGRESS` on `AWS::EC2::Subnet`, `AWS::EC2::NatGateway`, `AWS::EC2::RouteTable`. Subnet replacement is the high-risk signal.
2. **If still updating, cancel:** `aws cloudformation cancel-update-stack --stack-name goosetown-prod-network --profile isol8-admin`. This rolls back the in-flight change. AZ outage scenarios cannot be cancelled this way — only CDK-driven changes.
3. **If already updated, revert via pipeline:** `git revert <network-stack-commit> && git push origin main`. Wait for dev → prod.
4. **If a NAT was replaced and route tables are wrong:** check `aws ec2 describe-route-tables --filter "Name=vpc-id,Values=$(aws cloudformation describe-stacks --stack-name goosetown-prod-network --query "Stacks[0].Outputs[?OutputKey=='VpcId'].OutputValue" --output text --profile isol8-admin)" --profile isol8-admin`. Each private route table should have a `0.0.0.0/0` route pointing at a NAT gateway. Missing route = stack rollback hung; intervene per `continue-update-rollback` in section 6.

**What if this fails?** A wedged NetworkStack rollback usually means an ENI is stuck attached to a Fargate task. Stop the affected ECS service (`aws ecs update-service --desired-count 0`), wait for ENIs to detach, then `continue-update-rollback`. Bring the service back to `desiredCount: 1` (or 2 for prod backend) afterward.

---

## 10. DatabaseStack Rollback

**Contains:** 5 DynamoDB tables (`goosetown-{env}-agents`, `-instances`, `-relationships`, `-conversations`, `-connections`) with GSIs. PITR enabled in prod only. `removalPolicy: RETAIN` in prod, `DESTROY` in dev.

### Typical failure modes

- A schema migration drops a GSI that the backend still depends on → `ResourceNotFoundException` on the next query that hits it.
- A bad write loop corrupts data (e.g., the relationship-affinity RMW race in known issues) → incorrect agent state.
- Accidental `DELETE` on a CDK resource → in dev the table is gone; in prod `RETAIN` keeps the table but CDK loses track of it.

DynamoDB itself does not have versioning. The recovery primitive is **Point-In-Time Recovery (PITR)**, which is enabled on prod tables and disabled on dev. PITR can restore any table to any second within the last 35 days, **into a new table name** — you cannot restore in-place.

### Rollback procedure (PITR restore on prod)

Rough RTO: 10-30 min for a single table (DynamoDB restores asynchronously and table size matters). RPO: ≤ 1 second (PITR resolution).

1. **Identify the target restore time.** UTC seconds. If the bad write started at 14:23 UTC, restore to 14:22:00.
2. **Restore to a new table name:**
   ```bash
   aws dynamodb restore-table-to-point-in-time \
     --source-table-name goosetown-prod-agents \
     --target-table-name goosetown-prod-agents-restore-20260424 \
     --restore-date-time 2026-04-24T14:22:00 \
     --profile isol8-admin
   ```
3. **Wait for the new table to reach `ACTIVE`:**
   ```bash
   aws dynamodb describe-table \
     --table-name goosetown-prod-agents-restore-20260424 \
     --query "Table.TableStatus" --profile isol8-admin
   ```
4. **Recreate GSIs.** PITR restore does **not** automatically restore GSIs. For the agents table you must add `userId-agentName-index`, `instanceId-index`, and `isActive-joinedAt-index` after the table is `ACTIVE`. Easiest path: write the GSI specs from `database-stack.ts:38-56` into a `aws dynamodb update-table` call. Each GSI takes 5-15 min to backfill.
5. **Atomic table swap via app config.** The backend reads tables via `DYNAMODB_TABLE_PREFIX` env var (`goosetown-prod`). To swap, you must rename the restored table to the canonical name. DynamoDB has no rename — so the swap is:
   - Stop writes (`aws ecs update-service --service goosetown-prod-backend --desired-count 0`).
   - `aws dynamodb delete-table --table-name goosetown-prod-agents` (requires PITR to be off — turn it off first if needed).
   - **Wait** for delete to finish (~1-2 min).
   - Use `restore-table-from-backup` (or manual data copy via `dynamodb export-table-to-point-in-time` + `import-table`) to recreate `goosetown-prod-agents` with the restored data.
   - Restart backend (`aws ecs update-service --service goosetown-prod-backend --desired-count 2`).
6. **Verify:** spot-check a few items via `aws dynamodb get-item` and the running backend's `/apartment` endpoint.

Per-table notes: the `connections` table is throwaway state (rebuilt on every WebSocket connect). Don't bother restoring it — just recreate it empty. Same for `conversations` if you're willing to lose recent conversation logs.

**What if this fails?** Don't lose the restored table — keep `goosetown-prod-agents-restore-20260424` around with PITR off until the new canonical table is verified. If the swap dance breaks halfway, point the backend at the `-restore-` table by changing `DYNAMODB_TABLE_PREFIX` env var on the Fargate task definition (gives you the rest of the day to do a clean swap).

---

## 11. AuthStack Rollback

**Contains:** 4 Secrets Manager secrets — `goosetown/{env}/clerk_issuer`, `goosetown/{env}/clerk_secret_key`, `goosetown/{env}/town_token_secret`, `goosetown/{env}/pixellab_api_key`. The `town_token_secret` is auto-generated; the other 3 are populated manually post-deploy.

### Typical failure modes

- A rotation Lambda (if/when one is added) failed mid-flight and `AWSCURRENT` points to a half-written value.
- The `clerk_issuer` URL was changed (e.g., switched Clerk environment) and in-flight Clerk JWTs are now signed by an issuer the cache doesn't know.
- Someone updated the wrong secret (e.g., put the prod Clerk key in the dev secret) so the backend rejects valid cross-env tokens.
- `town_token_secret` was rotated — every existing town token (already issued to running agents) becomes invalid; all daemons fail auth on next reconnect.

When a Clerk JWT verification fails, the authorizer Lambda denies the connection (`api-stack.ts:262`) — users can't sign in, agents can't connect. If `town_token_secret` is wrong, agents fail but Clerk users are fine.

### Rollback procedure

Secrets Manager keeps every prior value. Each `PutSecretValue` creates a new version with `AWSCURRENT` staging label; the previous version gets `AWSPREVIOUS`. Rollback = swap labels.

1. **List versions:**
   ```bash
   aws secretsmanager list-secret-version-ids \
     --secret-id goosetown/prod/clerk_issuer \
     --include-deprecated \
     --profile isol8-admin
   ```
2. **Promote `AWSPREVIOUS` to `AWSCURRENT`:**
   ```bash
   aws secretsmanager update-secret-version-stage \
     --secret-id goosetown/prod/clerk_issuer \
     --version-stage AWSCURRENT \
     --move-to-version-id <previous-version-id> \
     --remove-from-version-id <bad-current-version-id> \
     --profile isol8-admin
   ```
3. **Force backend task restart so it re-reads the env-injected secret:**
   ```bash
   aws ecs update-service --cluster goosetown-prod \
     --service goosetown-prod-backend \
     --force-new-deployment --profile isol8-admin
   ```
   The container only re-reads secrets at task start (ECS injects secrets via env vars, not Secrets Manager SDK) — no restart, no rollback.
4. **For ApiStack authorizer Lambda:** the JWKS / secrets cache is in-process (`_cache` dict at `api-stack.ts:71`). Rolling back the secret won't flush warm Lambda execution contexts. To force flush, `aws lambda update-function-configuration --function-name goosetown-prod-ws-authorizer --environment Variables='{...same values...}'` triggers a config change and new cold starts.

### Special case: town_token_secret rotation

The `town_token_secret` HMACs every agent registration token. Rotating it invalidates **every running agent's token**. You cannot roll this back without losing agent connections — you can only re-issue new tokens via the `/town/instance` endpoint and have users repaste them into their daemons. Treat town_token_secret rotation as a planned-downtime event.

**What if this fails?** If the secret is corrupted (e.g., partial JSON, wrong format), Secrets Manager won't reject the put — but the backend will throw on container start. Roll forward: `aws secretsmanager put-secret-value` with the known-good value (you'll need it from a password manager since Secrets Manager is the source of truth). If you don't have the value, you must re-enter it from Clerk dashboard / PixelLab dashboard.

---

## 12. DnsStack Rollback

**Contains:** A `route53.HostedZone.fromLookup` reference to the existing `isol8.co` zone (the zone itself is not managed by this stack), plus an ACM cert for `*.goosetown.isol8.co` with `goosetown.isol8.co` as a SAN. The cert validates via DNS records that CDK writes into the zone. Cert ARN is exported as `goosetown-{env}-certificate-arn`.

### Typical failure modes

Route53 records have **no built-in versioning**. If a CDK change accidentally points `dev.goosetown.isol8.co` at a wrong CloudFront distribution, you must revert the CDK template — or hand-edit the record back via `change-resource-record-sets`. ACM cert renewals are handled automatically by AWS; rollback is generally not needed.

Note: most of the actual A-records that resolve `*.goosetown.isol8.co` are **created by other stacks**, not DnsStack:
- `assets-{env}.goosetown.isol8.co` — StorageStack (`storage-stack.ts:124`)
- `api-{env}.goosetown.isol8.co` — ComputeStack (`compute-stack.ts:329`)
- `ws-{env}.goosetown.isol8.co` — ApiStack (`api-stack.ts:588`)
- `goosetown.isol8.co` / `dev.goosetown.isol8.co` — Vercel (set via `vercel alias` in CI)

So a DNS issue on `api-dev.goosetown.isol8.co` is an **ALB DNS record** owned by ComputeStack, not DnsStack. Roll back the right stack.

### Rollback procedure (CDK-driven record change)

1. **Identify the record.** `aws route53 list-resource-record-sets --hosted-zone-id $(aws route53 list-hosted-zones-by-name --dns-name isol8.co --query "HostedZones[0].Id" --output text --profile isol8-admin) --query "ResourceRecordSets[?contains(Name, 'goosetown')]" --profile isol8-admin`.
2. **Revert via pipeline:** `git revert <bad-sha> && git push origin main`. Route53 propagation: 60s for new resolvers (TTL is typically 300s on alias records).
3. **Manual emergency edit** (only if the pipeline path is broken or you need <60s recovery):
   ```bash
   aws route53 change-resource-record-sets \
     --hosted-zone-id <zone-id> \
     --change-batch '{
       "Changes": [{
         "Action": "UPSERT",
         "ResourceRecordSet": {
           "Name": "api-prod.goosetown.isol8.co",
           "Type": "A",
           "AliasTarget": {
             "HostedZoneId": "<alb-zone-id>",
             "DNSName": "<previous-alb-dns>",
             "EvaluateTargetHealth": false
           }
         }
       }]
     }' --profile isol8-admin
   ```
   You need the previous record value from `aws cloudformation describe-stacks` history or your last-known-good captured in incident notes. **Keep a copy of all current alias targets in your incident channel before making changes** — there's no undo.

### Cert rollback

If a cert change broke ACM validation (rare — the cert is wildcard-fixed), the previous cert is still valid until its expiry; no immediate action needed. ACM never deletes cert versions automatically. To swap back to a previous cert ARN, `git revert` the CDK change — every consumer of `dns.certificate` (ALB, CloudFront, API Gateway custom domain) will pick up the change on its next deploy.

**What if this fails?** Route53 propagation is fast on Route53-internal aliases but **24h+ on records cached by external DNS resolvers**. If a customer reports stale resolution, instruct them to flush their resolver cache; you cannot force this from AWS.

---

## 13. StorageStack Rollback

**Contains:** S3 bucket `goosetown-{env}-sprites`, CloudFront distribution at `assets-{env}.goosetown.isol8.co` with OAC, ResponseHeadersPolicy injecting CORS + CORP at the edge, an A-record for the CloudFront distribution. Bucket versioning is ON (PR #219, `versioned: true` + 30-day noncurrent-version lifecycle rule).

### Typical failure modes

- A CloudFront cache key/policy change serves stale CORS-less responses (the exact failure mode from Pam's sprite cache, see `storage-stack.ts:69`).
- A sprite was accidentally deleted from the bucket. Without bucket versioning, you cannot recover the bytes — but PixelLab can regenerate.
- A CORS policy change locked out the frontend.
- CloudFront cache poisoning: a malformed sprite gets cached for `max-age=31536000` (1 year).

### Rollback procedure

#### CloudFront policy change rollback

1. **Roll back via pipeline** (`git revert`). The new ResponseHeadersPolicy gets attached to the distribution on deploy.
2. **Force cache invalidation** (won't change the policy, but flushes the stale-cached responses):
   ```bash
   DIST_ID=$(aws cloudformation describe-stacks --stack-name goosetown-prod-storage --query "Stacks[0].Outputs[?OutputKey=='DistributionId'].OutputValue" --output text --profile isol8-admin)
   aws cloudfront create-invalidation \
     --distribution-id "$DIST_ID" \
     --paths "/*" \
     --profile isol8-admin
   ```
   Invalidation completes in 5-15 min. Cost: $0.005/path after the first 1000/month — `/*` counts as one path.

#### Accidentally deleted sprite (versioning fallback available)

Bucket versioning is ON (PR #219). For each lost sprite you can restore the previous version. For each lost sprite:

1. **Find the agent ID** that owned the sprite path (`sprites/{agent_id}/walk.png`).
2. **Restore from S3 versioning** (preferred):
   ```bash
   # List versions for the sprite
   aws s3api list-object-versions \
     --bucket goosetown-prod-sprites \
     --prefix "sprites/<agent-id>/walk.png" \
     --profile isol8-admin
   # Copy a prior version back to the current key:
   aws s3api copy-object \
     --bucket goosetown-prod-sprites \
     --copy-source "goosetown-prod-sprites/sprites/<agent-id>/walk.png?versionId=<prev-version-id>" \
     --key "sprites/<agent-id>/walk.png" \
     --profile isol8-admin
   ```
3. **If no prior version exists, re-trigger sprite generation** by clearing the sprite-ready flag in DynamoDB then enqueuing via the admin regenerate endpoint (or clearing `spriteStatus` and `pixellabCharacterId` so the sprite worker boot sweep picks it up):
   ```bash
   aws dynamodb update-item \
     --table-name goosetown-prod-agents \
     --key '{"id":{"S":"<agent-id>"}}' \
     --update-expression "SET spriteReady = :false, spriteStatus = :queued REMOVE spriteUrl" \
     --expression-attribute-values '{":false":{"BOOL":false},":queued":{"S":"queued"}}' \
     --profile isol8-admin
   # Then force-restart the sprite worker so the boot sweep picks up the row:
   aws ecs update-service --cluster goosetown-prod --service goosetown-prod-sprite-worker --force-new-deployment --profile isol8-admin
   ```
4. **Verify** the sprite reappears in S3 (`aws s3 ls s3://goosetown-prod-sprites/sprites/<agent-id>/`) and the CDN URL on the agent record updates.

#### CloudFront cache poisoning

Same invalidation procedure as the first sub-section. If a specific path is poisoned: `--paths "/sprites/<agent-id>/*"` is cheaper and more targeted.

**What if this fails?** Bucket-level rollback (e.g., cross-region replication target or backup) doesn't exist for this stack. The mitigation is "PixelLab can regenerate everything" — at the cost of API quota and 60-90s per agent.

---

## 14. GameServerStack Rollback (image rollback)

**Contains:** ECS Fargate Godot headless task on a dedicated cluster (Container Insights enabled), Cloud Map service `gameserver.goosetown-{env}.local`, log group `/ecs/goosetown-{env}-gameserver`, the task-missing alarm (section 1). Single replica (`desiredCount: 1`).

Section 2 already covers restart. This section covers **rolling back a bad game server image** (e.g., a deploy that introduced a navigation-breaking change in `agent_controller.gd`).

### Typical failure modes

- A bad GDScript change passes synth + dev smoke but breaks prod (e.g., agents spawn outside navmesh, instant crashes on certain proximity events).
- The `.pck` was not committed alongside `.gd` changes (see "Critical Rules" — agents will run old logic with new state, undefined behavior).
- The Godot binary itself segfaults on a specific input (rare).

### Rollback procedure

The CDK pipeline pushes the Docker image to ECR via `DockerImageAsset`. The image tag is content-addressed (SHA hash). To roll back to a previous tag, you must override the task definition's image URI manually — the pipeline will not "pick up" an older image automatically.

1. **Find the previous good image tag.** ECR keeps a history:
   ```bash
   aws ecr list-images \
     --repository-name $(aws ecr describe-repositories --query "repositories[?contains(repositoryName, 'gameserver')].repositoryName" --output text --profile isol8-admin) \
     --filter tagStatus=TAGGED \
     --query "imageIds[*].imageTag" \
     --output table --profile isol8-admin
   ```
   The CDK asset hash is the tag; correlate with git SHA via the asset manifest in the pipeline artifact.
2. **Register a new task definition revision** pointing at the old image:
   ```bash
   # Get current task def
   aws ecs describe-task-definition \
     --task-definition $(aws ecs describe-services --cluster goosetown-prod --services <gs-service> --query "services[0].taskDefinition" --output text --profile isol8-admin) \
     --query "taskDefinition" --profile isol8-admin > /tmp/td.json
   # Edit /tmp/td.json: set containerDefinitions[0].image to the previous URI; remove fields like taskDefinitionArn, status, revision, requiresAttributes, registeredAt, registeredBy
   aws ecs register-task-definition --cli-input-json file:///tmp/td.json --profile isol8-admin
   ```
3. **Force-deploy:** the next `update-service --force-new-deployment` picks up the latest revision automatically. To explicitly point at the rolled-back revision:
   ```bash
   aws ecs update-service \
     --cluster <gs-cluster> \
     --service <gs-service> \
     --task-definition goosetown-prod-gameserver:<prev-revision> \
     --profile isol8-admin
   ```
4. **All agent connections drop.** The backend's `game_server_client.py` will reconnect within 5 attempts. Agents dropped from the game server respawn at plaza on next connect.

After verifying the rolled-back image is healthy in prod, **also revert the bad commit on `main`** so the next pipeline run doesn't re-deploy the broken image.

**What if this fails?** ECS circuit breaker (`circuitBreaker: { rollback: true }` on the service) auto-rolls back if 5 health checks fail. If circuit breaker engages, you'll see `ROLLBACK_COMPLETED` in service events — no manual action. If it doesn't engage (e.g., the health check passes but agents fail logically), you need the manual procedure above.

---

## 15. ComputeStack Rollback (notes)

ComputeStack rollback for backend ALB / Fargate tasks is covered by sections 3 (task missing) and 6 (emergency revert). One clarification:

**ComputeStack does not own CloudFront.** A common mistake during incident response is grepping for "cloudfront" in `compute-stack.ts` and finding nothing — that's correct. The CloudFront distribution that fronts sprites lives in **StorageStack** (`storage-stack.ts:104`). The ALB (which ComputeStack owns) is fronted directly via Route53; there is no CloudFront in front of the API.

If the failure shape is "frontend can't load `assets-{env}.goosetown.isol8.co/sprites/...`", it's StorageStack (section 13). If it's "frontend can't load `api-{env}.goosetown.isol8.co/...`", it's ComputeStack (section 3).

The Compute access logs bucket (`goosetown-{env}-alb-logs`) is `RemovalPolicy.RETAIN` and `enforceSSL: true`. Rollbacks of the access-logging config will not delete logs — they'll just stop writing new ones until the rollback completes.

---

## 16. ApiStack Rollback (authorizer + Lambdas)

**Contains:** WebSocket API Gateway V2 (`goosetown-{env}-ws`), Lambda authorizer + connect + disconnect + default-route Lambdas (all Python 3.12, inline code at `api-stack.ts:54-288`), custom domain `ws-{env}.goosetown.isol8.co`. The disconnect + default-route Lambdas run inside the VPC private subnets (so they can reach the backend ALB internally — though the ALB is internet-facing, this is for future-proofing); the authorizer + connect Lambdas are not VPC-attached.

Section 5 covers authorizer Lambda **timeout** issues. This section covers **rolling back a bad authorizer deploy** — e.g., a code change to the inline JWT verification logic that incorrectly denies valid tokens.

### Typical failure modes

- Inline Lambda code change breaks JWT verification → all WebSocket connects denied → frontend can't load town, agents can't connect.
- Authorizer's `urllib.request.urlopen(jwks_url, timeout=5)` change accidentally raised the timeout → cold starts go past 15s → hits Lambda timeout on cold start → see section 5.
- A new route was added with the wrong integration → `$default` route silently swallows messages.

### Rollback procedure

The pipeline rebuilds + redeploys all 4 Lambdas as part of every `goosetown-{env}-api` stack deploy. A revert through the pipeline is the canonical path.

1. **Pipeline revert:** `git revert <bad-sha> && git push origin main`. Total time: ~25-40 min (full pipeline).
2. **Emergency manual override (prod-down only).** If you need to restore in <5 min:
   ```bash
   # Pull the previous good Lambda code from a known-good git commit
   git checkout <last-known-good-sha> -- apps/infra/lib/stacks/api-stack.ts
   # Extract the inline code block (between the lambda_.Code.fromInline backticks)
   # and save to /tmp/authorizer.py + a zip:
   cd /tmp && zip authorizer.zip index.py  # rename your extract to index.py
   # Force-update the function code:
   aws lambda update-function-code \
     --function-name goosetown-prod-ws-authorizer \
     --zip-file fileb:///tmp/authorizer.zip \
     --profile isol8-admin
   # Restore the workspace:
   git checkout HEAD -- apps/infra/lib/stacks/api-stack.ts
   ```
   This bypasses CDK. **The next pipeline deploy will overwrite your manual change** — you must `git revert` the bad commit before the next pipeline run, or your manual fix will get clobbered.
3. **Verify:** `aws logs tail /aws/lambda/goosetown-prod-ws-authorizer --follow --profile isol8-admin`, then have someone connect to the WS endpoint and watch for `Allow` decisions.

### Connect / Disconnect / Default-route Lambdas

Same procedure — these Lambda function names are `goosetown-{env}-ws-connect`, `goosetown-{env}-ws-disconnect`, `goosetown-{env}-ws-default`. The default-route Lambda forwards to `http://api-{env}.goosetown.isol8.co/api/v1/town/ws-message` — if rolled back, also confirm the backend at that URL is healthy.

### WS API Gateway routes / authorizer wiring

The `$connect` route wires to the authorizer via L1 CFN constructs (`api-stack.ts:513-543`). A bad `routeKey` or `authorizerType` change here means **every connect attempt fails immediately** — there's no Lambda involved in the failure. Symptoms: API Gateway access logs show `403` on `$connect` with no authorizer-Lambda invocations. Rollback path is identical (pipeline revert).

**What if this fails?** Authorizer Lambdas can be re-pointed manually if the IAM trust is intact. If you see `AccessDeniedException` on the manual `update-function-code` call, the Fargate task role's IAM was rolled with the bad commit — revert via pipeline is the only path.

---

## 17. Vercel Rollback

The frontend is deployed to Vercel by the same GitHub deploy pipeline that deploys CDK stacks (`apps/infra/lib/app.ts:56,118`). Each deploy:
1. Runs `vercel build` → `vercel deploy --prebuilt` → produces a unique deployment URL.
2. Aliases the deployment URL to `dev.goosetown.isol8.co` (or `goosetown.isol8.co` for prod) via `vercel alias`.

Vercel keeps every prior deployment indefinitely (well — within the project's retention policy). Rollback is **alias-based**: re-point the canonical domain at an older deployment.

### Default rollback path: revert + push

The cheapest, most-traceable rollback. `git revert <bad-sha> && git push origin main` triggers the full pipeline including a fresh Vercel deploy. Total time: ~25-40 min.

### Faster: re-alias to a prior deployment

If you need <5 min recovery:

1. **List recent deployments:**
   ```bash
   vercel ls goosetown --token=$VERCEL_TOKEN
   ```
   (You'll need `VERCEL_TOKEN` from the GitHub secrets vault, or your own Vercel personal token with project access.)
2. **Find the previous good deployment URL** — typically the second entry in the list (the one before the bad current alias).
3. **Re-alias:**
   ```bash
   # Prod
   vercel alias <prev-deploy-url> goosetown.isol8.co --token=$VERCEL_TOKEN
   # Dev
   vercel alias <prev-deploy-url> dev.goosetown.isol8.co --token=$VERCEL_TOKEN
   ```
   Alias change is near-instant; users hit the new alias on their next page load.
4. **Verify:** `curl -I https://goosetown.isol8.co | grep -i 'x-vercel-id'` — the deployment ID in the header should now match the prior good one.

### Full rollback (via Vercel CLI)

`vercel rollback` exists but is less precise — it rolls back the **production deployment** to the immediately-prior deployment. Use it when you don't know the deployment URL but trust the previous one was good:

```bash
vercel rollback --token=$VERCEL_TOKEN
```

This sets the canonical domain alias to the immediately-prior production deployment. Equivalent to `vercel alias` with auto-discovery of the previous URL.

### Coordinating with backend rollback

Frontend and backend deploy in parallel from the same `main` push. If the bad commit broke both, **don't roll back only one** — you'll get version-skew between the frontend bundle expecting a v2 API and a backend serving v1 (or vice versa). Either:
1. Revert the commit (rolls back both).
2. Manually re-alias Vercel **and** force-deploy the previous backend image (section 14 procedure, but for `goosetown-{env}-backend` instead of game server).

**What if this fails?** Vercel's domain DNS itself is managed by Vercel — `goosetown.isol8.co` is a CNAME (or alias) into Vercel that's set in Route53. If you accidentally delete the Vercel project, the DNS record is intact but resolves to nothing. To recover: `vercel link` from a fresh checkout, redeploy, and the alias re-attaches. Don't change the Route53 record.

---

## 18. CDK Bootstrap Deploy Role — `cloudformation:DescribeEvents`

The `cdk-hnb659fds-deploy-role-877352799272-us-east-1` role assumed by `aws-actions/aws-cloudformation-github-deploy@v2` (used by `cdk-pipelines-github` 0.4.135+) requires `cloudformation:DescribeEvents` to satisfy the action's stack-event polling thread. The standard `cdk bootstrap` template does **not** include this action — it only grants `cloudformation:DescribeStackEvents` (a different, older API).

Without this permission, every `NO_CHANGES` stack deploy fails: the polling thread cannot read events, retries five times, escalates to fatal, and the action exits 1 even though the stack was already up-to-date. Symptom: `Deploy {stack} failed` with `User ... not authorized to perform: cloudformation:DescribeEvents` in the failed job's log.

Applied 2026-04-27 via direct AWS CLI patch to the inline `default` policy on the role (added `cloudformation:DescribeEvents` to the existing `CliPermissions` Sid). To re-apply (e.g. after a future `cdk bootstrap` rerun overwrites the policy):

```bash
aws iam get-role-policy \
  --role-name cdk-hnb659fds-deploy-role-877352799272-us-east-1 \
  --policy-name default \
  --profile isol8-admin --region us-east-1 \
  --query 'PolicyDocument' > /tmp/policy.json

jq '(.Statement[] | select(.Sid=="CliPermissions") | .Action) += ["cloudformation:DescribeEvents"]' \
  /tmp/policy.json > /tmp/policy-patched.json

aws iam put-role-policy \
  --role-name cdk-hnb659fds-deploy-role-877352799272-us-east-1 \
  --policy-name default \
  --policy-document file:///tmp/policy-patched.json \
  --profile isol8-admin --region us-east-1
```

`cdk bootstrap` is idempotent on the inline policy — running it later will reset the policy to the standard template, removing this addition. If you re-bootstrap, re-run the patch above. (Long-term, file an upstream issue with `aws-cdk` to add this permission to the standard bootstrap template.)

---

## 19. SpriteWorkerStack Rollback & Recovery

**Contains:** SQS queue `goosetown-{env}-sprite-jobs` + DLQ `goosetown-{env}-sprite-jobs-dlq`, four CloudWatch alarms (DLQ depth, backlog age, worker-missing, CreditExhausted), a single-replica Fargate service `goosetown-{env}-sprite-worker` running the backend image with a command override (`python -m sprite_worker`). `desiredCount=1` by design — global PixelLab concurrency is correct only because there is exactly one process generating.

Jobs are **durable in SQS** (4-day message retention). A worker restart, crash, or deploy never loses queued jobs — in-flight messages reappear within ≤5 min (visibility timeout expires) and are reprocessed via the resume-aware pipeline. Safe to restart the worker at any time.

### Worker restart

```bash
aws ecs update-service \
  --cluster goosetown-{env} \
  --service goosetown-{env}-sprite-worker \
  --force-new-deployment \
  --profile isol8-admin
```

### Image rollback (bad deploy)

Same procedure as section 14 (GameServerStack image rollback) — find the previous ECR image tag, register a new task definition revision pointing at it, and force-deploy. The worker boot sweep will re-enqueue any rows stuck in `spriteStatus` in (`queued`, `creating`, `building_static`, `animating`, `downloading`) so nothing is lost.

### DLQ replay

Inspect the DLQ first before redriving — a message may have failed 3× due to a transient bug that is now fixed, or there may be a permanent data-shape issue you need to fix first.

```bash
# Inspect the first DLQ message without consuming it:
aws sqs receive-message \
  --queue-url "https://sqs.us-east-1.amazonaws.com/877352799272/goosetown-{env}-sprite-jobs-dlq" \
  --attribute-names All \
  --message-attribute-names All \
  --profile isol8-admin

# Redrive all DLQ messages back to the source queue:
aws sqs start-message-move-task \
  --source-arn "arn:aws:sqs:us-east-1:877352799272:goosetown-{env}-sprite-jobs-dlq" \
  --profile isol8-admin
```

After redriving, monitor the `ApproximateNumberOfMessagesVisible` on the source queue to confirm messages are being consumed. If the same messages DLQ again, check the sprite worker logs:

```bash
aws logs tail /ecs/goosetown-{env}-sprite-worker \
  --follow \
  --profile isol8-admin
```

### Credit-exhaustion alarm response (`GooseTown/SpriteWorker:CreditExhausted`)

1. Top up PixelLab credits at https://pixellab.ai (account login required).
2. No manual action needed for the queue — on a 402 the worker re-enqueues the message as a fresh SQS message with a 15-minute delay, then deletes the original. A fresh message resets the receive count, so credit outages cycle every 15 minutes indefinitely without burning the 3-receive DLQ budget. Jobs auto-resume within 15 minutes of the credit top-up.
3. If the DLQ alarm also fired alongside this alarm, those messages represent genuine processing failures (not credit outages). Redrive them after fixing the underlying cause.

### Per-env PixelLab API key split (DEFERRED — single shared account by decision)

Both `dev` and `prod` share one PixelLab account key (`goosetown/dev/pixellab_api_key` and `goosetown/prod/pixellab_api_key` hold the same value). **Decision 2026-06-12: this stays — there is no second account and none is planned.** The shared cap is managed instead by per-env worker concurrency (`PIXELLAB_MAX_CONCURRENT_ANIMATIONS`: prod 2, dev 1 in `sprite-worker-stack.ts`) plus `ACTIVITY_ANIMATION_GENERATION=false` on the dev backend. An email to PixelLab asking about the concurrent-job cap and an enterprise/higher-concurrency tier is the pending path to more headroom; revisit a key split only if PixelLab offers multiple keys per account or an enterprise plan lands.

If that ever happens, the mechanical steps are:

1. Obtain the second key (separate account or per-account key, whichever PixelLab supports).
2. Store the new dev key in Secrets Manager:
   ```bash
   aws secretsmanager put-secret-value \
     --secret-id goosetown/dev/pixellab_api_key \
     --secret-string '<new dev key>' \
     --profile isol8-admin \
     --region us-east-1
   ```
3. Force new deployments of the dev backend and dev sprite worker so containers pick up the updated secret:
   ```bash
   aws ecs update-service --cluster goosetown-dev --service goosetown-dev-backend \
     --force-new-deployment --profile isol8-admin
   aws ecs update-service --cluster goosetown-dev --service goosetown-dev-sprite-worker \
     --force-new-deployment --profile isol8-admin
   ```
4. Verify: register a throwaway dev agent; confirm the sprite generates and the prod account's generation count does not move.

---

## 20. Per-user agent quota drift

If a user gets a 429 for agents with fewer than 5 active (or holds more than 5), the `activeAgents` counter on their `goosetown-{env}-users` row has drifted. Run `REMOVE activeAgents` on that row (`aws dynamodb update-item --table-name goosetown-{env}-users --key '{"userId":{"S":"<user_id>"}}' --update-expression "REMOVE activeAgents" --profile isol8-admin`). The next activation, deactivation or registration reseeds it from the real active-agent count.

### Avatar generation request ceiling

The sprite worker receives `PIXELLAB_REQUESTS_PER_DAY` from CDK context
`pixellabDailyRequests:dev` / `pixellabDailyRequests:prod` (nonnegative integer,
default **0**). Set a reviewed value explicitly when preparing the release,
for example `-c pixellabDailyRequests:dev=N` with the approved integer replacing
`N`. Zero pauses paid generation with a queued placeholder; it does not disable
town access or read-only retrieval of existing sprites. Do not select a budget
by enabling a workflow. Dev and prod have separate counters, so the account's
combined request allowance is their sum. This is a request ceiling, not measured
dollar spend. Each write retry consumes a slot, including ambiguous failures.
Per-user agent/sprite fairness is enforced separately at registration.
