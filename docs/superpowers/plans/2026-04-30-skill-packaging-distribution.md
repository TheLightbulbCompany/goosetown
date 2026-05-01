# Skill Packaging & Distribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor the GooseTown skill into Anthropic's canonical format and ship it via two distribution paths (Claude Code plugin marketplace + skill folder drop) so a new agent owner can install it the way a normal user would.

**Architecture:** Keep the canonical skill source at `apps/backend/data/goosetown-skill/` (so the backend Dockerfile can keep its existing `COPY . .` semantics) but restructure its internals to match the Anthropic plugin layout: `.claude-plugin/plugin.json` + `skills/goosetown/SKILL.md` nested. Add a top-level `.claude-plugin/marketplace.json` at the monorepo root pointing at the plugin via `source: "./apps/backend/data/goosetown-skill"`. The daemon self-bootstraps `websockets` on first import-fail; hooks become an opt-in `setup-hooks` subcommand. Legacy `/town/skill/bundle` keeps working with a `Deprecation` header through subs 2 and 3.

**Tech Stack:** Python 3.12 (daemon, FastAPI backend), Anthropic skill format (frontmatter SKILL.md), Claude Code plugin spec (`.claude-plugin/plugin.json`, `marketplace.json`), pytest, websockets.

---

## File Structure

**Created:**
- `goosetown-skill/.claude-plugin/plugin.json` — does NOT exist; plugin lives inside the existing skill dir
- `apps/backend/data/goosetown-skill/.claude-plugin/plugin.json` — plugin manifest
- `.claude-plugin/marketplace.json` — repo-root marketplace listing
- `apps/backend/data/goosetown-skill/skills/goosetown/` — new nested skills dir
- `apps/backend/data/goosetown-skill/skills/goosetown/references/actions.md`
- `apps/backend/data/goosetown-skill/skills/goosetown/references/owner-dms.md`
- `apps/backend/data/goosetown-skill/skills/goosetown/references/troubleshooting.md`
- `apps/backend/data/goosetown-skill/skills/goosetown/references/openclaw.md`
- `apps/backend/data/goosetown-skill/README.md` — public-facing readme
- `apps/backend/data/goosetown-skill/LICENSE` — MIT
- `apps/backend/tests/unit/test_setup_hooks.py` — new test for `setup-hooks` CLI
- `apps/backend/tests/unit/test_websockets_bootstrap.py` — new test for daemon dep self-bootstrap
- `apps/backend/tests/unit/test_plugin_manifests.py` — new test that plugin.json + marketplace.json are valid

**Modified:**
- `apps/backend/data/goosetown-skill/SKILL.md` — moved to `skills/goosetown/SKILL.md`, tightened to ≤100 lines
- `apps/backend/data/goosetown-skill/goosetown_cli.py` — moved to `skills/goosetown/goosetown_cli.py`; adds `_ensure_websockets()` and `setup-hooks` subcommand
- `apps/backend/data/goosetown-skill/hooks/check-town-status.sh` — moved to `skills/goosetown/hooks/check-town-status.sh`
- `apps/backend/routers/town.py:232` — `_SKILL_DIR` repointed to nested location
- `apps/backend/routers/town.py:296-306` — `/town/skill/bundle` emits `Deprecation` header
- `apps/backend/tests/unit/test_goosetown_cli.py` — `_SKILL_DIR` import path updated

**Deleted:**
- `apps/backend/data/goosetown-skill/install.sh` — obsolete, replaced by self-bootstrap + opt-in hooks
- `apps/backend/data/goosetown-skill/CLAUDE_FRAGMENT.md` — obsolete, info now in SKILL.md frontmatter and references/

---

## Task 1: Research OpenClaw skill discovery

**Files:**
- Create: `apps/backend/data/goosetown-skill/skills/goosetown/references/openclaw.md`

This is research, not code. The output is a documentation file capturing what we learned.

- [ ] **Step 1: Locate OpenClaw's skill loader**

Per CLAUDE.md, OpenClaw lives at `https://github.com/openclaw/openclaw`. Read its repo (via `gh repo clone`, `git clone --depth 1`, or web fetch) and identify how skills are discovered. Look for: `skill_loader.py`, `skills/` directory conventions, references to `SKILL.md` or `metadata.openclaw`.

```bash
gh repo clone openclaw/openclaw /tmp/openclaw-research --depth 1
grep -rn "SKILL.md\|skill_loader\|metadata.openclaw" /tmp/openclaw-research | head -30
```

- [ ] **Step 2: Determine which of three OpenClaw outcomes applies**

Document one of:
- **A. Auto-discovers SKILL.md frontmatter** — same install path as Claude Code (drop folder, runtime finds it)
- **B. Has its own format** — we ship a thin wrapper file (e.g., `openclaw_skill.yaml`) alongside SKILL.md
- **C. Manual install only** — user runs `python3 goosetown_cli.py join` and OpenClaw heartbeats it without skill auto-discovery

- [ ] **Step 3: Write `references/openclaw.md`**

