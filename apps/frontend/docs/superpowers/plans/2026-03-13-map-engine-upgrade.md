# GooseTown Map Engine Upgrade Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single-PNG map renderer with a tile-based multi-layer renderer that loads Tiled TMJ maps directly, enabling foreground occlusion, Y-sorted agents, animated tiles, and TMJ-derived collision.

**Architecture:** Frontend loads `town-map.tmj` via `fetch()`, renders tile layers using `@pixi/tilemap@4.1.0` `CompositeTilemap` wrapped in a `PixiComponent()` bridge. Backend pathfinding reads collision from the same TMJ file. Agents render between object and foreground layers with Y-sorting.

**Tech Stack:** PixiJS 7, @pixi/tilemap 4.1.0, @pixi/react 7.1.0, Tiled TMJ format, Python (FastAPI backend)

**Spec:** `docs/superpowers/specs/2026-03-13-map-engine-upgrade-design.md`

---

## File Structure

### New Files
| File | Responsibility |
|------|---------------|
| `src/lib/tmjParser.ts` | Parse TMJ JSON, extract layers, compute tile source rects from tileset |
| `src/components/TiledMapRenderer.tsx` | `PixiComponent` bridge rendering `CompositeTilemap` per layer |
| `public/assets/town-map.tmj` | Tiled map exported as JSON (single source of truth) |
| `backend/data/town-map.tmj` | Copy of TMJ for backend collision parsing |

### Modified Files
| File | Changes |
|------|---------|
| `package.json` | Add `@pixi/tilemap@4.1.0` |
| `src/components/PixiGame.tsx` | Replace `PixiStaticMap` with `TiledMapRenderer`, add Y-sort container |
| `src/components/Player.tsx` | Accept and set `zIndex` prop |
| `src/types/town.ts` | Simplify `WorldMap` (remove tile arrays, add `mapUrl`) |
| `src/hooks/useTownState.ts` | Adapt to simplified `WorldMap` |
| `backend/core/services/town_pathfinding.py` | Parse TMJ collision layer instead of `city_map.json` |
| `backend/routers/town.py` | Simplify `_load_map_data()` to return metadata only |
| `town-map.tmx` | Reorganize into ground/objects/foreground/collision layers |

### Deleted Files
| File | Reason |
|------|--------|
| `src/components/PixiStaticMap.tsx` | Replaced by `TiledMapRenderer` |
| `backend/data/city_map.json` | Replaced by `town-map.tmj` |

---

## Chunk 1: Foundation — Tile Rendering with Existing Map

Get the tile renderer working with the current single-layer map before adding complexity.

### Task 1: Install @pixi/tilemap and Export TMJ

**Files:**
- Modify: `package.json`
- Create: `public/assets/town-map.tmj`

- [ ] **Step 1: Install @pixi/tilemap**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/freebird/goosetown
npm install @pixi/tilemap@4.1.0
```

Verify: `node -e "require('@pixi/tilemap')"` should not error.

- [ ] **Step 2: Convert TMX to TMJ**

Use the Tiled MCP server's `convert_format` tool to convert `town-map.tmx` to `town-map.tmj`:

```
mcp__tiled__convert_format(
  source_path="town-map.tmx",
  target_format="json"
)
```

This creates `town-map.tmj` in the project root. Copy to public assets:

```bash
cp /Users/prasiddhaparthsarthy/Desktop/freebird/goosetown/town-map.tmj \
   /Users/prasiddhaparthsarthy/Desktop/freebird/goosetown/public/assets/town-map.tmj
```

- [ ] **Step 3: Verify TMJ structure**

Read `public/assets/town-map.tmj` and confirm:
- `width: 96`, `height: 64`, `tilewidth: 32`, `tileheight: 32`
- `layers` array contains `background` and `collision` layers
- Each layer has `data` array of length 6144 (96 × 64)
- `tilesets` array has entries with `firstgid` values

- [ ] **Step 4: Commit**

```bash
git add package.json package-lock.json public/assets/town-map.tmj
git commit -m "feat: add @pixi/tilemap and export TMJ map"
```

---

### Task 2: TMJ Parser Utility

**Files:**
- Create: `src/lib/tmjParser.ts`

- [ ] **Step 0: Create src/lib directory**

```bash
mkdir -p /Users/prasiddhaparthsarthy/Desktop/freebird/goosetown/src/lib
```

- [ ] **Step 1: Create TMJ parser**

```typescript
// src/lib/tmjParser.ts

/** Parsed tile layer from TMJ */
export interface TmjTileLayer {
  name: string;
  data: number[];
  width: number;
  height: number;
  visible: boolean;
}

/** Tileset reference from TMJ */
export interface TmjTileset {
  firstgid: number;
  name: string;
  tilewidth: number;
  tileheight: number;
  tilecount: number;
  columns: number;
  image: string;
  imagewidth: number;
  imageheight: number;
}

/** Parsed TMJ map */
export interface TmjMap {
  width: number;
  height: number;
  tilewidth: number;
  tileheight: number;
  layers: TmjTileLayer[];
  tilesets: TmjTileset[];
}

/** GID bit flags for tile flipping */
const FLIPPED_HORIZONTALLY_FLAG = 0x80000000;
const FLIPPED_VERTICALLY_FLAG = 0x40000000;
const FLIPPED_DIAGONALLY_FLAG = 0x20000000;
const GID_MASK = 0x1fffffff;

/** Strip flip/rotation bits from a GID to get the actual tile ID */
export function cleanGid(rawGid: number): number {
  return rawGid & GID_MASK;
}

/**
 * Parse a TMJ (Tiled JSON) file into typed structures.
 * Filters to tile layers only (ignores object/image layers).
 */
