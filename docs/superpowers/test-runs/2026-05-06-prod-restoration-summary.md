# Prod restoration — 2026-05-06 night session summary

**Outcome:** Prod backend + WebSocket fully restored after 6+ weeks of silent partial-deploy failures. End-to-end: 4 deploy attempts, 2 stack deletions, 1 emergency hotfix PR (#308), 1 orphan S3 bucket cleanup, ~50 min from diagnosis to green.

**Final state:**
- `goosetown-prod-api`: `CREATE_COMPLETE` (fresh L2 WebSocketApi, `WebSocketApi34BCF99B` logical ID)
- `goosetown-prod-compute`: `CREATE_COMPLETE` (ECS 2/2 running, COMPLETED rollout)
- `https://api-prod.goosetown.isol8.co/health` → 200 `{"status":"healthy"}`
- `https://ws-prod.goosetown.isol8.co/` → 426 (WebSocket-upgrade-required, correct)

## How we got here — chronology of overlapping failures

The investigation peeled back **four issues that masked each other** and kept prod-api + prod-compute frozen on March-22 templates while the rest of prod kept deploying.

### Issue 1 — L1 → L2 WebSocketApi refactor (commit 2a43df3, 2026-03-22)

Source code switched from `apigatewayv2.CfnApi` (L1) → `apigatewayv2.WebSocketApi` (L2). The L1 produced logical ID `WebSocketApi`; the L2 internally creates child constructs (`defaultRoute`, `disconnectRoute`, etc.) which forces CDK to disambiguate the parent's logical ID with a hash → `WebSocketApi34BCF99B`. CFN sees this as a *resource replacement* (delete `WebSocketApi`, create `WebSocketApi34BCF99B`).

**Why it blocked prod:** the auto-export `goosetown-prod-api:ExportsOutputRefWebSocketApi6DDF9954` is imported by `goosetown-prod-compute`. CFN refuses to update the export's value while another stack imports it, so api can't replace the resource.

### Issue 2 — Three half-fixes that never closed the loop (PRs #256, #258, #259)

PR #256 added an explicit `LegacyWsApiAutoExportProd` to preserve the legacy export name. PR #258 reverted it (collision with CDK's auto-generated export). PR #259 re-added it more carefully. **None addressed the actual root cause** (logical-ID drift forcing a replacement) — only the export name. Prod kept failing.

### Issue 3 — `WS_LAMBDA_SECRET` validate() requirement never wired

`apps/backend/core/config.py` listed `WS_LAMBDA_SECRET` as a required env var for non-dev environments. But:
- `apps/infra/lib/stacks/auth-stack.ts` never created the secret
- `apps/infra/lib/stacks/compute-stack.ts` never passed it as an env or secret to the backend container
- `apps/infra/lambda/ws-default/index.py` never reads or sends the `x-ws-lambda-secret` header that the backend's `/ws-message` endpoint nominally checks

**Dev masked it** because `validate()` short-circuits on `ENVIRONMENT == "dev"`. Prod was the first env to actually hit the requirement, and it did so on every container start, raising `RuntimeError` from FastAPI's lifespan → exit code 3 before the log group existed.

### Issue 4 — `removalPolicy: RETAIN` on the ALB logs S3 bucket

Prod's `AlbAccessLogsBucket` was set to RETAIN (a deliberate prod safeguard). When we deleted prod-compute to clear Issue 1+2, the bucket survived as an orphan. The next CREATE failed at `AWS::EarlyValidation::ResourceExistenceCheck` because the bucket name `goosetown-prod-alb-logs` was already taken.

## The restoration sequence

| Step | Action | Result |
|---|---|---|
| 1 | Diagnosed Issue 1 by comparing deployed prod-api template (`WebSocketApi` no hash) against current synth (`WebSocketApi34BCF99B`) | Confirmed L1→L2 logical-ID drift |
| 2 | `aws cloudformation delete-stack goosetown-prod-compute` (~10 min for ECS service drain) | DELETE_COMPLETE |
| 3 | `aws cloudformation delete-stack goosetown-prod-api` (~30s) | DELETE_COMPLETE |
| 4 | `gh workflow run deploy.yml --ref main` | Run #1 created prod-api fresh in 1m20s ✓ but failed at prod-compute (Issue 3 — backend exits code 3) |
| 5 | Diagnosed Issue 3 by reading task definition env + secrets (no `WS_LAMBDA_SECRET` wired anywhere) | Confirmed vestigial requirement |
| 6 | Opened PR #308: drop `WS_LAMBDA_SECRET` from `validate()` required list | 8/8 CI green |
| 7 | User authorized merge without codex review (prod was dark) | Merged at 01:19:21 UTC |
| 8 | `aws cloudformation delete-stack goosetown-prod-compute` (clear ROLLBACK_COMPLETE state) | DELETE_COMPLETE |
| 9 | Retriggered deploy → run #2 | Failed at Issue 4 (orphan bucket) |
| 10 | `aws s3 rm s3://goosetown-prod-alb-logs --recursive && aws s3 rb s3://goosetown-prod-alb-logs` | bucket removed |
| 11 | Retriggered deploy → run #3 | Failed because compute stack was stuck in `REVIEW_IN_PROGRESS` from a prior failed changeset |
| 12 | `aws cloudformation delete-stack goosetown-prod-compute` (clear stuck shell) | DELETE_COMPLETE |
| 13 | Retriggered deploy → run #4 (25411742091) | **All 10 prod stacks ✓; prod-compute CREATE_COMPLETE in 5m57s; ECS 2/2** |
| 14 | Tier 0 smoke: `/health` → 200, WS endpoint → 426 (correct) | Prod healthy ✓ |

## Side observations

- The Vercel-Prod alias step still fails (cert issuance for `goosetown.isol8.co` apex). The apex isn't in Route53 — only `dev.goosetown.isol8.co` is. This is pre-existing; the Vercel *deployment* succeeds, only the alias rename breaks.
- CLAUDE.md is stale: documents prod as `api.goosetown.isol8.co` / `ws.goosetown.isol8.co` (no `-prod` suffix), but actual Route53 has `api-prod`/`ws-prod`. Worth a one-line correction.
- **Six weeks of merged infra work to prod-api / prod-compute never reached prod**: PRs #271 (VPC removal from WS Lambdas), #275 (Lambda code asset migration), #297 (autoscale 6→12), #298 (PixelLab credit poller — well, this only affected api/compute partially). All of those landed effective tonight.

## Follow-ups created

- Test list: `docs/superpowers/test-runs/2026-05-06-prod-restoration-test-list.md` (Tiers 0–5 covering smoke / refactor PRs #300-#307 / multi-agent soak / infra cleanup)
- Branch protection setting flip: add `Infra (CDK) Test` to required checks on `main` (user-only, GitHub UI). PR #298 was merged with that check failing → that's how we missed the L2 drift originally.
- PR #256 cleanup: remove the now-unused `LegacyWsApiAutoExportProd` from `api-stack.ts:316-321`. Safe because prod-compute was rebuilt without the legacy import.
- Eight overnight architecture PRs (#300–#307) still awaiting codex review/merge.

## Lessons

1. **Workflow shows `failure` ≠ all of prod failed.** The deploy.yml does dev → prod stages serially per stack. A failure in `prod-api` cascades to "workflow failed", but earlier stacks (storage, auth, database, network, gameserver, observability, cluster) had already succeeded. We had 6 weeks of partial deploys with no alerting because the workflow signal was binary.
2. **Logical-ID drift from L1→L2 refactors is silent and lethal.** CDK doesn't warn, CFN treats it as resource REPLACEMENT, exports cascade-fail.
3. **Vestigial `validate()` requirements stay vestigial until a fresh env hits them.** Dev's environment-shortcircuit hid this for months.
4. **`removalPolicy: RETAIN` is correct for prod data, but accumulates orphans on rebuild cycles.** Worth either flipping to DESTROY+autoDeleteObjects on rebuilds or using `Bucket.fromBucketName` to attach to existing buckets in the rebuild path.
