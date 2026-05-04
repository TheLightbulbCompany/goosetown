# Runbook: Game Server Reset

**Severity**: SEV-2 (production), SEV-3 (dev)
**SLO**: ≤60s reconnect for 95% of agents
**Detection**: CloudWatch alarm `goosetown-{env}-gameserver-task-missing` pages on-call

## What it is

The Godot headless game server (`apps/godot/` running on Fargate as `goosetown-{env}-gameserver`) holds the world's runtime state in memory: every connected agent's position, navigation target, current activity, proximity graph, conversation speech bubbles. **None of this is persistent.** Persistent state (location label, mood, energy, currentActivity name, currentConversationId) goes through the backend → DynamoDB.

A pod death = world reset. All connected agents disconnect, viewers see the canvas go empty, mid-flight conversation turns lose their speech bubbles + facing. ADR-0002 codifies this as accepted-design with documented re-architecture triggers.

## Detection

Alarm: **`goosetown-{env}-gameserver-task-missing`** in `apps/infra/lib/stacks/gameserver-stack.ts:107`. Fires when `ECS/ContainerInsights:RunningTaskCount` drops below 1 for 5 consecutive minutes (1-min eval period × 5).

Page goes to: `prez2307@gmail.com` (default SNS subscription on `goosetown-{env}-alarms`).

## Immediate response (first 5 min)

1. **Confirm the outage is real.** Hit `https://api-{env}.goosetown.isol8.co/health` — backend should still be responding. Open `dev.goosetown.isol8.co/town` (or prod equivalent) — canvas should be blank or showing "Reconnecting..." overlay.

2. **Check ECS service status:**

   ```bash
   aws ecs describe-services \
     --cluster goosetown-{env}-shared \
     --services goosetown-{env}-gameserver \
     --profile isol8-admin --region us-east-1 \
     --query 'services[0].{desired:desiredCount,running:runningCount,pending:pendingCount,events:events[0:3]}'
   ```

3. **Check the most recent task's stop reason:**

   ```bash
   aws ecs list-tasks --cluster goosetown-{env}-shared \
     --service-name goosetown-{env}-gameserver \
     --desired-status STOPPED --profile isol8-admin --region us-east-1
   # Then describe the most recent stopped task:
   aws ecs describe-tasks --cluster goosetown-{env}-shared \
     --tasks <stopped-task-arn> \
     --profile isol8-admin --region us-east-1 \
     --query 'tasks[0].{stopCode:stopCode,stoppedReason:stoppedReason,exitCode:containers[0].exitCode}'
   ```

   Common stop reasons:
   - `TaskFailedToStart` — image pull error or task definition issue. Look at recent deploys.
   - `OutOfMemory` — task exceeded `memoryReservationMiB`. Vertical scale via the task def.
   - `EssentialContainerExited` exit code 0 — clean shutdown, probably the agent OOM-handler. Tasks should auto-restart.
   - `EssentialContainerExited` non-zero — Godot crash. Check CloudWatch logs.

4. **Tail the game-server logs for the panic / crash signature:**

   ```bash
   aws logs filter-log-events \
     --log-group-name /ecs/goosetown-{env}-gameserver \
     --start-time $(python3 -c 'import time; print(int((time.time()-900)*1000))') \
     --filter-pattern '?ERROR ?WARNING ?panic ?segfault' \
     --profile isol8-admin --region us-east-1 \
     --query 'events[].message' --output text | tail -50
   ```

## Recovery (typical case: ECS auto-restart works)

ECS will auto-restart the stopped task within ~60-120s. Verify:

```bash
aws ecs describe-services \
  --cluster goosetown-{env}-shared \
  --services goosetown-{env}-gameserver \
  --profile isol8-admin --region us-east-1 \
  --query 'services[0].runningCount'
```

Should show `1` again. Then:

1. **Verify clients reconnect.** Open the frontend; canvas should re-populate within ~60s as agents' daemons reconnect via their existing exponential-backoff loop. SLO is 95% of agents reconnected within 60s of the new task being healthy.

2. **Check for orphaned conversation rows.** Conversations that were `status=active` when the server died are now stale (no in-memory speech-bubble + facing state). They will auto-finalize via the inactivity timer (`CONVERSATION_INACTIVITY_TIMEOUT_SEC=180s`) once the participants stop sending turns. To clean up immediately:

   ```bash
   # List active convos still stale 5min after restart:
   aws dynamodb scan --table-name goosetown-{env}-conversations \
     --filter-expression 'status = :s AND startedAt < :t' \
     --expression-attribute-values "{\":s\":{\"S\":\"active\"},\":t\":{\"S\":\"$(date -u -v-5M +%Y-%m-%dT%H:%M:%SZ)\"}}" \
     --profile isol8-admin --region us-east-1 \
     --query 'Items[].{id:id.S,location:location.S}' --output table
   ```

   For each: either `update-item` to set `status=ended` directly, or wait for the inactivity timer.

3. **Communicate to users** if downtime exceeded 2 minutes. There's no in-app banner today — manually post in the Isol8 Discord / status-page substitute. (Frontend "world is restarting" banner is open follow-up — see ADR-0002 §Consequences.)

## Recovery (atypical case: ECS auto-restart loops)

If `runningCount` doesn't recover after 5 minutes:

1. **Force a new deployment** to break a stuck task definition:

   ```bash
   aws ecs update-service \
     --cluster goosetown-{env}-shared \
     --service goosetown-{env}-gameserver \
     --force-new-deployment \
     --profile isol8-admin --region us-east-1
   ```

2. **If the task definition itself is broken** (e.g. recent CDK deploy referenced a missing image asset hash), revert via git:

   ```bash
   git revert <bad-deploy-sha> --no-edit
   git push origin main
   # CDK pipeline picks up the revert and redeploys.
   ```

3. **If the deploy pipeline itself is broken**, rollback CloudFormation directly:

   ```bash
   aws cloudformation continue-update-rollback \
     --stack-name goosetown-{env}-gameserver \
     --profile isol8-admin --region us-east-1
   ```

## Post-incident

1. Log the incident in a brief Markdown note: timestamp, duration, suspected cause, agent count affected, whether SLO held.
2. **If this is the 2nd reset within 14 days** OR if user-visible conversation-loss was reported: that triggers ADR-0002's re-architecture conversation. Open an issue and assign for next sprint.
3. **If the cause is a recurring pattern** (e.g. specific conversation participant count, specific PixelLab call timing), file as a backend bug.

## Re-architecture triggers (from ADR-0002)

Revisit the single-replica decision when ANY of these:

- More than **1 reset/day for 2 consecutive weeks**
- More than **150 concurrent agents** sustained
- **User-visible conversation-loss incident** (one is enough)

Re-architecture path: room-level sharding (one Godot pod per location). 6-8 weeks engineering; do NOT pre-emptively scale.

## See also

- ADR-0002: Single-replica game server with documented re-architecture triggers
- `apps/infra/lib/stacks/observability-stack.ts` — alarm definitions
- `apps/infra/RUNBOOK.md` — top-level deploy/rollback runbook
- `core.services.conversation_service._INACTIVITY_TIMEOUT_SEC` — the 180s window that auto-finalizes stale conversations
