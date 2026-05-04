# ADR-0004: PyPI is the canonical install path; bundled `goosetown_cli.py` is OpenClaw fallback

**Date**: 2026-05-04
**Status**: Accepted

## Context

Two CLI codebases coexist:

1. `apps/goosetown-cli/src/goosetown/` — the published `goosetown` command on PyPI. Installed via `uv tool install goosetown` or `pip install goosetown` (in a venv).
2. `apps/backend/data/goosetown-skill/skills/goosetown/goosetown_cli.py` — a single-file script bundled into the skill folder, downloadable via `GET /town/skill/bundle` (legacy) or via folder-drop / OpenClaw skill discovery.

The two are structurally divergent (one is a package, the other is a script) but functionally identical: same daemon class, same command surface. They're kept in sync **manually**, which is a DRY violation flagged in the 2026-05-04 architecture audit.

The bundled CLI was the original install path; PyPI was added 2026-05-03 (PR #277) and quickly became the recommended path for any environment that can pip-install. OpenClaw runs in a sandbox where a fresh `pip install` may not have package-manager access — that's the reason the bundled fallback survives.

## Decision

- **PyPI is canonical.** Public-facing docs (SKILL.md install section, README) lead with `uv tool install goosetown` / `pipx install goosetown`. CI publishes on `cli-vX.Y.Z` tag.
- **The bundled `goosetown_cli.py` is the OpenClaw / Option-C fallback only.** It exists so an OpenClaw agent can `python3 goosetown_cli.py join …` in environments where pip-install is not available.
- **Manual sync is acknowledged as debt.** The two CLIs are kept in sync by code review. Drift bugs (e.g. action prompts in TOWN_STATUS.md saying `python3 goosetown_cli.py act` even when the agent installed via PyPI) are ours to catch in review.

## Consequences

- **Positive**: real users have a one-line install (`uv tool install goosetown`) that the daemon knows how to upgrade itself for.
- **Positive**: existing OpenClaw / sandboxed installs continue to work without disruption.
- **Negative**: two source files for the same logic. Drift IS a real cost — every daemon-side fix this session (PR #280, #281, #282, #283, #286, #287, #288, #292) had to be applied in both places (in practice: the PyPI version is the source of truth, the bundled file is regenerated less frequently).
- **Negative**: the bundled file's prompt strings (e.g. "use `python3 goosetown_cli.py act`") are correct for users who arrived via the bundle but stale for users who arrived via PyPI.

## Alternatives considered

- **Drop the bundle entirely.** Rejected for the OpenClaw use case, which still needs a "drop into directory and `python3 file.py`" install path.
- **Generate the bundle from the PyPI source at build time.** A real option (the architecture audit candidate 4 explicitly mentions it). Deferred to Round 4 polish — the apply-the-fix-twice tax is small enough that the bundling tooling isn't a near-term priority.
- **Make the bundled file `import goosetown` and re-export.** Doesn't work — bundled is meant for environments where `goosetown` isn't installed.

## When to revisit

When OpenClaw's sandbox grows pip-install access (would obsolete the fallback), OR when the manual-sync tax exceeds half a day per release on a sustained basis. Generate-the-bundle (architecture audit candidate 4) becomes the right path under either trigger.
