# OGA 16x16 JRPG Town Generator Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a TypeScript procedural town generator that outputs a Tiled-compatible TMJ map using the OGA 16x16 JRPG tileset.

**Architecture:** A pure-data pipeline in `src/town-gen/` with no rendering dependencies. The generator creates a zone map, fills tile layers, places buildings and props, generates collision metadata, and exports TMJ. The existing `TiledMapRenderer` consumes the output unchanged.

**Tech Stack:** TypeScript, Node (tsx for CLI), OGA 16x16 JRPG tileset, Tiled JSON format

**Spec:** `docs/superpowers/specs/2026-03-14-oga-town-generator-design.md`

---

## File Structure

| File | Purpose |
|------|---------|
| Create: `src/town-gen/schema.ts` | Type definitions for tilemap, layers, objects |
| Create: `src/town-gen/asset-registry.ts` | OGA tile ID mappings, terrain families, building kits, prop defs |
| Create: `src/town-gen/layout.ts` | Zone map generation (town composition phases) |
| Create: `src/town-gen/terrain.ts` | Ground tile placement with transitions |
| Create: `src/town-gen/buildings.ts` | Building kit stamping onto plots |
| Create: `src/town-gen/props.ts` | Prop placement rules |
| Create: `src/town-gen/collision.ts` | Collision rect generation from zones + buildings + props |
| Create: `src/town-gen/exporter.ts` | Serialize to Tiled-compatible TMJ JSON |
| Create: `src/town-gen/generator.ts` | Main pipeline: compose all modules, validate, export |
| Create: `src/town-gen/index.ts` | CLI entry point |
| Create: `src/town-gen/__tests__/layout.test.ts` | Layout zone map tests |
| Create: `src/town-gen/__tests__/terrain.test.ts` | Terrain placement tests |
| Create: `src/town-gen/__tests__/buildings.test.ts` | Building placement tests |
| Create: `src/town-gen/__tests__/exporter.test.ts` | TMJ export format tests |
| Download: `public/assets/tilesets/oga-jrpg-tileset.png` | OGA tileset image |
| Modify: `src/components/PixiGame.tsx:10-16` | Location labels + map URLs |
| Modify: `backend/routers/town.py:70-78` | Map metadata dimensions |
| Modify: `backend/core/services/town_pathfinding.py:24-50` | Tileset-agnostic collision |
| Modify: `backend/core/town_constants.py` | Location coordinates from manifest |

---

## Chunk 1: Schema, Asset Registry, and Tileset

### Task 1: Download OGA tileset and inspect layout

**Files:**
- Download: `public/assets/tilesets/oga-jrpg-tileset.png`

- [ ] **Step 1: Download the OGA 16x16 JRPG tileset**

Go to https://opengameart.org/content/oga-16x16-jrpg-sprites-tiles and download the tileset. Extract the town tileset PNG. Place it at `public/assets/tilesets/oga-jrpg-tileset.png`.

- [ ] **Step 2: Inspect the tileset grid**

Open the PNG in any image viewer. Map out the grid: count columns and rows, identify terrain tiles (water, grass, road, plaza), building parts (walls, roofs, doors, windows), and props (trees, benches, lamps). Record the sheet dimensions (width x height in pixels) and tile count.

- [ ] **Step 3: Commit the tileset asset**

```bash
git add public/assets/tilesets/oga-jrpg-tileset.png
git commit -m "asset: add OGA 16x16 JRPG tileset"
```

---

### Task 2: Schema types

**Files:**
- Create: `src/town-gen/schema.ts`
- Test: `src/town-gen/__tests__/exporter.test.ts` (later — schema is types-only)

- [ ] **Step 1: Create the schema file**

```typescript
// src/town-gen/schema.ts

// --- Tilemap structure (Tiled-compatible) ---

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

export interface TmjTileLayer {
  name: string;
  type: 'tilelayer';
  width: number;
  height: number;
  data: number[];
  visible: boolean;
  opacity: number;
  x: number;
  y: number;
}

export interface TmjObject {
  id: number;
  name: string;
  type: string;
  x: number;
  y: number;
  width: number;
  height: number;
  properties?: { name: string; type: string; value: unknown }[];
}

export interface TmjObjectLayer {
  name: string;
  type: 'objectgroup';
  objects: TmjObject[];
  visible: boolean;
  opacity: number;
  x: number;
  y: number;
}

export type TmjLayer = TmjTileLayer | TmjObjectLayer;

export interface TmjMap {
  width: number;
  height: number;
  tilewidth: number;
  tileheight: number;
  orientation: 'orthogonal';
  renderorder: 'right-down';
  tilesets: TmjTileset[];
  layers: TmjLayer[];
  type: 'map';
  version: '1.10';
  tiledversion: string;
}

// --- Generator internal types ---

export type Zone =
  | 'water'
  | 'shore'
  | 'grass'
  | 'road'
  | 'plaza'
  | 'canal'
  | 'dock'
  | 'park'
  | 'building'
  | 'bridge';

export type ZoneMap = Zone[][];

export interface TerrainFamily {
  primary: number;
  alternates: number[];
  weights?: number[];
}

export interface TransitionSet {
  n: number; s: number; e: number; w: number;
  ne: number; nw: number; se: number; sw: number;
  innerNE: number; innerNW: number; innerSE: number; innerSW: number;
}

export interface BuildingKit {
  name: string;
  class: 'residential' | 'commercial' | 'civic';
  footprint: { w: number; h: number };
  tiles: number[][];
  doorOffset: { x: number; y: number };
  collision: { x: number; y: number; w: number; h: number };
  foregroundRows: number;
}

export interface PropDef {
  name: string;
  footprint: { w: number; h: number };
  tiles: number[][];
  collision: 'none' | 'base' | 'full';
  groundLayer: string;
  foregroundLayer?: string;
  foregroundTiles?: number[][];
}

export interface PlacedBuilding {
  kit: BuildingKit;
  x: number;
  y: number;
}

export interface PlacedProp {
  def: PropDef;
  x: number;
  y: number;
}

export interface CollisionRect {
  x: number;
  y: number;
  width: number;
  height: number;
  source: string;
}

export interface SpawnPoint {
  name: string;
  x: number;
  y: number;
}

export interface TownManifest {
  seed: number;
  width: number;
  height: number;
  tileDim: number;
  spawns: SpawnPoint[];
  locations: { name: string; label: string; x: number; y: number }[];
}

export interface TownData {
  zoneMap: ZoneMap;
  buildings: PlacedBuilding[];
  props: PlacedProp[];
  collisions: CollisionRect[];
  spawns: SpawnPoint[];
  manifest: TownManifest;
}

// --- Seeded PRNG ---

export interface RNG {
  next(): number;           // 0..1
  nextInt(max: number): number; // 0..max-1
  pick<T>(arr: T[]): T;
  weightedPick(ids: number[], weights?: number[]): number;
}
```

- [ ] **Step 2: Verify typecheck passes**

```bash
cd goosetown && npx tsc --noEmit
```

- [ ] **Step 3: Commit**

```bash
git add src/town-gen/schema.ts
git commit -m "feat(town-gen): add tilemap schema types"
```

---

### Task 3: Asset registry

**Files:**
- Create: `src/town-gen/asset-registry.ts`

This task depends on Task 1 (tileset inspection). The tile IDs below are placeholders — replace with actual IDs from the OGA tileset after inspecting the sheet.

- [ ] **Step 1: Create the asset registry**

