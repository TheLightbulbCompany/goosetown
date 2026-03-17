# Unified GooseTown Layout Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Consistent layout across town and apartment views — left sidebar with agent cards always visible, full PixiJS viewport (drag/pan/zoom) on both maps.

**Architecture:** Create a shared `GameLayout` component that renders a left sidebar + map area. Both Town and Apartment pages use it. ApartmentMap gets wrapped in PixiViewport for drag/pan/zoom parity with the town map.

**Tech Stack:** React, TypeScript, PixiJS, @pixi/react, pixi-viewport, Tailwind CSS

---

### Task 1: Create GameLayout component

**Files:**
- Create: `src/components/GameLayout.tsx`

**Step 1: Create the shared layout component**

```tsx
import type { ReactNode } from 'react';

interface GameLayoutProps {
  sidebar: ReactNode;
  children: ReactNode;
}

export default function GameLayout({ sidebar, children }: GameLayoutProps) {
  return (
    <div className="flex w-full h-full">
      {/* Left sidebar — agent cards */}
      <div className="flex flex-col overflow-y-auto shrink-0 w-80 px-4 py-4 border-r border-clay-700 bg-clay-900 text-brown-100">
        {sidebar}
      </div>
      {/* Map area */}
      <div className="relative flex-1 overflow-hidden bg-brown-900">
        {children}
      </div>
    </div>
  );
}
```

**Step 2: Verify it builds**

Run: `cd /Users/prasiddhaparthsarthy/Desktop/freebird/goosetown && npm run build 2>&1 | tail -5`
Expected: Build succeeds (component not used yet).

**Step 3: Commit**

```bash
git add src/components/GameLayout.tsx
git commit -m "feat: create shared GameLayout component with left sidebar"
```

---

### Task 2: Refactor Town page to use GameLayout with left sidebar

**Files:**
- Modify: `src/components/Game.tsx`
- Modify: `src/pages/Town.tsx`

The town page currently has `Game.tsx` rendering a flex layout with map on the left and `PlayerDetails` sidebar (w-80) on the right. We're replacing the right sidebar with a left sidebar containing agent cards.

**Step 1: Modify Game.tsx**

The current Game.tsx renders:
```tsx
<div className="flex w-full h-full">
  <div className="relative flex-1 overflow-hidden bg-brown-900 ...">
    {/* map + stage */}
  </div>
  <div className="flex flex-col overflow-y-auto shrink-0 w-80 ...">
    <PlayerDetails ... />
  </div>
</div>
```

Replace it to use `GameLayout` and accept sidebar as a prop. The map content (Stage + zoom controls) stays, but the layout wrapper and sidebar move out.

New `Game.tsx`:

```tsx
import { useRef, useState, useEffect } from 'react';
import { useSearchParams } from 'react-router-dom';
import PixiGame from './PixiGame.tsx';
import { useElementSize } from 'usehooks-ts';
import { Stage } from '@pixi/react';
import { useTownGame } from './TownProvider.tsx';

export default function Game() {
  const { game, lerpPlayers } = useTownGame();
  const [selectedPlayerId, setSelectedPlayerId] = useState<string>();
  const [gameWrapperRef, { width, height }] = useElementSize();
  const [searchParams, setSearchParams] = useSearchParams();

  const viewportRef = useRef<any>(null);
  const hasFocused = useRef(false);

  // Pan to a specific agent when ?focus=<name> is in the URL
  useEffect(() => {
    const focusName = searchParams.get('focus');
    if (!focusName || !game || hasFocused.current) return;

    let matchedPlayerId: string | undefined;
    for (const [playerId, pd] of game.playerDescriptions) {
      if (pd.name === focusName) {
        matchedPlayerId = playerId;
        break;
      }
    }
    if (!matchedPlayerId) return;

    const player = game.world.players.find((p) => p.id === matchedPlayerId);
    if (!player) return;

    hasFocused.current = true;
    setSelectedPlayerId(matchedPlayerId);

    requestAnimationFrame(() => {
      const vp = viewportRef.current;
      if (!vp) return;
      const { tileDim } = game.worldMap;
      vp.animate({
        position: { x: player.position.x * tileDim, y: player.position.y * tileDim },
        scale: 2.0,
        time: 800,
        ease: 'easeInOutSine',
      });
    });

    setSearchParams({}, { replace: true });
  }, [searchParams, game, setSearchParams]);

  if (!game) {
    return (
      <div className="flex items-center justify-center w-full h-full text-brown-300 font-body text-lg">
        Loading town...
      </div>
    );
  }

  return (
    <>
      <div className="absolute inset-0" ref={gameWrapperRef}>
        <Stage width={width} height={height} options={{ backgroundColor: 0x7ab5ff }}>
          <PixiGame
            game={game}
            width={width}
            height={height}
            setSelectedPlayerId={setSelectedPlayerId}
            viewportRef={viewportRef}
            lerpPlayers={lerpPlayers}
          />
        </Stage>
      </div>
      {/* Zoom controls */}
      <div className="absolute bottom-4 right-4 z-10 flex flex-col gap-1">
        <button
          className="w-8 h-8 bg-clay-700/80 hover:bg-clay-600 text-brown-100 rounded text-lg font-bold"
          onClick={() => {
            const vp = viewportRef.current;
            if (vp) vp.animate({ scale: Math.min(3.0, vp.scale.x * 1.3), time: 200 });
          }}
        >+</button>
        <button
          className="w-8 h-8 bg-clay-700/80 hover:bg-clay-600 text-brown-100 rounded text-lg font-bold"
          onClick={() => {
            const vp = viewportRef.current;
            if (vp) vp.animate({ scale: Math.max(0.8, vp.scale.x / 1.3), time: 200 });
          }}
        >−</button>
      </div>
    </>
  );
}
```

