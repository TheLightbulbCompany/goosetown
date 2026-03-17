# GooseTown Map Engine Upgrade

## Problem

GooseTown renders its map as a single pre-rendered PNG. This prevents multi-layer rendering, Y-sorted agents, foreground occlusion, animated tiles, and accurate collision. Agents walk on water, through fountains, and overlap incorrectly. Collision data lives in a separate hand-maintained `city_map.json` file that is out of sync with the visual map.

## Solution

Replace the single-image renderer with a standard tile-based renderer that loads the Tiled map (TMJ format) directly in the frontend, renders tiles per-layer using `@pixi/tilemap`, and derives collision from the map file itself.

## Architecture

### Data Flow

```
Tiled editor (.tmx) → Export as .tmj (JSON) → goosetown/public/assets/town-map.tmj
                                                        ↓
                                             Frontend: fetch() on mount
                                                        ↓
                                             Parse layers + tileset refs
                                                        ↓
                                     @pixi/tilemap CompositeTilemap per layer
                                                        ↓
                          Render: ground → objects → agents (Y-sorted) → foreground
                                                        ↓
                                     Backend: parse same .tmj for collision grid
```

Single source of truth: `town-map.tmj`. Both frontend rendering and backend pathfinding read from this file. No separate `city_map.json`.

### Tiled Map Layer Structure

Current: 1 tile layer (`background`) using two tilesets — the main 6176-tile tileset (`town-tileset.png`) and a single-tile collision marker tileset (firstgid 6177). The collision data is baked into the same layer via the collision tileset.

New: 4 layers

| Layer | Type | Purpose |
|-------|------|---------|
| `ground` | tile | Roads, grass, water, paths — the base terrain |
| `objects` | tile | Buildings, fences, benches — rendered below agents |
| `foreground` | tile | Tree canopies, rooftops, fountain top — rendered above agents |
| `collision` | tile | Walkability grid (invisible at runtime, consumed by backend) |

The existing tileset image (`town-tileset.png`, 6176 tiles) stays unchanged. Layer reorganization moves tiles between layers in Tiled — no new art needed.

### Frontend Renderer

**New component: `TiledMapRenderer.tsx`** (replaces `PixiStaticMap.tsx`)

Responsibilities:
- Fetch and parse `town-map.tmj` on mount
- Load tileset PNG as a PixiJS `BaseTexture`
- For each tile layer, create a `CompositeTilemap` from `@pixi/tilemap@4.1.0`
- Render layers in correct order with agent container between objects and foreground

Render tree:

```
<Container>
  <CompositeTilemap />        ← ground layer
  <CompositeTilemap />        ← objects layer
  <Container                  ← water tiles (subset of ground)
    filters={[displacementFilter]}
  />
  <Container sortableChildren={true}>  ← agents
    <Character zIndex={y} />
    <Character zIndex={y} />
  </Container>
  <CompositeTilemap />        ← foreground layer
</Container>
```

**TMJ parsing:** The TMJ format is JSON. Each tile layer has a `data` array of GIDs (global tile IDs) in row-major order (index = `y * width + x`). GID 0 = empty. GID > 0 maps to a tile in the tileset via `firstgid` offset.

**GID bit masking:** TMJ GIDs can encode flip/rotation flags in the highest bits. Always mask with `0x1FFFFFFF` before tileset lookup to strip these flags. Even if the current map doesn't use flipping, this prevents subtle bugs if Tiled auto-applies flips later.

**Tile rendering:** For each non-zero GID, compute the source rectangle (u/v) in the tileset image. Create a single `Texture` from the full tileset `BaseTexture`, then use the `u`/`v`/`tileWidth`/`tileHeight` options on `CompositeTilemap.tile()` to select sub-rectangles: `tilemap.tile(tilesetTexture, x * 32, y * 32, { u, v, tileWidth: 32, tileHeight: 32 })`. Note: `CompositeTilemap.tile()` accepts `Texture` (not `BaseTexture`) as its first argument.

**`@pixi/react` integration:** `@pixi/react@7.1.0` does not have a built-in wrapper for `CompositeTilemap`. Create a custom `PixiComponent()` bridge (same pattern as the existing `PixiStaticMap` uses) to integrate `CompositeTilemap` into the React render tree.

**Library:** `@pixi/tilemap@4.1.0` — compatible with PixiJS 7 (built against `@pixi/core ^7.0.4`). Provides `CompositeTilemap` which handles batching tiles into efficient draw calls.

**Map dimensions:** `TiledMapRenderer` exposes parsed map dimensions (width, height, tileDim) upward via a callback prop so `PixiGame.tsx` can use them for viewport setup, zoom bounds, and label positioning.

### Y-Sorting

Set `zIndex = y` on each `Character` container. The parent container has `sortableChildren={true}`. PixiJS sorts children by `zIndex` each frame. Agents lower on screen render in front of agents higher on screen.

No changes to `Character.tsx` needed beyond passing `zIndex`.

### Collision (Backend)

**Replace `city_map.json`** with direct TMJ parsing in the backend.