```typescript
// src/town-gen/asset-registry.ts

import type { TerrainFamily, TransitionSet, BuildingKit, PropDef } from './schema';

// -------------------------------------------------------
// Tileset metadata (update after inspecting OGA sheet)
// -------------------------------------------------------
export const TILESET = {
  name: 'oga_jrpg',
  image: 'assets/tilesets/oga-jrpg-tileset.png',
  tilewidth: 16,
  tileheight: 16,
  columns: 0,      // SET after inspecting sheet
  imagewidth: 0,    // SET after inspecting sheet
  imageheight: 0,   // SET after inspecting sheet
  tilecount: 0,     // SET after inspecting sheet
  firstgid: 1,
} as const;

// -------------------------------------------------------
// Terrain families — each has primary + alternates
// Tile IDs are LOCAL (add firstgid for GID)
// -------------------------------------------------------
export const TERRAIN: Record<string, TerrainFamily> = {
  water:      { primary: 0,  alternates: [1, 2] },
  grass:      { primary: 0,  alternates: [0, 0, 0] },   // PLACEHOLDER
  road:       { primary: 0,  alternates: [0, 0] },       // PLACEHOLDER
  plaza:      { primary: 0,  alternates: [0, 0, 0] },    // PLACEHOLDER
  dock:       { primary: 0,  alternates: [0] },           // PLACEHOLDER
  canal_wall: { primary: 0,  alternates: [] },            // PLACEHOLDER
  shore:      { primary: 0,  alternates: [] },            // PLACEHOLDER
};

// -------------------------------------------------------
// Transition sets — edge/corner tiles for terrain pairs
// -------------------------------------------------------
export const TRANSITIONS: Record<string, TransitionSet> = {
  // grass_to_water, grass_to_road, etc.
  // PLACEHOLDER — populate from tileset inspection
  grass_to_water: {
    n: 0, s: 0, e: 0, w: 0,
    ne: 0, nw: 0, se: 0, sw: 0,
    innerNE: 0, innerNW: 0, innerSE: 0, innerSW: 0,
  },
  grass_to_road: {
    n: 0, s: 0, e: 0, w: 0,
    ne: 0, nw: 0, se: 0, sw: 0,
    innerNE: 0, innerNW: 0, innerSE: 0, innerSW: 0,
  },
  grass_to_plaza: {
    n: 0, s: 0, e: 0, w: 0,
    ne: 0, nw: 0, se: 0, sw: 0,
    innerNE: 0, innerNW: 0, innerSE: 0, innerSW: 0,
  },
  water_to_canal_wall: {
    n: 0, s: 0, e: 0, w: 0,
    ne: 0, nw: 0, se: 0, sw: 0,
    innerNE: 0, innerNW: 0, innerSE: 0, innerSW: 0,
  },
  water_to_dock: {
    n: 0, s: 0, e: 0, w: 0,
    ne: 0, nw: 0, se: 0, sw: 0,
    innerNE: 0, innerNW: 0, innerSE: 0, innerSW: 0,
  },
};

// -------------------------------------------------------
// Building kits
// -------------------------------------------------------
export const BUILDINGS: BuildingKit[] = [
  {
    name: 'house_small',
    class: 'residential',
    footprint: { w: 3, h: 3 },
    tiles: [
      [0, 0, 0],  // roof row → foreground
      [0, 0, 0],  // wall row
      [0, 0, 0],  // base row with door
    ],
    doorOffset: { x: 1, y: 2 },
    collision: { x: 0, y: 1, w: 3, h: 2 },
    foregroundRows: 1,
  },
  {
    name: 'house_medium',
    class: 'residential',
    footprint: { w: 4, h: 4 },
    tiles: [
      [0, 0, 0, 0],
      [0, 0, 0, 0],
      [0, 0, 0, 0],
      [0, 0, 0, 0],
    ],
    doorOffset: { x: 1, y: 3 },
    collision: { x: 0, y: 1, w: 4, h: 3 },
    foregroundRows: 1,
  },
  {
    name: 'shop',
    class: 'commercial',
    footprint: { w: 4, h: 3 },
    tiles: [
      [0, 0, 0, 0],
      [0, 0, 0, 0],
      [0, 0, 0, 0],
    ],
    doorOffset: { x: 1, y: 2 },
    collision: { x: 0, y: 1, w: 4, h: 2 },
    foregroundRows: 1,
  },
  {
    name: 'shop_wide',
    class: 'commercial',
    footprint: { w: 5, h: 3 },
    tiles: [
      [0, 0, 0, 0, 0],
      [0, 0, 0, 0, 0],
      [0, 0, 0, 0, 0],
    ],
    doorOffset: { x: 2, y: 2 },
    collision: { x: 0, y: 1, w: 5, h: 2 },
    foregroundRows: 1,
  },
  {
    name: 'civic',
    class: 'civic',
    footprint: { w: 6, h: 5 },
    tiles: [
      [0, 0, 0, 0, 0, 0],
      [0, 0, 0, 0, 0, 0],
      [0, 0, 0, 0, 0, 0],
      [0, 0, 0, 0, 0, 0],
      [0, 0, 0, 0, 0, 0],
    ],
    doorOffset: { x: 2, y: 4 },
    collision: { x: 0, y: 1, w: 6, h: 4 },
    foregroundRows: 1,
  },
  {
    name: 'inn',
    class: 'commercial',
    footprint: { w: 5, h: 4 },
    tiles: [
      [0, 0, 0, 0, 0],
      [0, 0, 0, 0, 0],
      [0, 0, 0, 0, 0],
      [0, 0, 0, 0, 0],
    ],
    doorOffset: { x: 2, y: 3 },
    collision: { x: 0, y: 1, w: 5, h: 3 },
    foregroundRows: 1,
  },
];

// -------------------------------------------------------
// Props
// -------------------------------------------------------
export const PROPS: Record<string, PropDef> = {
  fountain: {
    name: 'fountain',
    footprint: { w: 3, h: 3 },
    tiles: [[0,0,0],[0,0,0],[0,0,0]],
    collision: 'full',
    groundLayer: 'Props_Back',
    foregroundLayer: 'Foreground_Low',
    foregroundTiles: [[0,0,0]],  // top row
  },
  tree: {
    name: 'tree',
    footprint: { w: 2, h: 2 },
    tiles: [[0,0],[0,0]],
    collision: 'base',
    groundLayer: 'Props_Back',
    foregroundLayer: 'Foreground_High',
    foregroundTiles: [[0,0]],  // canopy
  },
  bench: {
    name: 'bench',
    footprint: { w: 2, h: 1 },
    tiles: [[0, 0]],
    collision: 'none',
    groundLayer: 'Props_Back',
  },
  lamp: {
    name: 'lamp',
    footprint: { w: 1, h: 2 },
    tiles: [[0],[0]],
    collision: 'base',
    groundLayer: 'Props_Back',
    foregroundLayer: 'Props_Front',
    foregroundTiles: [[0]],
  },
  bush: {
    name: 'bush',
    footprint: { w: 1, h: 1 },
    tiles: [[0]],
    collision: 'full',
    groundLayer: 'Props_Back',
  },
  sign: {
    name: 'sign',
    footprint: { w: 1, h: 2 },
    tiles: [[0],[0]],
    collision: 'none',
    groundLayer: 'Props_Back',
  },
  planter: {
    name: 'planter',
    footprint: { w: 1, h: 1 },
    tiles: [[0]],
    collision: 'full',
    groundLayer: 'Props_Back',
  },
};
```

NOTE: All tile IDs are `0` (placeholder). Task 1's tileset inspection will provide the real values. The structure and interfaces are what matter — IDs are filled during implementation.

- [ ] **Step 2: Verify typecheck passes**

```bash
npx tsc --noEmit
```

- [ ] **Step 3: Commit**

```bash
git add src/town-gen/asset-registry.ts
git commit -m "feat(town-gen): add asset registry with OGA tile mappings"
```

---

## Chunk 2: Layout and Terrain

### Task 4: Seeded PRNG utility

**Files:**
- Create: `src/town-gen/rng.ts`

- [ ] **Step 1: Create a simple seeded PRNG**