export function parseTmj(json: unknown): TmjMap {
  const raw = json as Record<string, unknown>;
  const width = raw.width as number;
  const height = raw.height as number;
  const tilewidth = raw.tilewidth as number;
  const tileheight = raw.tileheight as number;

  const rawLayers = raw.layers as Record<string, unknown>[];
  const layers: TmjTileLayer[] = rawLayers
    .filter((l) => l.type === 'tilelayer')
    .map((l) => ({
      name: l.name as string,
      data: l.data as number[],
      width: l.width as number,
      height: l.height as number,
      visible: l.visible !== false,
    }));

  // NOTE: Tiled can export tilesets as external references ({firstgid, source: "file.tsj"}).
  // This parser requires EMBEDDED tilesets. When exporting TMJ from Tiled, check
  // "Embed tilesets" in the export dialog, or use File → Export As with JSON format.
  // If a tileset has a `source` field instead of inline data, it is external and will
  // cause undefined values below.
  const rawTilesets = raw.tilesets as Record<string, unknown>[];
  const tilesets: TmjTileset[] = rawTilesets
    .filter((ts) => !ts.source) // Skip external tileset references (not supported)
    .map((ts) => ({
      firstgid: ts.firstgid as number,
      name: (ts.name as string) ?? '',
      tilewidth: (ts.tilewidth as number) ?? 32,
      tileheight: (ts.tileheight as number) ?? 32,
      tilecount: (ts.tilecount as number) ?? 0,
      columns: (ts.columns as number) ?? 1,
      image: (ts.image as string) ?? '',
      imagewidth: (ts.imagewidth as number) ?? 0,
      imageheight: (ts.imageheight as number) ?? 0,
    }));

  return { width, height, tilewidth, tileheight, layers, tilesets };
}

/**
 * Get the layer with the given name from a parsed TMJ map.
 */
export function getLayer(map: TmjMap, name: string): TmjTileLayer | undefined {
  return map.layers.find((l) => l.name === name);
}

/**
 * For a given GID, find which tileset it belongs to and compute
 * the source rectangle (u, v) in the tileset image.
 * Returns null for GID 0 (empty tile).
 */
export function getTileSourceRect(
  gid: number,
  tilesets: TmjTileset[],
): { tilesetIndex: number; u: number; v: number } | null {
  const cleanedGid = cleanGid(gid);
  if (cleanedGid === 0) return null;

  // Find the tileset this GID belongs to (highest firstgid <= cleanedGid)
  let tilesetIndex = 0;
  for (let i = tilesets.length - 1; i >= 0; i--) {
    if (tilesets[i].firstgid <= cleanedGid) {
      tilesetIndex = i;
      break;
    }
  }

  const tileset = tilesets[tilesetIndex];
  const localId = cleanedGid - tileset.firstgid;
  const col = localId % tileset.columns;
  const row = Math.floor(localId / tileset.columns);
  const u = col * tileset.tilewidth;
  const v = row * tileset.tileheight;

  return { tilesetIndex, u, v };
}
```

- [ ] **Step 2: Commit**

```bash
git add src/lib/tmjParser.ts
git commit -m "feat: add TMJ parser utility"
```

---

### Task 3: TiledMapRenderer Component

**Files:**
- Create: `src/components/TiledMapRenderer.tsx`

This is the core new component. It uses `PixiComponent()` to bridge `CompositeTilemap` into React, same pattern as `PixiStaticMap`.

- [ ] **Step 1: Create TiledMapRenderer**

```typescript
// src/components/TiledMapRenderer.tsx
import { PixiComponent } from '@pixi/react';
import * as PIXI from 'pixi.js';
import { CompositeTilemap } from '@pixi/tilemap';
import { useState, useEffect } from 'react';
import { Container } from '@pixi/react';
import {
  parseTmj,
  getLayer,
  getTileSourceRect,
  type TmjMap,
  type TmjTileset,
} from '../lib/tmjParser';

/** Props for the PixiComponent tilemap bridge */
interface TilemapProps {
  tmjMap: TmjMap;
  layerName: string;
  tilesetTextures: PIXI.Texture[];
}

/**
 * PixiComponent bridge for CompositeTilemap.
 * Renders a single tile layer from a TMJ map.
 */
const TileLayer = PixiComponent<TilemapProps, CompositeTilemap>('TileLayer', {
  create() {
    return new CompositeTilemap();
  },
  applyProps(tilemap, oldProps, newProps) {
    const { tmjMap, layerName, tilesetTextures } = newProps;
    const layer = getLayer(tmjMap, layerName);
    if (!layer) return;

    tilemap.clear();

    const { width, height, tilewidth, tileheight } = tmjMap;

    for (let y = 0; y < height; y++) {
      for (let x = 0; x < width; x++) {
        const index = y * width + x;
        const rawGid = layer.data[index];
        if (!rawGid) continue;

        const rect = getTileSourceRect(rawGid, tmjMap.tilesets);
        if (!rect) continue;

        const texture = tilesetTextures[rect.tilesetIndex];
        if (!texture) continue;

        tilemap.tile(texture, x * tilewidth, y * tileheight, {
          u: rect.u,
          v: rect.v,
          tileWidth: tilewidth,
          tileHeight: tileheight,
        });
      }
    }
  },
});

/** Callback with parsed map dimensions */
export interface MapDimensions {
  widthTiles: number;
  heightTiles: number;
  tileDim: number;
  widthPx: number;
  heightPx: number;
}

interface TiledMapRendererProps {
  mapUrl: string;
  tilesetUrl: string;
  onMapLoaded?: (dims: MapDimensions) => void;
  /** Layer names to render, in order. Defaults to all visible tile layers. */
  layers?: string[];
  children?: React.ReactNode;
}

/**
 * Multi-layer tile map renderer.
 * Loads a TMJ map file and renders specified tile layers using CompositeTilemap.
 * Children are rendered between layers if `layers` is not specified,
 * or after all specified layers.
 */
