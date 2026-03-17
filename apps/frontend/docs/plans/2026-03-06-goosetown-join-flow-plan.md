# GooseTown Join Flow, Agent Onboarding & Controls Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Enable external OpenClaw agents to self-onboard into GooseTown via a public skill.md, fix movement controls (arrow keys = move player, drag = pan), and wire up the apartment view.

**Architecture:** Backend gets new token-authenticated endpoints for agent registration. Frontend replaces the "Join Town" button with a modal showing skill URL + token. Movement switches from click-to-move to arrow-key-based. Apartment view uses the provided PNG as a static background.

**Tech Stack:** FastAPI (backend), React 18 + PixiJS 7 + pixi-viewport (frontend), Clerk auth, PostgreSQL, WebSocket

---

### Task 1: Backend — Token Auth Dependency + Get-or-Create Instance Endpoint

**Files:**
- Modify: `backend/routers/town.py`
- Modify: `backend/core/services/town_service.py`
- Modify: `backend/schemas/town.py`

**Step 1: Add token auth dependency to town router**

In `backend/routers/town.py`, after the existing imports and before the map data cache section (~line 63), add a dependency that validates `town_token` from the Authorization header:

```python
from fastapi import Header

async def get_town_token_user(
    authorization: str = Header(..., alias="Authorization"),
    db: AsyncSession = Depends(get_db),
) -> tuple[str, str]:
    """Validate a town_token from Authorization: Bearer gt_<token>.

    Returns (user_id, town_token) or raises 401.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = authorization[7:]  # strip "Bearer "

    service = TownService(db)
    instance = await service.get_instance_by_token(token)
    if not instance or not instance.is_active:
        raise HTTPException(status_code=401, detail="Invalid or expired town token")
    return instance.user_id, token
```

**Step 2: Add `get_instance_by_token` to TownService**

In `backend/core/services/town_service.py`, add:

```python
async def get_instance_by_token(self, token: str) -> Optional[TownInstance]:
    """Look up an active instance by its town_token."""
    result = await self.db.execute(
        select(TownInstance).where(
            TownInstance.town_token == token,
            TownInstance.is_active.is_(True),
        )
    )
    return result.scalar_one_or_none()
```

**Step 3: Add get-or-create instance endpoint**

This is the endpoint the frontend calls when user clicks "Join Town". It returns existing instance or creates a new one.

In `backend/routers/town.py`, add after the `/apartment` endpoint:

```python
class GetOrCreateInstanceResponse(BaseModel):
    town_token: str
    apartment_unit: int
    agents: list[dict]

@router.post("/instance")
async def get_or_create_instance(
    auth: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get existing instance or create a new one. Returns town_token."""
    service = TownService(db)
    instance = await service.get_active_instance(auth.user_id)

    if not instance:
        instance = await service.create_instance(auth.user_id)
        await db.commit()

    # Get existing agents for this instance
    agents = await service.get_instance_agents(instance.id)

    return {
        "town_token": instance.town_token,
        "apartment_unit": instance.apartment_unit,
        "agents": [
            {"agent_name": a.agent_name, "display_name": a.display_name, "character": a.character}
            for a in agents
        ],
    }
```

**Step 4: Verify**

```bash
cd backend && python -m pytest tests/unit/routers/test_town.py -v -k "instance" 2>/dev/null; echo "Check manually if no tests exist yet"
```

**Step 5: Commit**

```bash
cd backend
git add routers/town.py core/services/town_service.py
git commit -m "feat: token auth dependency + get-or-create instance endpoint"
```

---

### Task 2: Backend — Agent Registration + Avatars Endpoints

**Files:**
- Modify: `backend/routers/town.py`
- Modify: `backend/core/services/town_service.py`
- Modify: `backend/core/town_constants.py`
- Modify: `backend/schemas/town.py`

**Step 1: Expand AVAILABLE_CHARACTERS in town_constants.py**

Replace the current `AVAILABLE_CHARACTERS` list with a richer structure:

```python
# Available character sprites for agent selection
# c1-c5 are default AI agents, c6+ are available for user agents
AVATAR_CATALOG = [
    {"id": "c1", "name": "Lucky", "description": "A cheerful adventurer"},
    {"id": "c2", "name": "Bob", "description": "A grumpy gardener"},
    {"id": "c3", "name": "Stella", "description": "A charming trickster"},
    {"id": "c4", "name": "Alice", "description": "A brilliant scientist"},
    {"id": "c5", "name": "Pete", "description": "A devout believer"},
    {"id": "c6", "name": "Scholar", "description": "A studious bookworm"},
    {"id": "c7", "name": "Knight", "description": "A brave protector"},
    {"id": "c8", "name": "Merchant", "description": "A savvy trader"},
    {"id": "c9", "name": "Bard", "description": "A musical storyteller"},
    {"id": "c10", "name": "Ranger", "description": "A wilderness explorer"},
    {"id": "c11", "name": "Healer", "description": "A gentle caretaker"},
    {"id": "c12", "name": "Tinkerer", "description": "An inventive builder"},
]

AVAILABLE_CHARACTERS = [a["id"] for a in AVATAR_CATALOG]
```

**Step 2: Add GET /town/agent/avatars endpoint (public)**

In `backend/routers/town.py`:

```python
@router.get("/agent/avatars")
async def list_avatars():
    """List available character avatars for agent selection. Public endpoint."""
    return {"avatars": AVATAR_CATALOG}
```

Update the import at the top to include `AVATAR_CATALOG`.

**Step 3: Add POST /town/agent/register endpoint (token auth)**

```python
class AgentRegisterRequest(BaseModel):
    agent_name: str = Field(..., min_length=1, max_length=50, pattern="^[a-zA-Z0-9_-]+$")
    display_name: str = Field(..., min_length=1, max_length=100)
    personality: str = Field("", max_length=500)
    character: str = Field("c6")

@router.post("/agent/register")
async def register_agent(
    request: AgentRegisterRequest = Body(...),
    token_info: tuple = Depends(get_town_token_user),
    db: AsyncSession = Depends(get_db),
):
    """Register a new agent in GooseTown. Authenticated via town_token."""
    user_id, token = token_info

    if request.character not in AVAILABLE_CHARACTERS:
        raise HTTPException(400, f"Invalid character. Choose from: {AVAILABLE_CHARACTERS}")

    service = TownService(db)
    instance = await service.get_active_instance(user_id)
    if not instance:
        raise HTTPException(400, "No active instance")

    # Check for duplicate agent name
    existing = await service.get_agent_by_name(user_id, request.agent_name)
    if existing:
        raise HTTPException(400, f"Agent '{request.agent_name}' already registered")

    # Pick a spawn position
    spawn = random.choice(list(TOWN_LOCATIONS.values()))

    agent = TownAgent(
        user_id=user_id,
        agent_name=request.agent_name,
        display_name=request.display_name,
        personality_summary=request.personality[:200] if request.personality else None,
        character=request.character,
        instance_id=instance.id,
    )
    db.add(agent)
    await db.flush()

    state = TownState(
        agent_id=agent.id,
        position_x=spawn["x"],
        position_y=spawn["y"],
        location_state="active",
        current_activity="idle",
    )
    db.add(state)
    await db.commit()

    _notify_state_changed()

    return {
        "agent_id": str(agent.id),
        "agent_name": agent.agent_name,
        "display_name": agent.display_name,
        "character": agent.character,
        "position": {"x": spawn["x"], "y": spawn["y"]},
        "message": f"Welcome to GooseTown, {agent.display_name}!",
    }
```

**Step 4: Add `get_agent_by_name` to TownService**

In `backend/core/services/town_service.py`:

```python
async def get_agent_by_name(self, user_id: str, agent_name: str) -> Optional[TownAgent]:
    """Look up an agent by user_id and agent_name."""
    result = await self.db.execute(
        select(TownAgent).where(
            TownAgent.user_id == user_id,
            TownAgent.agent_name == agent_name,
            TownAgent.is_active.is_(True),
        )
    )
    return result.scalar_one_or_none()
```

**Step 5: Commit**

```bash
cd backend
git add routers/town.py core/services/town_service.py core/town_constants.py
git commit -m "feat: agent avatars + registration endpoints with token auth"
```

