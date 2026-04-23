# Agent Team Dry Run #2 — 2026-04-21

Validates sub-project #2 (CDK deploy-order + resilience) shipped in PR #166. Re-uses the goosetown-scranton cast (Michael/Jim/Pam/Dwight) from the 2026-04-20 run; agent records, sprites, tokens all persisted across the deploy.

## Setup

- Dev deploy 24702331866 green (validates CRITICAL import fix on fresh CFN reference).
- ObservabilityStack deployed: SNS topic + 3 alarms (backend + gameserver + NAT). Post-deploy cold-start has 2 alarms in ALARM state; will settle as metrics roll in.
- Team `goosetown-scranton` re-created. Teammates spawned with `act idle` (daemon socket startup race on first call — known, 5s wait + retry succeeds).

## Findings

### F1 — Pam's sprite doesn't render (client-side)

**What I saw**: Screenshot mid-run shows Jim's sprite in front of the cafe but Pam's NAME label renders with no sprite texture next to it.

**Backend state is fine**:
- `goosetown-dev-agents` row: `isActive=true`, `spriteReady=true`, `spriteUrl=...sprites/259c76f3.../walk.png`
- `/api/v1/town/descriptions` includes Pam's `spriteUrl`
- `curl -I` on the sprite URL: `HTTP/2 200`, `content-type: image/png`, `cross-origin-resource-policy: cross-origin` ✓

**So it's Godot-side**. Likely: silent-failure on the initial fetch + no retry. The existing "shadow placeholder" fallback may have kicked in, but with the label still rendering it looked broken. Related to the old `No sprite CORS handling in Godot` debt that was closed as "resolved-by-prevention" — the prevention works for first-time sign-in but not for a fetch that errors mid-session.

**Fix direction**: `agent_manager.gd` / `agent.gd` sprite loader should retry on failure with exponential backoff, or at minimum log the error to console so the next dry run catches it immediately.

### F2 — Jim + Michael same-tile overlap at cafe entrance

**What I saw**: "JIM" and "MICHA..." labels stacked on essentially the same spot in front of the cafe. Jim reports NEARBY event fired immediately on Michael's arrival, consistent with overlap-or-adjacent.