export function TiledMapRenderer({
  mapUrl,
  tilesetUrl,
  onMapLoaded,
  layers: layerFilter,
  children,
}: TiledMapRendererProps) {
  const [tmjMap, setTmjMap] = useState<TmjMap | null>(null);
  const [tilesetTextures, setTilesetTextures] = useState<PIXI.Texture[]>([]);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      // Load TMJ
      const resp = await fetch(mapUrl);
      const json = await resp.json();
      const map = parseTmj(json);

      if (cancelled) return;
      setTmjMap(map);

      // Load tileset textures
      const textures: PIXI.Texture[] = [];
      for (const ts of map.tilesets) {
        // Resolve tileset image path relative to map
        // The TMJ tileset image path is relative to the TMJ file
        const baseTexture = PIXI.BaseTexture.from(tilesetUrl, {
          scaleMode: PIXI.SCALE_MODES.NEAREST,
        });
        textures.push(new PIXI.Texture(baseTexture));
      }

      if (cancelled) return;
      setTilesetTextures(textures);
      setReady(true);

      onMapLoaded?.({
        widthTiles: map.width,
        heightTiles: map.height,
        tileDim: map.tilewidth,
        widthPx: map.width * map.tilewidth,
        heightPx: map.height * map.tileheight,
      });
    }

    void load();
    return () => {
      cancelled = true;
    };
  }, [mapUrl, tilesetUrl]);

  if (!ready || !tmjMap) return null;

  // Determine which layers to render
  const visibleLayers = tmjMap.layers.filter(
    (l) => l.visible && l.name !== 'collision',
  );
  const layerNames = layerFilter ?? visibleLayers.map((l) => l.name);

  return (
    <Container>
      {layerNames.map((name) => (
        <TileLayer
          key={name}
          tmjMap={tmjMap}
          layerName={name}
          tilesetTextures={tilesetTextures}
        />
      ))}
      {children}
    </Container>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add src/components/TiledMapRenderer.tsx
git commit -m "feat: add TiledMapRenderer component with CompositeTilemap"
```

---

### Task 4: Wire TiledMapRenderer into PixiGame

**Files:**
- Modify: `src/components/PixiGame.tsx`
- Delete: `src/components/PixiStaticMap.tsx`

- [ ] **Step 1: Update PixiGame.tsx**

Replace the full file content. Key changes from the original:
- Import `TiledMapRenderer` instead of `PixiStaticMap`
- Add `useState` import and `mapDims` state
- `worldMap.width/height/tileDim` still come from backend (available immediately) for zoom/viewport/labels
- `TiledMapRenderer` renders tiles (async load), but viewport doesn't wait for it
- `onMapLoaded` callback is for future use (multi-layer needs it)

```typescript
import * as PIXI from 'pixi.js';
import { useApp, PixiComponent } from '@pixi/react';
import { Player } from './Player.tsx';
import { useEffect, useRef, useState } from 'react';
import { TiledMapRenderer, type MapDimensions } from './TiledMapRenderer.tsx';
import PixiViewport from './PixiViewport.tsx';
import type { TownGameState, TownPlayer } from '../types/town';

// Location labels to render on the map (hover-only)
const LOCATION_LABELS: { label: string; x: number; y: number }[] = [
  { label: 'Plaza', x: 40, y: 22 },
  { label: 'Library', x: 40, y: 11 },
  { label: 'Cafe', x: 10, y: 15 },
  { label: 'Activity Center', x: 65, y: 8 },
  { label: 'Residence', x: 69, y: 22 },
];

const HoverLabel = PixiComponent('HoverLabel', {
  // ... unchanged from current file (lines 18-72) ...
  // Keep the entire HoverLabel PixiComponent as-is
});

export const PixiGame = (props: {
  game: TownGameState;
  width: number;
  height: number;
  setSelectedPlayerId: (id?: string) => void;
  viewportRef: React.MutableRefObject<any>;
  lerpPlayers: () => TownPlayer[];
}) => {
  const pixiApp = useApp();
  const viewportRef = props.viewportRef;
  const { lerpPlayers } = props;
  const [mapDims, setMapDims] = useState<MapDimensions | null>(null);

  // Use backend WorldMap for immediate dimensions (zoom, viewport, labels).
  // These are available before the async TMJ load completes.
  const { width, height, tileDim } = props.game.worldMap;

  // Ctrl/Cmd + wheel = zoom
  useEffect(() => {
    const canvas = pixiApp.view as HTMLCanvasElement;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      if (!e.ctrlKey && !e.metaKey) return;
      const viewport = viewportRef.current;
      if (!viewport) return;
      const zoomFactor = e.deltaY > 0 ? 0.9 : 1.1;
      const fitScale = Math.min(
        props.width / (width * tileDim),
        props.height / (height * tileDim),
      );
      const newScale = Math.min(3.0, Math.max(fitScale, viewport.scale.x * zoomFactor));
      viewport.setZoom(newScale, true);
    };
    canvas.addEventListener('wheel', onWheel, { passive: false });
    return () => canvas.removeEventListener('wheel', onWheel);
  }, [pixiApp, props.width, props.height, width, height, tileDim]);

  // On first load, smoothly zoom into the Residence
  const hasAnimatedInitial = useRef(false);
  useEffect(() => {
    if (!viewportRef.current || hasAnimatedInitial.current) return;
    hasAnimatedInitial.current = true;
    const focusX = 69 * tileDim;
    const focusY = 25 * tileDim;
    viewportRef.current.animate({
      position: new PIXI.Point(focusX, focusY),
      scale: 1.5,
      time: 1500,
      ease: 'easeInOutSine',
    });
  }, [width, height, tileDim]);

  const interpolatedPlayers = lerpPlayers();

  return (
    <PixiViewport
      app={pixiApp}
      screenWidth={props.width}
      screenHeight={props.height}
      worldWidth={width * tileDim}
      worldHeight={height * tileDim}
      viewportRef={viewportRef}
    >
      <TiledMapRenderer
        mapUrl="/assets/town-map.tmj"
        tilesetUrl="/assets/town-tileset.png"
        onMapLoaded={setMapDims}
      />
      {LOCATION_LABELS.map((loc) => (
        <HoverLabel key={loc.label} label={loc.label} x={loc.x} y={loc.y} tileDim={tileDim} />
      ))}
      {interpolatedPlayers.map((p) => (
        <Player
          key={`player-${p.id}`}
          game={props.game}
          player={p}
          onClick={(id) => props.setSelectedPlayerId(id)}
          tileDim={tileDim}
        />
      ))}
    </PixiViewport>
  );
};
export default PixiGame;
```

**Why no null-guard on mapDims:** The viewport dimensions and zoom animation use `worldMap.width/height/tileDim` from the backend (available immediately). The tile renderer loads async but the viewport, labels, and agents render immediately using backend dimensions. This avoids any blank flash.

- [ ] **Step 2: Delete PixiStaticMap.tsx**

```bash
rm src/components/PixiStaticMap.tsx
```

- [ ] **Step 3: Verify it renders**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/freebird/goosetown
npm run dev
```

