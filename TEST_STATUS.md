# GooseTown Test Status

Running log of what's been built/changed and what's been verified end-to-end. Update after every deploy and every test session.

**Legend:**
- ✅ Built + tested live (verified working)
- 🟡 Built + deployed, not yet tested live
- 🟠 Built, not yet deployed
- 🔴 Not built yet

---

## Phase D — Ship-Ready Sweep

| Item | Status | Notes |
|------|--------|-------|
| D1: Replace debug print with logger | 🟡 | Deployed |
| D2: 10s WebSocket connect timeout | 🟡 | Deployed |
| D3: X-WS-Lambda-Secret header check | 🟡 | Backend only — infra (D10) not built |
| D4: Paginate get_all_relationships scan | 🟡 | Deployed |
| D5: Paginate viewer relay seed scan | 🟡 | Deployed |
| D6: Config validation — fail fast | 🟡 | Deployed |
| D7: Sprite race condition test | 🟡 | Deployed |
| D8: Reconnect max retries test | 🟡 | Deployed |
| D9: Router integration tests | 🔴 | Not yet written |
| D10: CDK infra for WS_LAMBDA_SECRET | 🔴 | Not yet built |

## Phase B — Make Agents Feel Alive (Godot)

| Item | Status | Notes |
|------|--------|-------|
| B1: Activity animation state machine | 🟡 | Will only show if custom animations exist |
| B2: Smooth activity transitions | 🟡 | Deployed |
| B3: Speech bubble word-wrap, fade, z-order | 🟡 | Deployed |
| B4: Conversation visual indicators | 🟠 | Group facing built (B8), no "..." waiting indicator |
| B5: Idle bob animation | 🟡 | Deployed |
| B6: Day/night shader | ❌ | REJECTED — flickers, looks bad. Replacing with tileset swap approach. |
| B6-v2: Tileset swap for time-of-day | 🔴 | Waiting on user to generate 4 tileset variants (dawn/day/dusk/night) via ChatGPT |

## Phase B+ — Group Conversations

| Item | Status | Notes |
|------|--------|-------|
| BP1-5: participants list, join/leave, N-party broadcast | 🟡 | Backend deployed. Tested 7-turn conversation in first Friends run (backend only). |
| BP6: nearby_conversations in agent_state | 🟡 | Deployed |
| BP7: Daemon group conversation handlers | 🟡 | Unit tested, not live |
| BP8: Game server group facing (centroid) | 🟡 | Deployed |

## Bug Fixes (from Friends test)

| Bug | Status | Notes |
|-----|--------|-------|
| Debug print `print()` in routers/town.py | ✅ | Replaced with logger.debug |
| Chat across the map (no proximity) | ✅ | Fixed: pixel distance check (96px = 3 tiles) |
| Daemon exits permanently on timeout | ✅ | Fixed: run_forever reconnects unless daemon.running=False |
| Nav target defaults to (0,0) → game server churn loop | 🟡 | **ROOT CAUSE of daemon instability** — deployed to dev, not yet confirmed resolved |
| Custom animation frames not fetched from job results | 🟡 | Deployed, needs test — `talking` animation should now appear |

## Features

| Feature | Status | Notes |
|---------|--------|-------|
| Shadow sprite fallback (replace stuck-on-sprite-failure) | 🟡 | Agents connect immediately with shadow silhouette |
| Frontend header bar sync with Godot scene | 🟡 | Dispatches custom event on scene change |
| "View in town" button fix | 🟡 | Now switches Godot scene + navigates |
| Event-driven think triggers on proximity | 🟡 | Cannot fully test (requires agent LLM heartbeat) |
| Sprite job queue (bounded concurrency + retry) | 🟡 | Deployed, needs test with 3+ simultaneous signups |
| Day/night uses real wall-clock time | ❌ | Being replaced with tileset swap |

## Not Built Yet

| Item | Notes |
|------|-------|
| Lazy activity animation generation | Future — agents request `sitting`/`reading`/etc on first use via sprite queue |
| D10: CDK for WS_LAMBDA_SECRET | Infra task, not blocking test |
| D9: Router integration tests | Lower priority |

---

## Test Sessions

### 2026-04-12 Friends Test #1 (6 agents)
**Result:** Core loop validated. Critical bugs found.

**What worked:**
- Registration, token auth, sprite generation (when PixelLab succeeded)
- Movement across 5+ locations
- Activities with mood/energy effects
- 7-turn conversation between Ross and Rachel (backend only)
- Proximity detection

**Bugs found and fixed:**
- Nav target (0,0) game server churn — root cause of daemon disconnects
- Chat across the map (no proximity check)
- Daemon exiting on non-fatal timeout
- Custom animation frames not fetched from job results

**Bugs found, not yet addressed:**
- PixelLab rate limiting on 6 simultaneous signups (sprite queue built to fix)
- Frontend header bar out of sync (fix built, not tested)
- "View in town" button broken (fix built, not tested)

### 2026-04-13 Friends Test #2 (in progress / aborted)
**Result:** Day/night overlay rejected — flickering and looks bad. Agents died. Moving to tileset swap approach.

---

## Next Test Plan

1. User generates 4 tileset variants via ChatGPT from `combined_tileset_atlas.png`
2. Implement tileset swap based on real wall-clock time (remove shader overlay)
3. Re-export .pck
4. Deploy
5. Spawn 3-6 agents, verify:
   - Shadow sprites show during Phase A/B
   - Talking animation appears in conversations
   - Agents stay connected (no churn loop from nav fix)
   - Sprite queue processes 3 at a time (no 500 errors)
   - Time-of-day tileset swap happens cleanly
   - Frontend header bar stays in sync
   - Group conversations work with proximity
