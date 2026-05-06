# Prod restoration & post-overnight test list — 2026-05-06

Tracker for what to verify after tonight's prod restoration and the
overnight architectural refactor PR queue (#300–#307).

---

## Tier 0 — prod-restoration smoke (do first, right after deploy run #4 lands)

These should take <2 min total. If any fail, prod is still broken.

- [ ] `curl https://api.goosetown.isol8.co/health` returns `200` with body `{"status":"healthy"}`
- [ ] `wscat -c wss://ws.goosetown.isol8.co?token=test` returns `401`/`403` (auth rejects, but the endpoint is reachable)
- [ ] `curl https://goosetown.isol8.co/` returns `200` (Vercel/landing page)
- [ ] `aws cloudformation describe-stacks --stack-name goosetown-prod-compute --query 'Stacks[0].StackStatus'` = `CREATE_COMPLETE`
- [ ] `aws cloudformation describe-stacks --stack-name goosetown-prod-api --query 'Stacks[0].StackStatus'` = `CREATE_COMPLETE`
- [ ] ECS service `goosetown-prod-backend` reports `runningCount == desiredCount` (2/2 expected with autoscale floor)
- [ ] No `ROLLBACK_COMPLETE` or `REVIEW_IN_PROGRESS` orphans on prod-* stacks

## Tier 1 — sanity for #308 (the WS_LAMBDA_SECRET startup fix)

The validate() drop is what unblocked the deploy. Confirm it's correct:

- [ ] Fresh ECS task starts cleanly (no exit-code-3 failure loop) — confirmed by Tier 0 ECS service stable
- [ ] `POST /api/v1/town/ws-message` from a Lambda-shaped fake request still works (vacuous-pass behaviour preserved)
- [ ] CloudWatch logs `/ecs/goosetown-prod` show backend Uvicorn startup line + `Configuration validated for environment: prod`

## Tier 2 — overnight refactor PRs (verify before/after merge)

Run these after each batch of related PRs merges to dev. Each item references the PR(s) that touch it.

### Onboarding phase lattice (#300)
- [ ] Fresh agent registers → goes through `pending_sprite → static → alive` without backwards flips (Vera-finding Q regression)
- [ ] If sprite generation fails mid-flight, phase stays at `pending_sprite` (not silently advanced to alive)

### Cross-location proximity (#301)
- [ ] Two agents at *different* locations: agent A in cafe, agent B in library. B tries `act join_conversation <conv-A>` → rejected with `not_in_proximity`
- [ ] Two agents at *same location, far apart*: both at plaza, but one at NW corner, other at SE. → still rejected with `not_in_proximity` (96px radius)
- [ ] Two agents within 96px: join succeeds, both get `participant_joined` events

### DaemonMailbox refactor (#302 + #305 + #306)
This touches every backend → daemon outbound message. Soak should exercise:
- [ ] `chat` initiator gets `conversation_started`; target gets `conversation_invite`
- [ ] `say` echoes back to sender + forwards to all participants
- [ ] `join_conversation` sends `participant_joined` to existing participants
- [ ] `end_conversation` leaver gets `conversation_ended` with `reason=left_by_self`; finalize loop sends to remaining
- [ ] Activity start/complete events deliver
- [ ] Context transition (town↔apartment) events deliver
- [ ] Reject paths: `chat_rejected`, `say_rejected`, `join_rejected`, `end_rejected`, `activity_rejected`, `move_rejected` all reach the daemon

### Render helpers (#303 + #307)
TOWN_STATUS.md content equivalence — Phase 1 and Phase 2 templates should be byte-identical to pre-refactor:
- [ ] Connect with sprite ready → static-status template rendered with location/activity/nearby/action menu
- [ ] Connect with sprite pending → "your sprite is being generated" blurb (sprite-pipeline Phase A)
- [ ] Sprite ready event → "your sprite is ready" template
- [ ] Context change to apartment → apartment action menu
- [ ] Context change back to town → town action menu
- [ ] Wake event → wake message + abbreviated action list
- [ ] Live world_update with nearby agents → mood/activity/nearby formatted correctly (already tested as pure functions, but verify under live agent)

### Documentation PR (#304)
- [ ] No code change; just verify the spec docs render correctly on github.com

## Tier 3 — multi-agent stress (the planned soak)

Schedule for after Tier 0–2 are clean. Costs PixelLab credits + LLM tokens — defer until the user is ready.

- [ ] 5-agent concurrent soak (TeamCreate + 5 agents register → walk → chat → idle)
  - Looking for: stuck conversations, sprite queue throughput, daemon reconnect behaviour, mood/energy convergence
  - Reference: `docs/superpowers/test-runs/2026-05-04-5-agent-soak.md` (if it exists)
- [ ] 15-agent soak (`scripts/launch-soak.sh`) — production-launch-readiness milestone
  - Looking for: PixelLab rate limit / credit drain alarm, conversation auto-end timer behaviour, ECS autoscale headroom

## Tier 4 — infrastructure follow-ups

Not user-facing, but landed during the prod-restoration sweep:

- [ ] Verify `goosetown-prod-alb-logs` S3 bucket is freshly created with `removalPolicy: RETAIN` intact (re-clean on next redeploy will recur until source policy is changed)
- [ ] Run [PR #256 cleanup task](https://github.com/Isol8AI/goosetown/pull/256): remove `LegacyWsApiAutoExportProd` from `apps/infra/lib/stacks/api-stack.ts:316-321`. Now safe because prod-compute was rebuilt without the legacy import.
- [ ] Branch protection on `main`: add `Infra (CDK) Test` to *required* checks. (PR #298 merged with this check failing → triggered the 8-deploy stall on 2026-05-05.)
- [ ] CloudFormation orphan-resource sweep: hunt for any remaining resources from the deleted prod-compute (target groups, security groups, IAM roles) — most should have been cleaned, but worth a `aws resourcegroupstaggingapi` scan.

## Tier 5 — Tier 1 + Tier 2 launch-readiness items still open

From `docs/superpowers/specs/2026-05-04-launch-readiness-plan.md`:

- [ ] Tier 1 #1.3: `order_coffee` energy bug — needs a unit test before closing (audit said the registry already shows +6, just needs verification)
- [ ] Tier 2 #2.1: WAF rate-limit retune (deferred until usage signal)
- [ ] Tier 2 #2.7: Token refresh endpoint
- [ ] Wire `apps/infra/test/*.ts` Jest into CI so silent failures stop happening

## How we got here (root cause summary)

The prod-restoration cascade tonight uncovered four overlapping issues that masked each other for 6 weeks:

1. **L1→L2 WebSocketApi refactor** (commit 2a43df3, 2026-03-22) changed the resource's logical ID from `WebSocketApi` → `WebSocketApi34BCF99B`, forcing CFN to attempt resource REPLACEMENT. The auto-export `ExportsOutputRefWebSocketApi6DDF9954` couldn't be updated because `goosetown-prod-compute` imported it.
2. **Three workaround PRs** (#256, #258, #259) addressed the export name but not the root cause (logical-ID drift), so prod kept failing every deploy.
3. **`WS_LAMBDA_SECRET` was added to `validate()` requirements** without the corresponding CDK wiring. Dev short-circuits validation, so this never tripped until prod actually deployed for the first time.
4. **`removalPolicy: RETAIN`** on `AlbAccessLogsBucket` left an orphan S3 bucket after the delete-and-redeploy cycle, blocking the next CREATE.

Resolved by: deleting both stuck stacks → manually deleting orphan bucket → applying validate() fix in PR #308 → letting CDK rebuild fresh.
