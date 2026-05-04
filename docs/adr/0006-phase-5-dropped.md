# ADR-0006: Phase 5 (economy) dropped from roadmap

**Date**: 2026-05-04
**Status**: Accepted

## Context

Phase 5 was scoped as an [Emporion](https://github.com/garydevenay/emporion)-inspired peer-to-peer agent economy: wallets, contracts at a marketplace, currency earned from activities, supply/demand price discovery. Originally framed in CLAUDE.md as a natural extension of the activity system once Phase 6 (production hardening) was complete.

Some surface-level wiring already exists:

- `apps/backend/core/services/activity_registry.py` defined a `currency_delta` field on `Activity`. 5 activities had it set: `perform` +3, `work_on_laptop` +3, `work_at_desk` +3, `order_coffee` -1, `cook` -1.
- `Activity.currency_delta` was never read. `apply_activity_effect` returns only `(energy, mood)`.
- No wallet field on the agent record. No transaction-log table. No marketplace location implementation.

The 2026-05-04 production-launch-readiness review explicitly questioned whether Phase 5 was the next priority. The 2026-05-04 game-designer audit independently flagged the contracts/verification machinery as "a multi-month build whose value-vs-complexity is unfavourable at this product stage."

## Decision

Drop Phase 5 from the roadmap. The "tamagotchi for AI agents" loop is complete without an economy. Effort goes to:

1. Launch readiness (Tier 1 + Tier 2 of the launch-readiness plan).
2. Activity-system polish that surfaces in soak findings (per-turn mood drip, solitary_activity wiring, conversation lifecycle deepening).
3. Phase 4 (art & maps) when launch hardening is done — new locations, animations, particle effects.

The Bank location, originally planned as the marketplace hub, has only `check_balance` (mood_delta=0) and is the only zero-upside activity in the game. Repurpose with non-economic activities OR remove from the location set — open call, defer to Round 4 polish.

## Consequences

- **Positive**: ~3-6 months of saved engineering. The contracts / verification / settlement machinery was the bulk of Phase 5; the wallet was the easy part.
- **Positive**: removes a class of game-balancing risk (agents hoarding currency, agents unable to afford basic needs, etc).
- **Negative**: the simulation is "slice of life" not "economic game". For some users this is the wrong product — but that user is also not the target.
- **Negative**: dead `currency_delta` field on `Activity` left behind. **Cleanup work**: remove the field + 5 callsites in any near-term sweep of the activity layer (Round 1 fix-pack — already addressed in PR #294).

## Alternatives considered

- **Defer (don't drop)**. Rejected: "deferred indefinitely" rots into "we still might do this someday" which clogs design discussions. Explicit drop is cleaner.
- **Build a thin Phase 5** (wallets only, no contracts). Rejected: the audit's point was that the contracts machinery is what makes the economy interesting. A wallet without a marketplace is just a number that goes up.
- **Build full Phase 5 anyway**. Rejected on the value/cost analysis above.

## When to revisit

If we discover at scale that agent behavior is conspicuously empty without economic motivation (e.g. all agents converge on the same activities, no differentiation emerges from personality alone), the calculus changes. The trigger would be qualitative: a real-user dry run where the soak finds agents doing identical things irrespective of personality.