Open `http://localhost:5173` — the map should render with tiles instead of a single PNG. Agents should still appear on top. Visual appearance should be identical to before (same tiles, same positions).

- [ ] **Step 4: Commit**

```bash
git add src/components/PixiGame.tsx
git rm src/components/PixiStaticMap.tsx
git commit -m "feat: replace PixiStaticMap with TiledMapRenderer"
```

---

### Task 5: Simplify WorldMap Type and Backend

**Files:**
- Modify: `src/types/town.ts`
- Modify: `src/hooks/useTownState.ts`
- Modify: `backend/routers/town.py`

- [ ] **Step 1: Simplify WorldMap interface**

In `src/types/town.ts`, replace the `WorldMap` interface:

```typescript
export interface WorldMap {
  width: number;
  height: number;
  tileDim: number;
  tileSetUrl: string;
  mapUrl: string;
}
```

Remove the `AnimatedSprite` interface (now orphaned).

- [ ] **Step 2: Update useTownState.ts**

Check if `useTownState.ts` directly references any removed fields (`bgTiles`, `objectTiles`, `animatedSprites`, `tileSetDimX`, `tileSetDimY`). It likely passes `worldMap` through unmodified (receives from backend, sets on state), so this step may be a no-op. The `WorldMap` type change in Step 1 will cause TypeScript errors if any code accesses removed fields — fix those if they appear. The hook should tolerate extra fields from the old backend gracefully (TypeScript doesn't enforce missing extra fields at runtime).

- [ ] **Step 3: Update backend _load_map_data()**

**Deployment ordering:** The frontend change (simplified `WorldMap` type) is safe to deploy first — the frontend simply ignores extra fields (`bgTiles`, etc.) from the old backend. The backend change (removing tile arrays) must deploy AFTER the frontend, or simultaneously. Otherwise the old frontend will receive a `WorldMap` missing `bgTiles` and break.

In `backend/routers/town.py`, simplify `_load_map_data()`:

```python
_map_data: dict | None = None

def _load_map_data() -> dict:
    global _map_data
    if _map_data is not None:
        return _map_data

    _map_data = {
        "width": 96,
        "height": 64,
        "tileDim": 32,
        "tileSetUrl": "/assets/town-tileset.png",
        "mapUrl": "/assets/town-map.tmj",
    }
    return _map_data
```

Remove the entire `city_map.json` loading logic — the `json.load()` call, the field remapping (`bgtiles` → `bgTiles`, `objmap` → `objectTiles`, etc.), and the fallback default map dict. Delete all dead code related to tile arrays. The map dimensions are constants (96×64, 32px tiles).

- [ ] **Step 4: Fix any TypeScript errors**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/freebird/goosetown
npx tsc --noEmit
```

Fix any type errors from the simplified `WorldMap`.

- [ ] **Step 5: Commit**

```bash
git add src/types/town.ts src/hooks/useTownState.ts
git commit -m "feat: simplify WorldMap type — tile data now loaded from TMJ"
```

```bash
cd /Users/prasiddhaparthsarthy/Desktop/freebird/backend
git add routers/town.py
git commit -m "feat: simplify _load_map_data — tile arrays removed, frontend loads TMJ directly"
```

---

## Chunk 2: Multi-Layer Rendering, Y-Sorting, and Collision

### Task 6: Reorganize Layers in Tiled

**Files:**
- Modify: `town-map.tmx` (via Tiled MCP)
- Update: `public/assets/town-map.tmj`

The existing `background` layer contains ALL visual tiles. We need to split it into `ground`, `objects`, and `foreground` layers. This is done using the Tiled MCP server.

- [ ] **Step 1: Add new layers via Tiled MCP**

Use `position` parameter to insert at correct indices from the start. Current layers: background(0), collision(1). Insert ground at 0, objects at 1, foreground at 2 — pushing background to 3 and collision to 4.

```
mcp__tiled__add_layer(filePath="town-map.tmx", layerName="ground", type="tilelayer", position=0)
mcp__tiled__add_layer(filePath="town-map.tmx", layerName="objects", type="tilelayer", position=1)
mcp__tiled__add_layer(filePath="town-map.tmx", layerName="foreground", type="tilelayer", position=2)
```

After this, layer order is: ground(0), objects(1), foreground(2), background(3), collision(4).

- [ ] **Step 2: Copy background tiles to ground layer**

The `ground` layer starts as a copy of `background`. Use Tiled MCP to copy all tiles:

```
mcp__tiled__copy_region(
  filePath="town-map.tmx",
  sourceLayer="background",
  sourceX=0, sourceY=0,
  sourceWidth=96, sourceHeight=64,
  targetLayer="ground",
  targetX=0, targetY=0
)
```

- [ ] **Step 3: Identify foreground tiles**

Foreground tiles are those that should render above agents — tree canopies, rooftops, fountain top decorations. Use `mcp__tiled__get_tiles` and `mcp__tiled__search_tiles` to identify which tiles in the map represent these elements.

For each identified foreground region, move tiles from `ground` to `foreground`:
1. Read the tile GIDs from the `ground` layer in that region
2. Set those tiles on the `foreground` layer using `stamp` parameter
3. Clear those positions on the `ground` layer (fill with GID 0)

Example for a tree canopy region:
```
mcp__tiled__get_tiles(filePath="town-map.tmx", layerName="ground", x=10, y=5, width=3, height=2)
mcp__tiled__set_tiles(filePath="town-map.tmx", layerName="foreground", stamp={x:10, y:5, width:3, height:2, gids:[gid1,gid2,gid3,gid4,gid5,gid6]})
mcp__tiled__fill_region(filePath="town-map.tmx", layerName="ground", x=10, y=5, width=3, height=2, gid=0)
```

Repeat for all foreground elements (tree tops, building roofs, fountain top).

- [ ] **Step 5: Move object tiles from ground to objects layer**

Similarly, buildings, fences, benches — things rendered below agents but above ground — should move from `ground` to `objects`. Follow the same copy-and-clear pattern.

For the initial pass, keep it simple: only move obvious foreground elements (tree canopies). Objects layer can be populated incrementally.

- [ ] **Step 6: Remove old background layer**

```
mcp__tiled__remove_layer(filePath="town-map.tmx", layerName="background")
```

After removal, layer order is: ground(0), objects(1), foreground(2), collision(3). Then reorder collision to the end if needed:

```
mcp__tiled__reorder_layer(filePath="town-map.tmx", layerName="collision", newPosition=3)
```

- [ ] **Step 7: Re-export TMJ**

```
mcp__tiled__convert_format(sourceFile="town-map.tmx", targetFile="public/assets/town-map.tmj", overwrite=true)
```

- [ ] **Step 8: Verify and commit**

```bash
npm run dev  # Verify map still renders correctly
git add town-map.tmx public/assets/town-map.tmj
git commit -m "feat: reorganize Tiled layers — ground/objects/foreground/collision"
```

---

### Task 7: Multi-Layer Rendering in TiledMapRenderer

**Files:**
- Modify: `src/components/TiledMapRenderer.tsx`
- Modify: `src/components/PixiGame.tsx`

- [ ] **Step 1: Update PixiGame to use layered rendering**

Update the `TiledMapRenderer` usage in `PixiGame.tsx` to render agents between object and foreground layers. Use the `layers` prop to control render order:

```typescript
{/* Ground + objects layers */}
<TiledMapRenderer
  mapUrl="/assets/town-map.tmj"
  tilesetUrl="/assets/town-tileset.png"
  onMapLoaded={setMapDims}
  layers={['ground', 'objects']}
/>

{/* Agents render here (between objects and foreground) */}
<Container sortableChildren={true}>
  {players.map((p) => (
    <Player
      key={p.id}
      game={game}
      player={p}
      onClick={() => setSelectedPlayerId(p.id)}
      tileDim={mapDims?.tileDim ?? 32}
    />
  ))}
</Container>

{/* Foreground layer (renders above agents) */}
<TiledMapRenderer
  mapUrl="/assets/town-map.tmj"
  tilesetUrl="/assets/town-tileset.png"
  layers={['foreground']}
/>
```

Note: Two `TiledMapRenderer` instances share the same TMJ file — the parser should cache the fetch so it doesn't load twice. Add caching to the `load()` function:

```typescript
// In TiledMapRenderer.tsx, add module-level cache:
const tmjCache = new Map<string, TmjMap>();

// In the load() function:
if (tmjCache.has(mapUrl)) {
  const cached = tmjCache.get(mapUrl)!;
  setTmjMap(cached);
  // ... skip fetch, continue with textures
}
```

- [ ] **Step 2: Verify foreground occlusion**

```bash
npm run dev
```

Walk an agent behind a tree or building. The foreground layer tiles should render on top of the agent sprite.

- [ ] **Step 3: Commit**

```bash
git add src/components/TiledMapRenderer.tsx src/components/PixiGame.tsx
git commit -m "feat: multi-layer rendering — agents between objects and foreground"
```

---

### Task 8: Y-Sort Agents

**Files:**
- Modify: `src/components/Character.tsx`

- [ ] **Step 1: Add zIndex to Player**

The `Container` wrapping each `Player` already exists in `PixiGame.tsx` with `sortableChildren={true}`. Set `zIndex` on each `Character` based on the player's Y position:

In `Player.tsx`, add `zIndex` to the `Character` component's wrapping — but `Character` renders a `<Container>` which supports `zIndex`. The `Container` in `Character.tsx` at line 90 already has `x` and `y` props. Add `zIndex={y}`:

```typescript
// In Character.tsx, update the Container:
<Container
  x={x}
  y={y}
  zIndex={y}  // Add this line
  scale={scale}
  interactive={true}
  pointerdown={onClick}
  cursor="pointer"
>
```

This is the only change needed. The parent container in `PixiGame.tsx` already has `sortableChildren={true}` from Task 7.

- [ ] **Step 2: Verify Y-sorting**

```bash
npm run dev
```

When two agents cross paths, the one with a higher Y (lower on screen) should render in front of the one with a lower Y.

- [ ] **Step 3: Commit**

```bash
git add src/components/Character.tsx
git commit -m "feat: Y-sort agents — zIndex based on Y position"
```

---

### Task 9: Backend Collision from TMJ

**Files:**
- Create: `backend/data/town-map.tmj` (copy from frontend)
- Modify: `backend/core/services/town_pathfinding.py`
- Delete: `backend/data/city_map.json`

- [ ] **Step 1: Copy TMJ to backend**

```bash
cp /Users/prasiddhaparthsarthy/Desktop/freebird/goosetown/public/assets/town-map.tmj \
   /Users/prasiddhaparthsarthy/Desktop/freebird/backend/data/town-map.tmj
```

- [ ] **Step 2: Update town_pathfinding.py**

Replace `_load_objmap()` to parse the TMJ collision layer:

```python
import json
from pathlib import Path

GID_MASK = 0x1FFFFFFF

def _load_objmap() -> list[list[int]]:
    """Load walkability grid from TMJ collision layer.

    TMJ data is row-major (index = y * width + x).
    Returns grid indexed as grid[x][y] to match is_walkable(x, y) contract.
    0 = walkable, -1 = blocked.
    """
    tmj_path = Path(__file__).resolve().parent.parent.parent / "data" / "town-map.tmj"
    with open(tmj_path) as f:
        tmj = json.load(f)

    width = tmj["width"]
    height = tmj["height"]

    # Find collision layer
    collision_layer = None
    for layer in tmj["layers"]:
        if layer.get("type") == "tilelayer" and layer["name"] == "collision":
            collision_layer = layer
            break

    if collision_layer is None:
        raise ValueError("No 'collision' layer found in TMJ")

    data = collision_layer["data"]

    # Build grid[x][y] — transpose from row-major TMJ data
    grid: list[list[int]] = []
    for x in range(width):
        col: list[int] = []
        for y in range(height):
            raw_gid = data[y * width + x]
            gid = raw_gid & GID_MASK
            # GID 0 = walkable, any GID > 0 = blocked
            col.append(0 if gid == 0 else -1)
        grid.append(col)

    return grid
```

**Preserve the public API unchanged:** `get_objmap()`, `get_apartment_objmap()`, `is_walkable()`, `find_path()`, and `_nearest_walkable()` must remain exactly as they are — they already use `objmap[x][y]` indexing. Only `_load_objmap()` changes.

- [ ] **Step 3: Run backend tests**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/freebird/backend
source .venv/bin/activate
python -m pytest tests/ -v -k "pathfinding or town" --tb=short
```

Fix any failures. The pathfinding tests may need updating if they mock `city_map.json` loading.

- [ ] **Step 4: Delete city_map.json**

```bash
rm /Users/prasiddhaparthsarthy/Desktop/freebird/backend/data/city_map.json
```

- [ ] **Step 5: Verify pathfinding still works**

```bash
python -c "
from core.services.town_pathfinding import find_path, is_walkable
print('Walkable at (50, 30):', is_walkable(50, 30))
print('Path from (50,30) to (60,30):', find_path(50, 30, 60, 30))
"
```

- [ ] **Step 6: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/freebird/backend
git add data/town-map.tmj core/services/town_pathfinding.py
git rm data/city_map.json
git commit -m "feat: collision from TMJ — replace city_map.json with town-map.tmj parsing"
```

---

### Task 10: Collision Audit in Tiled

**Files:**
- Modify: `town-map.tmx` (via Tiled MCP)

- [ ] **Step 1: Audit fountain collision**

The fountain area (approximately tiles 44-50, 20-24) is currently walkable. Mark it as blocked:

```
mcp__tiled__fill_region(
  filePath="town-map.tmx",
  layerName="collision",
  x=44, y=20, width=7, height=5,
  gid=6177
)
```

- [ ] **Step 2: Audit water edges**

Check water tile areas and ensure collision layer blocks them. Use `mcp__tiled__get_tiles` to inspect current collision state around water, then `mcp__tiled__set_tiles` or `mcp__tiled__fill_region` to fix gaps.

```
mcp__tiled__get_tiles(filePath="town-map.tmx", layerName="collision", x=0, y=0, width=96, height=10)
```

- [ ] **Step 3: Open up road areas**

Use `mcp__tiled__get_layer_stats` to check the current blocked ratio. Identify road tiles that are unnecessarily blocked and clear them (fill with `gid=0`).

- [ ] **Step 4: Re-export TMJ and sync**

**Note:** After updating the TMJ, restart the backend to clear the cached `_objmap` singleton.

```
mcp__tiled__convert_format(sourceFile="town-map.tmx", targetFile="public/assets/town-map.tmj", overwrite=true)
```

```bash
cp /Users/prasiddhaparthsarthy/Desktop/freebird/goosetown/public/assets/town-map.tmj \
   /Users/prasiddhaparthsarthy/Desktop/freebird/backend/data/town-map.tmj
```

- [ ] **Step 5: Verify pathfinding**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/freebird/backend
python -c "
from core.services.town_pathfinding import get_objmap
objmap = get_objmap()
total = sum(1 for x in range(96) for y in range(64))
blocked = sum(1 for x in range(96) for y in range(64) if objmap[x][y] == -1)
print(f'Blocked: {blocked}/{total} ({blocked*100//total}%)')
"
```

Target: reduce from 61% blocked to ~40-50% blocked.

- [ ] **Step 6: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/freebird/goosetown
git add town-map.tmx public/assets/town-map.tmj
git commit -m "fix: audit collision — block fountain/water, open up roads"
```

```bash
cd /Users/prasiddhaparthsarthy/Desktop/freebird/backend
git add data/town-map.tmj
git commit -m "fix: sync TMJ collision audit from Tiled"
```

---

## Chunk 3: Polish — Animated Tiles and Displacement Filter

### Task 11: Animated Water/Fountain Tiles

**Files:**
- Modify: `town-map.tmx` (tileset animation definitions — must be done in Tiled GUI)
- Modify: `src/components/TiledMapRenderer.tsx`
- Modify: `src/lib/tmjParser.ts`

- [ ] **Step 1: Identify water tile IDs**

Use `mcp__tiled__get_tile_info` to find which tile IDs represent water in the tileset. Search the ground layer for repeating tiles in water areas (e.g., the river/pond regions of the map):

```
mcp__tiled__get_tiles(filePath="town-map.tmx", layerName="ground", x=0, y=55, width=20, height=9)
```

Note the GIDs that appear in water areas. These are the tiles that need animation frames.

- [ ] **Step 2: Define tile animations in Tiled GUI**

Open `town-map.tmx` in the Tiled editor GUI. The MCP server cannot edit tileset animation properties — this must be done manually:

1. Open the tileset in the Tileset Editor (View → Tilesets → click "town-tileset")
2. Select a water tile
3. Open the Animation Editor (Tileset → Tile Animation Editor)
4. Add 3-4 frames using nearby similar water tiles at ~200ms per frame
5. **IMPORTANT:** Ensure animation frames are laid out **sequentially** (horizontally adjacent) in the tileset image. If they're not sequential, the `CompositeTilemap` built-in animation won't work and a ticker-based fallback is needed.
6. Repeat for each distinct water tile variant
7. Save the tileset

- [ ] **Step 3: Parse animation data from TMJ**

Add animation parsing to `tmjParser.ts`. Extend `TmjTileset` to include the `tiles` array from TMJ:

```typescript
/** Tile animation frame from TMJ tileset */
export interface TmjTileAnimFrame {
  tileid: number;
  duration: number;
}

/** Extended tileset info including per-tile data */
export interface TmjTileData {
  id: number;
  animation?: TmjTileAnimFrame[];
  properties?: Array<{ name: string; type: string; value: unknown }>;
}

// Add to TmjTileset interface:
//   tiles?: TmjTileData[];

/**
 * Build a map of GID → animation frames from all tilesets.
 * Call once per map load, not per tile.
 */
export function buildAnimationMap(
  tilesets: TmjTileset[],
): Map<number, TmjTileAnimFrame[]> {
  const animations = new Map<number, TmjTileAnimFrame[]>();
  for (const ts of tilesets) {
    if (!ts.tiles) continue;
    for (const tile of ts.tiles) {
      if (tile.animation && tile.animation.length > 0) {
        const globalId = ts.firstgid + tile.id;
        animations.set(globalId, tile.animation);
      }
    }
  }
  return animations;
}
```

Update the `parseTmj` function to include `tiles` in the tileset parsing (add `tiles: (ts.tiles as TmjTileData[]) ?? []` to the tileset mapping).

- [ ] **Step 4: Add tileAnim ticker to TiledMapRenderer**

`CompositeTilemap` built-in animation requires advancing `tilemap.tileAnim[0]` each frame. Without this, animations don't play even if `animX`/`animCountX` are set.

In the `TileLayer` PixiComponent `create()`, add a ticker callback:

```typescript
const TileLayer = PixiComponent<TilemapProps, CompositeTilemap>('TileLayer', {
  create() {
    const tilemap = new CompositeTilemap();
    // Advance tile animation counter each frame
    const onTick = () => {
      if (tilemap.tileAnim) {
        tilemap.tileAnim[0] = (tilemap.tileAnim[0] || 0) + 1;
      }
    };
    PIXI.Ticker.shared.add(onTick);
    // Store ref for cleanup
    (tilemap as any).__onTick = onTick;
    return tilemap;
  },
  willUnmount(tilemap) {
    // Clean up ticker to prevent memory leaks
    const onTick = (tilemap as any).__onTick;
    if (onTick) {
      PIXI.Ticker.shared.remove(onTick);
    }
  },
  // ... applyProps unchanged
});
```

- [ ] **Step 5: Implement animated tile rendering in applyProps**

In the tile rendering loop, build the animation map once before the loop, then check each tile:

```typescript
applyProps(tilemap, oldProps, newProps) {
  const { tmjMap, layerName, tilesetTextures } = newProps;
  const layer = getLayer(tmjMap, layerName);
  if (!layer) return;

  tilemap.clear();

  const { width, height, tilewidth, tileheight } = tmjMap;
  // Build animation map ONCE, outside the tile loop
  const animMap = buildAnimationMap(tmjMap.tilesets);

  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const index = y * width + x;
      const rawGid = layer.data[index];
      if (!rawGid) continue;

      const cleanedGid = cleanGid(rawGid);
      const rect = getTileSourceRect(rawGid, tmjMap.tilesets);
      if (!rect) continue;

      const texture = tilesetTextures[rect.tilesetIndex];
      if (!texture) continue;

      // Check for animation
      const anim = animMap.get(cleanedGid);
      if (anim && anim.length > 1) {
        // Verify frames are sequential in the tileset (horizontally adjacent)
        const ts = tmjMap.tilesets[rect.tilesetIndex];
        const baseLocalId = cleanedGid - ts.firstgid;
        const sequential = anim.every((f, i) => f.tileid === baseLocalId + i);

        if (sequential) {
          tilemap.tile(texture, x * tilewidth, y * tileheight, {
            u: rect.u,
            v: rect.v,
            tileWidth: tilewidth,
            tileHeight: tileheight,
            animX: tilewidth,
            animCountX: anim.length,
            animDivisor: Math.round(anim[0].duration / 16.67),
          });
        } else {
          // Non-sequential fallback: render static first frame
          // (Full ticker-based frame swapping is complex and deferred)
          tilemap.tile(texture, x * tilewidth, y * tileheight, {
            u: rect.u,
            v: rect.v,
            tileWidth: tilewidth,
            tileHeight: tileheight,
          });
        }
      } else {
        // Static tile
        tilemap.tile(texture, x * tilewidth, y * tileheight, {
          u: rect.u,
          v: rect.v,
          tileWidth: tilewidth,
          tileHeight: tileheight,
        });
      }
    }
  }
},
```

- [ ] **Step 6: Re-export TMJ and verify**

```
mcp__tiled__convert_format(sourceFile="town-map.tmx", targetFile="public/assets/town-map.tmj", overwrite=true)
```

```bash
npm run dev
```

Water tiles should shimmer/animate. If frames are not sequential in the tileset, they will render as static tiles (acceptable for now — rearranging tileset frames is a future improvement).

- [ ] **Step 7: Commit**

```bash
git add src/lib/tmjParser.ts src/components/TiledMapRenderer.tsx
git add town-map.tmx public/assets/town-map.tmj
git commit -m "feat: animated water/fountain tiles from Tiled animation data"
```

---

### Task 12: Scope Displacement Filter to Water

**Files:**
- Modify: `src/components/TiledMapRenderer.tsx`
- Modify: `src/components/PixiGame.tsx`
- Modify: `src/lib/tmjParser.ts`

Water tile identification strategy: **hardcoded GID set**. The tileset doesn't change (per spec, we keep existing art). This avoids needing custom Tiled properties and extra parser work. Identify the water tile GIDs in Task 11 Step 1 and define them as a constant.

- [ ] **Step 1: Define water tile GID set**

In `src/lib/tmjParser.ts`, add:

```typescript
/**
 * Water tile GIDs (from town-tileset, firstgid=1).
 * Populated after identifying water tiles in Task 11 Step 1.
 * These tiles get the displacement filter.
 */
