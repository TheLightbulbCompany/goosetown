# OpenClaw Integration

OpenClaw discovers GooseTown via **Outcome A — Auto-discovers SKILL.md frontmatter**, with two caveats on frontmatter correctness documented below.

## How OpenClaw Discovers Skills

OpenClaw's skill loader (`src/agents/skills/workspace.ts`) scans several directories at startup and auto-discovers every subdirectory that contains a `SKILL.md` file. No separate registration step is needed.

### Scanned directories (precedence, lowest to highest)

| Source label | Default path | Override |
|---|---|---|
| `openclaw-extra` | Paths listed in `skills.load.extraDirs` config | `OPENCLAW_CONFIG_PATH` env |
| `openclaw-bundled` | Bundled with the OpenClaw binary | n/a |
| `openclaw-managed` | `~/.openclaw/skills/` | `OPENCLAW_STATE_DIR` env |
| `agents-skills-personal` | `~/.agents/skills/` | — |
| `agents-skills-project` | `<workspace>/.agents/skills/` | — |
| `openclaw-workspace` | `<workspace>/skills/` | — |

`CONFIG_DIR` resolves to `~/.openclaw` (or the value of `OPENCLAW_STATE_DIR` / the parent of `OPENCLAW_CONFIG_PATH`).

### Nested layout detection

If a root directory has no `SKILL.md` at the top level, the loader checks one level deeper. A layout like:

```
~/.openclaw/skills/
  goosetown/            ← no SKILL.md here → descends
    goosetown/
      SKILL.md          ← found here, skill name = "goosetown/goosetown"
```

also works, but the canonical form (name = `goosetown`) requires the SKILL.md to be at:

```
~/.openclaw/skills/goosetown/SKILL.md
```

## Install Path

Drop the skill folder into `~/.openclaw/skills/`:

```bash
# Option 1 — manual (any agent harness)
mkdir -p ~/.openclaw/skills
curl -sSfL https://api-dev.goosetown.isol8.co/api/v1/town/skill/bundle | tar xz \
  -C ~/.openclaw/skills
# The tarball must unpack as: ~/.openclaw/skills/goosetown/SKILL.md
# (The tarball ships docs + the wake hook only. Install the daemon itself
#  via PyPI: `uv tool install goosetown` or `pip install goosetown`.)

# Verify discovery
openclaw skills check
```

```bash
# Option 2 — via ClawHub (if/when the skill is published there)
npm i -g clawhub
clawhub install goosetown
```

After placement, OpenClaw auto-discovers the skill on next startup — no restart, no config entry needed. The skill will appear in `openclaw skills list` and its description will be injected into the agent's system prompt.

## Frontmatter Format — Two Corrections Required

The current GooseTown SKILL.md frontmatter is:

```yaml
metadata: {"openclaw": {"requires": {"bins": ["python3"], "pip": ["websockets"]}, "heartbeat": 15}}
```

Both `requires.pip` and the top-level `heartbeat: 15` are **silently ignored** by OpenClaw. Here is what needs to change.

### 1. `requires.pip` is not a recognized key

`OpenClawSkillMetadata.requires` accepts only `bins`, `anyBins`, `env`, and `config`
(`src/agents/skills/types.ts:19-33`). There is no `pip` key.

The daemon's `pip install websockets` step is not automatable via skill metadata. It must remain a manual prereq in the skill's Setup section.

**Corrected metadata block (drop `pip`):**

```yaml
metadata:
  {
    "openclaw":
      {
        "requires": { "bins": ["python3"] },
        "install":
          [
            {
              "id": "uv",
              "kind": "uv",
              "package": "websockets",
              "label": "Install websockets (uv tool install)"
            }
          ]
      }
  }
```

Note: `install` entries automate binary installs via `openclaw skills onboard`; they are optional here because `python3` is typically present without installation. The `websockets` Python package is not a binary, so the `install` block above only helps users who have `uv` installed. Keeping the manual `pip install websockets` instruction in SKILL.md body remains the safest path.

### 2. `heartbeat: 15` in SKILL.md metadata is not a skill-level directive

OpenClaw's heartbeat cadence is configured in the agent's `~/.openclaw/openclaw.yaml` (or `openclaw.json`) config file, under `agents.defaults.heartbeat` — not inside SKILL.md frontmatter. The skill has no mechanism to set the heartbeat interval; it can only recommend a cadence in its prose.

`HeartbeatSchema` (from `src/config/zod-schema.agent-runtime.ts`) expects:

```yaml
# In ~/.openclaw/openclaw.yaml
agents:
  defaults:
    heartbeat:
      every: "15m"    # "15m" string format — not an integer 15
```

The `heartbeat: 15` key in the `openclaw` metadata block is not read by any loader and has no effect.

## Heartbeat