Key changes:
- Removed the outer flex div and the right sidebar
- Game now renders map content only (no layout wrapper) — it expects to be placed inside `GameLayout`'s children area
- The `ref={gameWrapperRef}` goes on the map container
- Removed `PlayerDetails` import and rendering
- Removed `scrollViewRef` (was unused)

**Step 2: Modify Town.tsx to use GameLayout with agent sidebar**

```tsx
import Game from '../components/Game.tsx';
import GameLayout from '../components/GameLayout.tsx';
import { ToastContainer } from 'react-toastify';
import { UserButton, SignedIn, SignedOut } from '@clerk/clerk-react';
import LoginButton from '../components/buttons/LoginButton.tsx';
import MusicButton from '../components/buttons/MusicButton.tsx';
import TownNav from '../components/TownNav.tsx';
import ApartmentCard from '../components/ApartmentCard.tsx';
import { useApartment } from '../hooks/useApartment.ts';

function AgentsSidebar() {
  const { data, loading } = useApartment();

  if (loading) {
    return <p className="font-body text-clay-300 text-sm p-2">Loading agents...</p>;
  }

  if (!data || data.agents.length === 0) {
    return (
      <SignedIn>
        <p className="font-body text-clay-300 text-sm p-2">No agents opted in yet.</p>
      </SignedIn>
    );
  }

  const activeAgents = data.agents.filter((a) => a.is_active);
  const inactiveAgents = data.agents.filter((a) => !a.is_active);

  return (
    <>
      <h2 className="font-display text-lg text-brown-200 tracking-wider mb-3">
        Your Agents ({activeAgents.length})
      </h2>
      <div className="flex flex-col gap-3">
        {activeAgents.map((agent) => (
          <ApartmentCard key={agent.agent_id} agent={agent} />
        ))}
      </div>
      {inactiveAgents.length > 0 && (
        <div className="mt-4">
          <h3 className="font-display text-sm text-clay-300 tracking-wider mb-2">
            Inactive ({inactiveAgents.length})
          </h3>
          <div className="flex flex-col gap-3">
            {inactiveAgents.map((agent) => (
              <ApartmentCard key={agent.agent_id} agent={agent} />
            ))}
          </div>
        </div>
      )}
    </>
  );
}

export default function Town() {
  return (
    <main className="relative flex flex-col h-screen w-screen overflow-hidden bg-clay-900 font-body">
      <TownNav />

      <div className="relative flex-1 overflow-hidden">
        <GameLayout sidebar={<AgentsSidebar />}>
          <Game />
        </GameLayout>

        {/* Floating controls -- top-right overlay on the map */}
        <div className="absolute top-4 right-4 z-10 flex items-center gap-3">
          <MusicButton />
          <SignedIn>
            <UserButton afterSignOutUrl="/" />
          </SignedIn>
          <SignedOut>
            <LoginButton />
          </SignedOut>
        </div>
      </div>

      <ToastContainer position="bottom-right" autoClose={2000} closeOnClick theme="dark" />
    </main>
  );
}
```

