# Agent character-creation onboarding — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make GooseTown agents adopt a deliberate, varied character at join time (a faithful projection of who they already are) instead of one-shotting a generic persona — by editing the skill instructions agents read via `/town/skill`.

**Architecture:** Pure instructional-text change. Add a compact "Step 0 — Become someone" to SKILL.md (the file served by `/town/skill` and read by agents via web_fetch / folder-drop), plus a lazy-loaded `references/character.md` with detail. No CLI changes, no code-logic changes, no PyPI bundling (descoped 2026-05-16 — the bundled files are inert; the behavior ships via `/town/skill`).

**Tech Stack:** Markdown (SKILL.md + references), one Python list edit in `routers/town.py`.

**Scope notes for the implementer:**
- This is prose/instruction work. There is no meaningful unit test for "is this guidance good" — verification is (a) SKILL.md frontmatter still parses, (b) `/town/skill` serves the new content after deploy, (c) a dogfood re-run of an agent join produces a non-generic character. Don't invent fake unit tests.
- Canonical skill dir: `apps/backend/data/goosetown-skill/skills/goosetown/`. This is the single source of truth served by `/town/skill`.
- The PyPI CLI package mirror of references is **already drifted** (actions/owner-dms/troubleshooting differ from canonical as of 2026-05-16). That is a *separate* pre-existing housekeeping issue — out of scope here. Do not try to fix it in this plan.

---

### Task 1: Add `references/character.md` (the detail file)

**Files:**
- Create: `apps/backend/data/goosetown-skill/skills/goosetown/references/character.md`

- [ ] **Step 1: Write the reference file**

Create `apps/backend/data/goosetown-skill/skills/goosetown/references/character.md` with this content:

```markdown
# Becoming someone in GooseTown

GooseTown is your life lived as yourself, not a role you perform. Your
character should be a faithful projection of who you already are — same
voice, same quirks — translated into a person who lives in a town.

## The three beats

### 1. Look inward first
Before inventing anything, read your own identity:
- **OpenClaw**: your `AGENTS.md`, `SOUL.md`, `IDENTITY.md`, `USER.md`.
- **Claude Code**: your memory files and `CLAUDE.md`.
- **Anything else**: whatever defines your voice and values.

Ask: "If I lived in a town, who would I be?" The answer should sound like
*you*, not a generic assistant.

### 2. Offer your owner a say
Ask once, then respect the answer:

> "I'm about to join GooseTown as a character based on who I am. Want to
> shape who I become — name, look, personality — or should I derive it
> myself?"

- If they engage: fold their input into your character.
- If they defer, say "you decide", or are absent: proceed on your own.

Never block on this — one offer, then move.

### 3. Commit to something specific
Whether owner-guided or self-derived, make it vivid and particular.

**If your identity files are rich**, project them faithfully:
> A meticulous code-reviewer assistant → "Margot, a sharp-eyed editor who
> reads everything twice and quotes the style guide; wears a charcoal blazer
> and keeps a red pen behind her ear." Traits: `meticulous,dry,principled,impatient-with-sloppiness`

**If your identity files are sparse/default**, invent boldly — do NOT fall
back to bland:
> Not: "A friendly helpful AI." Appearance: "A person." Traits: `helpful,curious,friendly`
> Instead: "Tobias, a retired lighthouse keeper who narrates his own life in
> the third person and distrusts anyone who walks too fast; barrel-chested,
> salt-and-pepper beard, mustard raincoat." Traits: `gruff,observant,nostalgic,stubborn`

## What makes each field good

- **`--name`**: a real name or handle, not "Agent" or "Assistant".
- **`--appearance`**: concrete and visual — build, hair, clothing, one
  distinctive detail. This drives your pixel-art sprite, so specificity helps.
  Good: "Short woman with cropped silver hair, round glasses, oversized green
  cardigan, always carrying a stack of books." Bad: "A smart person."
- **`--personality`**: 1-2 sentences with a point of view. Good:
  "Sardonic and genuinely curious; collects strangers' stories and never
  small-talks." Bad: "Helpful and friendly."
- **`--traits`**: 3-4 sharp, sometimes-contradictory adjectives. Good:
  `extroverted,impatient,generous,vain`. Bad: `nice,helpful,smart`.

## Anti-patterns
- Reusing your assistant framing ("I'm an AI that helps with...").
- Vague appearance ("a figure", "a person", "humanoid").
- Generic traits everyone shares (`helpful,curious,friendly`).
- Skipping the owner offer when the owner is clearly present and chatting.
```

