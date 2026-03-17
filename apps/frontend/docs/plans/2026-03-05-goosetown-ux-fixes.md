# GooseTown UX Fixes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix four critical UX issues: scroll/zoom controls, anonymous viewing, join button, and visual quality.

**Architecture:** All changes are frontend-only in the goosetown Vite/React/PixiJS app. The Convex shim layer (`convex/isol8/react.ts`) handles data fetching; pixi-viewport handles map interaction. We fix viewport controls, ensure REST polling works without auth, add a join button, and tune visual scaling.

**Tech Stack:** React 18, PixiJS 7, pixi-viewport 5, pixi-react 7, Clerk, Tailwind CSS, Vite

---

### Task 1: Fix Scroll/Pan — Remove wheel-zoom, add Ctrl+wheel zoom

**Files:**
- Modify: `src/components/PixiViewport.tsx:39-48`
- Modify: `src/components/PixiGame.tsx:27-29,114-122`

**Step 1: Update PixiViewport to remove `.wheel()` and fix decelerate**

Replace lines 39-51 in `src/components/PixiViewport.tsx` with:

```typescript
    viewport
      .drag()
      .pinch({})
      .decelerate({ friction: 0.97 })
      .clamp({ direction: 'all', underflow: 'center' })
      .clampZoom({
        minScale: Math.max(0.5, fitScale * 0.9),
        maxScale: 3.0,
      });
    // Start at a zoom that shows detail (1.5x) rather than fitting entire map
    const initialScale = Math.max(fitScale, 1.5);
    viewport.moveCenter(props.worldWidth / 2, props.worldHeight / 2);
    viewport.setZoom(initialScale);
```

Key changes:
- Removed `.wheel({ smooth: 5 })` — prevents trackpad scroll from zooming
- Changed `.decelerate({ friction: 0.97 })` — less slippery panning
- Changed initial zoom from `fitScale` to `Math.max(fitScale, 1.5)` — starts zoomed in enough to see sprites

**Step 2: Add Ctrl/Cmd+wheel zoom handler in PixiGame**

Add a `useEffect` in `src/components/PixiGame.tsx` after line 29 (after `viewportRef`):

```typescript
  // Ctrl/Cmd + wheel = zoom (Google Maps convention)
  useEffect(() => {
    const canvas = pixiApp.view as HTMLCanvasElement;
    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey && !e.metaKey) return;
      e.preventDefault();
      const viewport = viewportRef.current;
      if (!viewport) return;
      const zoomFactor = e.deltaY > 0 ? 0.9 : 1.1;
      const newScale = Math.min(3.0, Math.max(0.5, viewport.scale.x * zoomFactor));
      viewport.setZoom(newScale, true);
    };
    canvas.addEventListener('wheel', onWheel, { passive: false });
    return () => canvas.removeEventListener('wheel', onWheel);
  }, [pixiApp]);
```

**Step 3: Update initial zoom animation**

In `src/components/PixiGame.tsx`, change line 95 from `scale: 1.2` to `scale: 1.5`:

```typescript
    viewportRef.current.animate({
      position: new PIXI.Point(centerX, centerY),
      scale: 1.5,
      time: 1500,
      ease: 'easeInOutSine',
    });
```

**Step 4: Verify locally**

Run: `cd goosetown && npm run dev`
- Open browser, two-finger scroll should PAN the map
- Ctrl+scroll should ZOOM
- Pinch-to-zoom should still work
- Panning should feel snappy, not slippery

**Step 5: Commit**

```bash
cd goosetown
git add src/components/PixiViewport.tsx src/components/PixiGame.tsx
git commit -m "fix: scroll pans instead of zooming, Ctrl+wheel to zoom"
```

---

### Task 2: Add Zoom Buttons

**Files:**
- Modify: `src/pages/Town.tsx:14-27`
- Modify: `src/components/Game.tsx:17-81` (expose viewportRef)
- Modify: `src/components/PixiGame.tsx` (accept and use external viewportRef)

**Step 1: Add zoom button UI to Town.tsx**

The zoom buttons need access to the pixi-viewport ref. The simplest approach: add zoom buttons in `Game.tsx` as a React overlay alongside the Stage, since Game already has the viewport ref indirectly through PixiGame.

Instead of threading the ref through, add the zoom buttons as sibling overlays in `Game.tsx`. Add a `viewportRef` state at the Game level and pass it down, then use it for zoom buttons.