```typescript
// src/town-gen/rng.ts

import type { RNG } from './schema';

/** Mulberry32 — simple, fast, seedable 32-bit PRNG. */
export function createRNG(seed: number): RNG {
  let s = seed | 0;

  function next(): number {
    s |= 0;
    s = (s + 0x6d2b79f5) | 0;
    let t = Math.imul(s ^ (s >>> 15), 1 | s);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  }

  return {
    next,
    nextInt(max: number) {
      return Math.floor(next() * max);
    },
    pick<T>(arr: T[]): T {
      return arr[Math.floor(next() * arr.length)];
    },
    weightedPick(ids: number[], weights?: number[]): number {
      if (!weights || weights.length === 0) {
        return ids[Math.floor(next() * ids.length)];
      }
      const total = weights.reduce((a, b) => a + b, 0);
      let r = next() * total;
      for (let i = 0; i < ids.length; i++) {
        r -= weights[i];
        if (r <= 0) return ids[i];
      }
      return ids[ids.length - 1];
    },
  };
}
```

- [ ] **Step 2: Commit**

```bash
git add src/town-gen/rng.ts
git commit -m "feat(town-gen): add seeded PRNG utility"
```

---

### Task 5: Layout — zone map generation

**Files:**
- Create: `src/town-gen/layout.ts`
- Test: `src/town-gen/__tests__/layout.test.ts`

- [ ] **Step 1: Write the layout test**

```typescript
// src/town-gen/__tests__/layout.test.ts

import { generateZoneMap } from '../layout';
import { createRNG } from '../rng';
import type { Zone } from '../schema';

const W = 128;
const H = 96;

describe('generateZoneMap', () => {
  const rng = createRNG(42);
  const zones = generateZoneMap(W, H, rng);

  test('returns correct dimensions', () => {
    expect(zones.length).toBe(W);
    expect(zones[0].length).toBe(H);
  });

  test('borders are water', () => {
    for (let x = 0; x < W; x++) {
      expect(zones[x][0]).toBe('water');
      expect(zones[x][H - 1]).toBe('water');
    }
    for (let y = 0; y < H; y++) {
      expect(zones[0][y]).toBe('water');
      expect(zones[W - 1][y]).toBe('water');
    }
  });

  test('has a plaza zone', () => {
    let hasPlaza = false;
    for (let x = 0; x < W; x++) {
      for (let y = 0; y < H; y++) {
        if (zones[x][y] === 'plaza') hasPlaza = true;
      }
    }
    expect(hasPlaza).toBe(true);
  });

  test('has road zones', () => {
    let roadCount = 0;
    for (let x = 0; x < W; x++) {
      for (let y = 0; y < H; y++) {
        if (zones[x][y] === 'road') roadCount++;
      }
    }
    expect(roadCount).toBeGreaterThan(100);
  });

  test('has water canal', () => {
    // Canal runs roughly through middle third
    const midY = Math.floor(H / 2);
    let canalCount = 0;
    for (let x = 0; x < W; x++) {
      for (let y = midY - 10; y < midY + 10; y++) {
        if (zones[x][y] === 'canal') canalCount++;
      }
    }
    expect(canalCount).toBeGreaterThan(50);
  });

  test('has bridge zones crossing canal', () => {
    let hasBridge = false;
    for (let x = 0; x < W; x++) {
      for (let y = 0; y < H; y++) {
        if (zones[x][y] === 'bridge') hasBridge = true;
      }
    }
    expect(hasBridge).toBe(true);
  });

  test('deterministic — same seed same output', () => {
    const rng2 = createRNG(42);
    const zones2 = generateZoneMap(W, H, rng2);
    for (let x = 0; x < W; x++) {
      for (let y = 0; y < H; y++) {
        expect(zones2[x][y]).toBe(zones[x][y]);
      }
    }
  });
});
```

- [ ] **Step 2: Run test — verify it fails**

```bash
npm test -- --testPathPattern=layout
```

Expected: FAIL — `generateZoneMap` not found.

- [ ] **Step 3: Implement layout.ts**

