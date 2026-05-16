# Tier 3: 5-agent soak — 2026-05-15

First proven multi-agent concurrency soak since the post-2026-05-04
launch-readiness plan was authored. Single-user 5-agent setup, mechanical
CLI orchestration (no LLM-driven teammates), all 5 agents reused one
`town_token` from one dev Clerk session.

## Scope

This is the **5-agent slice** of the Tier 3 soak documented in
`docs/superpowers/test-runs/2026-05-06-prod-restoration-test-list.md`.
The 15-agent variant requires 3 separate user accounts (per-user agent
quota is 5 since PR #284) and is **not** runnable from one Clerk session.

## Agents

| Name | ID prefix | Personality | Outcome |
|------|-----------|-------------|---------|
| tier2-test | 2f9db5dd | curious tester | sprite ready, mood=6 (people_watch +5, chat +1), energy=99 |
| soak-2 | f5fc66f5 | introspective wanderer | sprite ready, chat initiator |
| soak-3 | b683bba3 | warm librarian | sprite ready, did not join chat (proximity edge) |
| soak-4 | 9920a2a7 | energetic athlete | sprite ready, mid-conv joiner |
| soak-5 | 796f6dd6 | friendly barista | **sprite Phase C hit 5-min cap (PR #314)**, static-only |

## Verified working

### Sprite pipeline (PRs #181, #314)
- 5 simultaneous `POST /town/agent/register` calls — sprite_queue accepted all without rate-limiting errors
- 4/5 reached Phase C (animations) within 5 min
- 1/5 (soak-5, last in queue) hit PR #314 cap and stayed at Phase B (static)
- All 5 functional with static sprite regardless of Phase C state

### Onboarding lattice (#300)
- pending_sprite → static → alive without backwards flips for all 5
- No Vera-finding-Q regression observed

### Render helpers (#303, #307)
- `goosetown status` template: mood labels rendered, activity default = `idle`, no blank fields
- Phase 1 / Phase 2 template equivalence holds

### Movement + context transition
- `move bed → desk` within apartment: arrival event delivered
- `move bed → exit`: triggers `RETURNED_TO_TOWN`, `location_context` flips apartment → town
- `move player_spawn → plaza`: deterministic
- 4 simultaneous moves to plaza completed without collision

### DaemonMailbox event fanout (PRs #302, #305, #306)
All event types listed in the test list verified:
- `conversation_started` (initiator) ✅
- `conversation_invite` (target) ✅
- `CHAT_MESSAGE` say echoes to sender + forwards to participants ✅
- `PARTICIPANT_JOINED` to existing participants ✅
- `PARTICIPANT_LEFT` to remaining participants ✅
- `conversation_ended` to leaver with `reason: left_by_self` ✅
- Activity start/complete events ✅
- Context transition events ✅

### Mood + energy engine (PR #288)
- tier2-test: 0 → 5 (people_watch +5) → 6 (chat +1)
- soak-4: 0 → 1 (chat +1 mood)
- Energy decremented −1 on chat and on activity completion

### Quota leak fix (PR #313)
**Direct verification:** after `goosetown leave` on all 5 agents,
DynamoDB scan with filter `userId = X AND isActive = "true"` returns
`Count: 0`. All 5 agents reset `isActive` to `"false"` on clean
disconnect. Per-user quota fully reclaimed.

## Daemon-side inconsistencies (open items, not blockers)

### 1. Mid-conversation joiner doesn't backfill participant list
**Symptom**: soak-4 joined an existing chat between soak-2 and tier2-test.
Its local `active_conversation.participants` showed only `["soak-4"]`,
not `["soak-2", "tier2-test"]` as expected.

**Hypothesis**: `goosetown act join_conversation <conv>` response or the
matching DaemonMailbox event doesn't include the existing roster. The
daemon never learns who else is in the conversation.

**Impact**: agents joining mid-conversation can't address other
participants by name from their cached state — they'd need to call
`/town/conversations` over REST.

**Suspected location**: daemon.py `_on_participant_joined` or the
backend's `join_conversation` response shape.

### 2. Daemon local cache stale after rapid moves
**Symptom**: soak-3's local `state.json` showed
`location_context=town, location=player_spawn` even after its move to
plaza returned `{"status": "arrived", "location": "plaza", "note":
"arrived via state poll"}`. tier2-test's `NEARBY` event correctly listed
soak-3 (game-server truth), but soak-3's own `goosetown status` lagged.

**Impact**: agents may make decisions on stale location info.

### 3. active_conv=True + activity=idle inconsistency
**Symptom**: After PARTICIPANT_LEFT, soak-4 daemon showed
`activity='idle'` but `active_conversation` still populated. tier2-test
correctly remained `activity='talking'`.

**Hypothesis**: SPRITE_READY event landed at the same time and reset
activity but didn't clear active_conv.

### 4. Location flicker during chat (plaza ↔ bank)
**Symptom**: soak-2 TOWN_EVENTS during chat:
```
[20:22] ARRIVED plaza
[20:22] ARRIVED bank
[20:23] ARRIVED plaza
```
The agent oscillates between plaza and bank without moving (markers are
adjacent). Likely the location-resolution path in
`_get_agent_location` picks the nearest marker non-deterministically
when an agent stands between two close markers.

### 5. Spurious RETURNED_TO_TOWN paired with ARRIVED
Vera finding O, still present. Every apartment→town transition emits
both events. Log noise, not functional.

## What this soak did NOT cover

- **Conversation auto-end timer** (PR #194, 60s silence): didn't wait the
  full 60s with no activity — would need a longer soak run.
- **15-agent stress**: PixelLab credit-drain alarm, ECS autoscale
  headroom. Requires multi-user setup.
- **Energy → sleep transitions**: didn't burn an agent down to 0 energy.
- **Long-running stability**: 5-min run, not 1-hour.

## Cost

- PixelLab: 5 character creations + 4 animation jobs (1 agent left at
  Phase B). No credit-budget alarm fired in CloudWatch during run.
- ECS: no autoscale fired (5 idle daemons is well under threshold).
- LLM tokens: zero (mechanical CLI).

## Recommendation

The 5-agent slice is sufficient evidence that the post-launch-plan
refactor batch (#300–#307) did not regress core multi-agent flows.
The remaining test items (auto-end timer, 15-agent stress) are
separate exercises that need a longer run or multi-user setup.
