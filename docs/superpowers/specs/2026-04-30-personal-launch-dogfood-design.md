# GooseTown Personal Launch Dogfood (Sub-project 4) — Design Spec

**Date:** 2026-04-30
**Status:** Pending implementation (blocked on subs 1 + 2 + 3)
**Branch:** `spec/personal-launch-dogfood` (when started)

## Context

This is sub-project 4 of a 5-part path to a public-launchable platform:

1. **Skill packaging & distribution** — canonical Anthropic skill format, Claude Code plugin, OpenClaw auto-discovery, self-bootstrap deps
2. **Production hardening** — cluster consolidation 4→2, multi-NAT, WAF BLOCK, container scanning, infra-test CI wiring
3. **Multi-tenant app-level hardening** — per-user agent quotas, rate limits, token revocation, admin kill switch
4. **Personal launch dogfood (this spec)** — install the productionized skill *the way a user would*, spawn 15 OpenClaw agents on the user's laptop, weekend soak. Bug fixes (same-tile spawn, conversation inactivity, launchd plist) bundle here as soak prep.
5. **Onboarding & landing** — public landing page, friction-free signup → install → first-sprite UX

Sub 4 is the validation gate before sub 5: if 15 of the user's agents can't survive a weekend on the productionized stack, no one else's will either. Running it AFTER subs 1+2+3 means the dogfood tests the actual user-facing install path on production-hardened infra, not a pre-productionization workflow that no real user will follow.

## Goal

The user can spawn 15 OpenClaw agents on his laptop, register them in dev GooseTown, and have them survive an unattended weekend (laptop awake, lid open) — daemons reconnecting cleanly, conversations completing without mid-flight close, owner DMs delivered, no manual rescue required.

## Success criteria

- [ ] 15 OpenClaw agents registered and walking around dev concurrently
- [ ] 24-hour soak: zero daemon crash-loops, zero agents stuck with `think=true` indefinitely, zero silent agents (silent = no `act` for >30 min while energy > 0)
- [ ] At least 5 multi-turn conversations between agents over the soak window, all reaching natural close (ended via `act end <conv_id>` by a participant, or hitting `_MAX_CONVERSATION_TURNS = 20`) — not auto-closed by the 180s inactivity timeout
- [ ] At least 3 owner-DM round-trips (owner → agent → reply) completing within the agent's heartbeat window
- [ ] Agents survive a manual lid-close → wait 30s → lid-open cycle without manual intervention (launchd auto-restart works)
- [ ] Token expiry alarm fires correctly when one of the 15 tokens is rolled

## Bug surface to address

### Same-tile spawn on arrival
- **Where:** `apps/godot/scripts/server/agent_controller.gd:spawn_agent`
- **Today:** when an agent moves to a location where another agent already stands, the game server places them on the exact same tile. Documented in CLAUDE.md Known Issues + Dwight's diary 2026-04-21.
- **Fix:** spiral outward from target tile, return first unoccupied within radius 3; fall back to exact tile if all neighbors taken.
- **Test:** gdunit test that spawns 3 agents at the same location marker and asserts no two share a tile.

### Conversation 60s inactivity timer too short for slow local models
- **Where:** `apps/backend/core/services/conversation_service.py:_INACTIVITY_TIMEOUT_SEC = 60.0`
- **Today:** local-model agents may take 30–90s per think cycle. A two-turn exchange easily exceeds 60s of silence between turns and gets auto-closed mid-flight.
- **Fix:** raise default to 180s; expose via `CONVERSATION_INACTIVITY_TIMEOUT_SEC` env var so we can tune later without redeploy.
- **Test:** add a pytest case that monkeypatches the env var and asserts the timer reads it at module load.

### Daemon auto-restart across crashes, network blips, and laptop sleep
- **Where:** new — `apps/backend/data/goosetown-skill/launchd/com.goosetown.daemon.plist.template` + helper subcommand
- **Today:** the daemon is started by `python3 goosetown_cli.py join ...` and runs as a child of the user's terminal. It dies on terminal close, on Python crash, on OpenClaw OOM, on extended network outages, and on laptop sleep. Nothing auto-restarts it. Even with the lid kept open, day-to-day flakiness (network blips, OpenClaw restarts, kernel panics) silently kills agents.
- **Fix:** ship a launchd plist template; add `goosetown_cli.py install-launchd <agent-name>` that materializes the plist at `~/Library/LaunchAgents/com.goosetown.<agent-name>.plist` with `KeepAlive: { Crashed: true, NetworkState: true }` and runs `launchctl load`. Also document `caffeinate -d` for users who prefer keeping the laptop awake without launchd.
- **Test:** pytest that the install-launchd subcommand writes a parseable plist with the correct agent name + workspace path. Manual: kill the daemon process and verify launchd respawns it within ~10s; lid-close cycle.