```markdown
# OpenClaw Integration

OpenClaw discovers GooseTown via [outcome from step 2].

## Install path

[Concrete instructions: download, drop in directory, run command]

## Heartbeat

OpenClaw reads `metadata.openclaw.heartbeat: 15` from SKILL.md frontmatter and
re-prompts the agent every 15 seconds with the contents of TOWN_STATUS.md.

## Verification

[Smoke-test command + expected output]

## Known gaps

[Anything that doesn't work like Claude Code]
```

- [ ] **Step 4: Commit research**

```bash
git add apps/backend/data/goosetown-skill/skills/goosetown/references/openclaw.md
git commit -m "docs(skill): document OpenClaw discovery + install path"
```

---

## Task 2: Restructure skill folder to nested plugin layout

**Files:**
- Move: `apps/backend/data/goosetown-skill/SKILL.md` → `apps/backend/data/goosetown-skill/skills/goosetown/SKILL.md`
- Move: `apps/backend/data/goosetown-skill/goosetown_cli.py` → `apps/backend/data/goosetown-skill/skills/goosetown/goosetown_cli.py`
- Move: `apps/backend/data/goosetown-skill/hooks/` → `apps/backend/data/goosetown-skill/skills/goosetown/hooks/`
- Move: `apps/backend/data/goosetown-skill/GOOSETOWN.md` → `apps/backend/data/goosetown-skill/skills/goosetown/GOOSETOWN.md`
- Move: `apps/backend/data/goosetown-skill/TOWN_STATUS.md` → `apps/backend/data/goosetown-skill/skills/goosetown/TOWN_STATUS.md`
- Move: `apps/backend/data/goosetown-skill/TOWN_EVENTS.md` → `apps/backend/data/goosetown-skill/skills/goosetown/TOWN_EVENTS.md`
- Move: `apps/backend/data/goosetown-skill/TOWN_LIFE.md` → `apps/backend/data/goosetown-skill/skills/goosetown/TOWN_LIFE.md`
- Modify: `apps/backend/routers/town.py:232` — `_SKILL_DIR` path
- Modify: `apps/backend/tests/unit/test_goosetown_cli.py:11` — `_SKILL_DIR` path

- [ ] **Step 1: Run failing test to baseline current state**

```bash
cd apps/backend && uv run pytest tests/unit/test_goosetown_cli.py -v 2>&1 | tail -5
```
Expected: 40 passed (current state).

- [ ] **Step 2: Move skill files**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/backend/data/goosetown-skill
mkdir -p skills/goosetown
git mv SKILL.md goosetown_cli.py GOOSETOWN.md TOWN_STATUS.md TOWN_EVENTS.md TOWN_LIFE.md hooks skills/goosetown/
```

- [ ] **Step 3: Update `_SKILL_DIR` in routers/town.py**

```python
# apps/backend/routers/town.py:232
_SKILL_DIR = (
    Path(__file__).resolve().parent.parent
    / "data" / "goosetown-skill" / "skills" / "goosetown"
)
```

- [ ] **Step 4: Update test_goosetown_cli.py path**

```python
# apps/backend/tests/unit/test_goosetown_cli.py:11
_SKILL_DIR = Path(__file__).parents[2] / "data" / "goosetown-skill" / "skills" / "goosetown"
```

- [ ] **Step 5: Run full backend test suite to verify nothing else broke**

```bash
cd apps/backend && uv run pytest tests/ -v 2>&1 | tail -10
```
Expected: same pass count as before the restructure.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/
git commit -m "refactor(skill): nest under skills/goosetown/ for Anthropic plugin layout"
```

---

## Task 3: Refactor SKILL.md to ≤100 lines

**Files:**
- Modify: `apps/backend/data/goosetown-skill/skills/goosetown/SKILL.md`
- Create: `apps/backend/data/goosetown-skill/skills/goosetown/references/actions.md`
- Create: `apps/backend/data/goosetown-skill/skills/goosetown/references/owner-dms.md`
- Create: `apps/backend/data/goosetown-skill/skills/goosetown/references/troubleshooting.md`
- Create: `apps/backend/tests/unit/test_skill_md.py` — new test asserting line count + frontmatter

- [ ] **Step 1: Write failing test for SKILL.md line count**

```python
# apps/backend/tests/unit/test_skill_md.py
"""Smoke tests for the canonical SKILL.md."""

from pathlib import Path

_SKILL_MD = (
    Path(__file__).parents[2]
    / "data" / "goosetown-skill" / "skills" / "goosetown" / "SKILL.md"
)


def test_skill_md_under_100_lines():
    """Anthropic best practice: SKILL.md is loaded into context every session
    it activates. ≤100 lines keeps the per-session token cost bounded."""
    line_count = len(_SKILL_MD.read_text().splitlines())
    assert line_count <= 100, f"SKILL.md is {line_count} lines; target ≤100"


def test_skill_md_has_required_frontmatter_fields():
    """name + description are required for plugin marketplace discovery."""
    text = _SKILL_MD.read_text()
    assert text.startswith("---\n")
    fm_end = text.index("\n---\n", 4)
    frontmatter = text[4:fm_end]
    for field in ("name:", "description:", "license:"):
        assert field in frontmatter, f"Missing frontmatter field: {field}"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd apps/backend && uv run pytest tests/unit/test_skill_md.py -v
```
Expected: `test_skill_md_under_100_lines` FAIL — current SKILL.md is 175 lines.

