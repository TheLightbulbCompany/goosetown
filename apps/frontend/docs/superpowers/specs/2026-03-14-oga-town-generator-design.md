# OGA 16x16 JRPG Town Generator — Design Spec

## Goal

Replace the PixelLab-generated 32x32 town map with a procedurally generated 16x16 JRPG town using the OGA (OpenGameArt) 16x16 JRPG tileset. The generator is a TypeScript module that outputs Tiled-compatible JSON consumed by the existing renderer.

## Architecture

A pure-data TypeScript pipeline in `src/town-gen/` that:

1. Creates an abstract zone map (water, grass, road, plaza, etc.)
2. Fills tile layers from the zone map using an asset registry
3. Places buildings as atomic multi-tile kits
4. Scatters props with spatial rules
5. Generates collision, spawn, and interaction metadata
6. Exports a Tiled-compatible TMJ file

The generator has zero rendering dependencies. The existing `TiledMapRenderer` consumes its output unchanged.

## Technical Targets

- **Tile size:** 16x16 pixels
- **Map size:** 128x96 tiles (2048x1536px world — same pixel footprint as current)
- **Orientation:** Orthogonal
- **Tileset:** OGA 16x16 JRPG Sprites & Tiles (opengameart.org)
- **Output format:** Tiled JSON (.tmj)
- **Determinism:** Seeded PRNG — same seed produces same town

## File Structure

```
goosetown/src/town-gen/
  schema.ts          — Type definitions (Tilemap, TileLayer, ObjectLayer, etc.)
  asset-registry.ts  — OGA tile ID mappings, terrain families, building kits
  terrain.ts         — Ground tile placement with transitions
  buildings.ts       — Building kit stamping onto plots
  props.ts           — Prop placement rules
  layout.ts          — Zone map generation (town composition)
  collision.ts       — Collision rect generation
  exporter.ts        — Serialize to TMJ
  generator.ts       — Main pipeline: layout → terrain → buildings → props → export
  index.ts           — CLI entry: npx tsx src/town-gen/index.ts [--seed N]
```

## Layer Stack

All 17 layers from the spec, in order:

| # | Name | Type | Content |
|---|------|------|---------|
| 1 | Ground_Base | tilelayer | Primary terrain: water, grass, road, plaza, dock |
| 2 | Ground_Detail | tilelayer | Terrain transitions, edge tiles, surface variation |
| 3 | Water_Back | tilelayer | Water tiles for animation hooks |
| 4 | Terrain_Structures | tilelayer | Canal walls, dock edges, bridge bases, curbs |
| 5 | Buildings_Base | tilelayer | Building walls, doors, windows (lower rows) |
| 6 | Props_Back | tilelayer | Prop bases: tree trunks, bench seats, lamp bases |
| 7 | Animation_Back | tilelayer | Animated tile positions (water shimmer, fountain) |
| 8 | Collision | objectgroup | Blocking rectangles |
| 9 | Depth_Masks | objectgroup | Draw-order helpers (reserved) |
| 10 | Triggers | objectgroup | Zone entry markers |
| 11 | Spawn_Points | objectgroup | Named agent spawn locations |
| 12 | NPC_Paths | objectgroup | Waypoint chains along roads |
| 13 | Interaction | objectgroup | Door and interactable objects |
| 14 | Props_Front | tilelayer | Upper prop parts: lamp tops, sign tops |
| 15 | Foreground_Low | tilelayer | Building rooftops, awnings |
| 16 | Foreground_High | tilelayer | Tree canopies, tall overhangs |
| 17 | Animation_Front | tilelayer | Upper animated elements (banners, signs) |

## Asset Registry

### Terrain Families

Each terrain type has a primary tile and 2-4 weighted alternates to break visual repetition. No variant appears more than 3 times consecutively.

```typescript
interface TerrainFamily {
  primary: number;
  alternates: number[];
  weight?: number[];
}
```

Families: water (3 variants), grass (4 variants), road/cobblestone (3 variants), plaza stone (4 variants), dock planks (2 variants).

### Transition Sets

