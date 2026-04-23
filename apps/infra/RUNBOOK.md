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