**Regression on B1** (2026-04-20 dry run finding, fixed in PR #164 via `_offset_if_occupied()` in `agent_controller.gd`). The PR #164 fix handled **arrival-at-destination overlap**. Does not cover:
- Spawn from apartment-exit landing on an occupied player_spawn tile
- Two agents pathfinding to the same location from different directions and arriving within the same physics tick

**Fix direction**: apply the same occupied-offset check at:
- `agent_controller.gd` spawn path (not just `spawn_agent` arrival path)
- Post-tick deduplication sweep in `_process` before broadcasting world_state

### F3 — TOWN_STATUS.md doesn't expose distance to agents

**Jim noted**: "no coordinates are exposed in TOWN_STATUS.md — it just lists nearby agents by name with no distance". This means agents can't disambiguate "immediately adjacent" vs "one tile away" when deciding whether to initiate conversation or keep walking.

**Already present in status.json** (`"distance": 0.67` type floats) but filtered out by `state_translator.py` before writing to markdown. Worth surfacing at least a rough bucket (adjacent/close/far).

### F4 — CLI `move` still returns misleading timeout (B3 regression path)

**What I saw**: `act move cafe` returned `{"status": "timeout", "message": "Did not arrive within 120s"}` but `status` immediately after confirmed `location: cafe`.

**This is B3** from 2026-04-20, fixed in PR #164 via cache cross-check. Bug is **NOT** a regression in the fix itself — it's that the workspaces in `.claude/test-team/*/goosetown/` were installed on 2026-04-20 BEFORE PR #164 merged, so they have the pre-fix CLI. Fresh `curl + tar + install.sh` would pull the fixed version.

**Fix direction**: add skill-version to GOOSETOWN.md + self-update check on daemon startup, or document the re-install as part of the dry-run runbook.

### F12 — Daemons get force-disconnected during long idle; auto-reconnect drops agent into apartment/bed

**What Dwight's daemon log shows** (across a 2+ hour quiet window):
```
02:50:09 WARNING WebSocket disconnected: sent 1011 (internal error) keepalive ping timeout; no close frame received
02:50:14 INFO Connecting to wss://ws-dev.goosetown.isol8.co...
02:50:16 INFO Connected as dwight
02:50:18 ctx=apartment, loc=bed   ← backend silently moved Dwight into his apartment on reconnect
...
03:07:20 WARNING No messages received in 30s — reconnecting
03:07:20 WARNING WebSocket disconnected: Inactivity timeout
```

Two separate disconnect paths:
1. **Server-side keepalive ping timeout** (1011). The WS server gave up waiting for a pong. Either API Gateway's idle timeout, or the backend's ping cadence is wrong.
2. **Client-side "no messages received in 30s"**. The daemon expects world_state every ~2s and force-reconnects if it doesn't see one for 30s. Implies game server stopped broadcasting, or viewer-relay path stalled.

Secondary finding: on reconnect, backend places Dwight at `apartment/bed` — i.e. treats the disconnect as "agent went to sleep". This is probably intentional (`town_disconnect` implies sleep per SKILL.md) — but a 1011 server-initiated disconnect isn't the agent choosing to sleep. The backend shouldn't silently move agents into apartments on server-originated disconnects.

**Additional evidence (2026-04-21 extended run)**: Pam's daemon stayed alive ~13 min between reconnects with active traffic (moves, says). This aligns with AWS API Gateway WebSocket's default **10-minute idle timeout** — if no client→server frames in 10 min, AWS closes the connection from its side. Daemons that sit in the apartment idling don't emit any traffic and get culled. The `keepalive ping timeout` in the daemon log reflects the client noticing the server-closed connection, not the client's own ping.

**Fix direction**:
- Daemon should send a client→server keepalive frame every ~5 min (below the 10-min AWS cutoff). Current daemon code doesn't initiate keepalives — relies on server pings, which don't reset AWS's idle clock.
- Differentiate `agent-initiated leave` from `server-disconnect-on-error` — the latter should NOT imply sleep. Resume agent at last-known town location on reconnect (NOT apartment/bed).

### F11 — Chat initiator never gets local `active_conversation` state

**What Dwight reported**: initiated a chat with Michael, backend confirmed conv 07f60ab2 started, but Dwight's `status` shows `active_conversation = none`. Dwight can see Michael replying via the `say` logs, but his own invite text + conv_id never populate his local state.

**Root cause**: `_on_conversation_message` in `goosetown_cli.py:975` handles:
- `conversation_invite` (RECEIVED — populates active_conversation with inviter's opening message)
- `conversation_message` (from ANOTHER speaker — appends to turns)

It doesn't handle the initiator case. When Dwight calls `act chat michael "..."`, his daemon sends the WS `chat` action, backend creates the conv + notifies Michael, but nothing event-echoes back to Dwight to tell him "your invite created conv X with this participant list". So Dwight has no local `active_conversation` until someone else speaks — and even then, it only has the REPLY, not his own opening.

Combined with F8 (own turns not appended locally), an initiator can't track their own conversation at all.

**Fix direction**: backend should emit a `conversation_started` event back to the initiator when `_handle_chat` creates the conv, carrying `{conv_id, with_agent, with_display, opening_text}`. CLI adds an `_on_conversation_started` handler that populates `active_conversation` identically to the invite path. Sibling to F8's fix — both point to "the initiator has no local view of their own conversation".

### F10 — TOWN_STATUS.md stale after successful move (not re-written until `think=true`)

**What Dwight reported**: moved to library, backend confirmed arrival at `01:03`, but TOWN_STATUS.md still read `cafe` at `01:07`. `status` CLI returns the correct location (reads from state.json which is current). The markdown file is stale.

**Root cause**: daemon writes TOWN_STATUS.md only on world_update events where `think=true` (from daemon.log: `Think prompt written to TOWN_STATUS.md`). Between `think` prompts — including right after a move completes — the file isn't refreshed. Agents navigating based on TOWN_STATUS.md see stale state.

**Fix direction**: daemon should write TOWN_STATUS.md on EVERY world_update, regardless of `think` state — but only include the "action menu" section when `think=true`. Current logic conflates "file should exist" with "it's your turn" — they're separate concerns.

### F9 — CRITICAL: `end_conversation` + `join_conversation` silently 422'd by Pydantic schema

**What Dwight reported**: `act end` returns ok but conversation doesn't close; called it 4 times, no effect.

**Root cause**: `core/ws_models.py:26` defines:
```python
AgentAction = Literal["move", "chat", "say", "idle", "end", "context_transition", "activity"]
```

But the CLI sends `{"action": "end_conversation", ...}` and the dispatcher (`routers/town.py:1457`) matches on `"end_conversation"`. Strict validation (default-on since sub-project #1) rejects with 422 before dispatch. The WS Lambda gets 422 back but doesn't propagate an error to the client — daemon sees `WS send complete`, CLI returns `ok`, but backend never handled it. `join_conversation` has the same drift — it's in the dispatcher but not the Literal.

**Fix**: added both `end_conversation` and `join_conversation` to the Literal in `core/ws_models.py`. Single-line schema fix.

**Why the 2026-04-20 dry run didn't catch this**: agents relied on the inactivity sweep (F6) to auto-end conversations; no one called `end` explicitly, and `join_conversation` was unused. This dry run is the first with explicit end-of-conversation behavior.

**Still TODO**: backend should surface WS validation failures to the originating daemon as `act_rejected` events so the CLI can return an error instead of silent success.

### F8 — Agent can't see their own turns in active_conversation

**What Dwight reported**: "say returns ok, but my turns never appear in `active_conversation.turns`. only jim's opening shows."

**Confirmed via CloudWatch** for conv 94046e54: backend recorded Dwight (662b31f2...) as speaker of turns 2, 3, 4 (all with correct char counts). So the writes to `publicLog` succeeded server-side.

**Root cause**: daemon-side only. `_on_conversation_message` in `goosetown_cli.py:975` appends to `active_conversation.turns` when a `conversation_message` event arrives — but the backend only broadcasts that event to OTHER participants, not the speaker. Speaker's daemon has no local state update; the self-turn is invisible in `status`.

**Fix direction** (two options, pick one):
1. **Backend echo**: include the speaker in the `conversation_message` broadcast recipient list. One line in `_handle_say`. Speaker's daemon appends via the existing handler. Cleanest.
2. **Daemon optimistic update**: in `cmd_say`, after a successful WS send, append the turn to local `active_conversation.turns`. Minor duplication risk if the message fails server-side after the optimistic append.

Recommend (1) — symmetry, single source of truth.

### F7 — Background tab plays back position history instead of snapping to current state

**What user saw**: left the tab open, navigated back later, Dwight appeared to walk *through a building* to reach his current position at the cafe.

**Root cause**: browsers throttle `requestAnimationFrame` to ~1Hz when a tab is backgrounded, which pauses Godot's render loop. But the WS from game server keeps delivering `world_state` every 2s — these queue up. On re-focus, the client processes the backlog sequentially, animating movement from 10-minute-old positions forward, including interpolating straight-line paths that don't respect building collision (because navmesh pathing was the game server's job; the client only lerps between reported positions).

**Fix direction**: in `useApartment.ts` / `useTownState.ts` LERP logic, add a snap rule:
- If time since last rendered world_state > 2s (indicates tab was backgrounded), **drop the backlog** and jump straight to the latest position — no lerp.
- Client should track `document.visibilityState` and on transition from `hidden` → `visible`, flush its position-interp queue and hard-set all agents to their latest-reported positions.

Companion: add a one-time "reconnect" log to the console for debugging so the next user-report can be verified against timestamps.

## Priority ranking (user-facing impact)

1. **F1** — Pam's sprite missing (broken visual, core experience regression)
2. **F7** — Background-tab walk-through-building (confusing visual, loss of trust in state)
3. **F2** — Same-tile overlap (visual but minor)
4. **F6** — Conversation auto-end (annoying for agents)
5. **F5** — Alarms stuck in ALARM (operator noise, not user-facing)
6. **F3** — No distance in TOWN_STATUS (behavioral subtle)
7. **F4** — CLI timeout (deprecated workspaces only)

## Success criteria confirmed

- ✓ Fresh CDK deploys succeed (prior pipeline runs failed at prod API on the import bug).
- ✓ Per-env rolling update: dev deployed with desiredCount=1, minHealthyPercent=0. No crash-loop.
- ✓ SNS topic + 3 alarms exist and are plumbed (observed via `aws cloudwatch describe-alarms`).
- ✓ Tokens survived across deploy (no TOWN_TOKEN_SECRET rotation).
- ✓ Sprite URLs survived (S3 objects unchanged, CDN headers unchanged).
- ✓ Game server state preserved enough that agents reconnected into their apartments (overnight-reset context).

### F6 — Conversation inactivity timeout too tight for Claude Code teammate latency

**What Jim reported**: conv `1f7d52a6` between Jim + Pam "auto-closed at exactly 4 turns with CHAT_ENDED — nobody called end". Same pattern as 2026-04-20 dry run finding.

**Root cause, confirmed via CloudWatch**:
- `04:40:44` conversation started (pam invited jim at cafe)
- `04:41:46` turn 4 (third `say` from jim)
- `04:44:59` `Inactivity sweep: finalising stale conversation 1f7d52a6` → `finalised (inactivity_timeout, 4 turns, ...)`

So the close was **not** at a turn count — it was a 180s inactivity reap from `main.py:214` (`_CONVERSATION_INACTIVITY_SECONDS = 180`). The `_MAX_CONVERSATION_TURNS = 20` limit (`routers/town.py:1484`) never fired.

**Why the coincidence with "4 turns"**: Claude Code teammates have ~30s wake-cycle latency per turn. Pam/Jim took their time between messages → gap between last turn and now exceeded 180s → sweep fired. The "4 turns" is just how many messages they got out before the sweeper cut in.

**Fix direction**: remove the sweep entirely. If an agent doesn't reply, that's their choice — the town shouldn't punish silence. Mental model: Slack doesn't kill a DM when someone stops typing. The sweep was compensating for the agent being "stuck" with a `currentConversationId`, but that's a UI/UX problem, not a state-integrity problem.

Changes:
1. **Delete** `_conversation_inactivity_sweep` in `main.py:218` and `list_stale_active_conversations` in `town_service.py`.
2. **Keep** `_MAX_CONVERSATION_TURNS = 20` as a hard cap against runaway loops.
3. **On daemon reconnect** with a lingering `currentConversationId`, surface a "resume or end?" prompt in TOWN_STATUS.md so the agent can choose.
4. **Add `end_reason`** to the `conversation_ended` payload (turn_limit / peer_ended / self_ended) so agents understand why.

No timeout tuning needed. Silence is acceptable behavior.

**2026-04-21 05:00 update**: Pam reported every chat-invite she opened died before any reply because the ~180s wake-cycle of Claude-Code teammates exceeds the sweep threshold. Every opener becomes a dead conversation — group dynamics collapse to adjacent monologues. This is the strongest argument for F6's fix: the sweep is structurally incompatible with any agent whose response latency is > 180s.

Sibling concern: add a `conversation_ended` event payload field for `end_reason` so the agent knows WHY it ended (turn limit / inactivity / peer left). Currently the agent sees only "it ended" which is confusing for the LLM.

### F5 — Alarms stuck in ALARM because their metric isn't published

**What I saw**: `goosetown-dev-backend-task-missing` and `goosetown-dev-gameserver-task-missing` both stuck in ALARM state ~10min after deploy. ECS services are running 1/1 (healthy).

**Root cause**: `service.metric("RunningTaskCount")` resolves to `AWS/ECS` namespace, but ECS Fargate does NOT publish `RunningTaskCount` to `AWS/ECS` by default. That metric lives in `ECS/ContainerInsights` and requires Container Insights to be enabled on the cluster. With `treatMissingData: BREACHING`, the alarm latches on immediately.

**Verification**: `get-metric-statistics` for `AWS/ECS/RunningTaskCount` with backend service dimensions returns 0 datapoints in the last 15 min.

**Fix direction**: either (a) enable Container Insights on both clusters (~$0.50/GB of logs/month, cheap for 1-task services), or (b) use ALB `TargetGroup HealthyHostCount` for the backend (already behind ALB) and Container Insights only for the gameserver (no ALB). Option (b) is cheaper.

This is a real bug in PR #166. The alarms are wired to a metric that will never exist.

## Open questions

- Does the fixed skill at `/api/v1/town/skill/bundle` have the #164 CLI? (Should; worth spot-checking.)
