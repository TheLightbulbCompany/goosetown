#!/usr/bin/env python3
"""
City Map Generator for Goosetown

Combines two tileset PNGs into one composite image and generates a 64x48 tile
city map with background, detail, and object layers.

Usage:
    cd goosetown/scripts
    python generate_city_map.py

Requires: Pillow (pip install Pillow)

Output:
    - goosetown/public/assets/city-tileset.png  (combined tileset)
    - goosetown/data/city.ts                     (map data)
"""

import os
import sys
import math
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("ERROR: Pillow is required. Install with: pip install Pillow")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Paths (relative to this script's location)
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent  # goosetown/

RPG_TILESET_PATH = PROJECT_DIR / "public" / "assets" / "rpg-tileset.png"
MAGE_TILESET_PATH = PROJECT_DIR / "public" / "assets" / "magecity.png"
OUTPUT_TILESET_PATH = PROJECT_DIR / "public" / "assets" / "city-tileset.png"
OUTPUT_MAP_PATH = PROJECT_DIR / "data" / "city.ts"

# ---------------------------------------------------------------------------
# Map dimensions
# ---------------------------------------------------------------------------
MAP_W = 64   # tiles wide
MAP_H = 48   # tiles tall
TILE_DIM = 32  # pixels per tile

# ---------------------------------------------------------------------------
# Tileset layout
# ---------------------------------------------------------------------------
# RPG tileset: 1600x1600 = 50 cols x 50 rows (at 32px)
RPG_COLS = 50
RPG_ROWS = 50

# Magecity tileset: 256 wide = 8 cols, height varies (we'll handle it)
MAGE_COLS = 8

# Combined tileset width matches RPG tileset (1600px = 50 tile columns)
COMBINED_COLS = RPG_COLS  # 50


def mc(mage_index: int) -> int:
    """Convert a magecity tile index to a combined-tileset tile index.

    Magecity tiles are laid out in an 8-tile-wide grid. In the combined
    tileset they sit below the RPG tileset (which is 50 rows tall), still
    at their original column positions (0-7) but in combined rows 50+.
    """
    col = mage_index % MAGE_COLS
    row = mage_index // MAGE_COLS
    return col + (RPG_ROWS + row) * COMBINED_COLS


# ---------------------------------------------------------------------------
# Tile palette  (TUNE THESE in task #12)
#
#   RPG tileset indices: col + row*50, range 0-2499
#   Magecity indices: use mc(original_index)
#
# All indices refer to the COMBINED tileset.
# ---------------------------------------------------------------------------

# --- Grass / ground (RPG tileset) ---
# VERIFIED: all 100% opaque solid green fills via pixel analysis
GRASS_A = 51    # (r1,c1)  rgb(105,210,78) bright green, 1024/1024
GRASS_B = 501   # (r10,c1) rgb(162,212,74) lighter green, 1024/1024
GRASS_C = 2100  # (r42,c0) rgb(149,196,73) medium green, 1024/1024
GRASS_D = 2101  # (r42,c1) rgb(160,211,73) lighter green, 1024/1024
GRASS_E = 2102  # (r42,c2) rgb(146,187,74) medium green, 1024/1024
GRASS_F = 2050  # (r41,c0) rgb(149,193,73) medium green, 1012/1024

GRASS_TILES = [GRASS_A, GRASS_B, GRASS_C, GRASS_D, GRASS_E, GRASS_F]

# --- Stone / path (magecity rows 10-11 = warm gray cobblestone) ---
# These are from the magecity tileset (combined indices via mc())
STONE_A = mc(80)  # 3000 (r10,c0) rgb(154,139,126) warm gray, var=2977
STONE_B = mc(81)  # 3001 (r10,c1) rgb(160,145,131) warm gray, var=3044
STONE_C = mc(82)  # 3002 (r10,c2) rgb(154,139,126) warm gray, var=2892
STONE_D = mc(83)  # 3003 (r10,c3) rgb(158,144,130) warm gray, var=3062
STONE_E = mc(84)  # 3004 (r10,c4) rgb(158,144,130) warm gray, var=3039

STONE_TILES = [STONE_A, STONE_B, STONE_C, STONE_D, STONE_E]

# --- Plaza cobblestone (magecity rows 9 = slightly different warm stone) ---
COBBLE_A = mc(72)  # 2950 (r9,c0) rgb(149,133,121) warm stone, var=2642
COBBLE_B = mc(73)  # 2951 (r9,c1) rgb(152,137,124) warm stone, var=2891
COBBLE_C = mc(74)  # 2952 (r9,c2) rgb(148,132,120) warm stone, var=2557
COBBLE_D = mc(75)  # 2953 (r9,c3) rgb(158,143,129) lighter stone, var=3108

# --- Water (RPG tileset, verified solid blue fills) ---
WATER_A = 201   # (r4,c1)   rgb(121,155,255) solid blue, 1024/1024
WATER_B = 206   # (r4,c6)   rgb(121,155,255) solid blue, 1024/1024
WATER_C = 351   # (r7,c1)   rgb(121,155,255) solid blue, 1024/1024
WATER_D = 356   # (r7,c6)   rgb(121,155,255) solid blue, 1024/1024

# --- Trees (RPG tileset, rows 25-26) ---
# VERIFIED: dark green fills
TREE_TL = 1252  # (r25,c2) rgb(118,169,71) green, 1024/1024
TREE_TR = 1253  # (r25,c3) rgb(77,119,70) darker, 780/1024
TREE_BL = 1302  # (r26,c2) rgb(61,88,71) dark green, 1024/1024
TREE_BR = 1303  # (r26,c3) rgb(61,88,71)

# Single-tile tree/bush
BUSH = 1200     # (r24,c0) rgb(85,132,70) dark green, 880/1024

# --- Building tiles (magecity beige brick + dark slate roof) ---
# Beige brick walls (magecity row 4) - building body from top-down
BRICK_A   = mc(32)  # 2700 (r4,c0) rgb(175,145,95)  beige brick, var=5956
BRICK_B   = mc(34)  # 2702 (r4,c2) rgb(176,147,97)  beige brick, var=6254
BRICK_C   = mc(36)  # 2704 (r4,c4) rgb(171,138,92)  beige brick, var=5506
BRICK_D   = mc(37)  # 2705 (r4,c5) rgb(162,130,83)  beige brick, var=5731
# Dark slate roof (magecity row 7) - building roof edge
SLATE_A   = mc(56)  # 2850 (r7,c0) rgb(43,55,77)    dark slate, var=1032
SLATE_B   = mc(57)  # 2851 (r7,c1) rgb(40,51,74)    dark slate, var=0 (flat!)
SLATE_C   = mc(58)  # 2852 (r7,c2) rgb(42,54,76)    dark slate, var=796
# Door tile (flat brown from RPG tileset)
DOOR_TILE = 78      # (r1,c28)  rgb(154,114,57) flat brown, var=0
# Aliases used by place_building
ROOF_FILL = SLATE_B
WALL_FILL = BRICK_A

EMPTY = -1  # transparent / no tile


# ===========================================================================
# Image combining
# ===========================================================================

def combine_tilesets():
    """Stack rpg-tileset on top, magecity below, into one combined PNG."""
    print(f"Loading RPG tileset: {RPG_TILESET_PATH}")
    rpg = Image.open(RPG_TILESET_PATH).convert("RGBA")
    print(f"  Size: {rpg.size}")

    print(f"Loading Magecity tileset: {MAGE_TILESET_PATH}")
    mage = Image.open(MAGE_TILESET_PATH).convert("RGBA")
    print(f"  Size: {mage.size}")

    # Calculate combined dimensions
    combined_w = rpg.width  # 1600
    # Round magecity height up to next tile boundary
    mage_tile_rows = math.ceil(mage.height / TILE_DIM)
    mage_padded_h = mage_tile_rows * TILE_DIM
    combined_h = rpg.height + mage_padded_h

    print(f"Combined tileset: {combined_w}x{combined_h} "
          f"({combined_w // TILE_DIM} cols x {combined_h // TILE_DIM} rows)")

    combined = Image.new("RGBA", (combined_w, combined_h), (0, 0, 0, 0))
    combined.paste(rpg, (0, 0))
    combined.paste(mage, (0, rpg.height))

    os.makedirs(OUTPUT_TILESET_PATH.parent, exist_ok=True)
    combined.save(OUTPUT_TILESET_PATH)
    print(f"Saved combined tileset: {OUTPUT_TILESET_PATH}")

    return combined_w, combined_h


# ===========================================================================
# Map generation helpers
# ===========================================================================

def make_layer(default=-1):
    """Create a 64x48 layer: layer[x][y], column-major."""
    return [[default for _ in range(MAP_H)] for _ in range(MAP_W)]


def fill_rect(layer, x1, y1, x2, y2, tile):
    """Fill a rectangle (inclusive) with a single tile value."""
    for x in range(max(0, x1), min(MAP_W, x2 + 1)):
        for y in range(max(0, y1), min(MAP_H, y2 + 1)):
            layer[x][y] = tile


def fill_rect_pattern(layer, x1, y1, x2, y2, tiles):
    """Fill a rectangle with tiles cycling from a list."""
    idx = 0
    for x in range(max(0, x1), min(MAP_W, x2 + 1)):
        for y in range(max(0, y1), min(MAP_H, y2 + 1)):
            layer[x][y] = tiles[idx % len(tiles)]
            idx += 1


def fill_rect_checker(layer, x1, y1, x2, y2, tile_a, tile_b):
    """Fill a rectangle with a checkerboard pattern."""
    for x in range(max(0, x1), min(MAP_W, x2 + 1)):
        for y in range(max(0, y1), min(MAP_H, y2 + 1)):
            layer[x][y] = tile_a if (x + y) % 2 == 0 else tile_b


def place_2x2_tree(layer, x, y):
    """Place a 2x2 tree at (x, y) = top-left corner."""
    if x + 1 < MAP_W and y + 1 < MAP_H:
        layer[x][y] = TREE_TL
        layer[x + 1][y] = TREE_TR
        layer[x][y + 1] = TREE_BL
        layer[x + 1][y + 1] = TREE_BR


def place_building(obj_layer, detail_layer, x1, y1, x2, y2):
    """Place a building footprint: brick body with slate roof border on detail,
    solid collision on obj layer."""
    # Object layer: entire building is solid (blocks walking)
    fill_rect(obj_layer, x1, y1, x2, y2, BRICK_A)

    # Detail layer: slate border with beige brick interior
    # Border (1 tile) = dark slate roof edge
    fill_rect(detail_layer, x1, y1, x2, y2, SLATE_B)

    # Interior (inset 1 tile) = beige brick body
    if x2 - x1 >= 2 and y2 - y1 >= 2:
        # Alternate brick variants for visual interest
        for x in range(x1 + 1, x2):
            for y in range(y1 + 1, y2):
                brick = [BRICK_A, BRICK_B, BRICK_C, BRICK_D]
                detail_layer[x][y] = brick[(x + y) % len(brick)]

        # Door in bottom-center
        door_x = (x1 + x2) // 2
        detail_layer[door_x][y2] = DOOR_TILE


def place_apartment(obj_layer, detail_layer, x1, y1, x2, y2):
    """Place an apartment building -- larger and visually distinct from houses.

    Uses a double-slate border and alternating brick pattern to look like a
    multi-unit residential building.  Has two doors instead of one.
    """
    # Object layer: entire building is solid (blocks walking)
    fill_rect(obj_layer, x1, y1, x2, y2, BRICK_A)

    # Detail layer: double-slate border for a heavier, taller look
    fill_rect(detail_layer, x1, y1, x2, y2, SLATE_A)

    # Inner border ring uses SLATE_C for a two-tone roof effect
    if x2 - x1 >= 2 and y2 - y1 >= 2:
        fill_rect(detail_layer, x1 + 1, y1 + 1, x2 - 1, y2 - 1, SLATE_C)

    # Interior brick body with denser alternation
    if x2 - x1 >= 4 and y2 - y1 >= 4:
        for x in range(x1 + 2, x2 - 1):
            for y in range(y1 + 2, y2 - 1):
                brick = [BRICK_B, BRICK_D, BRICK_C, BRICK_A]
                detail_layer[x][y] = brick[(x * 3 + y * 2) % len(brick)]
    elif x2 - x1 >= 2 and y2 - y1 >= 2:
        # Fallback for smaller apartment (still distinct)
        for x in range(x1 + 1, x2):
            for y in range(y1 + 1, y2):
                brick = [BRICK_B, BRICK_D, BRICK_C, BRICK_A]
                detail_layer[x][y] = brick[(x * 3 + y * 2) % len(brick)]

    # Two doors side-by-side at the bottom (multi-unit entrance)
    mid_x = (x1 + x2) // 2
    detail_layer[mid_x][y2] = DOOR_TILE
    if mid_x - 1 >= x1:
        detail_layer[mid_x - 1][y2] = DOOR_TILE


# ===========================================================================
# Map generation
# ===========================================================================

def generate_city_map():
    """Generate the full city map (64x48) with all layers."""

    # Background layer 0: base terrain (grass)
    bg_base = make_layer(default=GRASS_A)

    # Fill with grass variants for visual interest
    for x in range(MAP_W):
        for y in range(MAP_H):
            # Use a deterministic pseudo-random pattern
            v = (x * 7 + y * 13 + x * y * 3) % len(GRASS_TILES)
            bg_base[x][y] = GRASS_TILES[v]

    # Background layer 1: detail (paths, plazas) -- mostly empty
    bg_detail = make_layer(default=EMPTY)

    # Object layer 0: collision (buildings=solid, trees=solid, -1=walkable)
    obj = make_layer(default=EMPTY)

    # ------------------------------------------------------------------
    # 1. Border: Trees along all 4 edges (2 tiles deep)
    # ------------------------------------------------------------------
    # Top border (y=0-1)
    for x in range(0, MAP_W, 2):
        for y_off in [0]:
            place_2x2_tree(obj, x, y_off)

    # Bottom border (y=46-47)
    for x in range(0, MAP_W, 2):
        place_2x2_tree(obj, x, MAP_H - 2)

    # Left border (x=0-1)
    for y in range(0, MAP_H, 2):
        place_2x2_tree(obj, 0, y)

    # Right border (x=62-63)
    for y in range(0, MAP_H, 2):
        place_2x2_tree(obj, MAP_W - 2, y)

    # ------------------------------------------------------------------
    # 2. Main Street: Vertical stone path at x=30-33, full height
    # ------------------------------------------------------------------
    fill_rect_checker(bg_detail, 30, 2, 33, MAP_H - 3, STONE_A, STONE_B)

    # ------------------------------------------------------------------
    # 3. Cross Street: Horizontal stone path at y=22-25, full width
    # ------------------------------------------------------------------
    fill_rect_checker(bg_detail, 2, 22, MAP_W - 3, 25, STONE_C, STONE_D)

    # ------------------------------------------------------------------
    # 4. Town Plaza: Stone area at center (28-35 x 20-27)
    # ------------------------------------------------------------------
    fill_rect_checker(bg_detail, 28, 20, 35, 27, STONE_B, STONE_E)

    # Plaza border accent (cobblestone ring)
    for x in range(28, 36):
        bg_detail[x][20] = COBBLE_A
        bg_detail[x][27] = COBBLE_C
    for y in range(20, 28):
        bg_detail[28][y] = COBBLE_B
        bg_detail[35][y] = COBBLE_D

    # ------------------------------------------------------------------
    # 5. Residential: 5 houses + apartment in northwest (around x=6-20, y=5-14)
    # ------------------------------------------------------------------
    # House 1: x=6-9, y=5-8
    place_building(obj, bg_detail, 6, 5, 9, 8)
    # House 2: x=11-14, y=5-8
    place_building(obj, bg_detail, 11, 5, 14, 8)
    # House 3: x=16-19, y=5-8
    place_building(obj, bg_detail, 16, 5, 19, 8)
    # House 4: x=6-9, y=10-13
    place_building(obj, bg_detail, 6, 10, 9, 13)
    # House 5: x=11-14, y=10-13
    place_building(obj, bg_detail, 11, 10, 14, 13)

    # Apartment building: x=16-20, y=10-14 (5x5, larger than 4x4 houses)
    # This is the spawn point for agents opting into GooseTown.
    place_apartment(obj, bg_detail, 16, 10, 20, 14)

    # ------------------------------------------------------------------
    # 6. Cafe: Building at west-center (x=8-12, y=18-21)
    # ------------------------------------------------------------------
    place_building(obj, bg_detail, 8, 18, 12, 21)

    # ------------------------------------------------------------------
    # 7. Library: Building at east-center (x=49-54, y=18-21)
    # ------------------------------------------------------------------
    place_building(obj, bg_detail, 49, 18, 54, 21)

    # ------------------------------------------------------------------
    # 8. General Store: Building at west-south (x=8-12, y=34-37)
    # ------------------------------------------------------------------
    place_building(obj, bg_detail, 8, 34, 12, 37)

    # ------------------------------------------------------------------
    # 9. Workshop: Building at southeast (x=47-51, y=34-37)
    # ------------------------------------------------------------------
    place_building(obj, bg_detail, 47, 34, 51, 37)

    # ------------------------------------------------------------------
    # 10. Park: Trees scattered in east-north (x=42-56, y=5-14)
    # ------------------------------------------------------------------
    park_trees = [
        (42, 5), (44, 7), (46, 5), (48, 9), (50, 6),
        (52, 5), (54, 8), (43, 11), (46, 12), (49, 11),
        (52, 13), (55, 10), (44, 9), (50, 13), (54, 5),
    ]
    for tx, ty in park_trees:
        place_2x2_tree(obj, tx, ty)

    # Also scatter some bushes in the park
    park_bushes = [
        (43, 6), (45, 8), (47, 6), (51, 7), (53, 10),
        (45, 13), (48, 12), (55, 6), (42, 10), (47, 14),
    ]
    for bx, by in park_bushes:
        if 0 <= bx < MAP_W and 0 <= by < MAP_H:
            obj[bx][by] = BUSH

    # ------------------------------------------------------------------
    # 11. Fountain: 2x2 water tiles at plaza center (31-32, 23-24)
    # ------------------------------------------------------------------
    bg_detail[31][23] = WATER_A
    bg_detail[32][23] = WATER_B
    bg_detail[31][24] = WATER_C
    bg_detail[32][24] = WATER_D
    # Fountain is also a collision object (can't walk through water)
    obj[31][23] = WATER_A
    obj[32][23] = WATER_B
    obj[31][24] = WATER_C
    obj[32][24] = WATER_D

    # ------------------------------------------------------------------
    # 12. Street trees: Along Main Street every 6 tiles
    # ------------------------------------------------------------------
    for y in range(4, MAP_H - 4, 6):
        # Skip if overlapping with cross street or plaza
        if 20 <= y <= 27:
            continue
        # Trees on left side of main street (x=28-29)
        place_2x2_tree(obj, 28, y)
        # Trees on right side of main street (x=34-35)
        place_2x2_tree(obj, 34, y)

    # ------------------------------------------------------------------
    # Assemble layers
    # ------------------------------------------------------------------
    bgtiles = [bg_base, bg_detail]   # 2 layers
    objmap = [obj]                   # 1 layer

    return bgtiles, objmap


# ===========================================================================
# TypeScript output
# ===========================================================================

def layer_to_ts(layer):
    """Convert a [x][y] layer to TypeScript array literal string.

    Output format matches gentle.js: each column is a JS array of row values.
    layer[x] is an array of MAP_H values (one per row).
    """
    lines = []
    lines.append("   [")
    for x in range(len(layer)):
        row_vals = " , ".join(str(layer[x][y]) for y in range(len(layer[x])))
        lines.append(f"[ {row_vals} , ],")
    lines.append("],")
    return "\n".join(lines)


def write_city_ts(bgtiles, objmap, tileset_w, tileset_h):
    """Write the city.ts map data file."""
    os.makedirs(OUTPUT_MAP_PATH.parent, exist_ok=True)

    with open(OUTPUT_MAP_PATH, "w") as f:
        f.write('// City map generated by generate_city_map.py\n')
        f.write('\n')
        f.write('export const tilesetpath = "/assets/city-tileset.png"\n')
        f.write(f'export const tiledim = {TILE_DIM}\n')
        f.write(f'export const screenxtiles = {MAP_W}\n')
        f.write(f'export const screenytiles = {MAP_H}\n')
        f.write(f'export const tilesetpxw = {tileset_w}\n')
        f.write(f'export const tilesetpxh = {tileset_h}\n')
        f.write('\n')

        # bgtiles: 2 layers
        f.write('export const bgtiles = [\n')
        for layer in bgtiles:
            f.write(layer_to_ts(layer))
            f.write('\n')
        f.write('];\n')
        f.write('\n')

        # objmap: 1 layer
        f.write('export const objmap = [\n')
        for layer in objmap:
            f.write(layer_to_ts(layer))
            f.write('\n')
        f.write('];\n')
        f.write('\n')

        # animatedsprites: empty
        f.write('export const animatedsprites: any[] = []\n')
        f.write('\n')

        # mapwidth / mapheight
        f.write('export const mapwidth = bgtiles[0].length   // 64\n')
        f.write('export const mapheight = bgtiles[0][0].length  // 48\n')

    print(f"Saved map data: {OUTPUT_MAP_PATH}")


# ===========================================================================
# Main
# ===========================================================================

def main():
    print("=" * 60)
    print("City Map Generator")
    print("=" * 60)
    print()

    # 1. Combine tilesets
    tileset_w, tileset_h = combine_tilesets()
    print()

    # 2. Generate map
    print("Generating 64x48 city map...")
    bgtiles, objmap = generate_city_map()
    print(f"  bgtiles: {len(bgtiles)} layers, each {len(bgtiles[0])}x{len(bgtiles[0][0])}")
    print(f"  objmap:  {len(objmap)} layers, each {len(objmap[0])}x{len(objmap[0][0])}")
    print()

    # 3. Write TypeScript output
    write_city_ts(bgtiles, objmap, tileset_w, tileset_h)
    print()

    # 4. Summary
    print("=" * 60)
    print("Done!")
    print(f"  Combined tileset: {OUTPUT_TILESET_PATH}")
    print(f"  Map data:         {OUTPUT_MAP_PATH}")
    print(f"  Tileset size:     {tileset_w}x{tileset_h} px")
    print(f"  Tileset grid:     {tileset_w // TILE_DIM}x{tileset_h // TILE_DIM} tiles")
    print(f"  Map size:         {MAP_W}x{MAP_H} tiles")
    print(f"  BG layers:        {len(bgtiles)}")
    print(f"  OBJ layers:       {len(objmap)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