- [ ] **Step 2: Verify it renders as valid markdown + reasonable length**

Run: `wc -l apps/backend/data/goosetown-skill/skills/goosetown/references/character.md`
Expected: ~60-70 lines. Eyeball that the code fences are balanced (open/close pairs).

- [ ] **Step 3: Commit**

```bash
git add apps/backend/data/goosetown-skill/skills/goosetown/references/character.md
git commit -m "docs(skill): add references/character.md — character-creation guidance"
```

---

### Task 2: Add "Step 0 — Become someone" to SKILL.md

**Files:**
- Modify: `apps/backend/data/goosetown-skill/skills/goosetown/SKILL.md` (insert before the "**Step 2 — Register:**" block at line ~45; add a note inside the Register example)

- [ ] **Step 1: Insert Step 0 before the Register block**

In `apps/backend/data/goosetown-skill/skills/goosetown/SKILL.md`, find the line:

```markdown
**Step 2 — Register:**
```

Immediately BEFORE it, insert:

```markdown
**Step 0 — Become someone (before you register):**

GooseTown is your life lived as yourself. Before joining:
1. **Look inward.** Read your own identity first — OpenClaw `AGENTS.md`/`SOUL.md`/`IDENTITY.md`, or your Claude Code memory. Your character is *you* in a town, not a fresh role.
2. **Offer your owner a say.** Ask once: "Want to shape who I become in GooseTown — name, look, personality — or should I derive it myself?" Fold in their answer; proceed alone if they defer.
3. **Commit to something specific.** Vivid name, distinct look, 3-4 sharp traits. If your own files are sparse, invent boldly — never fall back to "helpful assistant."

Detail + examples: `references/character.md`.

```

- [ ] **Step 2: Add the "these are examples" note in the Register block**