`town_pathfinding.py` changes:
- Parse `town-map.tmj` (JSON) instead of `city_map.json`
- Find the `collision` layer by name
- Build walkability grid: GID 0 = walkable, GID > 0 = blocked (mask GIDs with `0x1FFFFFFF` first)
- **Axis transposition:** TMJ data is row-major (`index = y * width + x`), but the existing pathfinding uses `grid[x][y]`. Transpose when building the grid to maintain the `is_walkable(x, y)` contract.
- Cache the parsed grid (map doesn't change at runtime)

**TMJ file location:** Copy `town-map.tmj` to `backend/data/town-map.tmj` (replacing `city_map.json`). The file must be kept in sync when the map changes in Tiled — both `goosetown/public/assets/town-map.tmj` and `backend/data/town-map.tmj` are copies of the same export. (A symlink or build step could automate this, but manual copy is sufficient for now.)

Delete `backend/data/city_map.json`. The TMJ file becomes the single source of collision truth.

Collision layer audit in Tiled:
- Fountain center: mark as blocked (currently walkable)
- Water edges: mark as blocked
- Open up road areas to reduce the 61% blocked ratio

### WorldMap Type and API Changes

The current `WorldMap` TypeScript interface carries `bgTiles`, `objectTiles`, and `animatedSprites` — all tile data sent from the backend via `_load_map_data()` in `routers/town.py`. With TMJ loading on the frontend, the backend no longer needs to send tile data.

**`WorldMap` interface** (in `src/types/town.ts`): Simplify to metadata only:
```typescript
export interface WorldMap {
  width: number;       // map width in tiles
  height: number;      // map height in tiles
  tileDim: number;     // tile size in pixels (32)
  tileSetUrl: string;  // URL to tileset PNG
  mapUrl: string;      // URL to town-map.tmj
}
```

Remove `bgTiles`, `objectTiles`, `tileSetDimX`, `tileSetDimY`, `animatedSprites`.

**Backend `_load_map_data()`** (in `routers/town.py`): Return only dimensions + URLs. Stop reading `city_map.json` tile arrays.

**`TownGameState`** (in `src/types/town.ts`): No structural change — still carries `worldMap`, but the type is simplified.

### Animated Tiles

Tiled supports tile animations natively: a tile can have a sequence of frames with durations defined in the tileset.

For water and fountain:
- Define animated tiles in the tileset (e.g., water tile cycles through 3-4 frames at 200ms intervals)
- TMJ exports animation data in `tileset.tiles[].animation[]` (array of `{tileid, duration}`)
- Frontend reads animation data and animates tiles. `CompositeTilemap` has built-in animation support via `animX`/`animY`/`animCountX`/`animCountY`/`animDivisor` options on the `tile()` call — prefer this over manual texture swapping for performance (avoids rebuilding the tilemap batch each frame). Fall back to ticker-based swapping only if the built-in system is too limited for the desired effect.

### Displacement Filter Scoping

Currently the displacement filter applies to the entire map (wavy effect everywhere). Scope it to water tiles only:
- During TMJ parsing, identify water tiles (by tile ID or a custom property in Tiled)
- Render water tiles into a separate `Container`
- Apply the displacement filter only to that container
- All other tiles render without distortion

## Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `@pixi/tilemap` | `4.1.0` | Tile rendering for PixiJS 7 |
| `pixi.js` | `^7.2.4` | Existing (no change) |

## Files Changed

### Frontend (goosetown/)
| File | Action |
|------|--------|
| `src/components/TiledMapRenderer.tsx` | **New** — tile-based map renderer using `PixiComponent()` bridge for `CompositeTilemap` |
| `src/components/PixiStaticMap.tsx` | **Delete** — replaced by TiledMapRenderer |
| `src/components/PixiGame.tsx` | **Modify** — use TiledMapRenderer, add Y-sort container for agents, receive map dimensions via callback |
| `src/components/Player.tsx` | **Modify** — accept and set `zIndex` prop |
| `src/types/town.ts` | **Modify** — simplify `WorldMap` interface (remove `bgTiles`, `objectTiles`, `animatedSprites`; add `mapUrl`) |
| `src/hooks/useTownState.ts` | **Modify** — adapt to simplified `WorldMap` |
| `public/assets/town-map.tmj` | **New** — Tiled map exported as JSON |
| `package.json` | **Modify** — add `@pixi/tilemap@4.1.0` |

### Backend (backend/)
| File | Action |
|------|--------|
| `core/services/town_pathfinding.py` | **Modify** — parse TMJ collision layer instead of city_map.json, transpose to `grid[x][y]` |
| `routers/town.py` | **Modify** — simplify `_load_map_data()` to return metadata only (no tile arrays) |
| `data/town-map.tmj` | **New** — copy of TMJ for backend collision parsing |
| `data/city_map.json` | **Delete** — replaced by TMJ |

### Tiled Project (goosetown/)
| File | Action |
|------|--------|
| `town-map.tmx` | **Modify** — reorganize into ground/objects/foreground/collision layers |

## Priority Order

1. Multi-layer renderer with `@pixi/tilemap` (foundation)
2. Foreground layer in Tiled (fountain/trees/roofs above agents)
3. Y-sort agents (correct overlap between agents)
4. Collision: migrate from `city_map.json` to TMJ, audit blocked tiles
5. Animated water/fountain tiles
6. Scope displacement filter to water tiles only

## Out of Scope

- New tileset art (keep existing `town-tileset.png`)
- PixelLab tile generation (style mismatch confirmed)
- Isometric or hex maps (orthogonal only)
- Runtime map editing
