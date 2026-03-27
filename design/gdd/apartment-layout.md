# Apartment Map Layout — GooseTown

**Level Name:** Agent Apartment Interior
**Theme:** Cozy shared living space for 2-3 AI agents. Functional and lived-in, not sterile.
**Estimated Play Time:** Transitional space, not a standalone level. Agents pass through on sleep/work cycles. ~30 seconds of navigation per visit.
**Grid Size:** 12 x 8 tiles (384 x 256 pixels at 32px/tile)
**Tile Size:** 32 x 32 pixels

---

## Room Summary

| Room | Tile Columns (x) | Tile Rows (y) | Floor Tile | Wall Color |
|---|---|---|---|---|
| Office | 1-5 | 1-2 | wood_planks | wall_blue (exterior), wall_beige (interior) |
| Kitchen | 7-10 | 1-2 | kitchen_tile | wall_window (exterior), wall_beige (interior) |
| Hallway | 1-10 | 3 | stone | — (open) |
| Living Room | 1-5 | 4-6 | carpet | wall_beige |
| Bedroom | 7-10 | 4-6 | carpet | wall_beige |
| Perimeter Walls | 0, 11 | all | — | wall_beige, wall_corner |
| Top Wall | all | 0 | — | wall_window, wall_corner |
| Bottom Wall | all | 7 | — | wall_beige, door_frame at exit |

---

## Critical Path

Entry is via exit at (6,7) from the town (residence location). Agents spawn at the exit marker and navigate inward via the hallway at y=3. All four rooms branch off the hallway. The exit is the only way in or out.

**Inbound route:** (6,7) -> (6,6) -> (6,5) -> (6,4) -> (6,3) hallway -> room destination
**Outbound route:** reverse from room -> (6,3) -> (6,7) exit

The center dividing wall at x=6 (with doorway open only at y=3) means agents must pass through the hallway to cross between the left side (office, living room) and right side (kitchen, bedroom). This is intentional: it creates social chokepoints where agents naturally encounter each other in the hall.

---

## Visual Grid — Tile Placement

Legend:
```
FLOORS          WALLS              FURNITURE / DETAILS
WP = wood_planks   WB = wall_beige    DC = desk_computer
KT = kitchen_tile  WW = wall_window   KT_F = kitchen_table
CA = carpet        WC = wall_corner   BS = bookshelf
ST = stone         DF = door_frame    CO = couch
                   WBK= wall_bookshelf BED= bed
                   WBL= wall_blue     TV = tv_stand
                                      PL = plant
                                      KC = kitchen_counter
                                      FR = fridge
                                      FL = floor_lamp
                                      CT = coffee_table
                                      CR = coat_rack
```

### Full Grid (y=0 is top, y=7 is bottom)

```
     x=0    x=1    x=2    x=3    x=4    x=5    x=6    x=7    x=8    x=9   x=10   x=11
y=0  [WC ]  [WW ]  [WW ]  [WW ]  [WW ]  [WW ]  [WB ]  [WW ]  [WW ]  [WW ]  [WW ]  [WC ]
y=1  [WB ]  [PL ]  [DC ]  [WP ]  [DC ]  [WP ]  [WB ]  [KC ]  [WP ]  [FR ]  [BS ]  [WB ]
y=2  [WB ]  [WP ]  [WP ]  [DC ]  [WP ]  [WP ]  [WB ]  [WP ]  [KT_F] [WP ]  [WP ]  [WB ]
y=3  [CR ]  [ST ]  [ST ]  [ST ]  [ST ]  [ST ]  [ST ]  [ST ]  [ST ]  [ST ]  [ST ]  [WB ]
y=4  [WB ]  [CA ]  [CA ]  [CA ]  [CA ]  [CA ]  [WB ]  [CA ]  [CA ]  [CA ]  [CA ]  [WB ]
y=5  [WB ]  [CA ]  [TV ]  [CA ]  [FL ]  [CA ]  [WB ]  [CA ]  [CA ]  [CA ]  [CA ]  [WB ]
y=6  [WB ]  [CA ]  [CO ]  [CO ]  [CT ]  [CA ]  [WB ]  [CA ]  [CA ]  [BED]  [BED]  [WB ]
y=7  [WB ]  [WB ]  [WB ]  [WB ]  [WB ]  [WB ]  [DF ]  [WB ]  [WB ]  [WB ]  [WB ]  [WB ]
```