export const WATER_TILE_GIDS: Set<number> = new Set([
  // Fill in actual GIDs after inspecting the tileset
  // e.g., 42, 43, 44, 74, 75, 76, ...
]);

export function isWaterTile(gid: number): boolean {
  return WATER_TILE_GIDS.has(cleanGid(gid));
}
```

- [ ] **Step 2: Add tileFilter prop to TileLayer**

In `TiledMapRenderer.tsx`, add a `tileFilter` prop to `TilemapProps`:

```typescript
interface TilemapProps {
  tmjMap: TmjMap;
  layerName: string;
  tilesetTextures: PIXI.Texture[];
  /** If set, only render tiles where filter returns true. */
  tileFilter?: (gid: number) => boolean;
  /** If set, exclude tiles where filter returns true (inverse). */
  tileExclude?: (gid: number) => boolean;
}
```

In the `applyProps` tile loop, add the filter check:

```typescript
const cleanedGid = cleanGid(rawGid);

// Apply tile filter/exclude
if (newProps.tileFilter && !newProps.tileFilter(cleanedGid)) continue;
if (newProps.tileExclude && newProps.tileExclude(cleanedGid)) continue;
```

Pass these props through from `TiledMapRenderer` to `TileLayer`.

- [ ] **Step 3: Update PixiGame render tree**

Render ground twice — once excluding water, once including only water with the displacement filter:

```typescript
import { isWaterTile } from '../lib/tmjParser';