```typescript
// src/town-gen/layout.ts

import type { Zone, ZoneMap, RNG } from './schema';

const MAP_W = 128;
const MAP_H = 96;

/** Generate the abstract zone map for the town. */
export function generateZoneMap(w: number, h: number, rng: RNG): ZoneMap {
  // Initialize all as water
  const zones: ZoneMap = Array.from({ length: w }, () =>
    Array.from({ length: h }, () => 'water' as Zone),
  );

  // Phase 1: Island shape — fill interior with grass
  fillIsland(zones, w, h, rng);

  // Phase 2: Canal
  const canalY = fillCanal(zones, w, h);

  // Phase 3: Main roads (cross shape)
  const { hRoadY, vRoadX, plazaBounds } = fillRoads(zones, w, h, canalY);

  // Phase 4: Plaza
  fillPlaza(zones, plazaBounds);

  // Phase 5: Secondary roads
  fillSecondaryRoads(zones, w, h, rng, hRoadY, vRoadX, canalY, plazaBounds);

  // Phase 6: Bridges over canal
  fillBridges(zones, w, h, vRoadX, canalY);

  // Phase 7: Dock
  fillDock(zones, w, h, rng);

  // Phase 8: Shore (buffer between water and land)
  fillShore(zones, w, h);

  // Phase 9: Parks
  fillParks(zones, w, h, rng);

  return zones;
}

function fillIsland(zones: ZoneMap, w: number, h: number, rng: RNG): void {
  const margin = 8;
  for (let x = margin; x < w - margin; x++) {
    for (let y = margin; y < h - margin; y++) {
      // Irregular edge: vary margin by ±2 using simple noise
      const edgeDist = Math.min(x - margin, w - margin - 1 - x, y - margin, h - margin - 1 - y);
      if (edgeDist < 2) {
        // Probabilistic edge — creates irregular coastline
        if (rng.next() < 0.3) continue; // stays water
      }
      zones[x][y] = 'grass';
    }
  }
}

function fillCanal(zones: ZoneMap, w: number, h: number): number {
  // Canal runs east-west through the middle third
  const canalY = Math.floor(h * 0.45);
  const canalWidth = 4;
  const margin = 10;

  for (let x = margin; x < w - margin; x++) {
    for (let dy = 0; dy < canalWidth; dy++) {
      const y = canalY + dy;
      if (y >= 0 && y < h) {
        zones[x][y] = 'canal';
      }
    }
    // Canal walls (one tile above and below)
    if (canalY - 1 >= 0 && zones[x][canalY - 1] !== 'water') {
      zones[x][canalY - 1] = 'road'; // Will become canal edge via terrain
    }
    if (canalY + canalWidth < h && zones[x][canalY + canalWidth] !== 'water') {
      zones[x][canalY + canalWidth] = 'road';
    }
  }

  return canalY;
}

function fillRoads(
  zones: ZoneMap,
  w: number,
  h: number,
  canalY: number,
): { hRoadY: number; vRoadX: number; plazaBounds: { x1: number; y1: number; x2: number; y2: number } } {
  const vRoadX = Math.floor(w / 2); // Vertical road at center
  const hRoadY = Math.floor(canalY / 2); // Horizontal road in upper half
  const roadW = 3;

  // Vertical road (full height, skipping canal — bridges handle that)
  for (let y = 8; y < h - 8; y++) {
    if (zones[vRoadX][y] === 'canal') continue;
    for (let dx = 0; dx < roadW; dx++) {
      const x = vRoadX - 1 + dx;
      if (x >= 0 && x < w && zones[x][y] !== 'canal') {
        zones[x][y] = 'road';
      }
    }
  }

  // Horizontal road (full width, upper half)
  for (let x = 10; x < w - 10; x++) {
    for (let dy = 0; dy < roadW; dy++) {
      const y = hRoadY - 1 + dy;
      if (y >= 0 && y < h && zones[x][y] !== 'canal') {
        zones[x][y] = 'road';
      }
    }
  }

  // Plaza at intersection
  const plazaW = 12;
  const plazaH = 10;
  const plazaBounds = {
    x1: vRoadX - Math.floor(plazaW / 2),
    y1: hRoadY - Math.floor(plazaH / 2),
    x2: vRoadX + Math.ceil(plazaW / 2),
    y2: hRoadY + Math.ceil(plazaH / 2),
  };

  return { hRoadY, vRoadX, plazaBounds };
}

function fillPlaza(
  zones: ZoneMap,
  bounds: { x1: number; y1: number; x2: number; y2: number },
): void {
  for (let x = bounds.x1; x < bounds.x2; x++) {
    for (let y = bounds.y1; y < bounds.y2; y++) {
      if (zones[x]?.[y] !== undefined && zones[x][y] !== 'water' && zones[x][y] !== 'canal') {
        zones[x][y] = 'plaza';
      }
    }
  }
}

function fillSecondaryRoads(
  zones: ZoneMap,
  w: number,
  h: number,
  rng: RNG,
  hRoadY: number,
  vRoadX: number,
  canalY: number,
  plazaBounds: { x1: number; y1: number; x2: number; y2: number },
): void {
  const roadW = 2;
  // Two horizontal side streets in upper half
  const sideStreetYs = [hRoadY - 12, hRoadY + 12].filter(
    (y) => y > 12 && y < canalY - 6,
  );
  for (const sy of sideStreetYs) {
    for (let x = 14; x < w - 14; x++) {
      for (let dy = 0; dy < roadW; dy++) {
        const y = sy + dy;
        if (zones[x]?.[y] && zones[x][y] === 'grass') {
          zones[x][y] = 'road';
        }
      }
    }
  }

  // Two vertical side streets in lower half (below canal)
  const lowerY = canalY + 6;
  const sideStreetXs = [vRoadX - 20, vRoadX + 20].filter(
    (x) => x > 14 && x < w - 14,
  );
  for (const sx of sideStreetXs) {
    for (let y = lowerY; y < h - 10; y++) {
      for (let dx = 0; dx < roadW; dx++) {
        const x = sx + dx;
        if (zones[x]?.[y] && zones[x][y] === 'grass') {
          zones[x][y] = 'road';
        }
      }
    }
  }
}

function fillBridges(
  zones: ZoneMap,
  w: number,
  h: number,
  vRoadX: number,
  canalY: number,
): void {
  const bridgeW = 3;
  // Bridge at main vertical road
  for (let dx = -1; dx < bridgeW - 1; dx++) {
    const x = vRoadX + dx;
    for (let y = canalY; y < canalY + 4; y++) {
      if (x >= 0 && x < w && y >= 0 && y < h) {
        zones[x][y] = 'bridge';
      }
    }
  }

  // Second bridge offset to the left
  const bridge2X = vRoadX - 25;
  if (bridge2X > 12) {
    for (let dx = 0; dx < bridgeW; dx++) {
      const x = bridge2X + dx;
      for (let y = canalY; y < canalY + 4; y++) {
        if (x >= 0 && x < w && y >= 0 && y < h) {
          zones[x][y] = 'bridge';
        }
      }
    }
  }
}

function fillDock(zones: ZoneMap, w: number, h: number, rng: RNG): void {
  // Dock along the south shore
  const dockStartX = Math.floor(w * 0.35);
  const dockEndX = Math.floor(w * 0.65);
  const dockY = h - 12;

  for (let x = dockStartX; x < dockEndX; x++) {
    for (let dy = 0; dy < 3; dy++) {
      const y = dockY + dy;
      if (y < h && (zones[x][y] === 'grass' || zones[x][y] === 'water' || zones[x][y] === 'shore')) {
        zones[x][y] = 'dock';
      }
    }
  }
}

function fillShore(zones: ZoneMap, w: number, h: number): void {
  // Shore = grass cells adjacent to water
  const toShore: [number, number][] = [];
  for (let x = 1; x < w - 1; x++) {
    for (let y = 1; y < h - 1; y++) {
      if (zones[x][y] !== 'grass') continue;
      const neighbors = [
        zones[x - 1][y], zones[x + 1][y],
        zones[x][y - 1], zones[x][y + 1],
      ];
      if (neighbors.some((n) => n === 'water' || n === 'canal')) {
        toShore.push([x, y]);
      }
    }
  }
  for (const [x, y] of toShore) {
    zones[x][y] = 'shore';
  }
}

function fillParks(zones: ZoneMap, w: number, h: number, rng: RNG): void {
  // Convert some grass patches near the plaza into park zones
  // Find grass clusters of 6x6+ and mark 1-2 as parks
  let parksPlaced = 0;
  for (let x = 16; x < w - 16 && parksPlaced < 2; x += 15) {
    for (let y = 16; y < h - 16 && parksPlaced < 2; y += 15) {
      // Check if a 6x6 area is all grass
      let allGrass = true;
      for (let dx = 0; dx < 6 && allGrass; dx++) {
        for (let dy = 0; dy < 6 && allGrass; dy++) {
          if (zones[x + dx]?.[y + dy] !== 'grass') allGrass = false;
        }
      }
      if (allGrass && rng.next() < 0.5) {
        for (let dx = 0; dx < 6; dx++) {
          for (let dy = 0; dy < 6; dy++) {
            zones[x + dx][y + dy] = 'park';
          }
        }
        parksPlaced++;
      }
    }
  }
}
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
npm test -- --testPathPattern=layout
```

- [ ] **Step 5: Commit**

```bash
git add src/town-gen/layout.ts src/town-gen/__tests__/layout.test.ts src/town-gen/rng.ts
git commit -m "feat(town-gen): layout module — zone map generation"
```

---

### Task 6: Terrain tile placement

**Files:**
- Create: `src/town-gen/terrain.ts`
- Test: `src/town-gen/__tests__/terrain.test.ts`

- [ ] **Step 1: Write terrain test**

```typescript
// src/town-gen/__tests__/terrain.test.ts

import { fillTerrainLayers } from '../terrain';
import { generateZoneMap } from '../layout';
import { createRNG } from '../rng';

const W = 128;
const H = 96;

describe('fillTerrainLayers', () => {
  const rng = createRNG(42);
  const zones = generateZoneMap(W, H, rng);
  const rng2 = createRNG(99);
  const layers = fillTerrainLayers(zones, W, H, rng2);

  test('returns Ground_Base layer with correct size', () => {
    expect(layers.Ground_Base.length).toBe(W * H);
  });

  test('returns Ground_Detail layer', () => {
    expect(layers.Ground_Detail.length).toBe(W * H);
  });

  test('returns Water_Back layer', () => {
    expect(layers.Water_Back.length).toBe(W * H);
  });

  test('returns Terrain_Structures layer', () => {
    expect(layers.Terrain_Structures.length).toBe(W * H);
  });

  test('water zones get water tile IDs (non-zero)', () => {
    // Find a water cell
    let found = false;
    for (let x = 0; x < W && !found; x++) {
      for (let y = 0; y < H && !found; y++) {
        if (zones[x][y] === 'water') {
          const gid = layers.Ground_Base[y * W + x];
          expect(gid).toBeGreaterThan(0);
          found = true;
        }
      }
    }
    expect(found).toBe(true);
  });

  test('no tile variant appears >3 times consecutively in a row', () => {
    const data = layers.Ground_Base;
    for (let y = 0; y < H; y++) {
      let streak = 1;
      for (let x = 1; x < W; x++) {
        const prev = data[y * W + (x - 1)];
        const curr = data[y * W + x];
        if (curr === prev && curr !== 0) {
          streak++;
          expect(streak).toBeLessThanOrEqual(3);
        } else {
          streak = 1;
        }
      }
    }
  });

  test('no tile variant appears >3 times consecutively in a column', () => {
    const data = layers.Ground_Base;
    for (let x = 0; x < W; x++) {
      let streak = 1;
      for (let y = 1; y < H; y++) {
        const prev = data[(y - 1) * W + x];
        const curr = data[y * W + x];
        if (curr === prev && curr !== 0) {
          streak++;
          expect(streak).toBeLessThanOrEqual(3);
        } else {
          streak = 1;
        }
      }
    }
  });
});
```