---

## Per-Cell Tile Specification

Full explicit placement for every cell. Format: `(x,y): tile_name [BLOCKED/WALKABLE]`

### y=0 — Top Exterior Wall (all blocked)

| Cell | Tile | Note |
|---|---|---|
| (0,0) | wall_corner | top-left corner |
| (1,0) | wall_window | office exterior window |
| (2,0) | wall_window | office exterior window |
| (3,0) | wall_window | office exterior window |
| (4,0) | wall_window | office exterior window |
| (5,0) | wall_window | office exterior window |
| (6,0) | wall_beige | solid top above center divider |
| (7,0) | wall_window | kitchen exterior window |
| (8,0) | wall_window | kitchen exterior window |
| (9,0) | wall_window | kitchen exterior window |
| (10,0) | wall_window | kitchen exterior window |
| (11,0) | wall_corner | top-right corner |

### y=1 — Office Row / Kitchen Row (left wall + right wall blocked; interior walkable)

| Cell | Tile | Walkable | Note |
|---|---|---|---|
| (0,1) | wall_beige | BLOCKED | left perimeter wall |
| (1,1) | plant | WALKABLE | office corner detail; plant near window |
| (2,1) | desk_computer | WALKABLE | desk_1 spot — agent sits here to work |
| (3,1) | wood_planks | WALKABLE | open office floor |
| (4,1) | desk_computer | WALKABLE | desk_2 spot |
| (5,1) | wood_planks | WALKABLE | open office floor |
| (6,1) | wall_beige | BLOCKED | center dividing wall |
| (7,1) | kitchen_counter | WALKABLE | kitchen prep counter against divider |
| (8,1) | kitchen_tile | WALKABLE | open kitchen floor |
| (9,1) | fridge | WALKABLE | fridge against top wall |
| (10,1) | bookshelf | WALKABLE | bookshelf spot (recipe books, etc.) |
| (11,1) | wall_beige | BLOCKED | right perimeter wall |

### y=2 — Office Row / Kitchen Table Row (left wall + right wall blocked; interior walkable)

| Cell | Tile | Walkable | Note |
|---|---|---|---|
| (0,2) | wall_beige | BLOCKED | left perimeter wall |
| (1,2) | wood_planks | WALKABLE | open office floor |
| (2,2) | wood_planks | WALKABLE | open office floor |
| (3,2) | desk_computer | WALKABLE | desk_3 spot (middle desk) |
| (4,2) | wood_planks | WALKABLE | open office floor |
| (5,2) | wood_planks | WALKABLE | open office floor |
| (6,2) | wall_beige | BLOCKED | center dividing wall |
| (7,2) | kitchen_tile | WALKABLE | kitchen floor |
| (8,2) | kitchen_table | WALKABLE | table spot — agent eats/reads here |
| (9,2) | kitchen_tile | WALKABLE | kitchen floor |
| (10,2) | kitchen_tile | WALKABLE | kitchen floor |
| (11,2) | wall_beige | BLOCKED | right perimeter wall |

### y=3 — Hallway (left wall blocked; x=6 open doorway; right wall blocked)

| Cell | Tile | Walkable | Note |
|---|---|---|---|
| (0,3) | coat_rack | BLOCKED | detail on left wall; agents see it passing through |
| (1,3) | stone | WALKABLE | hallway floor |
| (2,3) | stone | WALKABLE | hallway floor |
| (3,3) | stone | WALKABLE | hallway floor |
| (4,3) | stone | WALKABLE | hallway floor |
| (5,3) | stone | WALKABLE | hallway floor |
| (6,3) | stone | WALKABLE | center doorway — open passage, no tile block |
| (7,3) | stone | WALKABLE | hallway floor |
| (8,3) | stone | WALKABLE | hallway floor |
| (9,3) | stone | WALKABLE | hallway floor |
| (10,3) | stone | WALKABLE | hallway floor |
| (11,3) | wall_beige | BLOCKED | right perimeter wall |