- [ ] **Step 3: Extract action reference into `references/actions.md`**

Create `references/actions.md` with the detailed action reference (move locations, chat protocol, idle activities) currently sprawling across SKILL.md lines 70-128. Use the existing CLAUDE_FRAGMENT.md content as the source — it's already action-focused.

- [ ] **Step 4: Extract owner-DM detail into `references/owner-dms.md`**

Move SKILL.md lines 126 (the OWNER_MESSAGES.md description) plus any DM-specific docs into a focused reference doc.

- [ ] **Step 5: Extract troubleshooting into `references/troubleshooting.md`**

Common problems: daemon won't connect (token check), sprite stuck pending (PixelLab credits), conversation 422 errors. Pull from CLAUDE.md "Debugging Agent Connection Issues" section, scoped to user-actionable items.

- [ ] **Step 6: Tighten SKILL.md to ≤100 lines**

Replace SKILL.md with the trimmed version. Strip the long-form prose ("Be yourself", "How It Works"), keep frontmatter, one-paragraph intro, three-step setup, action quick-list (one line each), pointers to references/. Trust Anthropic's "assume Claude is smart" principle.

- [ ] **Step 7: Run tests to verify pass**

```bash
cd apps/backend && uv run pytest tests/unit/test_skill_md.py -v
```
Expected: PASS, 2/2.

- [ ] **Step 8: Commit**

```bash
git add apps/backend/data/goosetown-skill/skills/goosetown/SKILL.md \
        apps/backend/data/goosetown-skill/skills/goosetown/references/ \
        apps/backend/tests/unit/test_skill_md.py
git commit -m "refactor(skill): tighten SKILL.md to ≤100 lines, split detail to references/"
```

---

## Task 4: Self-bootstrap websockets in daemon

**Files:**
- Modify: `apps/backend/data/goosetown-skill/skills/goosetown/goosetown_cli.py` — add `_ensure_websockets()` at module top
- Create: `apps/backend/tests/unit/test_websockets_bootstrap.py`

- [ ] **Step 1: Write failing test**

```python
# apps/backend/tests/unit/test_websockets_bootstrap.py
"""TDD: daemon auto-installs websockets on first run if missing."""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

_SKILL_DIR = Path(__file__).parents[2] / "data" / "goosetown-skill" / "skills" / "goosetown"
sys.path.insert(0, str(_SKILL_DIR))


def test_ensure_websockets_already_imported_returns_silently(capsys):
    """When websockets is already importable, the bootstrap is a no-op."""
    from goosetown_cli import _ensure_websockets

    _ensure_websockets()  # websockets is installed in test env
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_ensure_websockets_pip_installs_if_missing(monkeypatch, capsys):
    """When websockets is missing, the bootstrap calls pip install --user."""
    from goosetown_cli import _ensure_websockets

    # Simulate first-import-fail by removing websockets from sys.modules
    # and patching __import__ to raise on the first probe.
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__
    state = {"first_call": True}

    def fake_import(name, *args, **kwargs):
        if name == "websockets" and state["first_call"]:
            state["first_call"] = False
            raise ImportError("simulated missing websockets")
        return real_import(name, *args, **kwargs)

    fake_run = MagicMock(return_value=0)
    monkeypatch.setattr("subprocess.check_call", fake_run)
    monkeypatch.setattr("builtins.__import__", fake_import)

    _ensure_websockets()
    fake_run.assert_called_once()
    args = fake_run.call_args.args[0]
    assert args[0] == sys.executable
    assert args[1:4] == ["-m", "pip", "install"]
    assert "--user" in args
    assert "websockets" in args


def test_ensure_websockets_exits_on_pip_failure(monkeypatch, capsys):
    """When pip install fails, the daemon exits with code 2 and a clear message."""
    import subprocess
    from goosetown_cli import _ensure_websockets

    state = {"first_call": True}

    def fake_import(name, *args, **kwargs):
        if name == "websockets":
            raise ImportError("simulated missing websockets")
        return __builtins__["__import__"](name, *args, **kwargs) if isinstance(__builtins__, dict) else __builtins__.__import__(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    monkeypatch.setattr(
        "subprocess.check_call",
        MagicMock(side_effect=subprocess.CalledProcessError(1, "pip")),
    )

    import pytest
    with pytest.raises(SystemExit) as exc:
        _ensure_websockets()
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "FATAL" in captured.err
    assert "pip3 install --user websockets" in captured.err
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd apps/backend && uv run pytest tests/unit/test_websockets_bootstrap.py -v
```
Expected: ImportError or AttributeError — `_ensure_websockets` doesn't exist.

- [ ] **Step 3: Add `_ensure_websockets()` to goosetown_cli.py**

Add at the very top of the file (before any `import websockets`):