- [ ] **Step 2: Run test — verify it fails**

```bash
npm test -- --testPathPattern=terrain
```

- [ ] **Step 3: Implement terrain.ts**

```typescript
// src/town-gen/terrain.ts

import type { ZoneMap, RNG } from './schema';
import { TERRAIN, TRANSITIONS, TILESET } from './asset-registry';

export interface TerrainLayers {
  Ground_Base: number[];
  Ground_Detail: number[];
  Water_Back: number[];
  Terrain_Structures: number[];
}

const ZONE_TO_TERRAIN: Record<string, string> = {
  water: 'water',
  shore: 'shore',
  grass: 'grass',
  road: 'road',
  plaza: 'plaza',
  canal: 'water',
  dock: 'dock',
  park: 'grass',
  building: 'grass',  // ground under buildings
  bridge: 'road',
};

/** Convert local tile ID to GID. */
function gid(localId: number): number {
  return localId + TILESET.firstgid;
}

/** Pick a terrain tile with streak limiting. */
function pickTerrain(
  familyKey: string,
  rng: RNG,
  lastTile: number,
  streak: number,
): { tile: number; newStreak: number } {
  const family = TERRAIN[familyKey];
  if (!family) return { tile: 0, newStreak: 0 };

  const allTiles = [family.primary, ...family.alternates];
  let picked: number;

  if (streak >= 3) {
    // Force a different tile
    const others = allTiles.filter((t) => gid(t) !== lastTile);
    picked = others.length > 0 ? rng.pick(others) : family.primary;
  } else {
    picked = rng.weightedPick(allTiles, family.weights);
  }

  const pickedGid = gid(picked);
  return {
    tile: pickedGid,
    newStreak: pickedGid === lastTile ? streak + 1 : 1,
  };
}

export function fillTerrainLayers(
  zones: ZoneMap,
  w: number,
  h: number,
  rng: RNG,
): TerrainLayers {
  const size = w * h;
  const groundBase = new Array<number>(size).fill(0);
  const groundDetail = new Array<number>(size).fill(0);
  const waterBack = new Array<number>(size).fill(0);
  const terrainStructures = new Array<number>(size).fill(0);

  // Pass 1: Fill Ground_Base with terrain tiles (horizontal streak limiting)
  for (let y = 0; y < h; y++) {
    let lastTile = 0;
    let streak = 0;

    for (let x = 0; x < w; x++) {
      const zone = zones[x][y];
      const terrainKey = ZONE_TO_TERRAIN[zone] || 'grass';
      const { tile, newStreak } = pickTerrain(terrainKey, rng, lastTile, streak);

      groundBase[y * w + x] = tile;
      lastTile = tile;
      streak = newStreak;

      // Water zones also go on Water_Back for animation
      if (zone === 'water' || zone === 'canal') {
        waterBack[y * w + x] = tile;
      }
    }
  }

  // Pass 1b: Vertical streak limiting — scan columns and swap violations
  for (let x = 0; x < w; x++) {
    let lastTile = groundBase[0 * w + x];
    let streak = 1;
    for (let y = 1; y < h; y++) {
      const tile = groundBase[y * w + x];
      if (tile === lastTile && tile !== 0) {
        streak++;
        if (streak > 3) {
          // Force a different tile for this zone
          const zone = zones[x][y];
          const terrainKey = ZONE_TO_TERRAIN[zone] || 'grass';
          const family = TERRAIN[terrainKey];
          if (family) {
            const allTiles = [family.primary, ...family.alternates];
            const others = allTiles.filter((t) => gid(t) !== tile);
            if (others.length > 0) {
              groundBase[y * w + x] = gid(rng.pick(others));
              streak = 1;
            }
          }
        }
      } else {
        streak = 1;
      }
      lastTile = groundBase[y * w + x];
    }
  }

  // Pass 2: Ground_Detail — transition tiles at zone boundaries
  for (let x = 1; x < w - 1; x++) {
    for (let y = 1; y < h - 1; y++) {
      const zone = zones[x][y];
      const neighbors = {
        n: zones[x][y - 1],
        s: zones[x][y + 1],
        e: zones[x + 1][y],
        w: zones[x - 1][y],
      };

      // Find transition tiles for zone boundaries
      const transitionTile = getTransitionTile(zone, neighbors);
      if (transitionTile !== 0) {
        groundDetail[y * w + x] = gid(transitionTile);
      }
    }
  }

  // Pass 3: Terrain_Structures — canal walls, bridge bases
  for (let x = 0; x < w; x++) {
    for (let y = 0; y < h; y++) {
      const zone = zones[x][y];
      if (zone === 'bridge') {
        // Bridge base tile on structures layer
        const bridgeTile = TERRAIN.road?.primary ?? 0;
        terrainStructures[y * w + x] = gid(bridgeTile);
      }
      // Canal wall edges
      if (zone !== 'canal' && zone !== 'water' && zone !== 'bridge') {
        const adj = [
          y > 0 ? zones[x][y - 1] : null,
          y < h - 1 ? zones[x][y + 1] : null,
        ];
        if (adj.includes('canal')) {
          const wallTile = TERRAIN.canal_wall?.primary ?? 0;
          if (wallTile) terrainStructures[y * w + x] = gid(wallTile);
        }
      }
    }
  }

  return {
    Ground_Base: groundBase,
    Ground_Detail: groundDetail,
    Water_Back: waterBack,
    Terrain_Structures: terrainStructures,
  };
}

function getTransitionTile(
  zone: string,
  neighbors: { n: string; s: string; e: string; w: string },
): number {
  // Simplified: find the most relevant transition
  const terrainKey = ZONE_TO_TERRAIN[zone] || 'grass';

  for (const [dir, nZone] of Object.entries(neighbors)) {
    const nKey = ZONE_TO_TERRAIN[nZone] || 'grass';
    if (nKey === terrainKey) continue;

    // Look up transition set
    const pairKey = `${terrainKey}_to_${nKey}`;
    const reversePairKey = `${nKey}_to_${terrainKey}`;
    const tSet = TRANSITIONS[pairKey] || TRANSITIONS[reversePairKey];
    if (!tSet) continue;

    // Return the edge tile for this direction
    const edgeTile = tSet[dir as keyof typeof tSet];
    if (edgeTile && edgeTile !== 0) return edgeTile;
  }

  return 0;
}
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
npm test -- --testPathPattern=terrain
```

- [ ] **Step 5: Commit**

```bash
git add src/town-gen/terrain.ts src/town-gen/__tests__/terrain.test.ts
git commit -m "feat(town-gen): terrain module — ground tiles with transitions"
```

---

## Chunk 3: Buildings, Props, Collision, Export

### Task 7: Building placement

**Files:**
- Create: `src/town-gen/buildings.ts`
- Test: `src/town-gen/__tests__/buildings.test.ts`

- [ ] **Step 1: Write building test**