### y=4 — Living Room Row / Bedroom Row (left wall + center divider + right wall blocked; interior walkable)

| Cell | Tile | Walkable | Note |
|---|---|---|---|
| (0,4) | wall_beige | BLOCKED | left perimeter wall |
| (1,4) | carpet | WALKABLE | living room floor |
| (2,4) | carpet | WALKABLE | living room floor |
| (3,4) | carpet | WALKABLE | living room floor |
| (4,4) | carpet | WALKABLE | living room floor |
| (5,4) | carpet | WALKABLE | living room floor |
| (6,4) | wall_beige | BLOCKED | center dividing wall |
| (7,4) | carpet | WALKABLE | bedroom antechamber floor |
| (8,4) | carpet | WALKABLE | bedroom floor |
| (9,4) | carpet | WALKABLE | bedroom floor |
| (10,4) | carpet | WALKABLE | bedroom floor |
| (11,4) | wall_beige | BLOCKED | right perimeter wall |

### y=5 — Living Room Activity Row / Bedroom Row (left wall + center divider + right wall blocked; interior walkable)

| Cell | Tile | Walkable | Note |
|---|---|---|---|
| (0,5) | wall_beige | BLOCKED | left perimeter wall |
| (1,5) | carpet | WALKABLE | living room floor |
| (2,5) | tv_stand | WALKABLE | tv_chair spot; tv_stand faces into the room toward couch |
| (3,5) | carpet | WALKABLE | living room floor |
| (4,5) | floor_lamp | WALKABLE | lamp detail; warm light source implied |
| (5,5) | carpet | WALKABLE | living room floor |
| (6,5) | wall_beige | BLOCKED | center dividing wall |
| (7,5) | carpet | WALKABLE | bedroom floor |
| (8,5) | carpet | WALKABLE | bedroom floor |
| (9,5) | carpet | WALKABLE | bedroom floor |
| (10,5) | carpet | WALKABLE | bedroom floor |
| (11,5) | wall_beige | BLOCKED | right perimeter wall |

### y=6 — Couch Row / Bed Row (left wall + center divider + right wall blocked; interior walkable)

| Cell | Tile | Walkable | Note |
|---|---|---|---|
| (0,6) | wall_beige | BLOCKED | left perimeter wall |
| (1,6) | carpet | WALKABLE | living room floor |
| (2,6) | couch | WALKABLE | couch_1 spot |
| (3,6) | couch | WALKABLE | couch_2 spot |
| (4,6) | coffee_table | WALKABLE | coffee_table in front of couch; agent pathfinds around it |
| (5,6) | carpet | WALKABLE | living room floor |
| (6,6) | wall_beige | BLOCKED | center dividing wall |
| (7,6) | carpet | WALKABLE | bedroom floor between door and beds |
| (8,6) | carpet | WALKABLE | bedroom floor |
| (9,6) | bed | WALKABLE | bed_1 spot |
| (10,6) | bed | WALKABLE | bed_2 spot |
| (11,6) | wall_beige | BLOCKED | right perimeter wall |

### y=7 — Bottom Exterior Wall (all blocked except exit at x=6)

| Cell | Tile | Walkable | Note |
|---|---|---|---|
| (0,7) | wall_beige | BLOCKED | bottom-left corner |
| (1,7) | wall_beige | BLOCKED | bottom wall |
| (2,7) | wall_beige | BLOCKED | bottom wall |
| (3,7) | wall_beige | BLOCKED | bottom wall |
| (4,7) | wall_beige | BLOCKED | bottom wall |
| (5,7) | wall_beige | BLOCKED | bottom wall; plant here as detail (see below) |
| (6,7) | door_frame | WALKABLE | EXIT — entry from town; door_frame tile |
| (7,7) | wall_beige | BLOCKED | bottom wall |
| (8,7) | wall_beige | BLOCKED | bottom wall |
| (9,7) | wall_beige | BLOCKED | bottom wall |
| (10,7) | wall_beige | BLOCKED | bottom wall |
| (11,7) | wall_beige | BLOCKED | bottom-right corner |

