# GooseTown Production Hardening (Sub-project 2) — Design Spec

**Date:** 2026-04-30
**Status:** Pending implementation (blocked on sub 1)
**Branch:** `spec/production-hardening` (when started)

## Context

This is sub-project 2 of a 5-part path to a public-launchable platform:

1. Skill packaging & distribution — canonical Anthropic skill, Claude Code plugin, OpenClaw auto-discovery
2. **Production hardening (this spec)** — cluster consolidation, multi-NAT, WAF BLOCK, container scanning, infra-test CI wiring
3. Multi-tenant app-level hardening — per-user agent quotas, rate limits, token revocation, admin kill switch
4. Personal launch dogfood — install the productionized skill the way a user would, weekend soak with 15 OpenClaw agents
5. Onboarding & landing — public landing page, friction-free signup → install → first-sprite UX

Sub 2 is what makes "production-level multi-tenant" actually real. Without multi-NAT, WAF in BLOCK mode, and container scanning, the platform is not at production bar regardless of how clean the app-level features are. Sub 2 also collapses the GooseTown ECS footprint from 4 clusters to 2, which the user flagged as wrong on 2026-04-30.

## Goal

GooseTown's infrastructure matches a baseline production posture: NAT redundancy across AZs, WAF blocking known bad traffic patterns, container images scanned on every deploy, infra tests gating CI, and one ECS cluster per environment instead of two.

The Isol8 ECS footprint is explicitly out of scope. Both `isol8-prod-container-Cluster*` and `isol8-dev-container-Cluster*` (and the per-user `openclaw-user_*` services they host) are untouched.

## Success criteria

- [ ] **One ECS cluster per env:** `goosetown-dev` and `goosetown-prod` host both backend and game-server services. The `goosetown-{env}-gameserver-Cluster*` clusters are deleted.
- [ ] **Multi-AZ NAT in prod:** `goosetown-prod-network` has 2 NAT gateways (one per AZ); a single-AZ NAT failure does not kill all egress. Dev stays single-NAT to save the $32/mo.
- [ ] **WAF CommonRuleSet at BLOCK** in both envs after access-log review confirms no false-positive operational traffic.
- [ ] **Trivy scans the backend Docker image** in CI before deploy; HIGH/CRITICAL CVEs fail the build.
- [ ] **Infra tests** (`apps/infra/test/*.ts`) run in CI on every PR; pre-existing `network-stack.test.ts` failure is fixed.
- [ ] **Bandit findings** (17 low-severity) triaged: each one is either suppressed with a justification comment or fixed.
- [ ] Zero downtime in prod during the cluster consolidation rollout (dev tolerates ~30s during the swap).

## Architecture

### Cluster consolidation (4 → 2)

Today:
- `ComputeStack` creates `goosetown-{env}` cluster, hosts `goosetown-{env}-backend` ECS service
- `GameServerStack` creates `goosetown-{env}-gameserver-Cluster*` cluster, hosts `goosetown-{env}-gameserver` ECS service

After:
- New thin `ClusterStack` per env creates the single `goosetown-{env}` cluster
- `ComputeStack` and `GameServerStack` both reference it via cross-stack import
- The legacy `goosetown-{env}-gameserver-Cluster*` clusters are deleted by CloudFormation when GameServerStack stops creating them

CDK can't seamlessly rename a cluster — it'll create a new one and delete the old. To avoid game-server downtime in prod, the migration is two-step:

1. **Step 1 (deploy A):** GameServerStack switches to importing the cluster from ClusterStack. CloudFormation creates a new `goosetown-{env}-gameserver` ECS service in the existing backend cluster, drains the old service in the legacy gameserver cluster, then deletes the legacy cluster. Service Discovery namespace stays put — only the cluster underneath changes. Brief drain window (~30-60s) but the WS connections to viewers and agents are auto-reconnected by the daemon and viewer protocols.
2. **Step 2 (deploy B):** Once dev is verified clean, prod ships during a quiet maintenance window.

Cluster ARN lookup in code (`game_server_client.py`, etc.) goes through Cloud Map service discovery, not direct ECS service ARN — so the rename doesn't break runtime references.

### Multi-NAT in prod

`network-stack.ts:18` today creates `natGateways: 1`. After:

```typescript
const natGateways = isProd ? 2 : 1;
this.vpc = new ec2.Vpc(this, "Vpc", {
  ...,
  natGateways,
  maxAzs: 2,
});
```

CDK distributes them across the 2 AZs automatically.

Cost impact: ~$32/mo additional in prod. Acceptable for "production-level" requirement.

### WAF CommonRuleSet COUNT → BLOCK

Per PR #212, `AmazonIpReputationList` and `KnownBadInputs` are already at BLOCK. `CommonRuleSet` is at COUNT pending FP review. Verify >7 days of dev + prod access logs in CloudWatch:

```
aws logs filter-log-events \
  --log-group-name "aws-waf-logs-goosetown-prod" \
  --filter-pattern "{ $.action = \"COUNT\" && $.terminatingRuleId = \"AWS-AWSManagedRulesCommonRuleSet*\" }" \
  --max-items 200
```

If the matched events are all malicious (no real user traffic, no Clerk webhooks, no PixelLab callbacks), flip to BLOCK. If FPs exist, add them to `excludedRules`.

### Trivy in CI

The deploy pipeline already builds the backend Docker image. Add a Trivy scan step:

```yaml
- name: Scan backend image with Trivy
  uses: aquasecurity/trivy-action@master
  with:
    image-ref: ${{ steps.build.outputs.image-uri }}
    severity: HIGH,CRITICAL
    exit-code: 1
    ignore-unfixed: true
```

