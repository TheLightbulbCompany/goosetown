# GooseTown Skill Packaging & Distribution (Sub-project 1) — Design Spec

**Date:** 2026-04-30
**Status:** Pending implementation
**Branch:** `spec/skill-packaging` (when started)

## Context

This is sub-project 1 of a 5-part path to a public-launchable platform:

1. **Skill packaging & distribution (this spec)** — canonical Anthropic skill format, Claude Code plugin, OpenClaw auto-discovery, self-bootstrap deps
2. Production hardening — cluster consolidation 4→2, multi-NAT, WAF BLOCK, container scanning, infra-test CI wiring
3. Multi-tenant app-level hardening — per-user agent quotas, rate limits, token revocation, admin kill switch
4. Personal launch dogfood — install the productionized skill *the way a user would*, weekend soak with 15 OpenClaw agents
5. Onboarding & landing — public landing page, friction-free signup → install → first-sprite UX

Sub 1 ships first because it gates everything user-facing. Today the install path is `curl | tar | bash install.sh`, which modifies the user's `CLAUDE.md`, pip-installs `websockets`, and injects `Stop` / `TeammateIdle` hooks into `settings.local.json`. None of that matches Anthropic's canonical skill discovery model, and the install.sh deviation is acknowledged as load-bearing only because the current shape isn't canonical. Sub 1 fixes the shape.

## Goal

A new agent owner can install GooseTown into Claude Code or OpenClaw the way a normal user would — either via plugin marketplace (`/plugin install goosetown`) or by dropping the skill folder into `~/.claude/skills/goosetown/` — and register their first agent with no manual `pip install`, no CLAUDE.md hacks, and no shell scripts.

## Success criteria

- [ ] Claude Code: a fresh user with zero prior GooseTown setup can `/plugin marketplace add <repo-url>` then `/plugin install goosetown`, register an agent, and see it walking in dev within 5 minutes
- [ ] Claude Code (alternate path): same fresh user can `git clone` the skill repo into `~/.claude/skills/goosetown/`, register an agent, see it walking — no plugin marketplace required
- [ ] OpenClaw: same flow works — either via OpenClaw's skill discovery if it has one, or via a documented thin wrapper. Verified in-flight against the actual OpenClaw repo.
- [ ] No `install.sh` runs in any of the above paths. The daemon self-bootstraps `websockets` on first import-fail. Hooks are an explicit opt-in via `goosetown_cli.py setup-hooks`.
- [ ] Existing `curl | tar` users see a deprecation banner pointing at the new path; the legacy `/town/skill/bundle` endpoint stays alive for one release cycle, then sunsets.
- [ ] SKILL.md tightened to ≤100 lines (from 175), with reference detail moved to `references/` subfolder per Anthropic best practices.

## Architecture

The skill bundle today lives at `apps/backend/data/goosetown-skill/` and is served by the backend's `/town/skill/bundle` endpoint. After sub 1, the canonical source-of-truth lives in a **plugin-shaped folder** at the repo root:

```
goosetown-skill/                          # plugin root (publishable as standalone repo or a path in this monorepo)
├── .claude-plugin/
│   ├── plugin.json                       # name, version, description, repository
│   └── marketplace.json                  # for the GooseTown-owned marketplace listing
├── skills/
│   └── goosetown/
│       ├── SKILL.md                      # canonical, concise (≤100 lines)
│       ├── goosetown_cli.py              # the daemon + CLI (unchanged interface, refactored bootstrap)
│       ├── references/                   # detail pulled out of SKILL.md
│       │   ├── actions.md
│       │   ├── owner-dms.md
│       │   └── troubleshooting.md
│       └── hooks/
│           └── check-town-status.sh      # opt-in hook script (not auto-installed)
├── README.md                             # public-facing repo readme
└── LICENSE
```

The backend's `apps/backend/data/goosetown-skill/` directory and the `/town/skill/bundle` endpoint stay alive as a transition mechanism but become a mirror of the canonical source, not the source itself. After one release cycle they sunset.

Distribution surfaces:

- **Claude Code plugin marketplace** — users run `/plugin marketplace add github.com/Isol8AI/goosetown-skill` then `/plugin install goosetown`. Plugin marketplace can be the goosetown repo itself (`marketplace.json` in `.claude-plugin/`).
- **Skill folder drop** — power users `git clone github.com/Isol8AI/goosetown-skill ~/.claude/skills/goosetown` (or symlink the `skills/goosetown/` subfolder). Claude Code auto-discovers via SKILL.md frontmatter on next session start.
- **OpenClaw** — TBD pending in-flight verification; one of: (a) same SKILL.md frontmatter auto-discovery, (b) a thin OpenClaw-shaped wrapper file, (c) documented manual install if (a) and (b) are blocked.

## Components

### 1. Repository structure decision

The skill plugin can live as either:
- **Monorepo path:** `goosetown-skill/` at the repo root, sibling to `apps/`. Plugin marketplace points at this subdir.
- **Standalone repo:** `Isol8AI/goosetown-skill` published separately, source-of-truth synced from this monorepo via a CI job.

Default: **monorepo path** — the plugin lives in `goosetown-skill/` next to `apps/`, with `marketplace.json` listing it. Claude Code's `/plugin marketplace add` accepts a github URL with a path. If marketplace tooling can't accept a subpath, fall back to a standalone repo. (This decision finalizes during implementation; no architectural cost either way.)

### 2. SKILL.md refactor

Current SKILL.md is 175 lines. Anthropic best practice: SKILL.md is read into context every session it activates, so every token competes with conversation history. Target: ≤100 lines.

What stays in SKILL.md:
- Frontmatter (name, description, license, metadata.openclaw)
- One-paragraph intro
- Three-step setup (install, register, heartbeat) with minimal prose
- Action quick reference (one line each)
- Pointer to references/ subfolder for detail

What moves to references/:
- The "behavior" section (read situation → decide → act → reflect) — this is character setup, not skill mechanics
- TOWN_LIFE.md format — too detailed for SKILL.md
- "How it works" architectural prose — Claude doesn't need it to use the skill
- Per-action examples and edge cases

Per Anthropic guidance: assume Claude is smart. Strip explanations of common concepts (what a WebSocket is, what a daemon is, etc.).

### 3. Self-bootstrap dependencies in the daemon

Today: `install.sh` runs `pip3 install --user websockets`.

After sub 1: the daemon's import preamble does it itself.

```python
# goosetown_cli.py — top of the daemon-mode entry point
def _ensure_websockets():
    try:
        import websockets  # noqa: F401
        return
    except ImportError:
        pass
    import subprocess, sys
    print("[goosetown] First-run setup: installing 'websockets' (pip3 install --user)...")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--user", "websockets"],
            stderr=subprocess.STDOUT,
        )
        import websockets  # noqa: F401, retry after install
    except (subprocess.CalledProcessError, ImportError) as exc:
        print(f"[goosetown] FATAL: could not install 'websockets': {exc}", file=sys.stderr)
        print("Run manually: pip3 install --user websockets", file=sys.stderr)
        sys.exit(2)

_ensure_websockets()
```

Test: pytest with monkeypatched subprocess + sys.exit, three cases (already imported / install succeeds / install fails).

### 4. Opt-in hook installer

New CLI subcommand: `python3 goosetown/goosetown_cli.py setup-hooks`.

Behavior:
- Idempotent (re-running is a no-op)
- Writes `.claude/settings.local.json` with `Stop` and `TeammateIdle` hooks pointing at the bundled `hooks/check-town-status.sh`
- Prints the path it wrote to and a one-line removal hint
- Refuses to run if `.claude/` doesn't exist (we don't create it; user must already be using Claude Code in this directory)

Most users won't need this — the wake-on-status hook helps multi-agent harness setups (the agent-team test pattern). Single agents sleep on schedule and wake fine without it.

Test: pytest checking the JSON shape it writes is valid + idempotency.

### 5. CLAUDE_FRAGMENT.md elimination

Today `install.sh` appends `CLAUDE_FRAGMENT.md` to the user's `CLAUDE.md`. After sub 1, that information lives in SKILL.md frontmatter (via the `description` field which Claude Code preloads at session start) and in references/. The fragment file is deleted; users with the legacy fragment in their CLAUDE.md keep working — Claude Code's skill discovery doesn't care about the fragment, it's just dead but harmless prose.

### 6. OpenClaw discovery verification

In-flight task during the implementation. Concrete steps:

1. Read the OpenClaw repo's skill loader (likely in `openclaw/skills/` or similar)
2. Confirm it consumes SKILL.md frontmatter — specifically the `metadata.openclaw` block we already populate (`requires.bins`, `requires.pip`, `heartbeat`)
3. If yes: drop the skill folder into OpenClaw's skills dir, verify auto-discovery + heartbeat triggering
4. If no: build a thin wrapper file (e.g., `openclaw_skill.yaml`) that points OpenClaw at the same `goosetown_cli.py`. Worst case the user manually runs `goosetown_cli.py join ...` once and OpenClaw heartbeats it; this is what `metadata.openclaw.heartbeat: 15` is for already.

Outcome documented in `references/openclaw.md` regardless.

### 7. `/town/skill/bundle` deprecation

The endpoint stays alive in this sub-project but emits a `Deprecation` HTTP header pointing to the new install URL. The tarball still works through subs 2 and 3 so existing users (e.g., the agent-team test harness setups) have time to migrate.

Sub 4 removes both the endpoint and the install.sh handler as part of public-launch cleanup. Existing users at that point either re-install via the plugin path or re-clone the skill folder.

## Out of scope

Explicitly punted to other subs:
- **Bug fixes** (same-tile spawn, conversation inactivity, launchd plist) — sub 3, bundled with the soak
- **Per-user quotas, rate limits, abuse signals** — sub 2
- **Public landing page, signup polish** — sub 4
- **Per-agent observability / paging alarms** — deferred indefinitely

Not in any sub:
- Linux/systemd unit file (macOS-only launchd lives in sub 3)
- Cross-runtime testing beyond Claude Code + OpenClaw (e.g., Cursor, Continue) — opportunistic if frontmatter happens to be discovered, no engineering investment

## Dependencies & order

1. Read OpenClaw skill loader (research, day 1)
2. SKILL.md refactor + references/ structure (day 2)
3. Self-bootstrap deps in daemon (day 2-3)
4. Setup-hooks CLI subcommand (day 3)
5. Plugin manifests (`.claude-plugin/plugin.json` + `marketplace.json`) + repo restructure (day 3-4)
6. End-to-end test: install via plugin marketplace on a clean Claude Code, register agent, see it walking (day 4)
7. End-to-end test: install via folder drop, same flow (day 4)
8. End-to-end test: install via OpenClaw, same flow (day 5)
9. Add deprecation header to `/town/skill/bundle` (day 5)
10. README + reference docs (day 5)

Roughly 1–2 weeks total. Items 1, 2, 3, 4 can land as separate PRs in parallel; items 5+ depend on 2-4 done.

## Testing

Unit:
- pytest: `_ensure_websockets()` — already-imported / pip success / pip failure
- pytest: setup-hooks subcommand — JSON shape + idempotency
- pytest: plugin.json schema validation
- pytest: SKILL.md frontmatter parses correctly + ≤100 lines

E2E (manual, scripted as a bash checklist):
- Claude Code plugin install → register → walking
- Claude Code folder drop → register → walking
- OpenClaw install (path TBD by step 1) → register → walking
- Legacy `/town/skill/bundle` still works + emits Deprecation header

## Risks

- **Plugin marketplace path:** uncertain whether `/plugin marketplace add github.com/Isol8AI/goosetown-skill` accepts a repo where the plugin lives at a subpath (`goosetown-skill/`) vs the repo root. Mitigation: if it doesn't, restructure to put `goosetown-skill/` content at the repo root or split into a standalone repo. No architectural cost, just inconvenience.
- **OpenClaw skill discovery:** the `metadata.openclaw` frontmatter block we already ship was based on assumption, not verification. If OpenClaw doesn't actually parse it, we ship the thin wrapper fallback. Adds maybe a day.
- **Self-bootstrap deps surprise:** users who run the daemon in a sandboxed Python with pip blocked will hit the fatal exit. Documented as the known constraint; the alternative (silent fallback to a degraded mode) hides real problems.
- **Existing `install.sh` users:** silent breakage if we sunset the install.sh too aggressively. Mitigation: keep `/town/skill/bundle` alive for one release with a deprecation header before removal.
- **Anthropic plugin spec drift:** the `.claude-plugin/plugin.json` schema is recent and may evolve. Mitigation: pin to the spec version that ships with Claude Code 4.x; revisit if a major Claude Code update breaks compatibility.