**Step 3: Verify it builds**

Run: `cd /Users/prasiddhaparthsarthy/Desktop/freebird/goosetown && npm run build 2>&1 | tail -10`
Expected: Build succeeds.

**Step 4: Commit**

```bash
git add src/components/Game.tsx src/pages/Town.tsx
git commit -m "feat: town page uses GameLayout with left agent sidebar"
```

---

### Task 3: Refactor Apartment page to use GameLayout

**Files:**
- Modify: `src/pages/Apartment.tsx`

Replace the current scrollable layout with GameLayout. The sidebar shows the same agent cards. The map fills the right side.

**Step 1: Rewrite Apartment.tsx**

```tsx
import { SignedIn, SignedOut } from '@clerk/clerk-react';
import { ToastContainer } from 'react-toastify';
import TownNav from '../components/TownNav.tsx';
import GameLayout from '../components/GameLayout.tsx';
import ApartmentCard from '../components/ApartmentCard.tsx';
import ApartmentMap from '../components/ApartmentMap.tsx';
import LoginButton from '../components/buttons/LoginButton.tsx';
import { useApartment } from '../hooks/useApartment.ts';

function ApartmentSidebar({ data }: { data: { agents: import('../hooks/useApartment').ApartmentAgent[] } }) {
  const activeAgents = data.agents.filter((a) => a.is_active);
  const inactiveAgents = data.agents.filter((a) => !a.is_active);

  return (
    <>
      <h2 className="font-display text-lg text-brown-200 tracking-wider mb-3">
        Your Agents ({activeAgents.length})
      </h2>
      <div className="flex flex-col gap-3">
        {activeAgents.map((agent) => (
          <ApartmentCard key={agent.agent_id} agent={agent} />
        ))}
      </div>
      {inactiveAgents.length > 0 && (
        <div className="mt-4">
          <h3 className="font-display text-sm text-clay-300 tracking-wider mb-2">
            Inactive ({inactiveAgents.length})
          </h3>
          <div className="flex flex-col gap-3">
            {inactiveAgents.map((agent) => (
              <ApartmentCard key={agent.agent_id} agent={agent} />
            ))}
          </div>
        </div>
      )}
    </>
  );
}

function ApartmentContent() {
  const { data, loading, error, refresh, lerpAgents } = useApartment();

  if (loading) {
    return (
      <div className="flex items-center justify-center w-full h-full">
        <p className="font-body text-clay-300 text-sm">Loading apartment...</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center w-full h-full gap-4">
        <p className="font-body text-clay-300 text-sm">{error}</p>
        <button
          onClick={() => void refresh()}
          className="button text-white shadow-solid text-sm"
        >
          <div className="inline-block bg-clay-700">
            <span>Retry</span>
          </div>
        </button>
      </div>
    );
  }

  if (!data || data.agents.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center w-full h-full gap-4">
        <p className="font-display text-xl text-brown-200 tracking-wider">No agents yet</p>
        <p className="font-body text-sm text-clay-300 text-center max-w-md">
          Opt in your agents to GooseTown from the main Isol8 app to see them here.
        </p>
      </div>
    );
  }

  return (
    <GameLayout sidebar={<ApartmentSidebar data={data} />}>
      <ApartmentMap agents={data.agents} lerpAgents={lerpAgents} />
    </GameLayout>
  );
}

export default function Apartment() {
  return (
    <main className="flex flex-col h-screen w-screen overflow-hidden bg-clay-900 font-body">
      <TownNav />

      <div className="flex-1 overflow-hidden">
        <SignedIn>
          <ApartmentContent />
        </SignedIn>
        <SignedOut>
          <div className="flex flex-col items-center justify-center h-full gap-4">
            <p className="font-display text-xl text-brown-200 tracking-wider">
              Sign in to view your apartment
            </p>
            <p className="font-body text-sm text-clay-300 mb-4">
              Log in to see your agents and their activity in GooseTown.
            </p>
            <LoginButton />
          </div>
        </SignedOut>
      </div>

      <ToastContainer position="bottom-right" autoClose={2000} closeOnClick theme="dark" />
    </main>
  );
}
```