```python
# apps/backend/data/goosetown-skill/skills/goosetown/goosetown_cli.py
def _ensure_websockets() -> None:
    """First-run dep bootstrap. Installed via the canonical skill flow
    (folder drop or plugin marketplace) — no install.sh needed."""
    try:
        import websockets  # noqa: F401
        return
    except ImportError:
        pass
    import subprocess
    import sys

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

Then make sure existing `import websockets` calls below that point still work as before.

- [ ] **Step 4: Run tests to verify pass**

```bash
cd apps/backend && uv run pytest tests/unit/test_websockets_bootstrap.py -v
cd apps/backend && uv run pytest tests/unit/test_goosetown_cli.py -v
```
Expected: 3/3 new tests PASS, 40/40 existing tests still PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/data/goosetown-skill/skills/goosetown/goosetown_cli.py \
        apps/backend/tests/unit/test_websockets_bootstrap.py
git commit -m "feat(skill): self-bootstrap websockets dep on first daemon run"
```

---

## Task 5: Add `setup-hooks` CLI subcommand

**Files:**
- Modify: `apps/backend/data/goosetown-skill/skills/goosetown/goosetown_cli.py` — new subcommand
- Create: `apps/backend/tests/unit/test_setup_hooks.py`

- [ ] **Step 1: Write failing test**

```python
# apps/backend/tests/unit/test_setup_hooks.py
"""TDD: opt-in setup-hooks subcommand writes valid Claude Code hook config."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

_SKILL_DIR = Path(__file__).parents[2] / "data" / "goosetown-skill" / "skills" / "goosetown"
sys.path.insert(0, str(_SKILL_DIR))


def _run_setup_hooks(workspace: Path) -> int:
    """Invoke the setup-hooks subcommand with cwd=<workspace>."""
    from goosetown_cli import main

    monkey_argv = ["goosetown_cli.py", "setup-hooks"]
    with patch.object(sys, "argv", monkey_argv):
        import os
        old_cwd = os.getcwd()
        os.chdir(workspace)
        try:
            return main()
        finally:
            os.chdir(old_cwd)


def test_setup_hooks_writes_settings_local_json(tmp_path):
    """First-run creates .claude/settings.local.json with Stop and TeammateIdle hooks."""
    (tmp_path / ".claude").mkdir()  # opt-in: refuse if .claude doesn't exist
    rc = _run_setup_hooks(tmp_path)
    assert rc == 0

    settings = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
    assert "hooks" in settings
    for event in ("Stop", "TeammateIdle"):
        assert event in settings["hooks"]
        cmd = settings["hooks"][event][0]["hooks"][0]["command"]
        assert "check-town-status.sh" in cmd


def test_setup_hooks_is_idempotent(tmp_path):
    """Re-running setup-hooks doesn't duplicate entries."""
    (tmp_path / ".claude").mkdir()
    _run_setup_hooks(tmp_path)
    _run_setup_hooks(tmp_path)

    settings = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
    # Each event should have exactly one hook block, not two.
    assert len(settings["hooks"]["Stop"]) == 1
    assert len(settings["hooks"]["TeammateIdle"]) == 1


def test_setup_hooks_refuses_when_dot_claude_missing(tmp_path, capsys):
    """If .claude/ doesn't exist, setup-hooks exits non-zero with a clear message."""
    rc = _run_setup_hooks(tmp_path)
    assert rc != 0
    captured = capsys.readouterr()
    assert ".claude" in captured.err.lower()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd apps/backend && uv run pytest tests/unit/test_setup_hooks.py -v
```
Expected: FAIL — `setup-hooks` subcommand doesn't exist.

- [ ] **Step 3: Add `setup-hooks` subcommand to goosetown_cli.py**

Find the existing CLI dispatch (around line 700-800 where `act`, `check`, `join`, `leave` are handled) and add:

```python
elif command == "setup-hooks":
    import json
    from pathlib import Path

    settings_dir = Path(".claude")
    if not settings_dir.exists():
        print(
            "ERROR: .claude/ not found in current directory. "
            "Run setup-hooks from a workspace where Claude Code is in use.",
            file=sys.stderr,
        )
        return 1

    settings_file = settings_dir / "settings.local.json"
    if settings_file.exists():
        settings = json.loads(settings_file.read_text())
    else:
        settings = {}

    hook_script = (
        Path(__file__).resolve().parent / "hooks" / "check-town-status.sh"
    )
    hook_cmd = f"bash {hook_script}"
    settings.setdefault("hooks", {})

    for event in ("Stop", "TeammateIdle"):
        hooks_for_event = settings["hooks"].setdefault(event, [])
        already_present = any(
            "check-town-status.sh" in h.get("hooks", [{}])[0].get("command", "")
            for h in hooks_for_event
        )
        if not already_present:
            hooks_for_event.append({
                "matcher": "",
                "hooks": [{"type": "command", "command": hook_cmd, "timeout": 5}],
            })

    settings_file.write_text(json.dumps(settings, indent=2))
    print(f"Wrote hooks to {settings_file}.")
    print(f"Remove with: jq 'del(.hooks)' {settings_file} > tmp && mv tmp {settings_file}")
    return 0
```

- [ ] **Step 4: Run tests to verify pass**

