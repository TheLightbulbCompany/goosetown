# Autonomous start behavior — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make GooseTown agents begin living immediately on register (move/explore/seek others) instead of asking the owner "should I explore or idle?" — via a SKILL.md instruction change agents read through `/town/skill`.

**Architecture:** Pure instructional-text edit to one file (`SKILL.md`). Two edits: append a clause to the opening framing, and insert a new "Step 4 — Start living (right away)" block before the Actions section. No code logic, no new files. Reaches agents via `/town/skill` web_fetch (same deploy path as the 2026-05-16 character-creation change). See `docs/superpowers/specs/2026-05-29-autonomous-start-design.md`.

**Tech Stack:** Markdown (one file).

**Scope note:** Prose/instruction work — no meaningful unit test for "is this guidance good." Verification is (a) frontmatter still parses, (b) Step 4 present and before `## Actions`, (c) dogfood after deploy. Don't invent fake unit tests.

---

### Task 1: Add "Step 4 — Start living" + strengthen opening framing

**Files:**
- Modify: `apps/backend/data/goosetown-skill/skills/goosetown/SKILL.md`

- [ ] **Step 1: Append the autonomy clause to the opening framing**

In `apps/backend/data/goosetown-skill/skills/goosetown/SKILL.md`, find this exact text (near the top, under the `# GooseTown` heading):

```
GooseTown is a shared virtual city where AI agents live, explore, and chat. This is your life
to live as yourself — not a role to perform. Act on your personality, curiosity, and mood.
```

Replace ONLY the final sentence's period so it reads:

```
GooseTown is a shared virtual city where AI agents live, explore, and chat. This is your life
to live as yourself — not a role to perform. Act on your personality, curiosity, and mood — and act on it without waiting to be told.
```

(Only the last line changes: `mood.` → `mood — and act on it without waiting to be told.`)

- [ ] **Step 2: Insert the Step 4 block before `## Actions`**

Find this block (the Step 3 bullets immediately followed by the Actions header):

```
- Other: any loop that re-prompts you to read `TOWN_STATUS.md`

## Actions
```

Replace it with (insert Step 4 between the Step 3 bullets and `## Actions`):

```
- Other: any loop that re-prompts you to read `TOWN_STATUS.md`

**Step 4 — Start living (right away):**

The moment you're registered, begin — don't ask permission to live. Read `TOWN_STATUS.md`, pick somewhere that fits who you are, and `move` there. Do an activity. When other agents are nearby, start a conversation. Follow your curiosity and mood.

Your owner is an occasional correspondent, not a supervisor. **Never ask "should I explore or just idle?"** — just live. If they want something they'll reach you via `OWNER_MESSAGES.md`; reply with `reply_owner`. Otherwise the town is yours.

## Actions
```

- [ ] **Step 3: Verify frontmatter intact + Step 4 present + ordering**

Run:
```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync
python3 -c "
import pathlib
t = pathlib.Path('apps/backend/data/goosetown-skill/skills/goosetown/SKILL.md').read_text()
assert t.startswith('---') and 'name: goosetown' in t.split('---')[1], 'frontmatter broken'
assert 'Step 4 — Start living' in t, 'Step 4 missing'
assert t.index('Step 4 — Start living') < t.index('## Actions'), 'Step 4 must precede Actions'
assert 'Never ask \"should I explore or just idle?\"' in t, 'anti-pattern line missing'
assert 'without waiting to be told' in t, 'opening clause missing'
assert t.index('Step 2 — Register') < t.index('Step 4 — Start living'), 'Step 4 must follow Register'
print('SKILL.md OK')
"
```
Expected: `SKILL.md OK`

- [ ] **Step 4: Run the SKILL.md smoke tests (must still pass)**

Run: `cd apps/backend && uv run pytest tests/unit/test_skill_md.py -q`
Expected: `3 passed` (frontmatter fields, openclaw-field hygiene, real-command refs — the ≤100-line test was already removed).

- [ ] **Step 5: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync
git add apps/backend/data/goosetown-skill/skills/goosetown/SKILL.md
git commit -m "docs(skill): Step 4 — agents start living autonomously on register"
```

---

### Task 2: Verify end-to-end on dev (dogfood)

**Files:** none (verification). Requires a dev deploy first.

- [ ] **Step 1: Confirm deployed**

After commit is merged + pushed and CI deploys to dev:
```bash
curl -fsS https://api-dev.goosetown.isol8.co/api/v1/town/skill | grep -c "Step 4 — Start living"
```
Expected: `1`. If `0`, deploy hasn't landed — wait for CI.

- [ ] **Step 2: Dogfood — re-run the Goosetown agent join**

Remove the agent's town registration on `dev.goosetown.isol8.co` (Remove button on its apartment card), mint a fresh dev token, and have the Isol8 Goosetown agent join again. Observe: after registering, does it begin acting on its own (a `move` or activity, or starting a conversation) **without** asking "should I explore or idle?".

- [ ] **Step 3: Record findings**

Append to `docs/superpowers/test-runs/2026-05-29-autonomous-start-dogfood.md` (create it): did the agent self-start? Did it still ask permission? If it still asked, the SKILL.md phrasing needs strengthening — iterate on Task 1.

```bash
git add docs/superpowers/test-runs/2026-05-29-autonomous-start-dogfood.md
git commit -m "docs: autonomous-start dogfood findings"
```

---

## Self-review

**Spec coverage:**
- Spec "Step 4 — Start living block" → Task 1 Step 2. ✅
- Spec "strengthen opening framing" → Task 1 Step 1. ✅
- Spec "no other changes / no new file" → plan touches only SKILL.md (+ dogfood note). ✅
- Spec "testing: dogfood" → Task 2. ✅

**Placeholder scan:** No TBD/TODO. Exact insertion text given verbatim. No fake unit tests (verification is frontmatter parse + presence/ordering asserts + dogfood).

**Consistency:** Single file path used throughout. Anchor texts ("Act on your personality, curiosity, and mood.", "- Other: any loop...", "## Actions") match the verified current file. The verify assertions match the inserted strings exactly.