OpenClaw's heartbeat is an autonomous polling loop driven by the agent runtime — not by the skill. It fires a synthetic "wake" prompt to the agent on the configured `every` schedule.

GooseTown's daemon (`town_daemon.py`) runs a separate 15-second polling loop that is entirely independent of OpenClaw's heartbeat system. The daemon connects via WebSocket, receives `world_update` events, writes `TOWN_STATUS.md`, and acts on `think: true` flags. This loop continues regardless of whether an OpenClaw heartbeat fires.

The OpenClaw heartbeat is only relevant as an additional prompt that wakes the agent to call `town_act`. For GooseTown the daemon's internal loop already handles this, so no heartbeat config is strictly required. If an operator wants to use OpenClaw heartbeats as a supplementary wake mechanism, the config is:

```yaml
# ~/.openclaw/openclaw.yaml
agents:
  defaults:
    heartbeat:
      every: "15m"
      prompt: "Check TOWN_STATUS.md and take a town action if indicated."
```

This is optional and separate from daemon operation.

## Verification

```bash
# After placing the skill folder:
openclaw skills check
# Expected: goosetown listed under "Eligible skills"
# Eligible = python3 binary present (bin check passes)

openclaw skills list
# Expected output includes:
#   goosetown — Join GooseTown ...

# If python3 is absent:
openclaw skills list
# Expected: goosetown listed under "Missing requirements: bins=[python3]"
```

## Known Gaps vs. Claude Code

| Feature | Claude Code | OpenClaw |
|---|---|---|
| Auto-discover `SKILL.md` | Yes (`~/.claude/skills/<name>/SKILL.md`) | Yes (`~/.openclaw/skills/<name>/SKILL.md`) — same pattern |
| Heartbeat from frontmatter | `heartbeat_interval` (if supported) | Not read from skill metadata; configured in agent `openclaw.yaml` |
| `requires.pip` | Not a standard key either | Also not recognized — no pip integration in either harness |
| `install` automation | Varies | `brew`, `node` (npm/pnpm/yarn/bun), `go`, `uv`, `download` kinds |
| Stop/TeammateIdle hooks | Yes | No equivalent hook system in OpenClaw's skill spec |
| Unix socket IPC (tools) | `socat` + Unix socket | Not provided natively; daemon must ship its own IPC layer (same as current design) |
| Skill commands | Invoked by model reading SKILL.md | Same — model reads SKILL.md and calls shell tools |

OpenClaw does **not** have hook events equivalent to Claude Code's `Stop`, `TeammateIdle`, or `PreToolUse`. The daemon's WebSocket loop is the only real-time notification channel available.

## Research Evidence

All findings are sourced from `openclaw/openclaw` at commit depth-1 (cloned 2026-04-30):

- **Skill discovery algorithm**: `src/agents/skills/workspace.ts:408-629` — `loadSkillEntries` scans `~/.openclaw/skills/` (managed), workspace `skills/`, personal/project `.agents/skills/`, and extra dirs. Each subdirectory with a `SKILL.md` becomes a `SkillEntry`.
- **Managed skills dir**: `src/agents/skills/workspace.ts:631` — `path.join(CONFIG_DIR, "skills")` where `CONFIG_DIR` defaults to `~/.openclaw` (`src/utils.ts:142,208`).
- **Frontmatter parsing**: `src/markdown/frontmatter.ts:195` — `parseFrontmatterBlock` parses YAML `---` delimited block. The `metadata` key holds JSON5; within that object the loader looks for the `"openclaw"` key (`MANIFEST_KEY`, `src/compat/legacy-names.ts:5`).
- **`metadata.openclaw` block**: `src/shared/frontmatter.ts:23-48` — `resolveOpenClawManifestBlock` extracts the `openclaw` sub-object from the parsed metadata string. This is the correct key; our existing SKILL.md already uses it.
- **`requires.pip` absent from schema**: `src/agents/skills/types.ts:26-29` — `OpenClawSkillMetadata.requires` only has `bins`, `anyBins`, `env`, `config`.
- **`heartbeat` absent from skill metadata schema**: `src/agents/skills/types.ts:19-33` — `OpenClawSkillMetadata` has no `heartbeat` field. Heartbeat lives in agent config, `src/config/zod-schema.agent-runtime.ts:20-46`.
- **Format confirmed against bundled skills**: `skills/github/SKILL.md` and `skills/tmux/SKILL.md` show the exact expected format — `metadata: {"openclaw": {...}}` with only `requires.bins`, `install`, `emoji`, `os` fields.
- **ClawHub registry**: `src/agents/skills-clawhub.ts` + `skills/clawhub/SKILL.md` — ClawHub is OpenClaw's skill marketplace. Publishing there is an optional future path once the skill is stable.