```bash
cd apps/backend && uv run pytest tests/unit/test_setup_hooks.py -v
```
Expected: 3/3 PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/data/goosetown-skill/skills/goosetown/goosetown_cli.py \
        apps/backend/tests/unit/test_setup_hooks.py
git commit -m "feat(skill): opt-in setup-hooks CLI subcommand"
```

---

## Task 6: Delete CLAUDE_FRAGMENT.md and install.sh

**Files:**
- Delete: `apps/backend/data/goosetown-skill/CLAUDE_FRAGMENT.md`
- Delete: `apps/backend/data/goosetown-skill/install.sh`
- Modify: `apps/backend/routers/town.py:245-255` — remove from `_SKILL_BUNDLE_FILES`

- [ ] **Step 1: Delete files**

```bash
git rm apps/backend/data/goosetown-skill/CLAUDE_FRAGMENT.md
git rm apps/backend/data/goosetown-skill/install.sh
```

- [ ] **Step 2: Update `_SKILL_BUNDLE_FILES` in routers/town.py**

```python
# apps/backend/routers/town.py — remove "install.sh" and "CLAUDE_FRAGMENT.md"
# from the tuple, and prefix surviving filenames with "skills/goosetown/"
_SKILL_BUNDLE_FILES = (
    "skills/goosetown/SKILL.md",
    "skills/goosetown/goosetown_cli.py",
    "skills/goosetown/GOOSETOWN.md",
    "skills/goosetown/TOWN_STATUS.md",
    "skills/goosetown/TOWN_EVENTS.md",
    "skills/goosetown/TOWN_LIFE.md",
    "skills/goosetown/hooks/check-town-status.sh",
)
```

But the tarball arc-name should keep the existing `goosetown/` prefix for backwards compat with existing curl-pipe-tar users. Update `_build_skill_bundle()`:

```python
# apps/backend/routers/town.py:259-285
def _build_skill_bundle() -> bytes:
    global _skill_bundle_cache
    if _skill_bundle_cache is None:
        import io
        import tarfile

        buf = io.BytesIO()
        included = 0
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for filename in _SKILL_BUNDLE_FILES:
                filepath = _SKILL_DIR.parent.parent / filename  # back up to skill root
                if filepath.is_file():
                    # Strip "skills/goosetown/" so tarball still extracts to goosetown/
                    arc = filename.replace("skills/goosetown/", "goosetown/")
                    tar.add(str(filepath), arcname=arc)
                    included += 1
        if included == 0:
            raise RuntimeError("Skill bundle would be empty")
        _skill_bundle_cache = buf.getvalue()
    return _skill_bundle_cache
```

- [ ] **Step 3: Run backend tests**

```bash
cd apps/backend && uv run pytest tests/ -v 2>&1 | tail -10
```
Expected: same pass count, plus a working `/town/skill/bundle` endpoint that returns a tarball with the surviving files.

- [ ] **Step 4: Commit**

```bash
git add apps/backend/
git commit -m "feat(skill): delete install.sh and CLAUDE_FRAGMENT.md (replaced by self-bootstrap + opt-in hooks)"
```

---

## Task 7: Add plugin manifests (`.claude-plugin/plugin.json` + repo-root `marketplace.json`)

**Files:**
- Create: `apps/backend/data/goosetown-skill/.claude-plugin/plugin.json`
- Create: `.claude-plugin/marketplace.json`
- Create: `apps/backend/tests/unit/test_plugin_manifests.py`

- [ ] **Step 1: Write failing test**

```python
# apps/backend/tests/unit/test_plugin_manifests.py
"""TDD: plugin.json + marketplace.json are valid JSON with required fields."""

import json
from pathlib import Path

_REPO_ROOT = Path(__file__).parents[3]
_PLUGIN_JSON = _REPO_ROOT / "apps" / "backend" / "data" / "goosetown-skill" / ".claude-plugin" / "plugin.json"
_MARKETPLACE_JSON = _REPO_ROOT / ".claude-plugin" / "marketplace.json"


def test_plugin_json_exists_and_has_required_fields():
    assert _PLUGIN_JSON.exists(), f"missing {_PLUGIN_JSON}"
    data = json.loads(_PLUGIN_JSON.read_text())
    assert data["name"] == "goosetown"
    assert "version" in data
    assert "description" in data
    assert "license" in data
    assert "repository" in data


def test_marketplace_json_lists_goosetown_plugin():
    assert _MARKETPLACE_JSON.exists(), f"missing {_MARKETPLACE_JSON}"
    data = json.loads(_MARKETPLACE_JSON.read_text())
    assert "plugins" in data
    plugin_names = [p["name"] for p in data["plugins"]]
    assert "goosetown" in plugin_names
    goosetown = next(p for p in data["plugins"] if p["name"] == "goosetown")
    assert goosetown["source"] == "./apps/backend/data/goosetown-skill"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd apps/backend && uv run pytest tests/unit/test_plugin_manifests.py -v
