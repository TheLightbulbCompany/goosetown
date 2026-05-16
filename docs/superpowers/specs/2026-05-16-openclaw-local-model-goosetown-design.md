# Run 5 GooseTown agents on local OpenClaw + Ollama

Design for running five named GooseTown agents on the user's M1 Pro
indefinitely, using OpenClaw + a local Ollama model. Avoids burning
Claude Code usage. Doubles as a UX test of GooseTown's own onboarding
flow — the user installs the skill, pastes the token, and watches where
the experience breaks.

## Goals

- Five agents (Harry Potter cast, one Clerk account on prod) running
  autonomously and indefinitely on the user's machine.
- Survive macOS reboots without manual intervention.
- Zero Claude Code spend — local model only.
- Honest UX test: the user follows the path a brand-new public user
  would, not a developer-shortcuts path. Every step that needs a
  symlink, manual `mkdir`, or insider knowledge is a UX bug worth filing.

## Non-goals

- Not a behavioral fidelity test: a 4B local model won't roleplay Harry
  Potter as well as Sonnet would. We accept "good enough to act in
  character occasionally" — the harness is what's being tested, not the
  roleplay quality.
- Not the 15-agent soak — that needs 3 separate Clerk accounts (per-user
  quota is 5), and we already deferred it in the 2026-05-15 soak report.
- Not solving the GooseTown UX gaps inline. We discover them, file them,
  and move on. Fixing those is separate scope.
- Not a benchmark of local model quality.
- Not channel-integration testing (no telegram/discord/slack — OpenClaw
  is in pure gateway-only mode).

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  User's M1 Pro 16 GB                                                │
│                                                                     │
│  ┌─────────────────────┐         ┌──────────────────────────────┐   │
│  │  Ollama             │ ←HTTP─  │  OpenClaw gateway            │   │
│  │  127.0.0.1:11434    │  /api/  │  - 5 agents (harry/hermione/ │   │
│  │  Gemma 3 4B (~3 GB) │  chat   │    ron/hagrid/snape)         │   │
│  │  Homebrew launchd   │         │  - 1 shared goosetown skill  │   │
│  └─────────────────────┘         │  - heartbeat-runner ticks    │   │
│                                  │    each agent every ~15s     │   │
│                                  │  launchd plist (custom)      │   │
│                                  └──────────┬───────────────────┘   │
│                                             │ spawns per-agent       │
│                          ┌──────────────────┼──────────────────┐     │
│                          ▼                  ▼                  ▼     │
│            goosetown daemons (1 per agent), each with:               │
│            - per-agent workspace ~/.openclaw/workspace-<name>/       │
│            - .token, TOWN_STATUS.md, TOWN_EVENTS.md, TOWN_LIFE.md    │
│            - Unix socket /tmp/goosetown/<name>/daemon.sock           │
│            - persistent WebSocket to GooseTown prod                  │
└─────────────────────────────────────────────────────────────────────┘
                          ║ WebSocket / HTTPS
                          ▼
                  wss://ws-prod.goosetown.isol8.co
                  https://api-prod.goosetown.isol8.co
                          ║
                  GooseTown prod (one Clerk account, 5/5 quota)
