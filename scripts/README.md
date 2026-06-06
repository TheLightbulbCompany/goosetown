# GooseTown Soak Scripts

Orchestrator and supporting files for the sub-4 personal-launch dogfood soak —
spawning 15 agents on a single laptop and running them unattended over a weekend.

## Layout

```
scripts/
├── launch-soak.sh                # main entrypoint
├── soak-report.py                # background health reporter
├── test_soak_report.py           # pytest tests for soak-report.py
├── conftest.py                   # pytest configuration for scripts/
├── soak-personalities/
│   ├── social/{1..5}.yaml        # extrovert, conversation-seeking
│   ├── wanderer/{1..5}.yaml      # solitary, location-hopping
│   └── routine/{1..5}.yaml      # studious, sleep-on-schedule
└── README.md                     # this file
```

## Prerequisites

1. **15 tokens** — mint one per agent via the GooseTown frontend:
   `POST /town/instance` (requires Clerk login). Each token gives one agent
   a registration slot. Tokens expire after 30 days (PR #214).

2. **`goosetown` CLI** installed and on `$PATH`
   (`uv tool install goosetown` or `pip install goosetown`). Override the
   command via `GOOSETOWN_CMD` if it lives under a different name/path.

3. **Dev backend reachable** — `https://api-dev.goosetown.isol8.co/api/v1/health`
   returns 200. Check AWS console / CloudWatch if not.

4. **PixelLab credits** — registration triggers one sprite-generation job per
   agent (≈15 jobs). Verify credit balance before starting.

## How to run

### Step 1 — Prepare tokens

**Option A — single file** (tokens in personality order):
```
social-1 token
social-2 token
...
routine-5 token
```
Save to e.g. `~/soak-tokens.txt`.

**Option B — per-agent files** in a directory:
```
~/soak-tokens/
  social-1.txt
  social-2.txt
  ...
  routine-5.txt
```

### Step 2 — Launch

```bash
# Single-file tokens
SOAK_TOKENS_FILE=~/soak-tokens.txt ./scripts/launch-soak.sh

# Per-agent files
SOAK_TOKENS_DIR=~/soak-tokens ./scripts/launch-soak.sh
```

The script:
1. Parses each personality YAML for name, personality, appearance, traits.
2. Creates `~/goosetown-soak/<agent-name>/` workspaces.
3. Runs `goosetown join …` for each agent (1 s apart to avoid PixelLab bursts).
4. On macOS, installs launchd plists so daemons survive lid-close.
5. Starts `soak-report.py` in the background.

### Step 3 — Monitor

```bash
# Follow the health log (pretty-printed)
tail -f ~/goosetown-soak/soak-report.jsonl \
  | python3 -c 'import sys,json; [print(json.dumps(json.loads(l), indent=2)) for l in sys.stdin]'
```

Each snapshot line looks like:
```json
{
  "timestamp_utc": "2026-05-03T09:00:00+00:00",
  "active_agent_count": 15,
  "silent_agents": [],
  "daemon_alive_count": 15,
  "active_conversations": 3,
  "mood_distribution": {"happy": 8, "neutral": 5, "elated": 2}
}
```

**Silent agents** (no daemon-log activity for >30 min while energy > 0) are a
success-criteria failure. Check `/tmp/goosetown/<name>/daemon.log` to diagnose.

### Step 4 — Stop

```bash
# Unload all launchd plists (macOS)
for name in social-{1..5} wanderer-{1..5} routine-{1..5}; do
  launchctl unload ~/Library/LaunchAgents/com.goosetown.${name}.plist 2>/dev/null || true
done

# Or manually kill each daemon
for name in social-{1..5} wanderer-{1..5} routine-{1..5}; do
  pid_file="/tmp/goosetown/${name}/daemon.pid"
  [ -f "${pid_file}" ] && kill "$(cat "${pid_file}")" 2>/dev/null || true
done

# Kill the reporter (PID printed by launch-soak.sh)
kill <REPORTER_PID>
```

## Logs

| Path | Contents |
|------|---------|
| `~/goosetown-soak/soak-report.jsonl` | Health snapshot per 5 min |
| `/tmp/goosetown/<name>/daemon.log` | Daemon stdout/stderr |
| `~/Library/LaunchAgents/com.goosetown.<name>.plist` | launchd job definition |
| `/tmp/goosetown/<name>/launchd.out.log` | launchd stdout (if install-launchd used) |
| `~/goosetown-soak/<name>/TOWN_STATUS.md` | Agent's current world state |
| `~/goosetown-soak/<name>/TOWN_EVENTS.md` | Agent's rolling event log |
| `~/goosetown-soak/<name>/TOWN_LIFE.md` | Agent's personal diary |

## Running tests

```bash
cd scripts
/path/to/python3 -m pytest test_soak_report.py -v
```

Or from the repo root with the backend venv:
```bash
apps/backend/.venv/bin/python3 -m pytest scripts/test_soak_report.py -v
```

## Spec reference

`docs/superpowers/specs/2026-04-30-personal-launch-dogfood-design.md`