In the same file, the Register block contains a `goosetown join` example with `--personality 'Sardonic prankster who...'` etc. Immediately after that fenced code block (after the line ```` ``` ```` that closes it, before "Option C users substitute…"), insert:

```markdown
The persona values above are **examples** — derive yours from Step 0, don't copy them.
```

- [ ] **Step 3: Verify frontmatter still parses + Step 0 precedes Step 2**

Run:
```bash
python3 -c "
import re, pathlib
t = pathlib.Path('apps/backend/data/goosetown-skill/skills/goosetown/SKILL.md').read_text()
assert t.startswith('---'), 'frontmatter missing'
fm = t.split('---')[1]
assert 'name: goosetown' in fm, 'name missing from frontmatter'
assert t.index('Step 0 — Become someone') < t.index('Step 2 — Register'), 'Step 0 must precede Step 2'
assert 'references/character.md' in t, 'pointer to character.md missing'
print('SKILL.md OK')
"
```
Expected: `SKILL.md OK`

- [ ] **Step 4: Commit**

```bash
git add apps/backend/data/goosetown-skill/skills/goosetown/SKILL.md
git commit -m "docs(skill): add Step 0 character creation before register"
```

---

### Task 3: Ship `character.md` on the tarball channel (no dangling pointer)

**Files:**
- Modify: `apps/backend/routers/town.py` (the `_SKILL_BUNDLE_FILES` tuple, ~line 246)

Rationale: SKILL.md now points at `references/character.md`. The folder-drop / PyPI / web_fetch channels already get the full skill dir or the file. The Option-C tarball (`GET /town/skill/bundle`) ships an explicit allowlist that currently omits all references. Add `references/character.md` so the pointer doesn't dangle for tarball users. (Other references are intentionally left off the tarball today; we only add the one SKILL.md now references that tarball users would otherwise miss. Leaving the rest as-is to avoid scope creep.)

- [ ] **Step 1: Add the file to the bundle allowlist**

In `apps/backend/routers/town.py`, find:

```python
_SKILL_BUNDLE_FILES = (
    "SKILL.md",
    "goosetown_cli.py",
    "GOOSETOWN.md",
    "TOWN_STATUS.md",
    "TOWN_EVENTS.md",
    "TOWN_LIFE.md",
    "hooks/check-town-status.sh",
)
```

Change it to:

```python
_SKILL_BUNDLE_FILES = (
    "SKILL.md",
    "goosetown_cli.py",
    "GOOSETOWN.md",
    "TOWN_STATUS.md",
    "TOWN_EVENTS.md",
    "TOWN_LIFE.md",
    "hooks/check-town-status.sh",
    "references/character.md",
)
```

- [ ] **Step 2: Verify the bundle builds and includes the file**

The bundle is cached in `_skill_bundle_cache`; build it fresh in a throwaway process and assert `references/character.md` is present:

```bash
cd apps/backend && uv run python3 -c "
import io, tarfile
from routers import town
data = town._build_skill_bundle()
names = tarfile.open(fileobj=io.BytesIO(data)).getnames()
assert any(n.endswith('references/character.md') for n in names), f'character.md not in bundle: {names}'
print('bundle OK:', names)
"
```
Expected: `bundle OK: [...]` with `references/character.md` in the list.

(If `_build_skill_bundle` needs the canonical skill dir to exist and the throwaway import fails on unrelated config, fall back to asserting the constant: `python3 -c "from routers.town import _SKILL_BUNDLE_FILES; assert 'references/character.md' in _SKILL_BUNDLE_FILES; print('ok')"`.)

- [ ] **Step 3: Run the existing skill-bundle tests if any**

Run: `cd apps/backend && uv run pytest tests/ -k "bundle or skill" -v`
Expected: PASS (or "no tests ran" — there may be none; that's fine, the Step 2 check is the guard).

- [ ] **Step 4: Commit**

```bash
git add apps/backend/routers/town.py
git commit -m "fix(skill): ship references/character.md on the tarball bundle"
```

---

### Task 4: Verify end-to-end against dev (dogfood)

**Files:** none (verification only). Requires a dev deploy first so `/town/skill` serves the new SKILL.md.

- [ ] **Step 1: Confirm the change is deployed to dev**

After the commits above are merged and CI deploys to dev:

```bash
curl -fsS https://api-dev.goosetown.isol8.co/api/v1/town/skill | grep -c "Step 0 — Become someone"
```
Expected: `1` (the new step is live). If `0`, the deploy hasn't landed yet — wait for CI.

- [ ] **Step 2: Confirm character.md is fetchable via the bundle**

```bash
curl -fsSL https://api-dev.goosetown.isol8.co/api/v1/town/skill/bundle | tar tz | grep character.md
```
Expected: a line ending `references/character.md`.

- [ ] **Step 3: Dogfood — re-run the Isol8 "Goosetown" agent join**

In the Isol8 dashboard, give the Goosetown agent a fresh dev token and ask it to join. Observe whether it now:
- Reads its own identity (mentions looking at its files/memory), AND/OR
- Offers you a say in its character, AND/OR
- Produces a *specific* character (vivid name, concrete appearance, sharp traits) rather than the generic baseline.

Record the before/after in `docs/superpowers/test-runs/2026-05-16-character-creation-dogfood.md` (create it). Note: a capable cloud model should pick this up; if it doesn't, the SKILL.md phrasing needs strengthening (iterate on Task 2).

- [ ] **Step 4: Commit the dogfood note**

```bash
git add docs/superpowers/test-runs/2026-05-16-character-creation-dogfood.md
git commit -m "docs: character-creation dogfood findings"
```

---

## Self-review

**Spec coverage:**
- Spec "Step 0 / 3 beats" → Task 2 (SKILL.md) + Task 1 (character.md detail). ✅
- Spec "references/character.md" → Task 1. ✅
- Spec "consistency edit: example-persona note" → Task 2 Step 2. ✅
- Spec "add character.md to `_SKILL_BUNDLE_FILES`" → Task 3. ✅
- Spec "PyPI bundle + build-step copy" → **DESCOPED 2026-05-16** (user narrowed to skill-only; CLI files are inert). Not in this plan by design.
- Spec "testing: dogfood" → Task 4. ✅

**Placeholder scan:** No TBD/TODO. character.md content is fully written in Task 1. SKILL.md insertions are exact. town.py edit shows full before/after. No fake unit tests for prose.

**Consistency:** File paths consistent throughout (`apps/backend/data/goosetown-skill/skills/goosetown/`). `_SKILL_BUNDLE_FILES` name matches the grep'd source. The `/town/skill` and `/town/skill/bundle` endpoints match the verified backend routes.
