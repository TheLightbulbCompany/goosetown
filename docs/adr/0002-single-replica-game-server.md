# ADR-0002: Single-replica game server with documented re-architecture triggers

**Date**: 2026-05-04
**Status**: Accepted

## Context

The Godot game server holds the world state in memory: every connected agent's position, navigation target, current activity, proximity graph. There's no DDB write path for live position; only persistent state (location label, mood, energy, currentActivity name) goes through the backend.

This means a game-server pod death = world reset. Every active agent disconnects, every active conversation in mid-flight is lost (the persisted-DDB row remains but the in-memory speech-bubble + facing + activity timer state is gone), and viewers see the canvas go empty.

A natural reaction is to re-architect for resilience: shard by location (one pod per scene), replicate state across pods, run multi-master. Each adds complexity that, at current scale (≤5 agents per user, ≤100 active agents in dev), buys nothing.

The 2026-05-04 production-launch-readiness review (`docs/superpowers/specs/2026-05-04-launch-readiness-plan.md`) explicitly recommended NOT re-architecting before launch.

## Decision

The game server stays **single-replica with documented re-architecture triggers**. We invest in detection and fast recovery instead of replication:

1. CloudWatch alarm on `RunningTaskCount<1` for the `goosetown-{env}-gameserver` Fargate service. Pages on-call within 5 min.
2. ECS task auto-restart on crash (already in place).
3. Recovery runbook: when a game server restarts, agents reconnect via the daemon's existing exponential-backoff loop. Conversations in DDB with `status=active` get auto-finalized by the inactivity timer (180s) or remain stale until a participant explicitly ends them. SLO: ≤60s reconnect for 95% of agents.
4. A frontend "world is restarting" indicator (deferred — currently the canvas just goes blank, which is bad UX but doesn't block launch).

## Consequences

- **Positive**: small, deterministic codebase. The single in-memory state model is what makes proximity detection trivial and `world_state` broadcast cheap.
- **Positive**: deploys are instant (no rolling restart logic, no state migration).
- **Negative**: a pod death loses ~60s of agent presence and any unfinished conversation turns.
- **Negative**: vertical scaling has a ceiling (single Fargate task max).

## Re-architecture triggers

Revisit when ANY of these occur:

- More than **1 reset per day** for two consecutive weeks (means the failure rate is no longer rare).
- More than **150 concurrent agents** (means the single-replica vertical limit is approaching — Fargate task can take it but proximity detection becomes O(N²) in tile count).
- A **user-visible conversation-loss incident** (a customer flags "my agent's conversation just disappeared"). One incident is signal; we don't need a pattern.

## Re-architecture path (when the time comes)

Room-level sharding: one Godot pod per location (plaza, cafe, library, …, plus apartments). Migrate state when an agent moves between rooms. Estimated 6–8 weeks for the first pass. Multi-master / state replication is NOT the answer — too complex for the scale we'd reach before sharding suffices.

## Alternatives considered

- **Replicated multi-master** (Raft / CRDT). Rejected: complexity disproportionate to the problem. The world state is intentionally ephemeral; we don't need durability.
- **DDB-backed live position** (write every world tick to a position table). Rejected: ~600 writes/sec at 100 agents, 4× cost increase for marginal recovery benefit. Persistent fields (location label, activity) already go through DDB.
- **Pre-emptive sharding now**. Rejected: solving a problem we don't have. The launch-readiness review concurs.

## When to revisit

See triggers above. Each trigger should generate a brief "we hit trigger X" note in the ADR superseding this one (or in PR description if the supersession is small).
