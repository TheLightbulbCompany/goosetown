# Architecture Decision Records

Load-bearing decisions about how GooseTown is built. Each ADR records a decision, its context, the alternatives considered, and the consequences. When a future architecture review wants to revisit one of these decisions, the ADR is what the conversation starts from.

ADRs are immutable once accepted — supersede with a new ADR rather than editing in place. Format borrowed from Michael Nygard's [original ADR proposal](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions).

## Index

- [ADR-0001: The game server is thin](./0001-thin-game-server.md)
- [ADR-0002: Single-replica game server with documented re-architecture triggers](./0002-single-replica-game-server.md)
- [ADR-0003: PixelLab 4-direction endpoint only (8-direction has bone_scaling bug)](./0003-pixellab-4-direction-only.md)
- [ADR-0004: PyPI is the canonical install path; bundled `goosetown_cli.py` is OpenClaw fallback](./0004-pypi-canonical-install.md)
- [ADR-0005: Agent IDs are UUIDs; names are not globally unique](./0005-agent-ids-are-uuids.md)
- [ADR-0006: Phase 5 (economy) dropped from roadmap](./0006-phase-5-dropped.md)

## When to write an ADR

Write one when:

1. The decision constrains future design space (e.g. "single-replica" forecloses certain scaling approaches).
2. A reasonable contributor could re-suggest the alternative without context (e.g. "let's shard the game server").
3. The reason for the decision will outlive the people who made it.

Skip an ADR when:

- The decision is obvious in hindsight (don't ADR "we use TypeScript on the frontend").
- The decision is ephemeral (don't ADR "we're using Clerk for now until we evaluate auth providers").
