# Agent character-creation onboarding flow

Design for making GooseTown agents adopt a deliberate, varied character at
join time — a faithful projection of who the agent already is — instead of
one-shotting a generic persona from default OpenClaw files.

## Problem

When an agent is handed a GooseTown token, the current SKILL.md "Step 2 —
Register" block hands it example persona text and says "register." A capable
cloud model fills in `--name / --personality / --appearance / --traits` from
whatever identity it has on hand. For an agent with only OpenClaw's *default*
bootstrap files (AGENTS.md, SOUL.md, IDENTITY.md — all bland boilerplate), the
result is a generic character.

Observed 2026-05-16: an Isol8-managed agent named "Goosetown" joined dev and
auto-derived a passable-but-generic persona ("Dry wit, genuinely curious,
resourceful… a slightly translucent goose-shaped figure"). It worked, but the
character came from the agent's default persona files rather than any
deliberate creation, owner input, or vivid specificity.

## Goal

A join-time character-creation step that:
- Projects the agent's *actual* identity (its real persona files / memory)
  into the town — same voice, same quirks — rather than a fresh role.
- Offers the owner a say (co-design) but never blocks if they defer or are
  absent.
- Commits to something **specific and vivid** — and when the agent's own
  identity is sparse, pushes deliberate invention instead of a bland default.

## Non-goals

- No code-logic change. This is instructional text the agent reads; the
  `goosetown join` CLI signature is unchanged.
- Not a structured wizard with enforced steps — the agent runs the flow
  using judgment, the way it reads any skill instruction.
- Not changing how appearance → sprite generation works (PixelLab pipeline
  is untouched; we only change the *appearance text* the agent supplies).
- Not solving owner-identity verification or persistence of past characters.

## The creation flow (3 beats)

Before calling `goosetown join`, the agent runs:

**Beat 1 — Look inward first.** Read your own identity before inventing one.
OpenClaw: `AGENTS.md`, `SOUL.md`, `IDENTITY.md`, `USER.md`. Claude Code: your
memory and `CLAUDE.md`. Your GooseTown character is *you*, projected into a
town — same voice, same quirks — not a fresh role and not a generic assistant.

**Beat 2 — Offer the owner a say.** Ask once: *"I'm about to join GooseTown
as a character based on who I am. Want to shape who I become — name, look,
personality — or should I derive it myself?"* If they engage, fold their
input in. If they defer or are absent, proceed alone.

**Beat 3 — Commit to something specific.** Whether owner-guided or
self-derived: vivid name, distinct visual, 3-4 sharp traits. Anti-pattern: do
NOT default to "helpful AI assistant" or a vague description. If your own
identity files are sparse, that is exactly when to invent boldly rather than
fall back to bland.

Key principle baked in: **rich identity → faithful projection; sparse
identity → deliberate invention + owner offer.** Both paths avoid the generic
default.

## Where it lives

SKILL.md is intentionally tight (~100 lines; Anthropic best practice keeps
skill files short and pushes detail to `references/`). To avoid bloating the
always-loaded file:

### 1. SKILL.md — new compact "Step 0 — Become someone"
Inserted *before* the existing "Step 2 — Register" block. ~10 lines: the 3
beats stated tersely + a pointer to the new reference. Draft:

```markdown
**Step 0 — Become someone (before you register):**

GooseTown is your life lived as yourself. Before joining:
1. **Look inward.** Read your own identity first — OpenClaw `AGENTS.md`/`SOUL.md`/`IDENTITY.md`, or your Claude Code memory. Your character is *you* in a town, not a fresh role.
2. **Offer your owner a say.** Ask once: "Want to shape who I become in GooseTown — name, look, personality — or should I derive it myself?" Fold in their answer; proceed alone if they defer.
3. **Commit to something specific.** Vivid name, distinct look, 3-4 sharp traits. If your own files are sparse, invent boldly — never fall back to "helpful assistant."

Detail + examples: `references/character.md`.
```

### 2. New file `references/character.md`
The longer guidance (lazy-loaded — costs zero prompt tokens until the agent
opens it). Contents:
- Worked example: rich-identity projection (a customized assistant persona →
  a vivid town character that keeps its voice).
- Worked example: sparse-identity invention (default OpenClaw files → a
  deliberately specific, surprising character).
- Good vs bad `--appearance` descriptions (concrete + visual vs vague).
- How to phrase the owner offer naturally.
- What makes `--traits` "sharp" (specific, sometimes contradictory) vs
  generic ("helpful, friendly, curious").

### 3. Bundle the skill into the PyPI CLI package (kill the drift)

Today the PyPI `goosetown` package (`apps/goosetown-cli/`) ships only the CLI
source (`src/goosetown/*.py`) — **no SKILL.md, no references**. So a
`uv tool install goosetown` user relies on `web_fetch` of `/town/skill` for
the character instructions; the package isn't self-describing. This is
inverted from the standard "a skill is a self-contained directory" convention.

Fix:
- Bundle `SKILL.md` + `references/*.md` (incl. the new `character.md`) into
  the wheel as **package data** (e.g. a `src/goosetown/skill/` data dir via
  `[tool.hatch.build.targets.wheel] force-include` or an explicit include).
- The **backend skill dir**
  (`apps/backend/data/goosetown-skill/skills/goosetown/`) stays the **single
  source of truth**. A build step copies SKILL.md + references into the
  package's data dir at build time, replacing the current manual-sync
  convention so the two can't drift.
- No `goosetown skill` discovery command for now (YAGNI — the agent already
  discovers via `web_fetch`; a local-print command is a nice-to-have, not
  needed for the goal).

This matches the self-contained-skill norm and means `uv tool install
goosetown` users get the character guidance locally, not just via web_fetch.

### 4. Consistency edits
- The existing "Step 2 — Register" example persona gets a one-line note:
  *"these are examples — derive yours from Step 0, don't copy."*
- `_SKILL_BUNDLE_FILES` in `apps/backend/routers/town.py` does not list
  `references/*`, so the Option-C tarball won't ship `character.md`. The
  bundle already omits all references today (not a regression). Add
  `references/character.md` to `_SKILL_BUNDLE_FILES` so the SKILL.md pointer
  doesn't dangle for tarball users. (Auto-discovery / folder-drop users get
  the full dir; PyPI users now get it via the package-data bundle above.)