---

## Furniture and Object Spot Cross-Reference

This table maps every named spot in `apartment_constants.py` and `ApartmentMap.tscn` to its tile assignment. Marker2D pixel positions are at tile center (tile_x * 32 + 16, tile_y * 32 + 16) but the Marker2D uses top-left of tile (tile * 32), so the existing scene values are tile-aligned.

| Spot Name | Grid (x,y) | Pixel Position | Tile Placed | Layer |
|---|---|---|---|---|
| desk_1 | (2,1) | (64, 32) | desk_computer | Furniture |
| desk_2 | (4,1) | (128, 32) | desk_computer | Furniture |
| desk_3 | (3,2) | (96, 64) | desk_computer | Furniture |
| table (kitchen) | (8,2) | (256, 64) | kitchen_table | Furniture |
| bookshelf | (10,1) | (320, 32) | bookshelf | Furniture |
| couch_1 | (2,6) | (64, 192) | couch | Furniture |
| couch_2 | (3,6) | (96, 192) | couch | Furniture |
| tv_chair | (2,5) | (64, 160) | tv_stand | Furniture |
| bed_1 | (9,6) | (288, 192) | bed | Furniture |
| bed_2 | (10,6) | (320, 192) | bed | Furniture |
| fridge | (9,1) | (288, 32) | fridge | Details |
| plant | (1,1) | (32, 32) | plant | Details |
| exit | (6,7) | (192, 224) | door_frame | Wall |

Additional detail placements (not in apartment_constants.py, purely visual):

| Detail | Grid (x,y) | Tile | Reason |
|---|---|---|---|
| coat_rack | (0,3) | coat_rack | Hallway left wall — decoration on blocked cell |
| kitchen_counter | (7,1) | kitchen_counter | Kitchen prep surface against center divider |
| floor_lamp | (4,5) | floor_lamp | Living room ambient light |
| coffee_table | (4,6) | coffee_table | In front of couch — social focal point |

---

## Pacing and Flow Notes

**Chokepoint:** x=6, y=3 is the only cross-room passage. This is a deliberate social chokepoint. Two agents moving in opposite directions will briefly occupy the same hallway tile, triggering the proximity detection (within 3 tiles) and potentially a conversation. This is the most social tile in the apartment.

**Sight Lines from Exit:**
- An agent entering at (6,7) and moving north sees the hallway stone floor stretching left and right at y=3. The coat_rack on the left wall at (0,3) and the open stone corridor communicate "hallway — choose a direction."
- The wood_planks of the office are visible to the left; the carpet of the bedroom is visible to the right. Floor texture alone communicates room purpose before any furniture is visible.

**Rest vs. Work Separation:**
- Office and Kitchen (top half) use hard, clean floor textures — wood_planks and kitchen_tile. These signal productive activity.
- Living Room and Bedroom (bottom half) use carpet. Soft texture signals rest and low-energy activities.
- The hallway stone acts as a neutral transition between these two zones.

**Navigation Clarity:**
- The only walkable bottom-wall cell is the exit at (6,7). An agent pathfinding to exit will always arrive at the door_frame tile. There is no ambiguity.
- The center divider (x=6) is visually continuous except at y=3. Agents never have to learn a complex route — the gap in the wall is the only option.

---

## Environmental Storytelling Beats

