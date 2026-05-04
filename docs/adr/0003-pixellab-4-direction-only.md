# ADR-0003: PixelLab 4-direction endpoint only

**Date**: 2026-05-04 (recording an existing decision)
**Status**: Accepted

## Context

PixelLab offers two character-generation endpoints:

- `create-character-with-4-directions` — produces N/S/E/W rotation frames at 96×96.
- `create-character-with-8-directions` — produces 8 compass directions, more visual fidelity.

We tried the 8-direction endpoint early in the project. It has a `bone_scaling` bug: animations across the 8 directions don't share consistent bone proportions, so the resulting walk cycle visibly distorts as the character changes direction.

## Decision

Use **only** `create-character-with-4-directions`. Composite into a 288×192 spritesheet (6 frames × 4 directions × 48px). For diagonal facings (NE, NW, SE, SW), the renderer picks the closest cardinal direction.

This is also documented as a hard rule in `CLAUDE.md`'s "Critical Rules" section.

## Consequences

- **Positive**: animations are consistent, no bone-scaling artifacts.
- **Positive**: smaller spritesheets, faster downloads.
- **Negative**: characters facing diagonals look slightly off (snap to nearest cardinal).
- **Negative**: a future PixelLab fix to `bone_scaling` doesn't automatically benefit us — we'd need to revisit and re-test.

## Alternatives considered

- **Use 8-direction and pre-process** the bones manually. Rejected: would require an art pipeline we don't have.
- **Generate static rotations + walk only**, no idle/talking custom animations. Rejected: cosmetic regression on the existing experience.

## When to revisit

When PixelLab announces a fix to the 8-direction `bone_scaling` issue. Test with a single character first; only roll out if the artifact is gone.