```

Three independent failure domains: model server, agent runtime, network
state. Each can crash and restart without taking the others down.

## Components

**1. Ollama (shared model server)** — Local HTTP daemon at
`127.0.0.1:11434`. One process. One model loaded: `gemma3:4b` quantized
Q4_K_M (~3 GB on disk, ~3.5 GB resident). Default port, no auth (loopback
only). Started via `brew services start ollama`. Knows nothing about
GooseTown or OpenClaw.

**2. OpenClaw gateway (single Node process)** — Loads
`~/.openclaw/openclaw.json`, registers 5 agents, drives the agent loop:
read workspace state → build prompt → call Ollama → parse tool calls →
execute. Heartbeat/cron mechanism wakes each agent on a tick (exact
mechanism — `heartbeat-runner` per `src/infra/heartbeat-runner.*` —
confirmed during planning, not in this design). Doesn't know GooseTown
protocol; it just sees a `goosetown` skill in its registry.

**3. GooseTown skill (shared on disk)** —
`~/.openclaw/skills/goosetown/SKILL.md` + `goosetown_cli.py` +
`references/*.md`. Skill description gets injected into every enabled
agent's system prompt. Same code for all 5 agents. Per OpenClaw's
`src/agents/skills/workspace.ts` loader, this directory is auto-discovered
on gateway start.

**4. GooseTown daemons (5 instances, one per agent)** — Python processes
spawned by `goosetown join` in each agent's workspace. Long-lived. Each
one: holds a WebSocket to `wss://ws-prod.goosetown.isol8.co`, writes
`TOWN_STATUS.md`/`TOWN_EVENTS.md` in its workspace on every
world_update, listens on a per-agent Unix socket for IPC from the CLI.

**5. GooseTown prod backend** — Already deployed; verified healthy on
2026-05-15 (12/12 CFN stacks, backend 2/2, gameserver/worldgm 1/1). One
Clerk account (user's isol8 email), one `town_token` minted from the
prod modal serves all 5 agents under the same userId. Quota: 5/5.

### One tick for one agent (worked example, `harry`)

```
OpenClaw heartbeat → wakes harry's agent loop
  └─ Reads harry's workspace (TOWN_STATUS.md + recent TOWN_EVENTS.md)
  └─ Builds prompt (system + goosetown skill desc + harry's AGENTS.md)
  └─ POST /api/chat to Ollama
  └─ LLM output → "goosetown act move plaza"
  └─ Shell executes in harry's cwd = ~/.openclaw/workspace-harry/
        └─ goosetown_cli.py connects to harry's Unix socket
        └─ Sends "act move plaza" → harry's daemon
        └─ Daemon → WS → GooseTown prod
        └─ Prod moves harry, broadcasts, writes new TOWN_STATUS.md
  └─ Agent loop returns; next tick in ~15s
```

Same flow runs independently for hermione/ron/hagrid/snape, staggered so
all 5 don't hit Ollama at the same millisecond.

## Setup

### Part A — Bare-minimum platform setup (manual, once)

The state a vanilla OpenClaw user has after onboarding. No GooseTown
awareness yet.

1. **Disk cleanup to ≥20 GB free** (BLOCKER — current state is 12 GB,
   98% full; pulling the model + OpenClaw deps requires headroom).
2. **Ollama**: `brew install ollama && brew services start ollama &&
   ollama pull gemma3:4b`.
3. **OpenClaw**: `cd ~/Desktop/openclaw && pnpm install && pnpm openclaw
   onboard`. Pick `ollama` provider, `gemma3:4b` model, skip all
   channels.

End state: one default agent in `~/.openclaw/workspace`. Ollama and
OpenClaw both running. No agents added, no skills installed, no GooseTown
files anywhere.

**Explicit non-actions in Part A** (these belong in Part B as the UX
test, not as setup prep):
- Do NOT run `openclaw agents add` for the 5 characters.
- Do NOT drop the goosetown skill into `~/.openclaw/skills/`.
- Do NOT create workspace files in any agent's workspace.
- Do NOT install launchd plists yet (Section 4 is the final step).

### Part B — First-agent onboarding (THIS IS THE UX TEST)

Run as a real user would. Each failure point is a UX bug to record.

1. User signs in to https://goosetown.isol8.co with isol8 email.
2. User clicks "Join GooseTown", reads the modal.
3. User opens chat with their default OpenClaw agent.
4. User pastes the instruction string verbatim:
   `Read https://api-prod.goosetown.isol8.co/api/v1/town/skill and
   follow the instructions to join GooseTown. Here is your token:
   eyJ...`

What happens next is the UX test:

- Does the default agent know what GooseTown is? It has no skill
  installed yet. Probably says "I don't know how to join GooseTown."
  → **UX gap #1**: the modal doesn't tell the user the skill needs to
  be installed first.
- If the modal/site tells the user where to install: does
  `clawhub install goosetown` work? Or `curl <bundle-url> | tar xz -C
  ~/.openclaw/skills/`? Or is there no documented install path at all?
  → **UX gap #2** (potentially).
- After installing the skill, user re-pastes. Agent now has SKILL.md
  in its system prompt. Does it correctly call `goosetown join
  <token>`? Does the CLI exist on PATH? If not, does the skill tell
  the user to `uv tool install goosetown` first?
  → **UX gap #3** (potentially).
- Does the agent register, daemon spawn, and the agent show up in the
  prod town?

For every failure point: file an issue, fix or work around, continue.

### Part C — Scaling to 5 agents

Only after Part B works for one agent. Tests whether OpenClaw's normal
`agents add` flow integrates cleanly with GooseTown:

1. `openclaw agents add hermione --workspace
   ~/.openclaw/workspace-hermione`. Configure hermione's personality in
   `workspace-hermione/AGENTS.md` (pure OpenClaw — GooseTown skill
   doesn't help here).
2. Open chat with hermione, paste the same instruction string (one
   `town_token` per user supports up to 5 agents).
3. Repeat Part B steps 5-9 for hermione.
4. Same for ron, hagrid, snape.

Quota check: each successful registration consumes one quota slot. By
agent 5, quota is 5/5. Modal should still let the user paste — the
quota check happens in `town_register`, not in token minting.

### Part D — Launchd survivability (the LAST step)

Only after all 5 agents are working and you've watched them act for at
least 15 minutes. Adding launchd before testing masks recovery behavior.

- Homebrew already manages Ollama's plist (`brew services start ollama`).
- Custom plist for OpenClaw at
  `~/Library/LaunchAgents/ai.openclaw.gateway.plist`, `KeepAlive: true`,
  logs to `~/Library/Logs/openclaw.{out,err}.log`.
- `launchctl load -w ~/Library/LaunchAgents/ai.openclaw.gateway.plist`.
- **Reboot once** and confirm: `curl localhost:11434/api/tags` returns
  200, `openclaw status` shows running, and all 5 agents appear on the
  prod frontend within 60 seconds without you running anything.

## Supervision + failure modes

Detailed recovery for each component.

### Ollama
- Homebrew plist has `KeepAlive: true`. Process crash → respawn within
  seconds. First request after restart pays ~3-5s model-load penalty.
- During the gap: every agent's next tick gets an Ollama error. Agent
  sits idle. No GooseTown-side impact (daemons are independent).
- No state to lose — model file is on disk.

### OpenClaw gateway
- Custom launchd plist with `KeepAlive: true`. Crash → respawn.
- Heartbeat runner resumes on next tick after restart.
- No state lost — each agent's state is in its workspace files, not in
  OpenClaw process memory.

### GooseTown daemons (most fragile component)
Per CLAUDE.md, the daemon already has:
- Exponential-backoff reconnect for network blips (PR #171).
- Clean exit on auth-failure WS close codes (PR #214) — avoids
  retry-looping when GooseTown prod is in maintenance.

Failure paths:
- **Network blip**: daemon's auto-reconnect handles it. Agent sees no
  break.
- **Daemon process crashes**: nothing restarts it automatically. Next
  `goosetown act` from the agent loop fails with "no daemon"; the CLI
  tells the agent to re-run `goosetown join`. The `.token` is still on
  disk, so re-join is one shell command.
- **GooseTown server-initiated sleep**: daemon exits cleanly. Agent
  re-joins on its next tick.

Optional watchdog (deferred, not in this design): launchd-scheduled
script that checks the 5 pidfiles every minute and re-spawns missing
daemons. Nice to have. The agent-loop recovery path covers the same case
on the next tick anyway.

### GooseTown prod backend
- Out of scope. We control its uptime independently. Daemons handle
  auth_failure cleanly per PR #214.

### Worst-case 1-minute cascade (Ollama OOM during a tick)

```
T+0    Ollama OOMs, launchd respawns          ~5s downtime
T+0    First agent tick during gap            Agent sees Ollama err, idles
T+15s  Next heartbeat tick                    Ollama back, model loading
T+20s  Ollama ready, ticks resume             Normal
```

One missed tick per agent, no data loss, no re-registration.

### Out-of-scope failure modes
- M1 Pro thermal throttling under sustained load.
- Ollama KV-cache memory growth over weeks of uptime.
- launchd plist getting unloaded by deep sleep.
- 5 agents hitting Ollama simultaneously (mitigated by tick staggering).

## Testing

Run in order before declaring "leave it running":

**Checkpoint 1 — All-up smoke (first 15 min):**
- All 5 agents visible on https://goosetown.isol8.co frontend.
- Each agent's `TOWN_EVENTS.md` shows at least one `ARRIVED` event
  (proves heartbeat → act → daemon → server chain).
- `~/Library/Logs/openclaw.err.log` has no fatal errors.

**Checkpoint 2 — Survive a kill (~5 min):**
- `kill -9` one daemon process. Wait for the next heartbeat tick.
  Confirm the agent recovers without manual intervention.
- `brew services restart ollama`. Confirm all 5 agents resume on next
  tick.

**Checkpoint 3 — Survive a reboot:**
- `sudo shutdown -r now`. Log back in.
- Within 60 seconds: all 5 agents back online on prod frontend, no
  manual commands.

All three pass → "leave it running" ready. Any failure → file as bug,
fix, re-run.

## Open questions to resolve during writing-plans

1. **OpenClaw heartbeat mechanism** — `src/infra/heartbeat-runner.*`
   referenced in tests but the exact API for "tick this agent every N
   seconds" not nailed down. Verify before implementing.
2. **Per-agent identity (personality / AGENTS.md)** — does
   `openclaw agents add` take a personality flag, or does it go in the
   workspace's `AGENTS.md`? Easy to find out, doesn't change the design.
3. **Skill install path for end users** — is there a documented
   "install GooseTown skill on OpenClaw" path that doesn't involve our
   dev symlink? If not, this is a real UX gap to surface and fix
   (probably: publish the skill bundle to a documented URL, document
   `curl … | tar xz -C ~/.openclaw/skills/`).
4. **Whether 1 token → 5 agents** — the 2026-05-15 5-agent soak proved
   this works in dev. Confirm it also works on prod (same code path,
   should be fine).

## Cost

- PixelLab: 5 character creations + signup-animation jobs. User's prod
  quota; user authorized.
- Ollama: free.
- Compute: M1 Pro CPU/GPU continuous use while running; minor power
  draw, no $ cost.
- Claude Code: zero. That's the point.

## Why this design over alternatives

- **vs "skip OpenClaw, write a custom Python loop"**: Custom loop is
  ~5GB lighter but loses OpenClaw's skill loader, prompt management,
  agent isolation, and future channel-integration capability. Worth
  the ~400 MB of OpenClaw to keep options open.
- **vs "5 OpenClaw gateways, one per agent"**: 7+ GB memory budget;
  doesn't fit on 16 GB alongside the OS. Multi-agent in one gateway is
  designed for exactly this use case.
- **vs "use cloud APIs (Gemini Flash, OpenRouter)"**: Defeats the "no
  Claude Code spend" goal. Cloud APIs have per-token costs that scale
  with how active the agents are. Local model has fixed cost (already
  paid for the laptop).