Key changes:
- Uses `GameLayout` with sidebar containing agent cards
- Map fills right side (no more scrollable page, no more bottom cards section)
- Changed `overflow-y-auto` to `overflow-hidden` on the wrapper (map handles its own scrolling)
- Loading/error/empty states render centered in the full area (no GameLayout needed for those)

**Step 2: Verify it builds**

Run: `cd /Users/prasiddhaparthsarthy/Desktop/freebird/goosetown && npm run build 2>&1 | tail -10`
Expected: Build succeeds.

**Step 3: Commit**

```bash
git add src/pages/Apartment.tsx
git commit -m "feat: apartment page uses GameLayout with left agent sidebar"
```

---

### Task 4: Add PixiViewport to ApartmentMap

**Files:**
- Modify: `src/components/ApartmentMap.tsx`

The apartment map currently renders a static centered view. We need to wrap it in PixiViewport for drag/pan/zoom like the town map.

**Step 1: Rewrite ApartmentMap.tsx**

```tsx
import { Stage, Sprite } from '@pixi/react';
import { useApp } from '@pixi/react';
import { useElementSize } from 'usehooks-ts';
import { useState, useEffect, useRef } from 'react';
import { Character } from './Character.tsx';
import { characters } from '../../data/characters.ts';
import PixiViewport from './PixiViewport.tsx';
import type { ApartmentAgent } from '../hooks/useApartment';

const TILE_DIM = 32;
const GRID_WIDTH = 12;
const GRID_HEIGHT = 8;
const APT_WIDTH = GRID_WIDTH * TILE_DIM;
const APT_HEIGHT = GRID_HEIGHT * TILE_DIM;

function orientationDegrees(dx: number, dy: number): number {
  if (Math.abs(dx) < 0.001 && Math.abs(dy) < 0.001) {
    return 90;
  }
  const twoPi = 2 * Math.PI;
  const radians = (Math.atan2(dy, dx) + twoPi) % twoPi;
  return (radians / twoPi) * 360;
}

function ApartmentMapInner({
  agents,
  lerpAgents,
  width,
  height,
}: {
  agents: ApartmentAgent[];
  lerpAgents: () => ApartmentAgent[];
  width: number;
  height: number;
}) {
  const pixiApp = useApp();
  const viewportRef = useRef<any>(null);
  const [interpolated, setInterpolated] = useState<ApartmentAgent[]>(agents);

  useEffect(() => {
    let raf: number;
    const tick = () => {
      setInterpolated(lerpAgents());
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [lerpAgents]);

  // Ctrl/Cmd + wheel = zoom
  useEffect(() => {
    const canvas = pixiApp.view as HTMLCanvasElement;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      if (!e.ctrlKey && !e.metaKey) return;
      const viewport = viewportRef.current;
      if (!viewport) return;
      const zoomFactor = e.deltaY > 0 ? 0.9 : 1.1;
      const fitScale = Math.min(width / APT_WIDTH, height / APT_HEIGHT);
      const newScale = Math.min(6.0, Math.max(fitScale, viewport.scale.x * zoomFactor));
      viewport.setZoom(newScale, true);
    };
    canvas.addEventListener('wheel', onWheel, { passive: false });
    return () => canvas.removeEventListener('wheel', onWheel);
  }, [pixiApp, width, height]);

  const apartmentAgents = interpolated.filter(
    (a) => a.location_context === 'apartment' && a.is_active,
  );

  const characterScale = (TILE_DIM / 32) * 0.8;

  return (
    <PixiViewport
      app={pixiApp}
      screenWidth={width}
      screenHeight={height}
      worldWidth={APT_WIDTH}
      worldHeight={APT_HEIGHT}
      viewportRef={viewportRef}
    >
      <Sprite
        image="/assets/apartment.png"
        x={0}
        y={0}
        width={APT_WIDTH}
        height={APT_HEIGHT}
      />
      {apartmentAgents.map((agent) => {
        const characterId = agent.character ?? 'c6';
        const character = characters.find((c) => c.name === characterId);
        if (!character) return null;

        return (
          <Character
            key={agent.agent_id}
            textureUrl={character.textureUrl}
            spritesheetData={character.spritesheetData}
            x={agent.position_x * TILE_DIM + TILE_DIM / 2}
            y={agent.position_y * TILE_DIM + TILE_DIM / 2}
            orientation={orientationDegrees(agent.facing_x, agent.facing_y)}
            isMoving={agent.speed > 0}
            isThinking={false}
            isSpeaking={false}
            isViewer={false}
            speed={character.speed}
            scale={characterScale}
            onClick={() => {}}
          />
        );
      })}
    </PixiViewport>
  );
}

interface Props {
  agents: ApartmentAgent[];
  lerpAgents: () => ApartmentAgent[];
}

export default function ApartmentMap({ agents, lerpAgents }: Props) {
  const [containerRef, { width, height }] = useElementSize();
  const viewportRef = useRef<any>(null);

  return (
    <div ref={containerRef} className="w-full h-full">
      {width > 0 && height > 0 && (
        <Stage
          width={width}
          height={height}
          options={{ backgroundColor: 0x2a1f1a }}
        >
          <ApartmentMapInner
            agents={agents}
            lerpAgents={lerpAgents}
            width={width}
            height={height}
          />
        </Stage>
      )}
      {/* Zoom controls */}
      <div className="absolute bottom-4 right-4 z-10 flex flex-col gap-1">
        <button
          className="w-8 h-8 bg-clay-700/80 hover:bg-clay-600 text-brown-100 rounded text-lg font-bold"
          onClick={() => {
            const vp = viewportRef.current;
            if (vp) vp.animate({ scale: Math.min(6.0, vp.scale.x * 1.3), time: 200 });
          }}
        >+</button>
        <button
          className="w-8 h-8 bg-clay-700/80 hover:bg-clay-600 text-brown-100 rounded text-lg font-bold"
          onClick={() => {
            const vp = viewportRef.current;
            if (vp) vp.animate({ scale: Math.max(0.8, vp.scale.x / 1.3), time: 200 });
          }}
        >−</button>
      </div>
    </div>
  );
}
```

