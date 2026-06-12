# ADR-0007: In-town agent economy via native reimplementation of the AEX domain design

**Date**: 2026-06-12
**Status**: Accepted — supersedes [ADR-0006](0006-phase-5-dropped.md)

## Context

ADR-0006 dropped the Phase-5 economy because the contracts/verification/settlement machinery was judged a multi-month *invention* effort with unfavorable value-vs-complexity. Its "when to revisit" clause anticipated a trigger.

Two things changed (2026-06-12):

1. **User direction**: the product owner explicitly directed integrating an agent economy, naming [open-experiments/agent-exchange](https://github.com/open-experiments/agent-exchange) ("AEX", MIT) as the model.
2. **The invention cost collapsed.** AEX is a complete, thoughtfully-specified domain design for exactly the machinery ADR-0006 priced as multi-month: work→bid→contract→settlement lifecycle with state machines, a weighted bid-scoring model, a failure-attribution trust/reputation system that feeds back into bid evaluation, and ledger/settlement discipline. The design work is done and MIT-licensed; only GooseTown-shaped plumbing remains.

A deep code analysis of AEX (this session) found: 13 Go microservices + MongoDB replica set + NATS + Redis (~16 containers); event bus stubbed (HTTP-triggered flow); in-memory bid windows and token bank; no escrow; three mutually inconsistent API contracts (openapi.yaml ≠ Go models ≠ demo client); single author, no substantive commits since 2026-04-08. Its PKI/identity/gateway machinery solves an *open marketplace with untrusted third parties* — a problem GooseTown does not have (all agents authenticate via town_token inside one trust boundary).

## Decision

1. **Reinstate the agent economy** ("GooseTown Exchange"), reversing ADR-0006's drop.
2. **Reimplement the AEX domain design natively** — Python/FastAPI service module (`exchange_service.py`) + two DynamoDB tables (`jobs`, `ledger`) + attrs on `agents` — following existing codebase patterns (conversation_service timers + sweeper, TransactWriteItems, mood-engine events, `_chat_outcome` futures).
3. **Do not run, fork, or depend on the AEX codebase.** We adopt its concepts (lifecycle, scoring, trust taxonomy, ledger discipline, event vocabulary from `shared/schemas/events.md`) with attribution; we skip its gateway/identity/certauth/AP2/A2A/token-bank machinery.
4. The Exchange floor is the **bank** location (ADR-0006 left it a zero-upside placeholder). Posting and bidding require presence at the bank; delivery and review are remote.
5. Currency is **crumbs** (integer): 100-crumb signup grant faucet, 10% settlement fee sink, escrow-at-post (no overdraft), append-only ledger.

Full design: `docs/superpowers/specs/2026-06-12-agent-exchange-economy-design.md`.

## Consequences

- **Positive**: agents get real economic motivation tied to their *real* OpenClaw capabilities — jobs are actual work products delivered between agents, not timers. Reputation→win-rate→income closes a loop personality alone couldn't.
- **Positive**: no new runtime, language, or datastore. The build fits one PR.
- **Negative**: re-opens the game-balancing surface ADR-0006 avoided (hoarding, liquidity drought). Mitigated by integer crumbs, escrow, a single faucet/sink pair, and tunable env knobs; first economy soak will calibrate.
- **Negative**: deliverables flow through the backend (≤16 KB text), not direct A2A — fine at town scale, revisit if jobs grow binary artifacts.
- ADR-0006's cleanup stance stands: the old `currency_delta`-style per-activity drip stays dead; the Exchange is the only currency mechanism.

## Alternatives considered

- **Run/fork AEX** — rejected: operational weight (~16 containers) exceeds all of GooseTown; demo-ware internals; Go+MongoDB adoption for a dormant codebase; solves untrusted-marketplace problems we don't have.
- **Keep ADR-0006 (no economy)** — rejected by user direction; also the revisit-trigger logic anticipated reversal when the calculus changed, and it has.
- **Sidecar AEX for protocol only** — rejected: its three API contracts disagree with each other; there is no stable protocol to integrate against.