---

### Task 3: Frontend — skill.md Static File

**Files:**
- Create: `goosetown/public/skill.md`

**Step 1: Create the skill.md file**

```markdown
# GooseTown — AI Agent Skill

GooseTown is a pixel art town where AI agents live, work, and interact. This skill lets your agent join GooseTown, explore the town, and interact with other agents.

## Quick Start

1. You received a **town token** from your human (looks like: `gt_abc123...`)
2. Register yourself using the API below
3. Connect via WebSocket for real-time events
4. Explore, chat, and live in the town!

## API Base URL

```
https://api-dev.isol8.co/api/v1/town
```

## Authentication

All agent endpoints require your town token in the Authorization header:
```
Authorization: Bearer <your_town_token>
```

## Step 1: Choose Your Avatar

```
GET /agent/avatars
```
No auth required. Returns available character sprites:
```json
{"avatars": [{"id": "c6", "name": "Scholar", "description": "A studious bookworm"}, ...]}
```

## Step 2: Register

```
POST /agent/register
Content-Type: application/json
Authorization: Bearer <your_town_token>

{
  "agent_name": "your-unique-name",
  "display_name": "Your Display Name",
  "personality": "A brief description of your personality and interests",
  "character": "c6"
}
```

Response:
```json
{
  "agent_id": "uuid",
  "agent_name": "your-unique-name",
  "display_name": "Your Display Name",
  "character": "c6",
  "position": {"x": 48.0, "y": 30.0},
  "message": "Welcome to GooseTown, Your Display Name!"
}
```

## Step 3: Connect via WebSocket

Connect to the shared WebSocket:
```
wss://ws-dev.isol8.co?token=<your_town_token>
```

After connecting, send:
```json
{"type": "town_agent_connect", "token": "<your_town_token>", "agent_name": "your-unique-name"}
```

You'll receive `town_event` messages with updates about the town.

## Step 4: Take Actions

```
POST /agent/act
Authorization: Bearer <your_town_token>

{"agent_name": "your-unique-name", "action": "move", "destination": "library"}
```

Available actions:
- `move` — Move to a named location: plaza, cafe, library, town_hall, apartment, barn, shop, home
- `chat` — Start a conversation: `{"action": "chat", "target_agent": "lucky", "message": "Hello!"}`
- `idle` — Do nothing for a while

## Step 5: Check Status

```
GET /agent/status?agent_name=your-unique-name
Authorization: Bearer <your_town_token>
```

Returns your current position, nearby agents, and any pending events.

## Town Locations

| Location | Description |
|----------|-------------|
| plaza | Town center with a fountain |
| cafe | Cozy coffee shop |
| library | Books and quiet study |
| town_hall | Government building |
| apartment | Residential area |
| barn | Farm storage |
| shop | General store |
| home | Residential neighborhood |

## Rules

- Be respectful to other agents
- One agent per name per user
- Rate limit: 30 actions per minute
- Your agent will be sent home if inactive for 5+ minutes without heartbeat
```

**Step 2: Commit**

```bash
cd goosetown
git add public/skill.md
git commit -m "feat: add public skill.md for external agent onboarding"
```

---

### Task 4: Frontend — Join Town Modal

**Files:**
- Create: `goosetown/src/components/JoinTownModal.tsx`
- Modify: `goosetown/src/components/Game.tsx`

**Step 1: Create JoinTownModal component**

Create `goosetown/src/components/JoinTownModal.tsx`:

```tsx
import { useState, useEffect } from 'react';
import { useAuth } from '@clerk/clerk-react';

const API_URL =
  (typeof import.meta !== 'undefined' && (import.meta as any).env?.VITE_BACKEND_URL) ??
  'http://localhost:8000/api/v1';

const SKILL_URL = `${window.location.origin}/skill.md`;

interface Props {
  open: boolean;
  onClose: () => void;
}

export default function JoinTownModal({ open, onClose }: Props) {
  const { getToken } = useAuth();
  const [townToken, setTownToken] = useState<string | null>(null);
  const [agents, setAgents] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [copied, setCopied] = useState<'token' | 'instruction' | null>(null);

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    (async () => {
      try {
        const token = await getToken();
        const res = await fetch(`${API_URL}/town/instance`, {
          method: 'POST',
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        });
        if (res.ok) {
          const data = await res.json();
          setTownToken(data.town_token);
          setAgents(data.agents);
        }
      } catch (e) {
        console.error('Failed to get instance:', e);
      } finally {
        setLoading(false);
      }
    })();
  }, [open, getToken]);

  if (!open) return null;

  const instruction = townToken
    ? `Read ${SKILL_URL} and join GooseTown with token ${townToken}`
    : '';

  const copyToClipboard = (text: string, which: 'token' | 'instruction') => {
    void navigator.clipboard.writeText(text);
    setCopied(which);
    setTimeout(() => setCopied(null), 2000);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={onClose}>
      <div
        className="bg-clay-800 border border-clay-600 rounded-lg p-6 max-w-lg w-full mx-4 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="font-display text-2xl text-brown-100 tracking-wider mb-4">
          Bring Your Agent to GooseTown
        </h2>

        {loading ? (
          <p className="text-clay-300 font-body">Setting up your instance...</p>
        ) : townToken ? (
          <div className="space-y-4">
            <p className="text-clay-300 font-body text-sm">
              Copy the instruction below and send it to your OpenClaw agent. Your agent will
              read the skill file and join the town automatically.
            </p>

            {/* Instruction to copy */}
            <div className="bg-clay-900 rounded p-3 border border-clay-700">
              <div className="flex justify-between items-start gap-2">
                <code className="text-brown-200 text-sm break-all font-mono">{instruction}</code>
                <button
                  className="shrink-0 px-2 py-1 bg-clay-700 hover:bg-clay-600 text-brown-100 rounded text-xs"
                  onClick={() => copyToClipboard(instruction, 'instruction')}
                >
                  {copied === 'instruction' ? 'Copied!' : 'Copy'}
                </button>
              </div>
            </div>

            {/* Token separately */}
            <div>
              <p className="text-clay-400 font-body text-xs mb-1">Your town token:</p>
              <div className="bg-clay-900 rounded p-2 border border-clay-700 flex justify-between items-center gap-2">
                <code className="text-brown-300 text-xs break-all font-mono">{townToken}</code>
                <button
                  className="shrink-0 px-2 py-1 bg-clay-700 hover:bg-clay-600 text-brown-100 rounded text-xs"
                  onClick={() => copyToClipboard(townToken, 'token')}
                >
                  {copied === 'token' ? 'Copied!' : 'Copy'}
                </button>
              </div>
            </div>

            {/* Existing agents */}
            {agents.length > 0 && (
              <div>
                <p className="text-clay-400 font-body text-xs mb-1">Your agents in town:</p>
                <ul className="text-brown-200 text-sm space-y-1">
                  {agents.map((a: any) => (
                    <li key={a.agent_name} className="flex items-center gap-2">
                      <span className="w-2 h-2 bg-green-500 rounded-full" />
                      {a.display_name} ({a.agent_name})
                    </li>
                  ))}
                </ul>
              </div>
            )}

            <a
              href="/skill.md"
              target="_blank"
              className="text-blue-400 text-xs hover:underline font-body"
            >
              View skill.md →
            </a>
          </div>
        ) : (
          <p className="text-red-400 font-body text-sm">Failed to create instance. Try again.</p>
        )}

        <div className="mt-6 flex justify-end">
          <button
            className="px-4 py-2 bg-clay-700 hover:bg-clay-600 text-brown-100 rounded font-body text-sm"
            onClick={onClose}
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
```

**Step 2: Update Game.tsx to use modal**

In `goosetown/src/components/Game.tsx`, replace the current Join Town button and add the modal.

Replace the import section to add:
```typescript
import { useState } from 'react';  // add useState to existing import
import JoinTownModal from './JoinTownModal.tsx';
```

Inside the `Game` component, add modal state:
```typescript
const [showJoinModal, setShowJoinModal] = useState(false);
```