// In the render return:
<PixiViewport ...>
  {/* Ground layer (excluding water tiles) */}
  <TiledMapRenderer
    mapUrl="/assets/town-map.tmj"
    tilesetUrl="/assets/town-tileset.png"
    layers={['ground']}
    tileExclude={isWaterTile}
    onMapLoaded={setMapDims}
  />

  {/* Water tiles with displacement filter */}
  <Container filters={[displacementFilter]}>
    <TiledMapRenderer
      mapUrl="/assets/town-map.tmj"
      tilesetUrl="/assets/town-tileset.png"
      layers={['ground']}
      tileFilter={isWaterTile}
    />
  </Container>

  {/* Objects layer */}
  <TiledMapRenderer
    mapUrl="/assets/town-map.tmj"
    tilesetUrl="/assets/town-tileset.png"
    layers={['objects']}
  />

  {/* Location labels */}
  {LOCATION_LABELS.map((loc) => (
    <HoverLabel key={loc.label} ... />
  ))}

  {/* Agents (Y-sorted) */}
  <Container sortableChildren={true}>
    {interpolatedPlayers.map((p) => (
      <Player key={`player-${p.id}`} ... />
    ))}
  </Container>

  {/* Foreground layer (above agents) */}
  <TiledMapRenderer
    mapUrl="/assets/town-map.tmj"
    tilesetUrl="/assets/town-tileset.png"
    layers={['foreground']}
  />