In `src/components/Game.tsx`, after the `scrollViewRef` declaration (line 37), add:

```typescript
  const viewportRef = useRef<any>(null);
```

Pass it to PixiGame as a new prop, and add zoom button overlay after the Stage closing tag (after line 61):

```tsx
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
                  if (vp) vp.animate({ scale: Math.max(0.5, vp.scale.x / 1.3), time: 200 });
                }}
              >−</button>
            </div>
```

Update PixiGame to accept and forward the `viewportRef`:
- Add `viewportRef` to PixiGame props
- Remove the local `useRef<Viewport | undefined>()` in PixiGame and use the prop instead

**Step 2: Verify locally**

Run: `cd goosetown && npm run dev`
- Click + to zoom in, − to zoom out
- Buttons should be bottom-right corner of the map area

**Step 3: Commit**

```bash
cd goosetown
git add src/components/Game.tsx src/components/PixiGame.tsx src/pages/Town.tsx
git commit -m "feat: add zoom +/- buttons"
```

---

### Task 3: Fix Anonymous/Spectator Viewing

**Files:**
- Modify: `convex/isol8/react.ts:300-357`

**Step 1: Fix useQuery to always use REST polling for game state when WS is unavailable**

The current issue: when `wsEnabled` is true but the user is unauthenticated, the WS never connects (no token). The REST polling fallback exists (lines 318-329) but it stops polling once WS delivers data — which never happens for spectators. However, looking more carefully, the polling does continue via `setInterval(fetchRest, 2000)` and only stops when `clearInterval(pollId)` is called inside the WS update handler. So the polling should keep running.

The real issue is that `client.query()` calls `_authHeaders()` which returns `{}` when unauthenticated. If the backend endpoints require auth, they'll 401. But per the design, `/town/state` and `/town/descriptions` are public GET endpoints.

The actual bug: the REST polling fallback has a condition `if (client.getWsCached(ref.endpoint) !== undefined) return;` at line 322. This is fine — it won't stop polling until WS has data. The polling should work. But let's make it more robust by also continuing to poll at a slower rate even after WS connects (in case WS drops):

Replace lines 314-338 in `convex/isol8/react.ts`:

```typescript
    if (client.wsEnabled && ref.endpoint in WS_KEY_FOR_ENDPOINT) {
      const cached = client.getWsCached(ref.endpoint);
      if (cached !== undefined) setData(cached);

      // REST polling — always active as fallback. For unauthenticated users
      // (spectators), WS never connects so this is the only data source.
      // For authenticated users, WS delivers data and polling stops.
      let cancelled = false;
      let wsDelivered = false;
      const fetchRest = async () => {
        if (cancelled || wsDelivered) return;
        try {
          const result = await client.query(ref, resolved);
          if (!cancelled && !wsDelivered) setData(result);
        } catch { /* retry on next interval */ }
      };
      void fetchRest();
      const pollId = setInterval(fetchRest, 2000);

      const unsub = client.onWsUpdate(() => {
        const val = client.getWsCached(ref.endpoint);
        if (val !== undefined) {
          setData(val);
          wsDelivered = true;
          clearInterval(pollId);
        }
      });
      return () => { cancelled = true; clearInterval(pollId); unsub(); };
    }
```

This is a minor clarity improvement — the logic was already correct, but the variable naming makes intent clearer.

**Step 2: Verify the loading state for spectators**

In `src/components/Game.tsx`, line 39 returns `null` when game data hasn't loaded yet. This means spectators see a blank screen while polling. Add a loading indicator.

Replace lines 39-41:

```typescript
  if (!worldId || !engineId || !game) {
    return (
      <div className="flex items-center justify-center w-full h-full text-brown-300 font-body text-lg">
        Loading town...
      </div>
    );
  }
```

**Step 3: Verify locally**

Run: `cd goosetown && npm run dev`
- Open in incognito (no auth) — should see "Loading town..." then the map with agents
- If backend endpoints require auth, you'll see "Loading town..." forever — that's a backend issue, not frontend

**Step 4: Commit**

```bash
cd goosetown
git add convex/isol8/react.ts src/components/Game.tsx
git commit -m "fix: spectator mode — REST polling for unauthenticated users + loading state"
```

---

### Task 4: Add Join Town Button

**Files:**
- Modify: `src/components/Game.tsx`
- Modify: `src/components/PixiGame.tsx` (expose humanPlayerId)

**Step 1: Add join button to Game.tsx**