Run before the deploy step that pushes to ECS. If HIGH/CRITICAL CVEs exist, the build fails. `ignore-unfixed: true` skips CVEs without a patch upstream — those become a separate triage item.

### Infra tests in CI

Today `apps/infra/test/*.ts` aren't run by `.github/workflows/ci.yml`. Add:

```yaml
- name: Infra tests
  working-directory: apps/infra
  run: npm test
```

Fix the pre-existing `network-stack.test.ts` failure noted in CLAUDE.md before flipping `continue-on-error: false`.

### Bandit cleanup

17 low-severity findings live in the backend codebase. Sweep:

```bash
cd apps/backend
uv run bandit -r . -ll -f json | jq '.results[] | {file: .filename, issue: .issue_text, line: .line_number}'
```

For each finding: either fix it or add `# nosec B<id>: <reason>` with a justification. Goal: zero unsuppressed low-severity findings, so future findings actually represent new debt.

## Out of scope

Not in any sub:
- **Isol8 ECS footprint** — explicitly untouched per user 2026-04-30
- **Multi-region** — single us-east-1 is fine for this launch class
- **Token refresh endpoint** — UX nicety, not security; deferred
- **WAF rate-limit tuning** — reactive once real traffic patterns emerge

Punted to other subs:
- App-level multi-tenant features (sub 3)
- Bug fixes for the soak (sub 4)
- Public landing page (sub 5)

Deferred indefinitely:
- Per-agent observability / paging alarms

## Dependencies & order

1. **Cluster consolidation** is the riskiest and largest piece. Do it first in dev, verify a full week of stability, then prod.
   - Day 1-2: refactor CDK (`ClusterStack` + cross-stack import in `ComputeStack` and `GameServerStack`)
   - Day 3: deploy to dev, verify game server + backend run on `goosetown-dev` cluster
   - Day 4-7: soak in dev (the existing dogfood-dev pattern)
   - Day 8: ship to prod during a quiet window
2. **Multi-NAT** — CDK 2-line change, deploy, verify both NAT gateways have traffic in CloudWatch (Day 9)
3. **Trivy in CI** — deploy.yml edit + first-run triage (Day 10)
4. **Infra tests in CI** — wire ci.yml + fix existing failing tests (Day 11)
5. **WAF CommonRuleSet → BLOCK** — log review + CDK change (Day 12)
6. **Bandit cleanup** — sweep + suppress with justification (Day 13)

Roughly 2 weeks total. Items 2-6 can run in parallel after the cluster consolidation lands.

## Testing

CDK:
- `npx cdk synth` succeeds for the new ClusterStack
- `npx cdk diff` on ComputeStack and GameServerStack shows expected service rebinding
- `apps/infra/test/cluster-stack.test.ts` (new) asserts cluster name + service references

Backend:
- Existing `pytest` suite remains green after backend ECS service moves clusters (no code change expected)

Manual:
- `aws ecs list-clusters` post-deploy shows exactly `goosetown-{dev,prod}` plus the untouched isol8 clusters
- `aws ec2 describe-nat-gateways --filter Name=tag:aws-cdk:cloudformation-stack-name,Values=goosetown-prod-network` returns 2 in different subnets in different AZs
- WAF CloudWatch metric `BlockedRequests` registers events post-flip
- Trivy fails a synthetic deploy with a planted HIGH-CVE base image

## Risks

- **Cluster consolidation rollout:** the riskiest piece. CloudFormation cluster swap drains the old game-server task and starts a new one — viewers and agents will see a 30-60s WS disconnect. Mitigation: dev rollout first with a week of soak; prod during a planned quiet window. Daemon and viewer protocols already auto-reconnect, so the disruption is bounded.
- **WAF BLOCK false positives in prod:** flipping to BLOCK could break a real traffic path (Clerk webhook, PixelLab callback, frontend polling) that wasn't visible in COUNT-mode logs because nobody triggered it during the review window. Mitigation: ship to dev first, leave for 48h, watch CloudWatch + alarm dashboard before flipping prod. Roll back path is one-line CDK revert.
- **Trivy noise on first run:** the current backend base image probably has open HIGH CVEs that haven't been pinned. Mitigation: first run logs the findings without failing the build (`continue-on-error: true` for one PR), triage them, then flip to fail-on-find. This is identical to the Semgrep rollout pattern (PR #220).
- **Infra test pre-existing failure:** `network-stack.test.ts` has a known failure that's been silent on main. Fixing it might surface that the test or the stack is wrong; either way needs investigation before CI gating.
- **CDK stateful refactors:** moving an ECS service to a different cluster sometimes triggers CloudFormation to mark a logical resource as "needs replace," which can ripple unexpectedly. Mitigation: `cdk diff` carefully and review with the lead-programmer agent before deploying.

## What this spec deliberately does NOT do

The user said "kill the legacy stuff" earlier in the brainstorm, then clarified "Don't touch the isol8 stuff." Neither this spec nor any related sub deletes:
- The two `isol8-*` ECS clusters
- The `isol8-{prod,dev}-service` ECS services
- Any `openclaw-user_*` per-tenant services running under the isol8 clusters
- Any IAM roles, log groups, or VPC peering relationships scoped to Isol8

If "kill the legacy stuff" referred to GooseTown internals (orphan task definitions, old log groups, stale Lambdas), those will surface during the cluster consolidation review and get cleaned up opportunistically. This spec doesn't pre-commit to that scope.
