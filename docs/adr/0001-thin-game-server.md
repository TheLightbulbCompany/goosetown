# ADR-0001: The game server is thin

**Date**: 2026-05-04 (recording an existing decision)
**Status**: Accepted

## Context

The game server (`apps/godot/`, Godot headless on Fargate) handles physics, navigation, proximity detection, and `world_state` broadcast. The agent's behavior — what they choose to do, what they say, when they sleep — is decided by the agent's LLM, not the game server.

There's a recurring temptation to add server-side decision logic: "the agent should leave the cafe when energy < 20", "the agent should accept any chat invite from a high-affinity friend". Each such addition turns the game server from a physics-and-presence service into a partial AI.

## Decision

The game server is **thin**: it manages physics (movement, collision, navmesh), proximity (who's near whom), and event dispatch (broadcast `world_state`, emit `arrived` / `nearby` / `move_rejected`). It does **NOT** decide what an agent should do.

Agent decisions live in the agent's LLM, which reads `TOWN_STATUS.md` and calls `goosetown act`. The backend translates `act` calls into game-server commands but does not synthesize them.

## Consequences

- **Positive**: agent personality is fully controlled by the LLM and the agent's `personality` / `traits` strings. Two agents with identical sprites in identical situations can choose differently.
- **Positive**: the game server is small, deterministic, and easy to test in isolation.
- **Negative**: agents can do "stupid" things the server could have prevented (move during a conversation; the daemon now guards this — see `_handle_chat`'s `currentActivity` check).
- **Negative**: every new agent capability requires LLM-side changes (prompts, TOWN_STATUS.md template), which is harder to roll out than a server-side rule.

## Alternatives considered

- **Server-side behavior trees** (e.g. "if hungry then go to cafe"). Rejected: removes the agent's autonomy; turns the project into a behavior-tree game with LLM flavor rather than a multi-agent simulation.
- **Hybrid (server proposes, agent confirms)**. Rejected as premature: hasn't been needed and would double the round-trip cost on every decision.

## When to revisit

If a class of bugs emerges that consistently arise from "the agent should have known better" — e.g. agents repeatedly walking into walls, repeatedly ignoring sleep at energy=0 — and the LLM-side fix is hard to roll out, the calculus changes. The energy=0 auto-sleep path (`_apply_event_with_sleep_guard`) is already such a server-side guard; we accepted that exception because energy-exhaustion was a hard-stop, not a behavioral nuance.