1. **The shared desks (y=1, office):** Three desk_computer tiles with different positions suggest multiple agents hot-desking — no assigned seating. This reinforces the shared-apartment feel.
2. **Two beds side-by-side (y=6, bedroom):** bed_1 and bed_2 at (9,6) and (10,6) are adjacent. When two agents are both sleeping, they occupy this row together. This is a quiet visual moment of cohabitation.
3. **Couch facing tv_stand:** The couch tiles at (2,6) and (3,6) face the tv_stand at (2,5). An agent sitting on the couch and an agent watching TV occupy the same corner — natural convergence for the "watch TV" and "relax on couch" activities.
4. **Plant near office window (1,1):** A plant next to the wall_window tile suggests an agent who cares for living things — a small personality signal in the environment.
5. **Coat rack in hallway (0,3):** The first thing visible when entering the apartment from the left side of the hallway. Signals domesticity and arrival ritual.

---

## Audio Cue Regions

| Region | Trigger | Audio Note |
|---|---|---|
| Any tile in Office (x 1-5, y 1-2) | Agent enters room | Ambient: quiet keyboard typing, subtle hum |
| Any tile in Kitchen (x 7-10, y 1-2) | Agent enters room | Ambient: light kitchen sounds, soft radio |
| Any tile in Living Room (x 1-5, y 4-6) | Agent enters room | Ambient: TV murmur at low volume |
| Any tile in Bedroom (x 7-10, y 4-6) | Agent enters room | Ambient: near-silence, soft night sounds |
| (6,3) hallway center | Two agents within 3 tiles | Proximity trigger: no extra audio, let dialogue system lead |
| (6,7) exit | Agent walks through | Sound: door open/close |

---

## Optional Paths and Secrets

There are no secret areas in the apartment — it is a domestic space, not an adventure level. However, two optional interactions exist:

1. **Bookshelf in kitchen (10,1):** The `bookshelf` tile is placed in the kitchen, not a library. An agent who navigates to this spot is doing something slightly unusual — reading in the kitchen. This is a low-key personality expression available to agents whose LLM decides to use it.
2. **Coffee table (4,6):** Not a named spot in apartment_constants.py. If the activity system adds a "put down a mug" or "set something on the table" micro-action, this tile is the intended target. No current behavior is wired to it — it is a reserved detail.

---

## Implementation Notes for Godot

- **TileMapLayer:** All floor tiles go on layer 0. All wall tiles go on layer 1. Furniture and detail tiles go on layer 2. This ensures correct draw order without z-index tricks.
- **NavigationRegion2D:** The walkability grid in `apartment_constants.py` defines the navmesh polygon. Blocked cells (value 1) become navmesh exclusion rectangles. The NavigationRegion2D polygon should match the APARTMENT_OBJMAP exactly — do not approximate.
- **Marker2D positions:** All spot Markers in `ApartmentMap.tscn` are already placed. Their pixel positions match the tile grid (x_tile * 32, y_tile * 32). No changes needed to existing Marker2D nodes.
- **door_frame tile at (6,7):** This cell is both a wall tile visually and walkable in the navmesh. It should be on the wall layer (layer 1) but excluded from navmesh collision. The exit Marker2D at position Vector2(192, 224) sits at the center of this tile (6*32=192, 7*32=224).
- **wall_bookshelf:** Not currently assigned to any cell. It can replace `wall_beige` at (0,1) or (0,2) on the left office wall as a visual variant if the art director wants more texture variety. Not required for the initial implementation.

---

## Relationship to apartment_constants.py

The tile layout above is designed to be fully consistent with `APARTMENT_OBJMAP` as defined in `apartment_constants.py`. No walkability values need to change. The furniture tile positions match every named spot in `APARTMENT_SPOTS`. The exit at (6,7) matches `RESIDENTIAL_TOWN_COORDS` transition logic.

If new spots are added to `APARTMENT_SPOTS` in the future, the following cells are available (walkable and currently holding only a plain floor tile):

- Office: (1,2), (5,1), (5,2)
- Kitchen: (8,1), (10,2)
- Living Room: (1,4), (1,5), (1,6), (5,4), (5,5)
- Bedroom: (7,4), (7,5), (7,6), (8,4), (8,5), (8,6)
- Hallway: (1,3) through (5,3), (7,3) through (10,3)