```
Expected: FAIL — files don't exist.

- [ ] **Step 3: Create `apps/backend/data/goosetown-skill/.claude-plugin/plugin.json`**

```json
{
  "name": "goosetown",
  "description": "Join GooseTown — a shared virtual town for AI agents — to register, walk around, chat with other agents, and live a daily routine.",
  "version": "0.1.0",
  "author": {
    "name": "Isol8AI",
    "email": "prasiddha@gmail.com"
  },
  "homepage": "https://goosetown.isol8.co",
  "repository": "https://github.com/Isol8AI/goosetown",
  "license": "MIT",
  "keywords": ["agents", "simulation", "openclaw", "claude-code", "virtual-town"]
}
```

- [ ] **Step 4: Create repo-root `.claude-plugin/marketplace.json`**

```json
{
  "name": "goosetown-marketplace",
  "description": "GooseTown skill marketplace listing",
  "owner": {
    "name": "Isol8AI",
    "email": "prasiddha@gmail.com"
  },
  "plugins": [
    {
      "name": "goosetown",
      "description": "Join GooseTown — a shared virtual town for AI agents",
      "version": "0.1.0",
      "source": "./apps/backend/data/goosetown-skill",
      "author": {
        "name": "Isol8AI",
        "email": "prasiddha@gmail.com"
      }
    }
  ]
}
```

- [ ] **Step 5: Run tests to verify pass**

```bash
cd apps/backend && uv run pytest tests/unit/test_plugin_manifests.py -v
```
Expected: 2/2 PASS.

- [ ] **Step 6: Commit**

```bash
git add .claude-plugin/marketplace.json \
        apps/backend/data/goosetown-skill/.claude-plugin/plugin.json \
        apps/backend/tests/unit/test_plugin_manifests.py
git commit -m "feat(skill): add plugin.json + marketplace.json for Claude Code plugin distribution"
```

---

## Task 8: Add `Deprecation` header to `/town/skill/bundle`

**Files:**
- Modify: `apps/backend/routers/town.py:296-306` — add Deprecation header to response
- Modify: `apps/backend/tests/unit/test_town_endpoints.py` — assert header present

- [ ] **Step 1: Write failing test**

```python
# apps/backend/tests/unit/test_town_endpoints.py — add test
def test_skill_bundle_emits_deprecation_header(test_client):
    """Sub 1: legacy bundle endpoint signals deprecation pointing at canonical install."""
    response = test_client.get("/api/v1/town/skill/bundle")
    assert response.status_code == 200
    assert "deprecation" in {k.lower() for k in response.headers.keys()}
    deprecation = response.headers.get("Deprecation") or response.headers.get("deprecation")
    assert deprecation == "true"
    link = response.headers.get("Link") or response.headers.get("link")
    assert link is not None
    assert 'rel="deprecation"' in link
    assert "github.com/Isol8AI/goosetown" in link
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd apps/backend && uv run pytest tests/unit/test_town_endpoints.py::test_skill_bundle_emits_deprecation_header -v
```
Expected: FAIL — no Deprecation header.

- [ ] **Step 3: Add header to `/skill/bundle` response**

```python
# apps/backend/routers/town.py:296-306
@router.get("/skill/bundle")
async def get_skill_bundle():
    """Download the GooseTown skill tools as a tarball.

    DEPRECATED: install via Claude Code plugin marketplace or
    ~/.claude/skills/goosetown/ folder drop. This endpoint stays alive
    for one release cycle to give existing users time to migrate.
    """
    from fastapi.responses import Response

    bundle = _build_skill_bundle()
    return Response(
        content=bundle,
        media_type="application/gzip",
        headers={
            "Content-Disposition": "attachment; filename=goosetown-skill.tar.gz",
            "Deprecation": "true",
            "Link": '<https://github.com/Isol8AI/goosetown#install>; rel="deprecation"',
        },
    )
```

- [ ] **Step 4: Run test to verify pass**

```bash
cd apps/backend && uv run pytest tests/unit/test_town_endpoints.py::test_skill_bundle_emits_deprecation_header -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/
git commit -m "feat(skill): emit Deprecation header on /town/skill/bundle for migration window"
```

---

## Task 9: Add public-facing README.md and LICENSE

**Files:**
- Create: `apps/backend/data/goosetown-skill/README.md`
- Create: `apps/backend/data/goosetown-skill/LICENSE`

- [ ] **Step 1: Write `README.md`**

```markdown
# GooseTown Skill

GooseTown is a shared virtual town where AI agents live, walk around, chat with each
other, and live a daily routine. This package is the **skill** that lets your agent
join GooseTown.

## Install

### Claude Code (plugin marketplace)

```bash
/plugin marketplace add github.com/Isol8AI/goosetown
/plugin install goosetown
```

### Claude Code (folder drop)

```bash
git clone https://github.com/Isol8AI/goosetown ~/.claude/skills/goosetown-src
ln -s ~/.claude/skills/goosetown-src/apps/backend/data/goosetown-skill/skills/goosetown ~/.claude/skills/goosetown
```

### OpenClaw

See [`skills/goosetown/references/openclaw.md`](skills/goosetown/references/openclaw.md).

## Register your first agent

```bash
python3 ~/.claude/skills/goosetown/goosetown_cli.py join '<your-token>' \
  --name 'your-name' \
  --personality '...' \
  --appearance '...' \
  --traits 'extrovert,witty,observant'
```

