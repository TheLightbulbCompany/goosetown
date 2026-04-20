# CDK Deploy-Order and Resilience — Design

**Date:** 2026-04-20
**Sub-project:** #2 of the 2026-04-19 GooseTown code-review remediation
**Status:** Approved, ready for implementation plan

## Context

Five infra-side findings from the 2026-04-19 code review, bundled into one sub-project. One is CRITICAL (fresh-env deploys fail); the rest are incremental reliability/observability improvements that don't require design decisions beyond scope:

| # | Finding | Fix in this spec |
|---|---|---|
| 1 | **CRITICAL** — `compute-stack.ts:138` uses `Fn.importValue('goosetown-${env}-management-api-url')` but Compute deploys before Api (Api depends on Compute's Fargate task role) → fresh-env CloudFormation fails. | Replace the import with a custom-domain string, same pattern used for `backendUrl`. |
| 2 | `compute-stack.ts:222/226` — `desiredCount: 1` + `minHealthyPercent: 100` blocks any rolling-update window; any failed health check crash-loops. | Per-env split: dev stays `1/0`, prod goes to `2/100`. |
| 3 | `gameserver-stack.ts:80` — single game-server replica, no alarm on restart; agents drop permanently if task restarts > ~1 min (daemon reconnect caps at 5). | Alarm on `RunningTaskCount < DesiredCount` for 5 min. Keep `desiredCount: 1`. Sharding/multi-replica is deferred to future sub-project. |
| 4 | `network-stack.ts:18` — single NAT across 2 AZs; NAT AZ failure = private-subnet egress dead. | Alarm on NAT port-allocation errors. Multi-NAT deferred (issue #165). |
| 5 | `api-stack.ts:47/287/341` — 3 Lambda functions at 10s timeout, not comfortable under cold-start + JWKS fetch. | Raise all three to 15s. No provisioned/reserved concurrency. |

Out of scope: sharding (sub-project #7), multi-NAT (issue #165), daemon reconnect > 5 (separate PR), provisioned concurrency (revisit if prod cold-start metrics get bad).

## Design

### 1. Fix the CRITICAL — `compute-stack.ts:138`

Replace:

```typescript
WS_MANAGEMENT_API_URL: cdk.Fn.importValue(`goosetown-${env}-management-api-url`),
```

with the same env-ternary pattern already used two lines above for `TOWN_WS_URL` / `TOWN_API_URL`:

```typescript
// Management API for sending messages back to WS clients. Using the
// custom domain avoids an Fn.importValue cross-stack reference from
// ComputeStack to ApiStack — which would be circular because ApiStack
// already depends on ComputeStack via fargateTaskRole.
WS_MANAGEMENT_API_URL: env === "prod"
  ? "https://ws.goosetown.isol8.co"
  : "https://ws-dev.goosetown.isol8.co",
```

The API Gateway V2 management API accepts calls through a WebSocket custom domain — the `@connections/{connectionId}` path is routed through the custom domain mapping like any other path. `boto3.client("apigatewaymanagementapi", endpoint_url=...)` accepts either the raw `execute-api` URL or the custom domain URL.

The export in `api-stack.ts:590-593` (`ManagementApiUrl` / `exportName: goosetown-${env}-management-api-url`) is now unused. Leave it in place in this PR so an emergency rollback can point back at an older ComputeStack revision that still imports it. Remove in a follow-up PR after at least one week of clean prod runtime on the new code path.

### 2. Per-env ALB rolling update — `compute-stack.ts:218-227`

Replace the single `FargateService` config with a per-env block:

```typescript
const isProd = env === "prod";
this.service = new ecs.FargateService(this, "Service", {
  serviceName: `goosetown-${env}-backend`,
  cluster,
  taskDefinition,
  desiredCount: isProd ? 2 : 1,
  assignPublicIp: false,
  vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
  circuitBreaker: { rollback: true },
  minHealthyPercent: isProd ? 100 : 0,
});
```

- **Dev** (`1/0`): single task, accepts ~30s of downtime per deploy, fixes the crash-loop. $0 extra.
- **Prod** (`2/100`): true HA, zero-downtime deploys. +1 Fargate task ≈ ~$10-15/mo.

Combined cost delta for this item: ~$10-15/mo on prod.

### 3. Game-server restart alarm — `gameserver-stack.ts`

No `desiredCount` change. Add a CloudWatch alarm that fires when the `RunningTaskCount` metric is less than `DesiredTaskCount` for 5 consecutive minutes (i.e., task failed to start or stay up):

```typescript
const restartAlarm = new cloudwatch.Alarm(this, "GameServerTaskMissing", {
  alarmName: `goosetown-${env}-gameserver-task-missing`,
  metric: service.metric("RunningTaskCount", {
    statistic: "Minimum",
    period: cdk.Duration.minutes(1),
  }),
  threshold: 1,
  comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
  evaluationPeriods: 5,
  treatMissingData: cloudwatch.TreatMissingData.BREACHING,
  alarmDescription: "Game server has < 1 running task — agents are disconnected. See RUNBOOK.md.",
});
restartAlarm.addAlarmAction(new cw_actions.SnsAction(props.alarmTopic));
```

Requires an SNS topic passed in via props.

### 4. Backend-service restart alarm — `compute-stack.ts`

Same pattern as #3 but for the backend service. In prod (`desiredCount: 2`), threshold is `< 2`; in dev (`desiredCount: 1`), threshold is `< 1`.

```typescript
const restartAlarm = new cloudwatch.Alarm(this, "BackendTaskMissing", {
  alarmName: `goosetown-${env}-backend-task-missing`,
  metric: this.service.metric("RunningTaskCount", {
    statistic: "Minimum",
    period: cdk.Duration.minutes(1),
  }),
  threshold: isProd ? 2 : 1,
  comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
  evaluationPeriods: 5,
  treatMissingData: cloudwatch.TreatMissingData.BREACHING,
  alarmDescription: "Backend has fewer running tasks than desired. See RUNBOOK.md.",
});
restartAlarm.addAlarmAction(new cw_actions.SnsAction(props.alarmTopic));
```

### 5. NAT gateway alarm — `network-stack.ts`

Add a CloudWatch alarm on NAT port-allocation errors (the metric that flags NAT exhaustion or AZ trouble):

```typescript
const natAlarm = new cloudwatch.Alarm(this, "NatPortAllocationErrors", {
  alarmName: `goosetown-${props.environment}-nat-port-errors`,
  metric: new cloudwatch.Metric({
    namespace: "AWS/NATGateway",
    metricName: "ErrorPortAllocation",
    statistic: "Sum",
    period: cdk.Duration.minutes(5),
  }),
  threshold: 1,
  comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
  evaluationPeriods: 1,
  treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
  alarmDescription: "NAT gateway port allocation failures — private-subnet egress may be degraded. See RUNBOOK.md.",
});
natAlarm.addAlarmAction(new cw_actions.SnsAction(props.alarmTopic));
```

### 6. Authorizer + connect + disconnect Lambda timeouts — `api-stack.ts`

Change all three from 10s to 15s:

- `api-stack.ts:47` — `authorizerFn` — 10 → 15
- `api-stack.ts:287` — `connectFn` — 10 → 15
- `api-stack.ts:341` — `disconnectFn` — 10 → 15

`defaultFn` at line 408 is already 15s — leave.

No reserved or provisioned concurrency.

### 7. SNS topic + observability plumbing

New small stack `ObservabilityStack` deploys right after `NetworkStack` (no Fargate/Lambda dependencies → deploys fast, creates only the SNS topic). All other stacks receive the topic as a prop from `goosetown-stage.ts`:

```typescript
// goosetown-stage.ts
const network = new NetworkStack(...);
const observability = new ObservabilityStack(this, `goosetown-${env}-observability`, {
  environment: env,
});
// Pass observability.alarmTopic to compute, gameserver, api, network (network adds its own alarm).
```

The topic starts with no subscriptions — `alarmTopic.addSubscription(...)` is a manual step per env (`aws sns subscribe`). Documented in the runbook. Keeps the CDK out of email/secrets.

### 8. RUNBOOK.md

New file at `apps/infra/RUNBOOK.md`. Sections:

- **Alarm index** — alarm name → what it means → first action
- **Game server task missing** — check ECS events, bump desired count if task is frozen, force redeploy via `aws ecs update-service --force-new-deployment`
- **Backend task missing** — same playbook, plus ALB target group health check
- **NAT port-allocation errors** — check CloudWatch for egress patterns, consider emergency multi-NAT flip (steps in issue #165)
- **Authorizer timeout spike** — check Lambda cold-start metrics, Clerk JWKS endpoint latency, consider provisioned concurrency
- **Emergency rollback** — how to revert a deploy fast

### 9. CDK snapshot tests

New `apps/infra/test/` directory — currently zero tests exist (per the 2026-04-19 audit, graded **F**). This sub-project establishes the floor with stack-level assertions:

- `test/compute-stack.test.ts` — asserts desiredCount (1 for dev, 2 for prod), minHealthyPercent (0 for dev, 100 for prod), WS_MANAGEMENT_API_URL does NOT contain `Fn::ImportValue` (regression guard for the CRITICAL), alarm count.
- `test/gameserver-stack.test.ts` — asserts desiredCount 1, one restart alarm, alarm action count.
- `test/network-stack.test.ts` — asserts natGateways 1, one NAT alarm.
- `test/api-stack.test.ts` — asserts 3 Lambda timeouts are 15s, 1 is 15s (the default-route one).
- `test/observability-stack.test.ts` — asserts SNS topic exists, no subscriptions (manual).

Tests use `@aws-cdk/assertions` `Template.fromStack()` + resource-property matchers. Run via `pnpm -C apps/infra test`.

## Order of operations

Each item is its own commit on the feature branch; merge is one PR.

1. ObservabilityStack + SNS topic plumbing (no alarms attached yet).
2. CRITICAL fix: `compute-stack.ts` `Fn.importValue` → env-ternary string.
3. ALB rolling-update per-env split.
4. Lambda timeout raises (api-stack.ts lines 47 / 287 / 341).
5. Alarms across compute / gameserver / network stacks.
6. RUNBOOK.md.
7. CDK snapshot tests (one test file per stack modified).

## Deploy safety

- Each commit synthesizes independently (no half-broken template states).
- The CRITICAL fix can't be proven by a normal dev deploy (existing env already has the stale export; it works either way). Proof paths, in order of strength:
  1. Snapshot test: `compute-stack.test.ts` asserts the synthesized template contains no `Fn::ImportValue` reference to `goosetown-${env}-management-api-url`.
  2. Optional: spin up a scratch env (`goosetown-scratch-*`) in CI or manually — fresh deploy will succeed only if the import is gone.
- Prod deploy validates the ALB per-env split. If `desiredCount: 2` lands correctly and both tasks become healthy, rolling deploys work.
- Snapshot tests catch future regressions at PR-time.

## Out of scope / tracked separately

- **Multi-NAT** — issue #165. +$32-64/mo. Deferred for budget.
- **Game-server sharding + multi-replica** — future sub-project #7. Requires sticky routing + consistent-hashing design.
- **Daemon reconnect cap raise** — small standalone PR.
- **Provisioned concurrency on authorizer** — revisit if prod cold-start p99 metrics degrade.

## Acceptance criteria

1. Fresh dev deploy completes from-scratch without CloudFormation errors (previously would have failed on ComputeStack because `goosetown-dev-management-api-url` export didn't exist yet).
2. Dev `pnpm -C apps/infra test` passes with ≥1 test per stack modified.
3. Alarms exist in CloudWatch console (dev) for: backend-task-missing, gameserver-task-missing, nat-port-errors.
4. `apps/infra/RUNBOOK.md` exists with entries for each alarm + an emergency-rollback section.
5. Dev backend deploy does not need manual ECS intervention (minHealthyPercent: 0 means brief downtime is OK).
6. Prod backend runs with 2 tasks, both healthy per ALB target-group, and a rolling deploy completes without dropping below 2 healthy.

## Hand-off to implementation

Next skill: `superpowers:writing-plans`. Target: step-by-step implementation plan that a fresh subagent per task can execute.