Edge and corner tiles for terrain-to-terrain boundaries. 12 tiles per set (4 edges, 4 outer corners, 4 inner corners).

```typescript
interface TransitionSet {
  n: number; s: number; e: number; w: number;
  ne: number; nw: number; se: number; sw: number;
  innerNE: number; innerNW: number; innerSE: number; innerSW: number;
}
```

Supported pairs: grass↔road, grass↔water, grass↔plaza, water↔canal wall, water↔dock.

### Building Kits

Each building is a named footprint with tile array, door position, collision rect, and foreground row count.

```typescript
interface BuildingKit {
  name: string;
  footprint: { w: number; h: number };
  tiles: number[][];
  doorOffset: { x: number; y: number };
  collision: { x: number; y: number; w: number; h: number };
  foregroundRows: number;
}
```

Kits: house_small (3x3), house_medium (4x4), shop (4x3), shop_wide (5x3), civic (6x5), inn (5x4).

### Prop Definitions

```typescript
interface PropDef {
  name: string;
  footprint: { w: number; h: number };
  tiles: number[][];
  collision: 'none' | 'base' | 'full';
  groundLayer: string;
  foregroundLayer?: string;
  foregroundTiles?: number[][];
}
```

Props: fountain (3x3), tree (2x2), bench (2x1), lamp (1x2), bush (1x1), sign (1x2), planter (1x1).

## Layout Generation (Zone Map)

The generator builds a 128x96 zone grid in phases:

1. **Water + coastline** — Water fills borders (8-12 tiles, irregular edge). Canal cuts east-west across middle third (4 tiles wide). Shore zones (2 tiles) buffer water-to-land.

2. **Main roads** — Horizontal + vertical roads (3 tiles wide) forming a cross/T. They intersect at the plaza and cross the canal via bridges.

3. **Plaza** — 10x8 to 14x10 tile rectangle at the road intersection.

4. **Secondary roads** — 2-3 narrower roads (2 tiles wide) branching off main roads, creating blocks.

5. **Building plots** — Rectangles bounded by roads, classified as civic (facing plaza), commercial (main road), or residential (secondary road).

6. **Parks** — 1-2 plots designated as green space instead of buildings.

7. **Bridges** — 3-tile-wide road zones crossing the canal, placed on Terrain_Structures.

8. **Dock** — 6-10 tiles of dock planks along waterfront edge.

## Terrain Placement

1. **Ground_Base fill** — Walk every zone cell, pick tile from terrain family with weighted random (seeded).

2. **Ground_Detail transitions** — Second pass: check 8 neighbors for zone changes, place edge/corner transition tiles from the transition set.

3. **Water_Back** — Duplicate water positions for animation hooks.

4. **Terrain_Structures** — Canal walls, dock edges, bridge bases placed at zone boundaries.

**Streak rule:** No tile variant appears more than 3 times consecutively (horizontal or vertical). Forces alternates.

## Building Placement

1. Buildings must face a road or plaza (door adjacent to walkable zone).
2. Buildings in the same block share a setback line (front walls align).
3. Minimum 2-tile clearance from door to any obstacle.
4. Minimum 1-tile gap between adjacent buildings.
5. Bottom rows → Buildings_Base. Top rows (rooftops) → Foreground_Low.
6. Collision rect → Collision object layer.

## Prop Placement

| Prop | Placement Rule | Layers | Collision |
|------|---------------|--------|-----------|
| Fountain | Plaza center | Props_Back + Foreground_Low | Rim + center |
| Trees | Parks, road edges, min 4 tiles apart | Props_Back (trunk) + Foreground_High (canopy) | Trunk only |
| Benches | Facing plazas/paths, never facing walls | Props_Back | None |
| Lamps | Every 6-8 tiles along main roads | Props_Back + Props_Front | Base only |
| Bushes | Building edges, park borders | Props_Back | Full |
| Signs | Adjacent to shop doors | Props_Back | None |
| Planters | Building fronts, plaza corners | Props_Back | Full |

## Collision Generation

Reads zone map + building footprints + prop positions. Outputs `CollisionRect[]`:

- Water zones → full tile rects
- Canal wall tiles → full tile rects
- Building kits → their defined collision rects
- Props → per-prop collision type
- Everything else → walkable

## Metadata Layers

- **Spawn_Points** — 5 named spawns (plaza, library/civic, cafe/shop, activity_center, residence) on walkable tiles adjacent to landmarks.
- **Interaction** — Door objects on each building with `target` property.
- **NPC_Paths** — Waypoint chains along main roads.
- **Triggers** — Zone entry markers at district boundaries.

## Export

Serializes to Tiled-compatible JSON:

```json
{
  "width": 128, "height": 96,
  "tilewidth": 16, "tileheight": 16,
  "orientation": "orthogonal",
  "tilesets": [{ "firstgid": 1, "name": "oga_jrpg", ... }],
  "layers": [ /* 17 layers */ ]
}
```

Output: `public/assets/town-center.tmj` (frontend) + copied to `backend/data/town-center.tmj` (pathfinding)
Tileset: `public/assets/tilesets/oga-jrpg-tileset.png`

Object layers (Collision, Spawn_Points, Interaction, etc.) are included in the TMJ but silently dropped by the frontend `tmjParser.ts` (which filters to tilelayers only). This is by design — object layers are consumed only by the backend Python code which loads the TMJ via `json.load()` directly.

The generator also outputs a `town-gen-manifest.json` sidecar with:
- Spawn point coordinates (for `town_constants.py`)
- Location names and bounds
- Generation seed used

## Validation

`generator.ts` runs a validation pass after generation:
- Confirms all 5 spawn points are on walkable tiles
- Runs A* between all spawn point pairs to verify connectivity
- Checks no buildings overlap
- Checks all doors are adjacent to walkable tiles

Generation fails with an error if validation doesn't pass.

## Backend Changes

| File | Change |
|------|--------|
| `routers/town.py` `_load_map_data()` | `width: 128, height: 96, tileDim: 16`, new URLs |
| `core/town_constants.py` | Location coordinates derived from generator manifest |
| `core/services/town_pathfinding.py` | Refactor to be tileset-agnostic: derive walkability from Collision objectgroup rects (blocked) + Ground_Base emptiness (GID 0 = blocked, any GID > 0 = walkable). Remove hardcoded `_BLOCKED_GROUND_GIDS`. Map filename → `town-center.tmj`. Bump `max_iterations` to 10000 (4x more tiles than before). |

No changes to: TiledMapRenderer, tmjParser, town_simulation, PixiViewport.

## Frontend Changes

| File | Change |
|------|--------|
| `PixiGame.tsx` | Location label coordinates (from manifest), map/tileset URLs, layer name lists for both TiledMapRenderer instances |

The two `TiledMapRenderer` instances in `PixiGame.tsx` split layers:
- **Instance 1 (below agents):** Ground_Base, Ground_Detail, Water_Back, Terrain_Structures, Buildings_Base, Props_Back, Animation_Back
- **Instance 2 (above agents):** Props_Front, Foreground_Low, Foreground_High

This matches the current split and the layer names are already passed as props.

## Asset Registry Note

Concrete tile IDs for `asset-registry.ts` require manual inspection of the OGA tileset PNG. The implementation plan will include a task to download the tileset, map its grid layout, and populate the registry with actual tile coordinates. The interfaces defined in this spec are stable; only the numeric values depend on the specific tileset sheet.

## Reserved Layers

`Depth_Masks` and `Animation_Front` are emitted as empty layers (empty objectgroup and empty tilelayer respectively). They exist as placeholders for future use.

## Acceptance Criteria

- Generated town has readable districts and circulation paths.
- No terrain variant appears >3 times consecutively.
- All terrain boundaries have proper transition tiles.
- Buildings align to roads and face public space.
- Bridges connect both sides of the canal.
- Foreground layers render above players (rooftops, canopies).
- Collision metadata is separate from art.
- All 5 locations are reachable via A* pathfinding.
- Same seed produces identical output.
- Output loads in existing TiledMapRenderer without code changes.