```typescript
// src/town-gen/__tests__/buildings.test.ts

import { placeBuildings } from '../buildings';
import { generateZoneMap } from '../layout';
import { createRNG } from '../rng';
import type { PlacedBuilding } from '../schema';

const W = 128;
const H = 96;

describe('placeBuildings', () => {
  const rng = createRNG(42);
  const zones = generateZoneMap(W, H, rng);
  const rng2 = createRNG(99);
  const { buildings, updatedZones } = placeBuildings(zones, W, H, rng2);

  test('places at least 5 buildings', () => {
    expect(buildings.length).toBeGreaterThanOrEqual(5);
  });

  test('places exactly 1 civic building', () => {
    const civics = buildings.filter((b) => b.kit.class === 'civic');
    expect(civics.length).toBe(1);
  });

  test('places at least 1 commercial building', () => {
    const shops = buildings.filter((b) => b.kit.class === 'commercial');
    expect(shops.length).toBeGreaterThanOrEqual(1);
  });

  test('no buildings overlap', () => {
    const occupied = new Set<string>();
    for (const b of buildings) {
      for (let dx = 0; dx < b.kit.footprint.w; dx++) {
        for (let dy = 0; dy < b.kit.footprint.h; dy++) {
          const key = `${b.x + dx},${b.y + dy}`;
          expect(occupied.has(key)).toBe(false);
          occupied.add(key);
        }
      }
    }
  });

  test('all building doors are adjacent to road or plaza', () => {
    for (const b of buildings) {
      const doorX = b.x + b.kit.doorOffset.x;
      const doorY = b.y + b.kit.doorOffset.y + 1; // tile in front of door
      if (doorY < H) {
        const zoneInFront = updatedZones[doorX]?.[doorY];
        expect(['road', 'plaza', 'park', 'grass']).toContain(zoneInFront);
      }
    }
  });
});
```

- [ ] **Step 2: Implement buildings.ts**

The implementation should:
- Scan the zone map for building plots (rectangular areas of grass adjacent to roads)
- Classify plots by adjacency (plaza-adjacent → civic, road-adjacent → commercial, else residential)
- Pick a kit from `BUILDINGS` matching the class and fitting the plot
- Stamp the building, mark zones as `building`
- Return `PlacedBuilding[]` and updated zone map

- [ ] **Step 3: Run tests, iterate until passing**

```bash
npm test -- --testPathPattern=buildings
```

- [ ] **Step 4: Commit**

```bash
git add src/town-gen/buildings.ts src/town-gen/__tests__/buildings.test.ts
git commit -m "feat(town-gen): building placement module"
```

---

### Task 8: Prop placement

**Files:**
- Create: `src/town-gen/props.ts`

- [ ] **Step 1: Implement props.ts**

```typescript
// src/town-gen/props.ts

import type { ZoneMap, PlacedProp, PlacedBuilding, RNG } from './schema';
import { PROPS } from './asset-registry';

export function placeProps(
  zones: ZoneMap,
  w: number,
  h: number,
  buildings: PlacedBuilding[],
  rng: RNG,
): PlacedProp[] {
  const props: PlacedProp[] = [];
  const occupied = new Set<string>();

  // Mark building footprints as occupied
  for (const b of buildings) {
    for (let dx = 0; dx < b.kit.footprint.w; dx++) {
      for (let dy = 0; dy < b.kit.footprint.h; dy++) {
        occupied.add(`${b.x + dx},${b.y + dy}`);
      }
    }
  }

  const canPlace = (def: typeof PROPS.fountain, x: number, y: number): boolean => {
    for (let dx = 0; dx < def.footprint.w; dx++) {
      for (let dy = 0; dy < def.footprint.h; dy++) {
        const key = `${x + dx},${y + dy}`;
        if (occupied.has(key)) return false;
        if (x + dx >= w || y + dy >= h) return false;
        const zone = zones[x + dx][y + dy];
        if (zone === 'water' || zone === 'canal' || zone === 'building') return false;
      }
    }
    return true;
  };

  const place = (def: typeof PROPS.fountain, x: number, y: number): void => {
    props.push({ def, x, y });
    for (let dx = 0; dx < def.footprint.w; dx++) {
      for (let dy = 0; dy < def.footprint.h; dy++) {
        occupied.add(`${x + dx},${y + dy}`);
      }
    }
  };

  // 1. Fountain at plaza center
  for (let x = 0; x < w; x++) {
    for (let y = 0; y < h; y++) {
      if (zones[x][y] === 'plaza') {
        // Find plaza center
        let maxX = x, maxY = y;
        while (maxX < w && zones[maxX][y] === 'plaza') maxX++;
        while (maxY < h && zones[x][maxY] === 'plaza') maxY++;
        const cx = Math.floor((x + maxX) / 2) - 1;
        const cy = Math.floor((y + maxY) / 2) - 1;
        if (canPlace(PROPS.fountain, cx, cy)) {
          place(PROPS.fountain, cx, cy);
        }
        break;
      }
    }
    if (props.some((p) => p.def.name === 'fountain')) break;
  }

  // 2. Trees in parks and along roads (min 4 tiles apart)
  for (let x = 10; x < w - 10; x += 4 + rng.nextInt(3)) {
    for (let y = 10; y < h - 10; y += 4 + rng.nextInt(3)) {
      const zone = zones[x][y];
      if ((zone === 'park' || zone === 'grass') && canPlace(PROPS.tree, x, y)) {
        if (rng.next() < 0.3) {
          place(PROPS.tree, x, y);
        }
      }
    }
  }

  // 3. Lamps along main roads (every 6-8 tiles)
  for (let x = 10; x < w - 10; x += 6 + rng.nextInt(3)) {
    for (let y = 10; y < h - 10; y++) {
      if (zones[x][y] === 'road' && canPlace(PROPS.lamp, x, y - 1)) {
        place(PROPS.lamp, x, y - 1);
        break; // One lamp per column segment
      }
    }
  }

  // 4. Benches near plazas and parks
  for (let x = 10; x < w - 10; x += 8 + rng.nextInt(4)) {
    for (let y = 10; y < h - 10; y += 8 + rng.nextInt(4)) {
      const zone = zones[x][y];
      if ((zone === 'plaza' || zone === 'park') && canPlace(PROPS.bench, x, y)) {
        if (rng.next() < 0.4) {
          place(PROPS.bench, x, y);
        }
      }
    }
  }

  // 5. Bushes along building edges
  for (const b of buildings) {
    // Try placing bushes on each side
    for (let dx = 0; dx < b.kit.footprint.w; dx++) {
      const bx = b.x + dx;
      const by = b.y - 1; // Above building
      if (by >= 0 && canPlace(PROPS.bush, bx, by) && rng.next() < 0.3) {
        place(PROPS.bush, bx, by);
      }
    }
  }

  // 6. Signs adjacent to shop doors
  for (const b of buildings) {
    if (b.kit.class !== 'commercial') continue;
    const signX = b.x + b.kit.doorOffset.x + 1; // Right of door
    const signY = b.y + b.kit.doorOffset.y;
    if (canPlace(PROPS.sign, signX, signY)) {
      place(PROPS.sign, signX, signY);
    }
  }

  // 7. Planters at building fronts and plaza corners
  for (const b of buildings) {
    // Planter at each front corner of building
    const frontY = b.y + b.kit.footprint.h;
    for (const dx of [0, b.kit.footprint.w - 1]) {
      const px = b.x + dx;
      if (canPlace(PROPS.planter, px, frontY) && rng.next() < 0.4) {
        place(PROPS.planter, px, frontY);
      }
    }
  }

  return props;
}
```

- [ ] **Step 2: Commit**

```bash
git add src/town-gen/props.ts
git commit -m "feat(town-gen): prop placement module"
```

---

### Task 9: Collision generation

**Files:**
- Create: `src/town-gen/collision.ts`

- [ ] **Step 1: Implement collision.ts**