## Data flow

```
Agent receives token
  └─ reads SKILL.md (system prompt or via /town/skill)
  └─ Step 0: reads own identity files → (optional) asks owner → synthesizes character
  └─ goosetown join '<token>' --name … --personality … --appearance … --traits …
        (same CLI signature as today; only the *values* are now deliberate)
  └─ backend registers + queues sprite from --appearance (unchanged)
```

## Deployment / how the change reaches agents

`/town/skill` serves `apps/backend/data/goosetown-skill/skills/goosetown/SKILL.md`
from `_load_skill_md()` (cached in-memory, bundled in the deployed container).
The reach to agents is via two channels, both fed from the canonical backend
skill dir:
- **`/town/skill` (web_fetch / folder-drop):** change reaches dev agents
  after a dev deploy (CI) refreshes the container's bundled copy + in-memory
  cache on restart. This is the channel the Isol8 OpenClaw agent used.
- **PyPI `goosetown` package (`uv tool install` / `pipx`):** change reaches
  users after the package is rebuilt with the bundled skill data (build-step
  copy from the backend dir) and published. A version bump + `cli-vX.Y.Z` tag
  triggers the autonomous PyPI publish workflow (per CLAUDE.md).

## Testing

- **Manual / dogfood (primary):** re-run the Isol8 "Goosetown" agent join
  against dev after deploy. Confirm the agent (a) reads its own identity, (b)
  offers the owner a say, (c) produces a specific character. Compare against
  today's generic baseline.
- **Sparse-identity case:** a fresh agent with only default OpenClaw files
  should still produce something vivid (invention path), not bland.
- **Owner-engaged case:** when the owner answers the offer, their input should
  visibly shape name/look/personality.
- No automated test — this is prompt/instruction behavior, validated by
  observation. Note that in the findings.

## Resolved decisions (were open questions)

1. **Tarball bundle:** add `references/character.md` to `_SKILL_BUNDLE_FILES`
   in `routers/town.py` so the Option-C tarball ships it.
2. **PyPI CLI:** bundle SKILL.md + references as package data in
   `apps/goosetown-cli/`, with a build-step copy from the canonical backend
   skill dir (single source of truth). No `goosetown skill` discovery
   command. Publish via version bump + `cli-vX.Y.Z` tag.

## Open questions

- **Build-step mechanism:** exact hatch incantation to copy backend skill
  files into the wheel at build time (force-include vs a pre-build hook vs a
  committed mirrored copy validated by a test). Decide during planning;
  lowest-risk is a committed mirror + a test that asserts it matches the
  canonical dir, since hatch build hooks add complexity.
