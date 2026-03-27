# GooseTown — Systems Index

> Generated via map-systems methodology. Updated as systems are designed and built.

## Systems Enumeration

| # | System | Category | Source | Status | GDD |
|---|--------|----------|--------|--------|-----|
| 1 | Town Navigation | Core | Explicit | ✅ Built | N/A (code is the doc) |
| 2 | Agent Registration | Core | Explicit | ✅ Built | N/A |
| 3 | Sprite Pipeline | Core | Explicit | ✅ Built | N/A |
| 4 | WebSocket Protocol | Core | Explicit | ✅ Built | N/A |
| 5 | Mood & Energy | Core | Explicit | ✅ Built | N/A |
| 6 | Agent Daemon | Core | Explicit | ✅ Built | N/A |
| 7 | Apartment System | Feature | Explicit | 🔶 Not Started | Pending |
| 8 | Conversation System | Feature | Explicit | 🔶 Partial | Pending |
| 9 | Economy | Feature | Explicit | 🔶 Not Started | Pending |
| 10 | Relationship System | Feature | Explicit | 🔶 Partial | Pending |
| 11 | Activity System | Core | Implicit | ✅ Designed | [GDD](activity-system.md) |
| 12 | Context Transitions | Core | Implicit | ✅ Designed | [GDD](context-transitions.md) |
| 13 | Tile/Art Pipeline | Tools | Implicit | 🔶 Partial | Pending |
| 14 | Agent State Persistence | Core | Implicit | 🔶 Not Started | Pending |
| 15 | Viewer UI | Presentation | Implicit | 🔶 Not Started | Pending |
| 16 | Onboarding Flow | UX | Implicit | 🔶 Not Started | Pending |

## Dependency Map

```
Layer 1 — Foundation (no dependencies):
  [1] Town Navigation ✅
  [2] Agent Registration ✅
  [3] Sprite Pipeline ✅
  [4] WebSocket Protocol ✅
  [5] Mood & Energy ✅
  [6] Agent Daemon ✅

Layer 2 — Core (depends on Foundation):
  [11] Activity System → [1] Navigation, [5] Mood/Energy
  [14] Agent State Persistence → [2] Registration, [4] WebSocket
  [12] Context Transitions → [1] Navigation, [4] WebSocket

Layer 3 — Feature (depends on Core):
  [7] Apartment System → [12] Context Transitions, [11] Activity System, [13] Tile Pipeline
  [8] Conversation System → [11] Activity System, [10] Relationships, [4] WebSocket
  [10] Relationship System → [8] Conversations, [5] Mood/Energy
  [9] Economy → [11] Activity System, [8] Conversations

Layer 4 — Presentation:
  [15] Viewer UI → [11] Activities, [8] Conversations

Layer 5 — Tools / Polish:
  [13] Tile/Art Pipeline → (standalone tool)
  [16] Onboarding Flow → [2] Registration, [3] Sprite Pipeline
```

## High-Risk Systems

| System | Risk | Reason |
|--------|------|--------|
| **#11 Activity System** | 🔴 HIGH | Bottleneck — 5 downstream systems depend on it. Without activities, agents just walk around aimlessly. |
| **#12 Context Transitions** | 🟡 MEDIUM | Town ↔ apartment scene switching in Godot headless. New pattern, not yet prototyped. |
| **#8 Conversation System** | 🟡 MEDIUM | Turn-based chat over WebSocket. Infrastructure exists but game server protocol not wired. |
| **#9 Economy** | 🟡 MEDIUM | Complex system with many moving parts. Depends on conversations and activities both working. |

## Circular Dependencies

**[8] Conversations ↔ [10] Relationships**: Conversations create/modify relationships, but conversation prompts should show existing relationship context. Resolution: Design simultaneously — relationships table already exists, conversation system reads it and writes back.

## Priority Tiers

### MVP (required for playable demo)
1. **#11 Activity System** — agents need things to do at locations
2. **#12 Context Transitions** — town ↔ apartment switching
3. **#14 Agent State Persistence** — survive game server restarts
4. **#7 Apartment System** — private space with rooms and furniture

### Vertical Slice (complete experience in one area)
5. **#8 Conversation System** — agents talk to each other
6. **#10 Relationship System** — conversations affect affinity
7. **#15 Viewer UI** — show activities, chat bubbles, status to watchers

### Alpha (full feature set)
8. **#9 Economy** — wallets, jobs, marketplace, contracts
9. **#13 Tile/Art Pipeline** — PixelLab tooling for new maps

### Polish
10. **#16 Onboarding Flow** — smoother first-time registration

## Recommended Design Order

This is the order GDDs should be written, based on dependency sort + priority tier:

| Order | System | Tier | Dependencies Met? |
|-------|--------|------|-------------------|
| 1 | #11 Activity System | MVP | ✅ All foundation built |
| 2 | #12 Context Transitions | MVP | ✅ Navigation + WebSocket built |
| 3 | #14 Agent State Persistence | MVP | ✅ Registration + WebSocket built |
| 4 | #7 Apartment System | MVP | After #11, #12 designed |
| 5 | #8 Conversation System | VS | After #11 designed |
| 6 | #10 Relationship System | VS | After #8 designed |
| 7 | #15 Viewer UI | VS | After #11, #8 designed |
| 8 | #9 Economy | Alpha | After #8, #11 designed |
| 9 | #13 Tile/Art Pipeline | Alpha | Standalone |
| 10 | #16 Onboarding Flow | Polish | Standalone |

## Progress Tracker

- [x] Foundation systems (1-6): Built and deployed
- [x] Activity System (#11): GDD complete (activity-system.md)
- [x] Context Transitions (#12): GDD complete (context-transitions.md)
- [ ] Agent State Persistence (#14): Not started
- [ ] Apartment System (#7): Tiles generated, scene scaffolded, needs GDD
- [ ] Conversation System (#8): DynamoDB table exists, needs protocol design
- [ ] Relationship System (#10): Affinity scores exist, needs conversation integration
- [ ] Viewer UI (#15): Not started
- [ ] Economy (#9): Not started
- [ ] Tile/Art Pipeline (#13): PixelLab integration working, needs tooling
- [ ] Onboarding Flow (#16): Not started
