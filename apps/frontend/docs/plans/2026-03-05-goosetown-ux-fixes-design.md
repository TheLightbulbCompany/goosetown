# GooseTown UX Fixes Design

**Date:** 2026-03-05
**Status:** Approved

## Problem

Four UX issues make GooseTown unusable:

1. **Scroll zooms instead of panning** — trackpad two-finger scroll fires wheel events, which pixi-viewport interprets as zoom. Users can't navigate the map naturally.
2. **Anonymous users see nothing** — the WebSocket connection requires auth; REST polling fallback exists but may not activate properly without a token.
3. **No agents visible after login** — likely a data pipeline issue where world state doesn't reach the renderer.
4. **No way to join the town** — `joinWorld` mutation exists but no UI triggers it.
5. **Visual quality** — 32x40px sprites on 16px tiles can look mismatched; default zoom too low to see detail.

## Design

### 1. Scroll/Pan Controls (`PixiViewport.tsx`)

**Current:** `.drag().pinch().wheel({ smooth: 5 }).decelerate({ friction: 0.92 })`

**Change to:**
- Remove `.wheel()` — plain scroll/two-finger swipe = pan (handled by `.drag()`)
- Add Ctrl/Cmd + wheel = zoom (custom event handler on the Pixi app's view canvas)
- Keep `.pinch()` for mobile
- Change `.decelerate({ friction: 0.97 })` — less slippery
- Add +/- zoom buttons as a React overlay in `Town.tsx`

### 2. Default Zoom & Camera (`PixiGame.tsx`, `PixiViewport.tsx`)

**Current:** Starts at `fitScale` (whole map visible), then animates to 1.2x center.

**Change to:**
- Start centered at ~1.5x zoom so sprites are clearly visible
- Keep zoom-to-player animation when human joins (already at 1.5x)

### 3. Visual Quality (`Character.tsx`, `PixiStaticMap.tsx`)

- Both map tiles and sprites already use `SCALE_MODES.NEAREST` — consistent pixel art rendering
- Tune character scale factor relative to tileDim so sprites feel proportional to the map at default zoom
- Test at 1.0x, 1.5x, 2.0x zoom to find the sweet spot

### 4. Anonymous/Spectator Viewing (`convex/isol8/react.ts`)

**Current:** `_connectWs()` bails if `_getToken` is null. REST polling fallback in `useQuery` exists but only runs when `wsEnabled && endpoint in WS_KEY_FOR_ENDPOINT`.

**Fix:** In `useQuery`, when there's no auth token, skip the WS path entirely and go straight to REST polling for game state endpoints (`/town/state`, `/town/descriptions`). These backend endpoints don't require auth.

### 5. Join Town Button (`Town.tsx` or `Game.tsx`)

- Add a floating "Join Town" button visible to `SignedIn` users who don't have a `humanPlayerId`
- Calls existing `joinWorld` mutation via `useMutation(api.world.joinWorld)`
- Button disappears once player is in the world

### 6. Agent Visibility

- If world state data arrives via REST polling or WS, agents render automatically via `PixiGame.tsx:136` `players.map()`
- Ensure `useWorldHeartbeat` runs for spectators too (restarts inactive worlds)
- No rendering changes needed — this is fixed by fixing the data pipeline (items 4 above)

## Files to Modify

| File | Change |
|------|--------|
| `src/components/PixiViewport.tsx` | Remove `.wheel()`, adjust `.decelerate()`, increase initial zoom |
| `src/components/PixiGame.tsx` | Add Ctrl+wheel zoom handler, adjust initial animation zoom |
| `src/pages/Town.tsx` | Add zoom buttons overlay, add Join Town button |
| `src/components/Game.tsx` | Pass `humanPlayerId` up for Join button visibility |
| `src/components/Character.tsx` | Tune sprite scale relative to tile size |
| `convex/isol8/react.ts` | Fix REST polling for unauthenticated users |

## Excluded

- Replacing tileset/map art (future)
- Backend simulation changes
- New sprite assets