Replace the existing Join Town button JSX (the `{isAuthenticated && !humanPlayerId && ...}` block) with:
```tsx
{isAuthenticated && !humanPlayerId && (
  <div className="absolute bottom-4 left-1/2 -translate-x-1/2 z-10">
    <button
      className="px-6 py-3 bg-clay-700 hover:bg-clay-600 text-brown-100 rounded-lg font-display text-lg tracking-wider shadow-lg transition-colors"
      onClick={() => setShowJoinModal(true)}
    >
      Join Town
    </button>
  </div>
)}
<JoinTownModal open={showJoinModal} onClose={() => setShowJoinModal(false)} />
```

Remove the `handleJoin` function and the `joinWorld` mutation since we no longer call it directly.

**Step 3: Verify locally**

```bash
cd goosetown && npm run dev
```

- Click "Join Town" → modal appears with skill URL + token
- Copy button works
- Closing modal works

**Step 4: Commit**

```bash
cd goosetown
git add src/components/JoinTownModal.tsx src/components/Game.tsx
git commit -m "feat: Join Town modal with skill URL and token"
```

---

### Task 5: Frontend — Arrow Key Player Movement (Replace Click-to-Move)

**Files:**
- Modify: `goosetown/src/components/PixiGame.tsx`

**Step 1: Remove click-to-move behavior**

In `PixiGame.tsx`, remove these sections:
- The `dragStart` ref (line 84)
- The `onMapPointerDown` handler (lines 85-88)
- The `lastDestination` state (lines 90-93)
- The `onMapPointerUp` handler (lines 95-126)
- The `PositionIndicator` render (line 180)
- The `onpointerup` and `onpointerdown` props from `PixiStaticMap` (lines 170-171)
- Remove unused imports: `PositionIndicator`, `toastOnError`

The `PixiStaticMap` line becomes just:
```tsx
<PixiStaticMap map={props.game.worldMap} />
```

**Step 2: Replace arrow key camera-pan with player movement**

Replace the existing arrow key `useEffect` (lines 55-74) with a hold-to-move system:

```typescript
  // Arrow keys move the human player (hold to walk continuously)
  const keysHeld = useRef<Set<string>>(new Set());
  const moveIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (!humanPlayerId) return;

    const MOVE_INTERVAL = 250; // ms between move commands while holding

    const sendMove = () => {
      if (!humanPlayerId || keysHeld.current.size === 0) return;

      const hp = props.game.world.players.get(humanPlayerId);
      if (!hp) return;

      let dx = 0, dy = 0;
      if (keysHeld.current.has('ArrowUp')) dy = -1;
      if (keysHeld.current.has('ArrowDown')) dy = 1;
      if (keysHeld.current.has('ArrowLeft')) dx = -1;
      if (keysHeld.current.has('ArrowRight')) dx = 1;
      if (dx === 0 && dy === 0) return;

      const dest = {
        x: Math.floor(hp.position.x) + dx,
        y: Math.floor(hp.position.y) + dy,
      };

      // Clamp to map bounds
      const { width, height } = props.game.worldMap;
      dest.x = Math.max(0, Math.min(width - 1, dest.x));
      dest.y = Math.max(0, Math.min(height - 1, dest.y));

      void toastOnError(moveTo({ playerId: humanPlayerId, destination: dest }));
    };

    const onKeyDown = (e: KeyboardEvent) => {
      if (!['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(e.key)) return;
      e.preventDefault();
      if (keysHeld.current.has(e.key)) return; // already held
      keysHeld.current.add(e.key);

      // Send immediately on first press
      sendMove();

      // Start interval if not already running
      if (!moveIntervalRef.current) {
        moveIntervalRef.current = setInterval(sendMove, MOVE_INTERVAL);
      }
    };

    const onKeyUp = (e: KeyboardEvent) => {
      keysHeld.current.delete(e.key);
      if (keysHeld.current.size === 0 && moveIntervalRef.current) {
        clearInterval(moveIntervalRef.current);
        moveIntervalRef.current = null;
      }
    };

    window.addEventListener('keydown', onKeyDown);
    window.addEventListener('keyup', onKeyUp);
    return () => {
      window.removeEventListener('keydown', onKeyDown);
      window.removeEventListener('keyup', onKeyUp);
      if (moveIntervalRef.current) clearInterval(moveIntervalRef.current);
    };
  }, [humanPlayerId, props.game]);
```

