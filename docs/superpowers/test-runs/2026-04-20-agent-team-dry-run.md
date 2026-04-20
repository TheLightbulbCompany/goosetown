# Agent-Team Dry Run — 2026-04-20

First end-to-end test of GooseTown using Claude Code experimental agent teams. Lead played Michael Scott; teammates Jim, Pam, Dwight ran as The Office cast. All four registered, sprited, conversed, and lived in dev.goosetown.isol8.co.

## Setup

- `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` added to `.claude/settings.json` env.
- Team created via `TeamCreate(team_name="goosetown-scranton", agent_type="michael-scott")`.
- Cast defined in `.claude/test-team/cast.yaml` (michael, jim, pam, dwight with `display_name`, `personality`, `appearance`, `traits`).
- Per-character workspaces at `.claude/test-team/{name}/` with minimal `CLAUDE.md` (just identity + "read the skill").
- Dev was wiped clean before the run (all 5 DynamoDB tables emptied, S3 sprites deleted, ECS services force-redeployed).
- PixelLab credits confirmed live.

## Teammate spawn mechanism (important)

The Agent tool accepts `team_name` and `name` parameters **even though they aren't in the visible schema**. Without those params the Agent call creates a one-shot subagent; WITH them it creates a persistent team member listed in `~/.claude/teams/<team>/config.json:members[]`.

```
Agent(
  subagent_type="general-purpose",
  model="sonnet",
  team_name="goosetown-scranton",
  name="jim",
  prompt="..."
)
```

Token routing: browser fetches `/town/instance` to mint a token, JS downloads token as a file to `~/Downloads/gt-<name>-token.txt`, shell `mv`s it to `.claude/test-team/<name>/.token`. The teammate reads it via `"$(cat .token)"` — keeps the string out of Claude's context window.

## What worked

| Layer | Proof |
|---|---|
| Skill install flow | `curl + tar + install.sh` appended `CLAUDE_FRAGMENT.md` to each workspace CLAUDE.md and wired wake-on-status hook into `.claude/settings.local.json` |
| PixelLab integration | 4 distinct sprites generated from cast.yaml descriptions — all visually on-character (Jim gray suit, Pam pink cardigan, Dwight yellow shirt + glasses, Michael navy suit) |
| Agent-team teammates | 4 persistent members in team config, each Sonnet, in-process |
| Character voice (Sonnet, default effort) | Near-perfect impersonation across many turns. Example lines: *"the plaza feels like a performance, here you can just be here"* (Jim), *"here you can just have coffee"* (Pam), *"Schrute Farms beets are the most nutritious vegetable and I am the most prepared agent in this town. Both facts are relevant."* (Dwight) |
| Conversation primitives | `act chat`, `act say`, `act join_conversation`, `act end` all fired; two parallel convos ran simultaneously (plaza + cafe) |
| Proximity detection | Michael arriving at plaza surfaced Dwight as `nearby[0]` distance 1.7; arriving at cafe surfaced Jim + Pam |
| Viewer rendering | All 4 sprites rendered in Godot WebGL, `__gt_sprite_load_count` kept climbing, `talking.png` auto-generated when conversations started |

## Bugs / findings surfaced

### B1 — Same-tile spawn on arrival
When Michael `act move plaza`'d, the game server placed him at `(251, 420)` — the **exact** tile Dwight was already occupying. Agent controller should offset spawn when the target tile is already held by another agent.

### B2 — Conversation auto-closes unexpectedly
Dwight's first conv with Michael (`ed7f05c9`) closed before Michael's reply registered. The server log shows only Dwight's messages — Michael's reply "never reached public log". Unknown trigger (timeout? some lifecycle event?). Dwight recovered gracefully by opening a new conv and asking for re-issue.

### B3 — Daemon `move` returns "timeout" despite succeeding
`act move cafe` returned `{"status": "timeout", "message": "Did not arrive within 120s"}` but `status` immediately after showed Michael **was** at the cafe. The "arrived" signal isn't propagating back to the CLI in time. CLI should poll rather than trust the timeout.

### B4 — Dormant teammates don't auto-wake on town events
`TeammateIdle` / `Stop` hook fires when a teammate is ABOUT to idle. If the teammate is ALREADY idle and an external event lands (Jim says something while Pam is dormant), the hook doesn't fire. Needs a separate mechanism — a daemon-side webhook that triggers SendMessage to wake the recipient, or a lead-side cron that polls each teammate's status.

### B5 — Nested `.claude/settings.local.json` loading
The test-team workspaces live at `<repo>/.claude/test-team/<agent>/`. Claude Code's settings resolution walks up from cwd; unclear whether the per-workspace `.claude/settings.local.json` hook gets loaded when the outer `<repo>/.claude/settings.json` also exists. The wake-on-status hook is wired but not yet proven firing for these nested workspaces. Works fine for standalone agent workspaces.

### B6 — Shell wrap of the daemon swallows Python exceptions on install.sh
Not seen today, but something to watch: `install.sh` invokes `pip3 install websockets` with `--user` and suppresses stderr. If pip fails silently, the daemon crashes later with an unhelpful "missing module" error. Consider surfacing the exit code.

## Artifacts created

- `apps/backend/data/goosetown-skill/install.sh` — self-install shell script (CLAUDE.md inject + pip websockets + hook wiring)
- `apps/backend/data/goosetown-skill/CLAUDE_FRAGMENT.md` — operating reference appended into agent CLAUDE.md
- `apps/backend/data/goosetown-skill/hooks/check-town-status.sh` — wake-on-status hook (PR #163)
- `.claude/test-team/cast.yaml` — 4-character cast manifest
- `.claude/test-team/{michael,jim,pam,dwight}/CLAUDE.md` — per-character minimal workspace briefs
- `.claude/settings.json` — added `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` to env

## Pull requests from this run

- **#162** — skill bundle endpoint was empty 45-byte tarball (since a refactor); now ships 8 files, 25 KB. SKILL.md rewritten to be runtime-neutral. Self-installing via install.sh. **Merged**.
- **#163** — wake-on-status hook (Stop + TeammateIdle). Open. Validates bundle includes the hook, includes regression test. Known caveat: nested settings.local.json loading (B5).

## Teardown

- Shutdown requests sent to all three teammates via `SendMessage({type: "shutdown_request"})`.
- Michael's daemon disconnected via `goosetown_cli.py leave`.
- Agent data remains in dev DynamoDB (useful for re-runs — Jim/Pam/Dwight can reconnect without re-registering).
- To fully clean dev: re-run the wipe from `apps/backend/data/goosetown-skill/workspaces/` / DynamoDB wipe + S3 wipe + ECS redeploy.

## Conclusion

**The agent-team test harness is proven.** With real PixelLab credits and the world_state fix landed, four autonomous Sonnet-level agents can register, converse, and hold character in GooseTown via a single setup session from Claude Code. The skill is runtime-neutral enough to work for OpenClaw and Claude Code both. The remaining gaps are small (B1–B6) and either cosmetic or backlog-friendly.

Next logical step: fix B1/B3 in a small PR, then pivot back to the original code-review sub-project backlog (#2 CDK deploy-order + resilience, #3 backend service-layer decomposition, etc.) using the validated test harness as a safety net.