### Concurrent WS load at 15 agents
- **Where:** `apps/backend/core/services/game_server_client.py` + dev backend Fargate sizing
- **Today:** the 2026-04-21 dry run had ~4 agents. 15 agents = 15 concurrent backend WS connections + 15 game-server agent connections.
- **Fix:** verify dev backend autoscale (1→3 tasks per PR #211) handles 15 agents without ALB 503s. No code change expected; if soak shows degradation we patch reactively in this sub-project.
- **Test:** the soak itself is the test.

## Validation: 15-agent soak

Three personality archetypes, five copies each (15 total):
- **Social** (extrovert, social, energetic) — initiates conversations
- **Wanderer** (solitary, calm, observant) — moves between locations, mostly alone
- **Routine** (studious, introvert, creative) — sleeps + wakes on alarm

Each agent gets a `town_disconnect 22:00` directive nightly to exercise the sleep/wake path.

### What we measure (logged to `soak-report.jsonl` every 5 min)
- Per-agent daemon process count (any drop below 1 = silent)
- Active conversation count (does it grow or stay flat?)
- World-state mood distribution
- Token expiry alarm status (manually expire one mid-soak; verify it fires)
- AWS cost delta over the soak window (DDB read/write units, ECS CPU, Fargate hours)
- PixelLab credit consumption

### Soak orchestrator
New `scripts/launch-soak.sh`:
1. Reads 15 personality YAMLs from `scripts/soak-personalities/`
2. Mints 15 tokens via `POST /town/instance` (requires user's Clerk JWT, supplied via env)
3. For each, runs `python3 goosetown/goosetown_cli.py join ...` in `~/goosetown-soak/<agent>/`
4. Polls `/town/state` every 5 min, dumps to `soak-report.jsonl`
5. At end, prints summary table

## Out of scope

Already shipped by subs 1–3 (this spec consumes them):
- **Canonical Anthropic skill format + plugin distribution** (sub 1)
- **Self-bootstrap deps + cross-harness verification** (sub 1)
- **Cluster consolidation, multi-NAT, WAF BLOCK, container scanning** (sub 2)
- **Per-user quotas, rate limits, token revocation flows** (sub 3)

Punted to sub 5:
- **Public landing page, onboarding polish**

Deferred indefinitely (per user 2026-04-30):
- Per-agent observability / paging alarms — existing Phase 6 alarms (Fargate `RunningTaskCount<1`, NAT errors, ALB health, multi-NAT errors post-sub-2) are sufficient

## Architecture

No architectural changes — bug-fix + harness-add scope. The dual-map game server, sprite pipeline, daemon model, and conversation service stay as-is.

The one new component is the launchd plist template + `install-launchd` helper, which lives in the skill bundle. macOS-only; Linux/systemd users get docs but not a unit file.

## Components

### `agent_controller.gd` — same-tile spawn fix
```gdscript
static func find_unoccupied_tile(target: Vector2i, occupied: Array[Vector2i], search_radius: int = 3) -> Vector2i:
    if target not in occupied:
        return target
    # Spiral outward
    for r in range(1, search_radius + 1):
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                if abs(dx) != r and abs(dy) != r:
                    continue  # Only the ring at exactly radius r
                var candidate = target + Vector2i(dx, dy)
                if candidate not in occupied:
                    return candidate
    return target  # Fallback: stack on the exact tile
```

### `conversation_service.py` — inactivity timer override
```python
import os
_INACTIVITY_TIMEOUT_SEC: float = float(
    os.getenv("CONVERSATION_INACTIVITY_TIMEOUT_SEC", "180.0")
)
```
ECS task definition gains the env var (default unset → uses 180s default). Test verifies override via `monkeypatch.setenv` + module reload.

### `goosetown_cli.py` — `install-launchd` subcommand
- Reads agent name + workspace path from CLI args (or `GOOSETOWN.md` if cwd is a workspace)
- Materializes plist from embedded template
- Writes to `~/Library/LaunchAgents/com.goosetown.<agent-name>.plist`
- Runs `launchctl load` (and prints `launchctl unload` instructions for cleanup)
- Plist sets `RunAtLoad`, `KeepAlive: { Crashed: true, NetworkState: true }`, `StandardOutPath` and `StandardErrorPath` to `/tmp/goosetown/<agent>/launchd.{out,err}.log`

### `scripts/launch-soak.sh` + `scripts/soak-personalities/*.yaml`
Bash orchestrator + 15 personality YAMLs (`social-{1..5}.yaml`, `wanderer-{1..5}.yaml`, `routine-{1..5}.yaml`). Each YAML has the personality, appearance, traits per the existing register API.

## Dependencies & order

1. Same-tile spawn fix (Godot) — independent
2. Inactivity timer raise (backend) — independent
3. launchd plist + helper (skill) — depends on goosetown_cli.py edits only
4. Soak harness (scripts) — depends on 1–3 deployed to dev
5. Soak run + report — depends on 4

Items 1–3 can land in parallel as separate PRs. Soak run is manual after deploy.

## Testing

- gdunit: same-tile spawn (Godot)
- pytest: inactivity timer env override
- pytest: install-launchd plist generation (mock filesystem)
- Manual: launchd auto-restart on lid-close cycle
- Manual: 24-hour 15-agent soak in dev with success criteria checklist

## Risks

- **PixelLab credits:** 15 fresh registrations × character-creation cost. Verify credit balance before kicking off the soak.
- **Local model throughput:** the user has one machine. 15 OpenClaw agents × model inference may saturate the GPU/CPU. Mitigation: start the soak with 5 agents, ramp to 15 as performance allows; soak report includes per-agent action latency so we can detect saturation.
- **Existing dwight's tokens:** verify the user's previously minted tokens haven't already expired (30-day TTL per PR #214) before counting on them; if expired, mint fresh.
- **OpenClaw multi-agent process model:** the user said "one OpenClaw can host multiple agents." If OpenClaw uses async/threading to multiplex agents in a single Python process, our daemon's singleton globals may cause cross-talk. Mitigation: spawn one OpenClaw process per agent (15 processes total) — proven model. If this turns out to be untenable, escalate to sub 2.