```typescript
// src/town-gen/collision.ts

import type { ZoneMap, PlacedBuilding, PlacedProp, CollisionRect, SpawnPoint } from './schema';

const BLOCKED_ZONES: Set<string> = new Set(['water', 'canal']);

export function generateCollision(
  zones: ZoneMap,
  w: number,
  h: number,
  buildings: PlacedBuilding[],
  props: PlacedProp[],
): CollisionRect[] {
  const rects: CollisionRect[] = [];

  // 1. Water and canal — merge adjacent water tiles into larger rects
  const visited = new Set<string>();
  for (let x = 0; x < w; x++) {
    for (let y = 0; y < h; y++) {
      const key = `${x},${y}`;
      if (visited.has(key)) continue;
      if (!BLOCKED_ZONES.has(zones[x][y])) continue;

      // Flood-fill to find rect extent
      let maxX = x;
      while (maxX + 1 < w && BLOCKED_ZONES.has(zones[maxX + 1][y]) && !visited.has(`${maxX + 1},${y}`)) {
        maxX++;
      }
      let maxY = y;
      outer: while (maxY + 1 < h) {
        for (let cx = x; cx <= maxX; cx++) {
          if (!BLOCKED_ZONES.has(zones[cx][maxY + 1]) || visited.has(`${cx},${maxY + 1}`)) break outer;
        }
        maxY++;
      }

      // Mark visited
      for (let cx = x; cx <= maxX; cx++) {
        for (let cy = y; cy <= maxY; cy++) {
          visited.add(`${cx},${cy}`);
        }
      }

      rects.push({
        x, y,
        width: maxX - x + 1,
        height: maxY - y + 1,
        source: `zone:${zones[x][y]}`,
      });
    }
  }

  // 2. Buildings
  for (const b of buildings) {
    rects.push({
      x: b.x + b.kit.collision.x,
      y: b.y + b.kit.collision.y,
      width: b.kit.collision.w,
      height: b.kit.collision.h,
      source: `building:${b.kit.name}`,
    });
  }

  // 3. Props
  for (const p of props) {
    if (p.def.collision === 'none') continue;
    if (p.def.collision === 'full') {
      rects.push({
        x: p.x, y: p.y,
        width: p.def.footprint.w,
        height: p.def.footprint.h,
        source: `prop:${p.def.name}`,
      });
    } else if (p.def.collision === 'base') {
      // Only bottom row blocks
      rects.push({
        x: p.x,
        y: p.y + p.def.footprint.h - 1,
        width: p.def.footprint.w,
        height: 1,
        source: `prop:${p.def.name}`,
      });
    }
  }

  return rects;
}

export function generateSpawnPoints(
  zones: ZoneMap,
  w: number,
  h: number,
  buildings: PlacedBuilding[],
): SpawnPoint[] {
  const spawns: SpawnPoint[] = [];

  // Find plaza center for "plaza" spawn
  for (let x = 0; x < w; x++) {
    for (let y = 0; y < h; y++) {
      if (zones[x][y] === 'plaza') {
        let maxX = x, maxY = y;
        while (maxX < w && zones[maxX][y] === 'plaza') maxX++;
        while (maxY < h && zones[x][maxY] === 'plaza') maxY++;
        spawns.push({ name: 'plaza', x: Math.floor((x + maxX) / 2), y: Math.floor((y + maxY) / 2) });
        break;
      }
    }
    if (spawns.length > 0) break;
  }

  // Spawn points near buildings by class
  const civic = buildings.find((b) => b.kit.class === 'civic');
  if (civic) {
    spawns.push({
      name: 'civic_hall',
      x: civic.x + civic.kit.doorOffset.x,
      y: civic.y + civic.kit.footprint.h + 1,
    });
  }

  const shops = buildings.filter((b) => b.kit.class === 'commercial');
  if (shops.length > 0) {
    const shop = shops[0];
    spawns.push({
      name: 'cafe',
      x: shop.x + shop.kit.doorOffset.x,
      y: shop.y + shop.kit.footprint.h + 1,
    });
  }
  if (shops.length > 1) {
    const shop = shops[1];
    spawns.push({
      name: 'activity_center',
      x: shop.x + shop.kit.doorOffset.x,
      y: shop.y + shop.kit.footprint.h + 1,
    });
  }

  const houses = buildings.filter((b) => b.kit.class === 'residential');
  if (houses.length > 0) {
    const house = houses[0];
    spawns.push({
      name: 'residence',
      x: house.x + house.kit.doorOffset.x,
      y: house.y + house.kit.footprint.h + 1,
    });
  }

  return spawns;
}
```

- [ ] **Step 2: Commit**

```bash
git add src/town-gen/collision.ts
git commit -m "feat(town-gen): collision and spawn point generation"
```

---

### Task 10: TMJ exporter

**Files:**
- Create: `src/town-gen/exporter.ts`
- Test: `src/town-gen/__tests__/exporter.test.ts`

- [ ] **Step 1: Write exporter test**

```typescript
// src/town-gen/__tests__/exporter.test.ts

import { exportTmj } from '../exporter';

describe('exportTmj', () => {
  const mockLayers = {
    Ground_Base: new Array(4).fill(1),
    Ground_Detail: new Array(4).fill(0),
  };
  const mockCollisions = [{ x: 0, y: 0, width: 1, height: 1, source: 'test' }];
  const mockSpawns = [{ name: 'plaza', x: 1, y: 1 }];

  const tmj = exportTmj(2, 2, mockLayers, [], mockCollisions, mockSpawns);

  test('has correct dimensions', () => {
    expect(tmj.width).toBe(2);
    expect(tmj.height).toBe(2);
    expect(tmj.tilewidth).toBe(16);
    expect(tmj.tileheight).toBe(16);
  });

  test('has all 17 layers', () => {
    expect(tmj.layers.length).toBe(17);
  });

  test('tile layers have correct data length', () => {
    const tileLayers = tmj.layers.filter((l) => l.type === 'tilelayer');
    for (const layer of tileLayers) {
      expect((layer as any).data.length).toBe(4);
    }
  });

  test('collision layer has objects', () => {
    const collision = tmj.layers.find((l) => l.name === 'Collision');
    expect(collision?.type).toBe('objectgroup');
    expect((collision as any).objects.length).toBeGreaterThan(0);
  });

  test('spawn points layer has objects', () => {
    const spawns = tmj.layers.find((l) => l.name === 'Spawn_Points');
    expect(spawns?.type).toBe('objectgroup');
    expect((spawns as any).objects.length).toBeGreaterThan(0);
  });

  test('has tileset reference', () => {
    expect(tmj.tilesets.length).toBe(1);
    expect(tmj.tilesets[0].firstgid).toBe(1);
  });
});
```

- [ ] **Step 2: Implement exporter.ts**

Serialize all 17 layers into Tiled-compatible JSON structure. Tile layers use `data` arrays. Object layers use `objects` arrays with pixel coordinates (x * 16, y * 16).

- [ ] **Step 3: Run tests**

```bash
npm test -- --testPathPattern=exporter
```

- [ ] **Step 4: Commit**

```bash
git add src/town-gen/exporter.ts src/town-gen/__tests__/exporter.test.ts
git commit -m "feat(town-gen): TMJ exporter"
```

---

### Task 11: Generator pipeline and CLI

**Files:**
- Create: `src/town-gen/generator.ts`
- Create: `src/town-gen/index.ts`

- [ ] **Step 1: Implement generator.ts**

Wire all modules together: `createRNG` → `generateZoneMap` → `fillTerrainLayers` → `placeBuildings` → `placeProps` → `generateCollision` → `generateSpawnPoints` → `exportTmj`.

Include a `validate()` function that runs these 4 checks (generation throws if any fail):

