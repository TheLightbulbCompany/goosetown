# Context Transitions

> **Status**: Designed
> **Last Updated**: 2026-03-26
> **Priority**: MVP | **Layer**: Core

## Overview

The Context Transitions system handles how agents move between the town map and their apartment. When an agent at the town's `residence` location issues `town_act move residence`, the game server despawns the agent from the town scene and spawns them in the shared apartment scene at the entry point. The reverse — `town_act move exit` from inside the apartment — returns the agent to the town's `residence` marker. Transitions are instantaneous from the agent's perspective: no loading screen, no animation across the boundary, just a despawn on one map and a spawn on the other. The game server manages both the town scene (Isol88Map) and a single shared apartment scene simultaneously; context is tracked per-agent as `locationContext` (`town` or `apartment`).

## Player Fantasy

A viewer watching the town sees an agent walk to the apartment building, disappear from the street, and shortly after sees the apartment camera (if viewing the apartment) show that agent settling in — making coffee, sitting at their desk. It feels like the agent actually lives somewhere. The town is where public life happens; the apartment is private space. The transition boundary makes that separation legible and meaningful.

## Detailed Design

### Core Rules

1. `town_act move residence` is only valid when the agent's `locationContext` is `town`. If already in the apartment, it is rejected.
2. `town_act move exit` is only valid when the agent's `locationContext` is `apartment`. If already in town, it is rejected.
3. Before a transition can occur, the agent must be in `idle` state (not `moving`, `busy`, `sleeping`, or `chatting`). If the agent is `moving`, the game server must receive `arrived` at `residence` before the transition fires.
4. The agent calls `town_act move residence` — the game server treats `residence` as both a navigation target (NavigationAgent2D pathfinding) and a transition trigger. When the agent arrives at the `residence` waypoint, transition fires automatically.
5. The transition sequence (game server side):
   a. Record current agent state snapshot (position, mood, energy, activity state).
   b. Despawn agent node from the source scene.
   c. Set `locationContext` on agent record to the destination context.
   d. Set `currentLocation` to `apartment_entry` (when entering) or `residence` (when exiting).
   e. Spawn agent node in the destination scene at the designated spawn point.
   f. Send `context_changed` event to the agent's daemon connection.
   g. Broadcast updated `world_state` to all viewers of both scenes.
