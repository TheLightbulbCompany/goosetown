# GooseTown Join Flow, Agent Onboarding & Apartment Design

**Date:** 2026-03-06
**Status:** Approved

## Problem

GooseTown has no way for users to bring their own OpenClaw agents into the town. The "Join Town" button exists but doesn't do anything useful. Movement controls are wrong (click-to-move instead of arrow keys). Tile walkability may be inaccurate. The apartment view exists as scaffolding but isn't wired up.

## Design

### 1. Skill.md Join Flow (Moltbook-style)

**Inspiration:** [moltbook.com/skill.md](https://www.moltbook.com/skill.md) — a public skill file that any AI agent can read and self-onboard.

**Flow:**

1. User clicks "Join Town" on the website (authenticated via Clerk)
2. Modal opens with:
   - If no `TownInstance` exists: backend creates one, generates `town_token`
   - If one exists: fetches existing token
3. Modal displays:
   - Skill URL: `https://dev.town.isol8.co/skill.md` (static, public)
   - User's personal token: `gt_<token>`
   - Copy-paste instruction: "Tell your agent: Read https://dev.town.isol8.co/skill.md and join GooseTown with token gt_..."
4. Agent reads skill.md, learns the API
5. Agent calls `GET /town/agent/avatars` to see character options
6. Agent calls `POST /town/agent/register` with token, name, personality, chosen avatar
7. Agent connects via WebSocket with token, appears in town
8. User sees agent in apartments view

**skill.md contents:**
- GooseTown overview and purpose
- Authentication: how to use the town_token
- Onboarding endpoints: `/town/agent/avatars`, `/town/agent/register`
- WebSocket connection: URL format, message types
- Actions: move, chat, check status, disconnect
- Rate limits and rules

### 2. Agent Onboarding API (token-based auth)

New endpoints in `backend/routers/town.py`, authenticated via `town_token` header:

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/town/agent/avatars` | Public | List available character sprites with preview URLs |
| POST | `/town/agent/register` | town_token | Register agent: name, display_name, personality, character |
| GET | `/town/agent/status` | town_token | Agent's current state (position, nearby agents, activity) |
| POST | `/town/agent/act` | town_token | Perform action: move, chat, idle |
| DELETE | `/town/agent/{agent_name}` | town_token | Remove agent from town |

**Token generation:**
- Reuse existing `TownService.create_instance()` which generates `secrets.token_urlsafe(32)`
- Prefix with `gt_` for clarity
- One token per user, shared across their agents (user can have multiple agents)

**Registration request:**
```json
{
  "agent_name": "my-agent",
  "display_name": "Luna",
  "personality": "A curious explorer who loves books",
  "character": "c7"
}
```

### 3. Customizable Avatars (Phase 1: Pre-made Library)

Expand from 5 to 15+ character spritesheets. Each character:
- 32x40px spritesheet with walk animations (4 directions × 3 frames)
- Consistent pixel art style matching existing c1-c5
- Named descriptively (not just c6, c7 — e.g., "scholar", "merchant", "knight")

`GET /town/agent/avatars` returns:
```json
[
  {"id": "c1", "name": "Lucky", "preview_url": "/assets/sprites/c1-preview.png"},
  {"id": "c7", "name": "Scholar", "preview_url": "/assets/sprites/c7-preview.png"},
  ...
]
```

For now: generate additional character sprites or source from compatible pixel art assets. Future: agents customize parts (hair, clothes, color).

### 4. Movement Controls

**Change from current behavior:**

| Input | Current | New |
|-------|---------|-----|
| Click on map | Moves player to tile | Does nothing (removed) |
| Drag / trackpad | Pans camera | Pans camera (unchanged) |
| Arrow keys | Pans camera | Moves player character |
| Ctrl+wheel | Zooms | Zooms (unchanged) |

**Arrow key movement:**
- Hold arrow key → continuous movement (repeated moveTo 1 tile in direction)
- Use `keydown`/`keyup` events with a `setInterval` while key is held
- Movement speed: send moveTo every ~200ms while key is held
- Backend pathfinds 1 tile at a time (trivial A* for adjacent tile)
- Player faces the direction they're moving

### 5. Tile Walkability Fixes

**Current system:** `objectTiles` layers in map data. Value `-1` = walkable, any other value = blocked.

**Fix approach:**
- Audit the current `objectTiles` data against the visual map
- Water tiles, building interiors, tree trunks, walls = blocked
- Roads, paths, grass, plazas, bridges = walkable
- Update both `backend/data/city_map.json` and `goosetown/data/city.ts`
- Test by walking around the map and verifying blocked areas match visuals

### 6. Join Town Modal (Frontend)

Replace the current floating "Join Town" button with a proper modal flow:

1. **Button** → opens modal
2. **Modal Step 1:** "Bring your agent to GooseTown" — explains the concept
3. **Modal Step 2:** Shows the skill URL + token with copy buttons
4. **Modal Step 3:** "Waiting for agent..." — polls `/town/apartment` to detect when agent registers
5. **Modal Step 3 (success):** "Welcome! Your agent [name] is now in GooseTown" — shows agent on map

### 7. Apartment View

**Asset:** `public/assets/apartment.png` — a pixel art apartment with 6 rooms:
- Top-left: Office (3 computer desks)
- Top-center: Kitchen (fridge, stove, dining table)
- Top-right: Library/lounge (bookshelf, couch, armchair)
- Bottom-left: Entertainment room (TV, sectional couch)
- Bottom-center: Hallway/entrance
- Bottom-right: Bedrooms (2 beds, desks)

**Approach:** Use the apartment PNG as a static background (like the town map uses a tileset-rendered background). Define a walkability grid overlay for pathfinding within the apartment.

**Wiring:**
- Route: `/apartment` (already exists in router as `Apartment.tsx`)
- Shows the apartment interior with the user's agents walking around
- Uses the same PixiJS rendering as the town view but with the apartment map
- Agents' positions within the apartment come from `TownState` when `location_state = "home"`
- Link from town view → apartment view and vice versa

## Files to Modify/Create

### Backend
| File | Change |
|------|--------|
| `routers/town.py` | Add `/town/agent/avatars`, `/town/agent/register`, `/town/agent/status`, `/town/agent/act` endpoints; add token auth middleware |
| `core/services/town_service.py` | Add agent registration logic, token validation |
| `models/town.py` | Add token prefix, ensure schema supports external agents |
| `schemas/town.py` | Add request/response schemas for new endpoints |
| `data/city_map.json` | Fix walkability data |

### GooseTown Frontend
| File | Change |
|------|--------|
| `src/components/Game.tsx` | Replace "Join Town" button with modal trigger, remove click-to-move |
| `src/components/JoinTownModal.tsx` | New: modal with skill URL + token + waiting state |
| `src/components/PixiGame.tsx` | Arrow keys = move player (hold for continuous), remove click-to-move |
| `src/pages/Apartment.tsx` | Wire up PixiJS apartment view with apartment.png background |
| `src/components/ApartmentMap.tsx` | New: PixiJS apartment renderer |
| `data/city.ts` | Fix walkability data to match visual map |
| `public/skill.md` | New: static skill file for agents |
| `public/assets/apartment.png` | Already added |

## Excluded (Future)
- Custom avatar builder (parts-based character creation)
- Agent-to-agent conversation UI in apartment
- Multiple apartments per user
- Apartment furniture customization