```typescript
function validate(
  zones: ZoneMap, w: number, h: number,
  buildings: PlacedBuilding[], spawns: SpawnPoint[],
  collisions: CollisionRect[],
): void {
  // 1. All 5 spawn points exist
  if (spawns.length < 5) {
    throw new Error(`Expected 5 spawn points, got ${spawns.length}`);
  }

  // 2. All spawns are on walkable tiles (not water, canal, building)
  const blocked = new Set<string>();
  for (const r of collisions) {
    for (let x = r.x; x < r.x + r.width; x++) {
      for (let y = r.y; y < r.y + r.height; y++) {
        blocked.add(`${x},${y}`);
      }
    }
  }
  for (const s of spawns) {
    if (blocked.has(`${s.x},${s.y}`)) {
      throw new Error(`Spawn "${s.name}" at (${s.x},${s.y}) is on a blocked tile`);
    }
  }

  // 3. A* connectivity — all spawn pairs reachable
  for (let i = 0; i < spawns.length; i++) {
    for (let j = i + 1; j < spawns.length; j++) {
      const path = aStarSimple(spawns[i], spawns[j], w, h, blocked);
      if (!path) {
        throw new Error(
          `No path from "${spawns[i].name}" to "${spawns[j].name}"`
        );
      }
    }
  }

  // 4. No buildings overlap
  const occupied = new Set<string>();
  for (const b of buildings) {
    for (let dx = 0; dx < b.kit.footprint.w; dx++) {
      for (let dy = 0; dy < b.kit.footprint.h; dy++) {
        const key = `${b.x + dx},${b.y + dy}`;
        if (occupied.has(key)) {
          throw new Error(`Building overlap at ${key}`);
        }
        occupied.add(key);
      }
    }
  }
}
```

Implement `aStarSimple()` as a minimal A* that checks if two points are connected given blocked tiles. This is for validation only — keep it simple.

- [ ] **Step 2: Implement index.ts (CLI)**

```typescript
// src/town-gen/index.ts

import { generateTown } from './generator';
import { writeFileSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const seed = parseInt(process.argv[2] || '42', 10);
console.log(`Generating town with seed ${seed}...`);

const { tmj, manifest } = generateTown(seed);

const tmjPath = resolve(__dirname, '../../public/assets/town-center.tmj');
writeFileSync(tmjPath, JSON.stringify(tmj));
console.log(`Written: ${tmjPath}`);

const manifestPath = resolve(__dirname, '../../public/assets/town-gen-manifest.json');
writeFileSync(manifestPath, JSON.stringify(manifest, null, 2));
console.log(`Written: ${manifestPath}`);

// Also copy to backend
const backendPath = resolve(__dirname, '../../../backend/data/town-center.tmj');
writeFileSync(backendPath, JSON.stringify(tmj));
console.log(`Written: ${backendPath}`);

console.log(`Done! ${manifest.spawns.length} spawn points generated.`);
```

- [ ] **Step 3: Add npm script**

Add to `package.json` scripts:
```json
"generate-town": "tsx src/town-gen/index.ts"
```

- [ ] **Step 4: Run the generator**

```bash
npm run generate-town -- 42
```

Verify `public/assets/town-center.tmj` and `backend/data/town-center.tmj` are created.

- [ ] **Step 5: Commit**

```bash
git add src/town-gen/generator.ts src/town-gen/index.ts package.json
git commit -m "feat(town-gen): generator pipeline and CLI"
```

---

## Chunk 4: Integration

### Task 12: Update backend pathfinding (tileset-agnostic)

**Files:**
- Modify: `backend/core/services/town_pathfinding.py:24-50`

- [ ] **Step 1: Refactor pathfinding to use Collision layer + Ground_Base emptiness**

Replace `_BLOCKED_GROUND_GIDS` and the multi-layer scanning with:
1. Start all tiles as blocked
2. Any tile with GID > 0 on Ground_Base → walkable
3. Read Collision objectgroup → mark those rects as blocked
4. Remove all hardcoded GID references

Also: change map filename to `town-center.tmj`, bump `max_iterations` to 10000.

- [ ] **Step 2: Run backend tests**

```bash
cd backend && source .venv/bin/activate && python -m pytest tests/ -v -x -q
```

- [ ] **Step 3: Commit**

```bash
cd backend && git add core/services/town_pathfinding.py
git commit -m "refactor: tileset-agnostic pathfinding using Collision layer"
```

---

### Task 13: Update backend map metadata and constants

**Files:**
- Modify: `backend/routers/town.py:70-78`
- Modify: `backend/core/town_constants.py`

- [ ] **Step 1: Update `_load_map_data()`**

```python
def _load_map_data() -> dict:
    return {
        "width": 128,
        "height": 96,
        "tileDim": 16,
        "tileSetUrl": "/assets/tilesets/oga-jrpg-tileset.png",
        "mapUrl": "/assets/town-center.tmj",
    }
```

- [ ] **Step 2: Update town_constants.py**

Read spawn coordinates from `town-gen-manifest.json` and update `TOWN_LOCATIONS` with the new coordinates.

- [ ] **Step 3: Run backend tests**

```bash
python -m pytest tests/ -v -x -q
```

- [ ] **Step 4: Commit**

```bash
git add routers/town.py core/town_constants.py
git commit -m "feat: update backend for 128x96 OGA town map"
```

---

### Task 14: Update frontend PixiGame

**Files:**
- Modify: `src/components/PixiGame.tsx:10-16, 139-172`

- [ ] **Step 1: Update location labels and map URLs**

Update `LOCATION_LABELS` with coordinates from the manifest. Update the two `TiledMapRenderer` instances to reference `town-center.tmj` and `oga-jrpg-tileset.png`.

Update the layer name arrays for each TiledMapRenderer instance:
- **Instance 1 (below agents):** `['Ground_Base', 'Ground_Detail', 'Water_Back', 'Terrain_Structures', 'Buildings_Base', 'Props_Back', 'Animation_Back']`
- **Instance 2 (above agents):** `['Props_Front', 'Foreground_Low', 'Foreground_High', 'Animation_Front']`

- [ ] **Step 2: Verify build passes**

```bash
cd goosetown && npm run build
```

- [ ] **Step 3: Commit**

```bash
git add src/components/PixiGame.tsx
git commit -m "feat: update PixiGame for OGA 16x16 town"
```

---

### Task 15: End-to-end verification

- [ ] **Step 1: Generate the town**

```bash
cd goosetown && npm run generate-town -- 42
```

- [ ] **Step 2: Run all frontend tests**

```bash
npm test
```

- [ ] **Step 3: Run all backend tests**

```bash
cd backend && python -m pytest tests/ -v -x -q
```

- [ ] **Step 4: Test pathfinding connectivity**

```bash
cd backend && python3 -c "
import sys, json; sys.path.insert(0, '.')
import core.services.town_pathfinding as tp
tp._objmap = None
grid = tp._load_objmap()
tp._objmap = grid
# Load spawn points from manifest
with open('../goosetown/public/assets/town-gen-manifest.json') as f:
    manifest = json.load(f)
spawns = manifest['spawns']
print(f'Testing {len(spawns)} spawn points...')
for i, a in enumerate(spawns):
    for b in spawns[i+1:]:
        path = tp.find_path(a['x'], a['y'], b['x'], b['y'])
        assert path is not None, f'No path from {a[\"name\"]} to {b[\"name\"]}'
        print(f'  {a[\"name\"]} -> {b[\"name\"]}: OK ({len(path)} steps)')
print('All spawn pairs connected!')
"
```

- [ ] **Step 5: Run dev server and verify visually**

```bash
cd goosetown && npm run dev
```

Open browser, verify the map renders with OGA tiles, buildings, props, and proper layering.

- [ ] **Step 6: Commit and push both repos**

```bash
cd goosetown && git push origin main
cd backend && git push origin main
```

- [ ] **Step 7: Watch CI runs**

```bash
gh run watch --repo Isol8AI/goosetown --exit-status
gh run watch --repo Isol8AI/backend --exit-status
```