</PixiViewport>
```

- [ ] **Step 4: Port displacement filter from old PixiStaticMap**

Add the displacement filter setup to `PixiGame.tsx` using `useEffect` with proper cleanup:

```typescript
const [displacementFilter, setDisplacementFilter] = useState<PIXI.DisplacementFilter | null>(null);

useEffect(() => {
  // Generate 128x128 noise canvas
  const canvas = document.createElement('canvas');
  canvas.width = 128;
  canvas.height = 128;
  const ctx = canvas.getContext('2d')!;
  const imageData = ctx.createImageData(128, 128);
  for (let i = 0; i < imageData.data.length; i += 4) {
    const v = Math.random() * 255;
    imageData.data[i] = v;
    imageData.data[i + 1] = v;
    imageData.data[i + 2] = v;
    imageData.data[i + 3] = 255;
  }
  ctx.putImageData(imageData, 0, 0);

  const noiseSprite = PIXI.Sprite.from(canvas);
  noiseSprite.texture.baseTexture.wrapMode = PIXI.WRAP_MODES.REPEAT;
  const filter = new PIXI.DisplacementFilter(noiseSprite, 4);

  // Animate displacement
  const onTick = () => {
    noiseSprite.x += 0.3;
    noiseSprite.y += 0.15;
  };
  PIXI.Ticker.shared.add(onTick);
  setDisplacementFilter(filter);

  return () => {
    PIXI.Ticker.shared.remove(onTick);
  };
}, []);
```

- [ ] **Step 5: Verify**

```bash
npm run dev
```

Only water tiles should have the wavy displacement effect. Ground, buildings, agents should render without distortion.

- [ ] **Step 6: Commit**

```bash
git add src/lib/tmjParser.ts src/components/TiledMapRenderer.tsx src/components/PixiGame.tsx
git commit -m "feat: scope displacement filter to water tiles only"
```

---

## Final Verification

- [ ] **Frontend renders correctly**: Map displays with ground/objects/foreground layers
- [ ] **Agents Y-sort**: Agents overlap correctly based on Y position
- [ ] **Foreground occlusion**: Agents walk behind tree canopies and rooftops
- [ ] **Collision works**: Agents cannot walk on water or through the fountain
- [ ] **Animated tiles**: Water tiles shimmer
- [ ] **Displacement filter**: Wavy effect only on water, not entire map
- [ ] **Backend pathfinding**: `find_path()` uses TMJ collision, no `city_map.json`
- [ ] **No regressions**: Agent sprites, speech bubbles, hover labels all work