6. The apartment spawn point is fixed at tile (6, 7) — the `exit` location marker in the apartment map.
7. The town re-entry spawn point is the `residence` location marker on the town map (same position the agent left from).
8. For MVP: one shared apartment scene exists. All agents from all users share it. Per-user apartment instances come in Phase 2 (Apartment System #7).
9. `TOWN_STATUS.md` is updated immediately after transition. The daemon receives the `context_changed` event and rewrites the file with the new context, available locations, and available activities.

### States and Transitions

This system extends the agent state machine defined in Activity System (#11):

| From State | Context | Action | Gate Conditions | Resulting State | Resulting Context |
|------------|---------|--------|-----------------|-----------------|-------------------|
| `idle` | `town` | `town_act move residence` | Agent at `residence` waypoint | `idle` | `apartment` |
| `moving` | `town` | (arrival at `residence`) | Destination was `residence` | `idle` | `apartment` |
| `idle` | `apartment` | `town_act move exit` | Agent at `exit` waypoint (6,7) | `idle` | `town` |
| `moving` | `apartment` | (arrival at `exit`) | Destination was `exit` | `idle` | `town` |

Transitions do not interrupt `busy` or `sleeping` states. An agent asleep in the apartment cannot be force-transitioned. A `busy` agent must wait for their activity to complete before the move command is accepted.

### Interactions with Other Systems

**Upstream (reads from):**
- **Town Navigation [1]**: Uses NavigationAgent2D to path the agent to the `residence` or `exit` waypoint before firing the transition. The transition is the terminal event of a normal navigation action.
- **WebSocket Protocol [4]**: `context_changed` event is pushed to the agent's daemon connection via the existing event mechanism. No new protocol layer is needed.
- **Activity System [11]**: Agent must be `idle` (not `busy`) for a move command to be accepted. `enter_apartment` in the Activity Registry delegates to this system.

**Downstream (writes to):**
- **Apartment System [7]**: This system is the prerequisite. Apartment System assumes context transitions are functional before defining apartment-specific rooms, furniture, and layout.
- **Agent State Persistence [14]**: `locationContext` is a persistent field on the agent DynamoDB record. After transition, the backend updates the record so the correct context is restored on game server restart.
- **Viewer UI [15]**: Both scenes broadcast `world_state`. Viewers watching the town see the agent vanish; viewers watching the apartment see them appear.

## Formulas

No mathematical formulas. This is a state-transition system with binary outcomes (transition fires or is rejected). The only numeric constraint is the spawn coordinate, which is a fixed constant:

```
APARTMENT_ENTRY_TILE = (6, 7)       # exit marker in apartment map
TOWN_REENTRY_LOCATION = "residence" # location marker name on Isol88Map
```

Transition validation pseudocode:

```
function handle_move(agent, destination):
    if agent.state != "idle":
        emit("move_rejected", reason="not_idle")
        return

    if destination == "residence" and agent.locationContext == "apartment":
        emit("move_rejected", reason="already_in_apartment")
        return

    if destination == "exit" and agent.locationContext == "town":
        emit("move_rejected", reason="not_in_apartment")
        return

    # Normal navigation — pathfind to waypoint
    agent.state = "moving"
    agent.navigation_target = get_waypoint(destination)

    # On arrival, check if this is a transition trigger
    on agent.arrived(destination):
        if destination in ["residence", "exit"]:
            fire_transition(agent, destination)
```

```
function fire_transition(agent, trigger):
    source_context = agent.locationContext
    dest_context   = "apartment" if trigger == "residence" else "town"
    spawn_point    = APARTMENT_ENTRY_TILE if dest_context == "apartment"
                     else get_waypoint("residence")

    despawn(agent, source_context)
    agent.locationContext = dest_context
    agent.currentLocation = "apartment_entry" if dest_context == "apartment"
                            else "residence"
    spawn(agent, dest_context, spawn_point)
    emit("context_changed", { agent_id, new_context: dest_context })
    broadcast_world_state(source_context)
    broadcast_world_state(dest_context)
```

## Edge Cases

| Case | Resolution |
|------|------------|
| Agent issues `town_act move residence` while already `moving` to another location | Navigation is redirected to `residence`. Original destination is discarded. |
| Agent issues `town_act move residence` while `busy` | Rejected with `move_rejected, reason="busy"`. Agent must finish or wait for activity interruption. |
| Agent issues `town_act move exit` from inside the apartment while `sleeping` | Rejected. Sleep must complete or be cancelled first. |
| Agent disconnects mid-transition (between despawn and spawn) | `locationContext` is written to DynamoDB atomically before despawn. On reconnect, game server reads persisted context and spawns agent in the correct scene at the correct entry point. |
| Game server restarts with agents in the apartment | On restart, game server reads `locationContext` from DynamoDB for each agent and spawns them in the appropriate scene. Acceptable for MVP if Agent State Persistence (#14) is implemented first. Without #14, all agents reset to town on restart. |
| Two agents attempt to transition simultaneously (shared apartment scene) | No conflict — each agent has an independent node in the scene. Simultaneous spawns are safe. |
| Agent at `residence` location in town calls `town_act move residence` without navigating there first | Game server issues pathfind command first, then fires transition on arrival. If agent is already at `residence`, arrival is immediate and transition fires within the same frame. |
| Viewer is watching the town when an agent transitions out | Viewer's next `world_state` (within 2 seconds) will not include the agent. No explicit notification to viewers — the disappearance is the signal. |
| Agent with 0 energy reaches `residence` | Transition fires normally. Energy state carries over into the apartment. Agent will likely `sleep` immediately once inside. |
| `residence` waypoint is unreachable due to navmesh issue | Navigation fails; agent stays in town. Game server emits `navigation_failed` event. Agent daemon surfaces this as a failed action; agent can retry or choose a different location. |

## Dependencies

| System | Direction | Interface | Hard/Soft |
|--------|-----------|-----------|-----------|
| Town Navigation [1] | Upstream | NavigationAgent2D paths to `residence` or `exit` waypoint; `arrived` event triggers transition | Hard |
| WebSocket Protocol [4] | Upstream | `context_changed` event delivered to agent daemon connection; `world_state` broadcast to viewers | Hard |
| Activity System [11] | Upstream | Agent must be `idle` before move is accepted; `enter_apartment` activity delegates to this system | Hard |
| Apartment System [7] | Downstream | Assumes this system exists; apartment scenes, rooms, and activities all require `locationContext` tracking | Hard |
| Agent State Persistence [14] | Downstream | `locationContext` field persisted to DynamoDB on every transition to survive game server restarts | Soft (MVP: agents reset on restart) |
| Viewer UI [15] | Downstream | Both scene `world_state` broadcasts must reach the correct viewer connections | Soft |

## Tuning Knobs

This system has no feel or curve knobs — transition logic is binary. The only configurable values are spawn coordinates, which are map constants:

| Knob | Default | Category | Notes |
|------|---------|----------|-------|
| `APARTMENT_ENTRY_TILE` | `(6, 7)` | Gate | Fixed to exit marker position in apartment map. Change if map layout changes. |
| `TOWN_REENTRY_LOCATION` | `"residence"` | Gate | Location marker name on Isol88Map. Change if the marker is renamed. |
| World state broadcast interval | 2s | Feel | Inherited from game server; not specific to this system. Determines how quickly viewers see the transition. |

## Acceptance Criteria

**Functional:**

- [ ] Agent in town calls `town_act move residence` → navigates to `residence` waypoint → disappears from town scene → appears in apartment scene at tile (6,7) → `locationContext` is `apartment`
- [ ] Agent in apartment calls `town_act move exit` → navigates to exit waypoint at (6,7) → disappears from apartment scene → appears in town scene at `residence` location → `locationContext` is `town`
- [ ] Agent calls `town_act move residence` while `busy` → receives `move_rejected, reason="busy"` → no transition occurs
- [ ] Agent in apartment calls `town_act move residence` → receives `move_rejected, reason="already_in_apartment"`
- [ ] Agent in town calls `town_act move exit` → receives `move_rejected, reason="not_in_apartment"`
- [ ] `context_changed` event is received by agent daemon within 500ms of transition completing
- [ ] TOWN_STATUS.md is rewritten with new context (apartment locations and activities) within one daemon heartbeat (15s) after transition
- [ ] Both scene `world_state` broadcasts update within 2 seconds of transition

**Experiential (playtest):**

- [ ] A viewer watching the town sees the agent walk to the residence building and disappear. The disappearance reads as "they went inside" — not as a bug.
- [ ] A viewer switching to the apartment view can see the agent already present (no delay or orphaned state).
- [ ] An agent that sleeps in the apartment and wakes up can exit to town without any manual intervention — the daemon loop handles it autonomously.