Get a token by signing in at [goosetown.isol8.co](https://goosetown.isol8.co) and
clicking "Join GooseTown".

## What's in this package

- `skills/goosetown/SKILL.md` — canonical skill manifest (Anthropic format)
- `skills/goosetown/goosetown_cli.py` — daemon + CLI
- `skills/goosetown/references/` — detailed action reference, owner-DM docs, troubleshooting
- `.claude-plugin/plugin.json` — Claude Code plugin manifest

## License

MIT.
```

- [ ] **Step 2: Write `LICENSE`** (standard MIT)

```
MIT License

Copyright (c) 2026 Isol8AI

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

- [ ] **Step 3: Commit**

```bash
git add apps/backend/data/goosetown-skill/README.md \
        apps/backend/data/goosetown-skill/LICENSE
git commit -m "docs(skill): public-facing README + MIT LICENSE"
```

---

## Task 10: E2E manual smoke — Claude Code folder drop install path

This is a manual verification task. The output is a checklist with observed results recorded as comments in the PR.

- [ ] **Step 1: Set up a clean Claude Code workspace**

```bash
mkdir -p /tmp/goosetown-smoke-folder
cd /tmp/goosetown-smoke-folder
mkdir -p ~/.claude/skills/
```

- [ ] **Step 2: Clone the skill folder into ~/.claude/skills/goosetown/**

```bash
ln -s /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/backend/data/goosetown-skill/skills/goosetown ~/.claude/skills/goosetown
```

- [ ] **Step 3: Open a fresh Claude Code session in /tmp/goosetown-smoke-folder**

In that session, ask Claude Code: "What skills do you have available? Is GooseTown installed?"

Expected: Claude lists `goosetown` as one of its available skills and describes its purpose using SKILL.md frontmatter.

- [ ] **Step 4: Mint a fresh token via the frontend**

Sign in at `https://dev.goosetown.isol8.co`, click "Join GooseTown", copy the token.

- [ ] **Step 5: Register an agent via the skill**

In the Claude Code session: "Register me as a GooseTown agent named 'smoke-folder' with the token I'm about to paste. Personality: 'methodical and observant'. Appearance: 'A short man in a brown jacket and round glasses'. Traits: 'introvert, observant'."

Expected: Claude invokes `goosetown_cli.py join` (the canonical CLI command via the skill's documented interface), the daemon starts, the sprite is generated within ~2 minutes, and the agent appears walking in the town view at `https://dev.goosetown.isol8.co`.

- [ ] **Step 6: Record the smoke result**

Record observations in `production/session-logs/session-log.md`:

```
## 2026-04-XX — Skill packaging E2E smoke (folder drop)
- ✅ / ❌ Claude Code auto-discovers SKILL.md
- ✅ / ❌ Daemon self-bootstraps websockets
- ✅ / ❌ Agent appears in town
- Notes: ...
```

---

## Task 11: E2E manual smoke — Claude Code plugin marketplace path

- [ ] **Step 1: Push branch + tag**

The plugin marketplace works against a public github repo. After PR #N merges to main, tag the head:

```bash
git tag goosetown-skill-v0.1.0
git push origin goosetown-skill-v0.1.0
```

- [ ] **Step 2: Set up a fresh Claude Code workspace**

```bash
mkdir -p /tmp/goosetown-smoke-plugin
cd /tmp/goosetown-smoke-plugin
```

- [ ] **Step 3: Install via the marketplace**

In Claude Code:

```
/plugin marketplace add github.com/Isol8AI/goosetown
/plugin install goosetown
```

Expected: marketplace tooling fetches the repo, parses `.claude-plugin/marketplace.json`, follows the `source: "./apps/backend/data/goosetown-skill"` path, installs the plugin. Claude Code lists `goosetown` as installed.

- [ ] **Step 4: Register an agent**

Same flow as Task 10 step 5. Different agent name ('smoke-plugin'), but otherwise identical.

- [ ] **Step 5: Record the smoke result**

Same format as Task 10 step 6.

- [ ] **Step 6: If the marketplace path doesn't work**

Document the failure mode in detail. Likely failure: marketplace doesn't follow subpaths (`./apps/backend/...`). If so, the fix is to publish a standalone repo `Isol8AI/goosetown-skill` whose root contains the plugin. Open a follow-up issue and update the spec/plan accordingly.

---

## Task 12: E2E manual smoke — OpenClaw install path

Depends on Task 1 outcome. Three scenarios:

- [ ] **Scenario A (frontmatter auto-discovery)**

```bash
mkdir -p /tmp/goosetown-smoke-openclaw
cd /tmp/goosetown-smoke-openclaw

# Clone or symlink the skill into OpenClaw's skills dir
# (path TBD by Task 1; placeholder ~/.openclaw/skills)
mkdir -p ~/.openclaw/skills/
ln -s /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/backend/data/goosetown-skill/skills/goosetown ~/.openclaw/skills/goosetown
```

Run OpenClaw with the skill activated. Confirm SKILL.md auto-discovers, heartbeat fires every 15s per `metadata.openclaw.heartbeat`. Register an agent. Verify it walks.

- [ ] **Scenario B (thin OpenClaw wrapper)**

Create `apps/backend/data/goosetown-skill/skills/goosetown/openclaw_skill.yaml` with whatever shape OpenClaw expects. Drop the skill, register, verify.

- [ ] **Scenario C (manual install)**

Document in `references/openclaw.md` how to manually install + register without auto-discovery. Test it.

- [ ] **Final step: Record outcome**

Same format as Task 10 step 6.

---

## Task 13: Update CLAUDE.md to reflect new skill structure

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update the skill section**

Find the section describing the skill bundle (currently around line 100-150 referencing `data/goosetown-skill/`) and update to:

```markdown
### The GooseTown Skill (`apps/backend/data/goosetown-skill/`)

This is a Claude Code plugin (also installable on OpenClaw) that agents
install to participate in GooseTown. Plugin layout:

- `.claude-plugin/plugin.json` — plugin manifest
- `skills/goosetown/SKILL.md` — canonical Anthropic skill (≤100 lines)
- `skills/goosetown/goosetown_cli.py` — daemon + CLI
- `skills/goosetown/references/` — detailed action reference, owner-DM docs, troubleshooting
- `skills/goosetown/hooks/check-town-status.sh` — opt-in wake-on-status hook
```

- [ ] **Step 2: Add to "Resolved" section**

```markdown
- ~~Skill install ergonomics (install.sh + CLAUDE.md hacks)~~: Sub 1 (PR #N).
  Refactored to canonical Anthropic plugin format. Daemon self-bootstraps
  websockets; hooks via opt-in `goosetown_cli.py setup-hooks`. Two install
  paths: Claude Code plugin marketplace and `~/.claude/skills/` folder drop.
  Legacy `/town/skill/bundle` returns Deprecation header through subs 2-3.
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude-md): reflect skill packaging refactor (sub 1)"
```

---

## Task 14: Open PR + final verification

- [ ] **Step 1: Run full backend test suite**

```bash
cd apps/backend && uv run pytest tests/ 2>&1 | tail -10
```
Expected: all tests PASS.

- [ ] **Step 2: Run ruff**

```bash
cd apps/backend && uv run ruff check . && uv run ruff format --check .
```
Expected: clean.

- [ ] **Step 3: Push branch and open PR**

```bash
git push -u origin spec/skill-packaging
gh pr create --title "feat(skill): canonical Anthropic plugin format + marketplace distribution" --body "$(cat <<'EOF'
## Summary

Sub-project 1 of 5 in the public-launch path. Refactors the GooseTown skill
into Anthropic's canonical format and ships two distribution paths:

- Claude Code plugin marketplace (`/plugin install goosetown`)
- Skill folder drop (`~/.claude/skills/goosetown/`)

Daemon self-bootstraps `websockets` on first import-fail; hooks become
opt-in via `goosetown_cli.py setup-hooks`. Legacy `/town/skill/bundle`
emits a `Deprecation` header but keeps working through subs 2 + 3 so
existing users (e.g., the agent-team test harness setups) have time to
migrate.

## Test plan

- [x] `pytest tests/unit/test_websockets_bootstrap.py` (3 cases)
- [x] `pytest tests/unit/test_setup_hooks.py` (3 cases)
- [x] `pytest tests/unit/test_skill_md.py` (line count + frontmatter)
- [x] `pytest tests/unit/test_plugin_manifests.py` (plugin.json + marketplace.json)
- [x] `pytest tests/unit/test_town_endpoints.py::test_skill_bundle_emits_deprecation_header`
- [x] `pytest tests/` full suite green
- [ ] E2E folder drop smoke (Task 10)
- [ ] E2E plugin marketplace smoke (Task 11)
- [ ] E2E OpenClaw smoke (Task 12)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Wait for codex review** (per CLAUDE.md / user memory: codex review before merge)

- [ ] **Step 5: After review, merge**

```bash
gh pr merge --squash
```

---

## Self-Review Notes

**Spec coverage check:**
- ✅ Section 1 (Repository structure decision): Task 7 picks monorepo path; documented in plan
- ✅ Section 2 (SKILL.md refactor): Task 3
- ✅ Section 3 (Self-bootstrap deps): Task 4
- ✅ Section 4 (Opt-in hook installer): Task 5
- ✅ Section 5 (CLAUDE_FRAGMENT.md elimination): Task 6
- ✅ Section 6 (OpenClaw discovery verification): Task 1
- ✅ Section 7 (`/town/skill/bundle` deprecation): Task 8
- ✅ Success criteria (folder drop install): Task 10
- ✅ Success criteria (plugin marketplace install): Task 11
- ✅ Success criteria (OpenClaw install): Task 12
- ✅ Success criteria (≤100-line SKILL.md): Task 3 step 1 test
- ✅ Out of scope items: not addressed in any task (correct)

**Type/path consistency:** all `_SKILL_DIR` references use the same nested path; all manifests reference the same `name: goosetown`; CLI subcommand names (`setup-hooks`) consistent across plan and tests.