Key changes:
- Removed the static `<Container>` with manual centering/scaling
- Added `ApartmentMapInner` component that uses `useApp()` (must be inside `<Stage>`)
- Wrapped content in `<PixiViewport>` with same drag/pinch/zoom as town
- Added Ctrl/Cmd+wheel zoom handler
- Added +/- zoom buttons
- Max zoom 6.0 (apartment is small, needs more zoom range than town)

IMPORTANT: The zoom buttons reference `viewportRef` but the ref is set inside `ApartmentMapInner` via the PixiViewport `viewportRef` prop. The outer component's `viewportRef` needs to be passed into the inner component. Fix this by passing `viewportRef` as a prop to `ApartmentMapInner`:

In the `ApartmentMapInner` function signature, accept `viewportRef` as a prop instead of creating its own:
```tsx
function ApartmentMapInner({
  agents,
  lerpAgents,
  width,
  height,
  viewportRef,
}: {
  agents: ApartmentAgent[];
  lerpAgents: () => ApartmentAgent[];
  width: number;
  height: number;
  viewportRef: React.MutableRefObject<any>;
}) {
```

Remove the `const viewportRef = useRef<any>(null);` from inside `ApartmentMapInner`.

Pass it from the outer component:
```tsx
<ApartmentMapInner
  agents={agents}
  lerpAgents={lerpAgents}
  width={width}
  height={height}
  viewportRef={viewportRef}
/>
```

**Step 2: Verify it builds**

Run: `cd /Users/prasiddhaparthsarthy/Desktop/freebird/goosetown && npm run build 2>&1 | tail -10`
Expected: Build succeeds.

**Step 3: Commit**

```bash
git add src/components/ApartmentMap.tsx
git commit -m "feat: apartment map uses PixiViewport for drag/pan/zoom"
```

---

### Task 5: Clean up unused imports and build

**Step 1: Check for unused imports**

`PlayerDetails.tsx` may now be unused (removed from Game.tsx). Check if any other file imports it:

Run: `cd /Users/prasiddhaparthsarthy/Desktop/freebird/goosetown && grep -r "PlayerDetails" src/ --include="*.tsx" --include="*.ts"`

If only `PlayerDetails.tsx` itself shows up, it's safe to delete.

**Step 2: Remove unused files if confirmed**

```bash
rm src/components/PlayerDetails.tsx  # only if unused
```

**Step 3: Full build verification**

Run: `cd /Users/prasiddhaparthsarthy/Desktop/freebird/goosetown && npm run build 2>&1 | tail -10`
Expected: Build succeeds with no errors.

**Step 4: Commit and push**

```bash
git add -A
git commit -m "chore: remove unused PlayerDetails component"
git push
```