The join button needs to know:
1. Is the user signed in? (use `useConvexAuth` from the shim)
2. Does the user already have a player in the world? (check `humanPlayerId`)
3. The `worldId` to join

Add imports and the join button in `src/components/Game.tsx`:

After the existing imports, add:
```typescript
import { useConvexAuth, useMutation } from 'convex/react';
```

Inside the `Game` component, after `useWorldHeartbeat()` (line 32), add:

```typescript
  const { isAuthenticated } = useConvexAuth();
  const joinWorld = useMutation(api.world.joinWorld);

  const humanTokenIdentifier = useQuery(api.world.userStatus, worldId ? { worldId } : 'skip') ?? null;
  const humanPlayerId = game ? [...game.world.players.values()].find(
    (p) => p.human === humanTokenIdentifier,
  )?.id : undefined;

  const handleJoin = async () => {
    if (!worldId) return;
    try {
      await joinWorld({ worldId });
    } catch (e) {
      console.error('Failed to join:', e);
    }
  };
```

Add the join button as a floating overlay inside the map area div (after the Stage, before the closing `</div>` of the map area):

```tsx
            {isAuthenticated && !humanPlayerId && (
              <div className="absolute bottom-4 left-1/2 -translate-x-1/2 z-10">
                <button
                  className="px-6 py-3 bg-clay-700 hover:bg-clay-600 text-brown-100 rounded-lg font-display text-lg tracking-wider shadow-lg transition-colors"
                  onClick={handleJoin}
                >
                  Join Town
                </button>
              </div>
            )}
```

**Step 2: Verify locally**

Run: `cd goosetown && npm run dev`
- Sign in → should see "Join Town" button at bottom center
- Click it → button disappears, player character spawns
- If already joined, no button shown

**Step 3: Commit**

```bash
cd goosetown
git add src/components/Game.tsx
git commit -m "feat: add Join Town button for authenticated users"
```

---

### Task 5: Visual Quality — Sprite Scaling

**Files:**
- Modify: `src/components/Character.tsx:86-108`
- Modify: `src/components/Player.tsx:65-72`

**Step 1: Add scale prop to Character and tune sprite size**

The sprites are 32x40px, tiles are 16px. At 1:1 scale, a character is 2 tiles wide and 2.5 tiles tall — too big relative to the environment.

In `src/components/Player.tsx`, the character is positioned at `historicalLocation.x * tileDim + tileDim / 2`. The sprite renders at 1:1 pixel scale. To make characters proportional to tiles, scale them to roughly 1 tile wide (16px from 32px = 0.5 scale). But that might be too small — test with 0.6-0.7.

Add a `scale` prop to Character. In `src/components/Character.tsx`, add to props interface:

```typescript
  scale?: number;
```

Add default: `scale = 1` in the destructured props.

Apply scale to the Container:

```tsx
    <Container x={x} y={y} scale={scale} interactive={true} pointerdown={onClick} cursor="pointer">
```

In `src/components/Player.tsx`, pass a scale based on tileDim. Since sprites are 32px wide and tiles are 16px, a scale of `tileDim / 32` (= 0.5) makes characters exactly 1 tile wide. Use `tileDim / 24` (≈ 0.67) for a slightly larger look:

```typescript
  const characterScale = tileDim / 24;
```

Pass it to Character:

```tsx
      <Character
        ...existing props...
        scale={characterScale}
      />
```

**Step 2: Verify locally**

Run: `cd goosetown && npm run dev`
- Characters should look proportional to tiles at 1.5x zoom
- They shouldn't tower over buildings
- Adjust the divisor (24) up/down if needed: smaller number = bigger characters

**Step 3: Commit**

```bash
cd goosetown
git add src/components/Character.tsx src/components/Player.tsx
git commit -m "fix: scale character sprites proportional to tile size"
```

---

### Task 6: Build Verification

**Step 1: Run typecheck**

```bash
cd goosetown && npm run typecheck
```

Expected: no errors. Fix any TypeScript issues.

**Step 2: Run lint**

```bash
cd goosetown && npm run lint
```

Expected: no new errors.

**Step 3: Run build**

```bash
cd goosetown && npm run build
```

Expected: successful build.

**Step 4: Run tests if they exist**

```bash
cd goosetown && npm test 2>/dev/null || echo "No tests configured"
```

**Step 5: Final commit if any fixes were needed**

```bash
cd goosetown
git add -A
git commit -m "fix: resolve typecheck/lint issues"
```