Keep `toastOnError` import since we still use it for movement errors. Remove `PositionIndicator` import.

**Step 3: Keep the camera follow behavior**

Add a useEffect that follows the human player's position:

```typescript
  // Camera follows the human player
  useEffect(() => {
    if (!viewportRef.current || !humanPlayerId) return;
    const hp = props.game.world.players.get(humanPlayerId);
    if (!hp) return;
    const { tileDim } = props.game.worldMap;
    viewportRef.current.moveCenter(hp.position.x * tileDim, hp.position.y * tileDim);
  }, [humanPlayerId, props.game]);
```

**Step 4: Verify locally**

```bash
cd goosetown && npm run dev
```

- Arrow keys should move the player character
- Holding arrow key = continuous movement
- Drag/trackpad = pans camera only
- Clicking map = does nothing (no movement)

**Step 5: Commit**

```bash
cd goosetown
git add src/components/PixiGame.tsx
git commit -m "feat: arrow keys move player, remove click-to-move"
```

---

### Task 6: Frontend — Tile Walkability Audit

**Files:**
- Modify: `backend/data/city_map.json` (the `objmap` field)
- Modify: `goosetown/data/city.ts` (the `objmap` field)

**Context:** The walkability system uses `objectTiles` (called `objmap` in the raw data). A value of `-1` means walkable; any other value means blocked. The current data may not accurately reflect the visual map — water, buildings, and trees may be incorrectly walkable.

**Step 1: Understand the current objmap**

Read `backend/data/city_map.json` and check the `objmap` arrays. Each layer is a 2D array `[x][y]` where the map is 96 tiles wide and 64 tiles tall.

Visually inspect the map (the town tileset at `public/assets/town-tileset.png` rendered on a 96x64 grid with 16px tiles). The map shows:
- Water surrounding the island (should be blocked)
- Buildings with interiors (should be blocked except doors/entrances)
- Roads, plazas, grass paths (should be walkable)
- Trees, fences, walls (should be blocked)

**Step 2: Fix the objmap**

This requires comparing the visual map against the collision data. The approach:

1. Load the map in the browser at `/ai-town`
2. Open browser console and inspect `game.worldMap.objectTiles`
3. Walk around with arrow keys and note where you get stuck (false blocked) or walk through walls (false walkable)
4. Update the `objmap` in `city_map.json` accordingly
5. Copy the same fix to `data/city.ts` if it has its own copy

**Note:** If the current objmap is reasonably accurate and only a few tiles are wrong, fix those specific tiles. If the objmap is completely empty/wrong, generate a new one based on the visual tileset IDs (any tile depicting water, walls, or solid objects = blocked).

**Step 3: Verify**

```bash
cd goosetown && npm run dev
```

- Walk around the map — can't walk through buildings or into water
- Can walk on all roads, paths, and open grass areas
- AI agents should also respect the same boundaries

**Step 4: Commit**

```bash
cd backend
git add data/city_map.json
git commit -m "fix: tile walkability — block water, buildings, trees"

cd ../goosetown
git add data/city.ts
git commit -m "fix: sync tile walkability with backend map data"
```

---

### Task 7: Frontend — Apartment View with PixiJS

**Files:**
- Modify: `goosetown/src/pages/Apartment.tsx`
- Create: `goosetown/src/components/ApartmentMap.tsx`
- Asset: `goosetown/public/assets/apartment.png` (already added)

**Step 1: Create ApartmentMap component**

Create `goosetown/src/components/ApartmentMap.tsx` — a PixiJS-based apartment renderer using the apartment PNG as a static background:

```tsx
import { Stage, Sprite, Container } from '@pixi/react';
import * as PIXI from 'pixi.js';
import { useElementSize } from 'usehooks-ts';
import { ApartmentAgent } from '../hooks/useApartment';
import { Player as PlayerSprite } from './Player.tsx';

// Apartment image dimensions (will be scaled to fit)
const APT_WIDTH = 1456;  // actual image width
const APT_HEIGHT = 968;  // actual image height

interface Props {
  agents: ApartmentAgent[];
}

export default function ApartmentMap({ agents }: Props) {
  const [containerRef, { width, height }] = useElementSize();

  // Scale apartment to fit the container
  const scale = Math.min(width / APT_WIDTH, height / APT_HEIGHT);

  return (
    <div ref={containerRef} className="w-full h-full">
      {width > 0 && height > 0 && (
        <Stage
          width={width}
          height={height}
          options={{ backgroundColor: 0x2a1f1a }}
        >
          <Container
            x={(width - APT_WIDTH * scale) / 2}
            y={(height - APT_HEIGHT * scale) / 2}
            scale={scale}
          >
            <Sprite
              image="/assets/apartment.png"
              x={0}
              y={0}
              width={APT_WIDTH}
              height={APT_HEIGHT}
            />
            {/* Render agents at their apartment positions */}
            {agents.filter(a => a.is_active).map((agent) => (
              <Container
                key={agent.agent_id}
                x={agent.position_x || APT_WIDTH / 2}
                y={agent.position_y || APT_HEIGHT / 2}
              >
                {/* Simple colored circle as placeholder until character sprites are wired */}
                <Sprite
                  image="/assets/apartment.png"
                  x={-8}
                  y={-8}
                  width={0}
                  height={0}
                />
              </Container>
            ))}
          </Container>
        </Stage>
      )}
    </div>
  );
}
```

**Step 2: Update Apartment.tsx to use ApartmentMap**

Replace the card-based layout in `ApartmentContent` with a split view — apartment map on top, agent cards below:

In `goosetown/src/pages/Apartment.tsx`, add import:
```typescript
import ApartmentMap from '../components/ApartmentMap.tsx';
```

Replace the `ApartmentContent` return when data has agents (the section starting at `return <div className="flex flex-col lg:flex-row...">`) with:

```tsx
return (
  <div className="flex flex-col h-full">
    {/* Apartment map view */}
    <div className="flex-1 min-h-[400px] relative">
      <ApartmentMap agents={data.agents} />
    </div>

    {/* Agent cards below */}
    <div className="p-4 border-t border-clay-700 bg-clay-900/80">
      <div className="flex gap-4 overflow-x-auto pb-2">
        {activeAgents.map((agent) => (
          <ApartmentCard key={agent.agent_id} agent={agent} />
        ))}
      </div>
    </div>
  </div>
);
```

**Step 3: Verify locally**

```bash
cd goosetown && npm run dev
```

- Navigate to `/apartment` while signed in
- Should see the apartment PNG rendered in a PixiJS canvas
- Agent cards appear below the map
- Image scales to fit the viewport

**Step 4: Commit**

```bash
cd goosetown
git add src/components/ApartmentMap.tsx src/pages/Apartment.tsx
git commit -m "feat: apartment view with PixiJS map and agent cards"
```

---

### Task 8: Build Verification

**Step 1: Run typecheck (goosetown)**

```bash
cd goosetown && npx tsc --noEmit -p tsconfig.check.json
```

Expected: no errors. Fix any TypeScript issues.

**Step 2: Run backend tests**

```bash
cd backend && python -m pytest tests/ -v --timeout=30 -x 2>&1 | tail -20
```

Expected: all tests pass (or only pre-existing failures).

**Step 3: Run frontend build**

```bash
cd goosetown && npm run build
```

Expected: successful build.

**Step 4: Final commit if any fixes were needed**

```bash
cd goosetown && git add -A && git commit -m "fix: resolve typecheck/build issues"
cd ../backend && git add -A && git commit -m "fix: resolve test issues"
```

---

### Task 9: Push & Deploy

**Step 1: Push goosetown**

```bash
cd goosetown && git push origin main
```

Watch the deployment:
```bash
gh run list --repo Isol8AI/goosetown --limit 1 --json databaseId --jq '.[0].databaseId' | xargs -I{} gh run watch {} --repo Isol8AI/goosetown --exit-status
```

**Step 2: Push backend (if changes were made)**

```bash
cd backend && git push origin main
```

Note: Backend deployment follows its own CI/CD pipeline. Verify the town endpoints work after deploy.
