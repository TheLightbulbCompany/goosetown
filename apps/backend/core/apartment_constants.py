"""Constants for apartment interior navigation.

24x16 tile grid (768x512 pixels at 32px/tile, displayed at 2x = 1536x1024).

Layout (24 wide x 16 tall):
┌────────────────────────────────────────────────┐
│                  TOP WALL                       │
├───────────┬────────────┬───────────┬────────────┤
│           │            │           │            │
│  OFFICE   │  KITCHEN   │ BATHROOM  │  STORAGE   │
│  (desks,  │  (table,   │ (future)  │  (future)  │
│  computer)│  fridge,   │           │            │
│           │  counter)  │           │            │
├───────────┴────────────┴───────────┴────────────┤
│                   HALLWAY                        │
├───────────┬────────────┬────────────────────────┤
│           │            │                        │
│  LIVING   │  DINING    │      BEDROOM           │
│  ROOM     │  AREA      │      (beds,            │
│  (couch,  │  (table,   │       wardrobe)        │
│  TV)      │  chairs)   │                        │
│           │            │                        │
├───────────┴────────────┴────────────────────────┤
│              BOTTOM WALL (exit at center)        │
└────────────────────────────────────────────────┘
"""

from typing import Dict, List

# Apartment grid dimensions
APARTMENT_WIDTH = 24
APARTMENT_HEIGHT = 16

# Room definitions
APARTMENT_ROOMS: Dict[str, Dict] = {
    "office": {
        "label": "Office",
        "description": "Work area with desks and computers",
        "activities": ["work at desk", "check computer", "write notes"],
    },
    "kitchen": {
        "label": "Kitchen",
        "description": "Kitchen with counter, fridge, and dining spot",
        "activities": ["cook a meal", "eat at the table", "make coffee"],
    },
    "living_room": {
        "label": "Living Room",
        "description": "Cozy area with couches and TV",
        "activities": ["watch TV", "relax on the couch", "chat with roommates"],
    },
    "bedroom": {
        "label": "Bedroom",
        "description": "Beds for sleeping and relaxing",
        "activities": ["take a nap", "rest", "go to sleep"],
    },
    "dining": {
        "label": "Dining Area",
        "description": "Table and chairs for meals",
        "activities": ["eat a meal", "read the paper", "chat over food"],
    },
    "bathroom": {
        "label": "Bathroom",
        "description": "Bathroom (future)",
        "activities": [],
    },
}

# Named spots agents can target directly
# Positions match Sprite2D placements in ApartmentMap.tscn (pixel coords / 32 = tile coords)
APARTMENT_SPOTS: Dict[str, Dict] = {
    # Office area (top-left)
    "desk": {"room": "office", "x": 4.1, "y": 2.4, "label": "Desk with computer"},
    "chair": {"room": "office", "x": 4.2, "y": 3.5, "label": "Office chair"},
    # Living area (top-center)
    "couch": {"room": "living_room", "x": 12.0, "y": 2.5, "label": "Couch"},
    "coffee_table": {"room": "living_room", "x": 12.0, "y": 4.9, "label": "Coffee table"},
    "aquarium": {"room": "living_room", "x": 6.5, "y": 6.5, "label": "Aquarium"},
    "record_player": {"room": "living_room", "x": 14.9, "y": 6.9, "label": "Record player"},
    # Library/reading area (top-right)
    "bookshelf": {"room": "living_room", "x": 18.7, "y": 2.6, "label": "Bookshelf"},
    "rug": {"room": "living_room", "x": 18.6, "y": 6.2, "label": "Reading rug"},
    # Bedroom (bottom-left)
    "bed": {"room": "bedroom", "x": 4.5, "y": 12.5, "label": "Bed"},
    "coat_rack": {"room": "bedroom", "x": 8.3, "y": 11.8, "label": "Coat rack"},
    # Kitchen (bottom-right)
    "fridge": {"room": "kitchen", "x": 21.0, "y": 11.0, "label": "Fridge"},
    "stove": {"room": "kitchen", "x": 18.1, "y": 12.9, "label": "Stove"},
    "shoe_rack": {"room": "kitchen", "x": 16.2, "y": 11.8, "label": "Shoe rack"},
    # Shared / decoration
    "plant": {"room": None, "x": 6.9, "y": 2.3, "label": "Potted plant"},
    "exit": {"room": None, "x": 12, "y": 15, "label": "Exit"},
}

# Walkability grid: 0 = walkable, 1 = blocked
# Row-major: APARTMENT_OBJMAP[y][x]
APARTMENT_OBJMAP: List[List[int]] = [
    # x: 0  1  2  3  4  5  6  7  8  9 10 11 12 13 14 15 16 17 18 19 20 21 22 23
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],  # y=0: top wall
    [1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],  # y=1: office / kitchen / bath / storage
    [1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],  # y=2
    [1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],  # y=3
    [1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],  # y=4
    [1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],  # y=5
    [1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],  # y=6
    [1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1],  # y=7: hallway (all doorways open)
    [1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1],  # y=8: hallway
    [1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1],  # y=9: living / dining / bedroom
    [1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1],  # y=10
    [1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1],  # y=11
    [1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1],  # y=12
    [1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1],  # y=13
    [1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1],  # y=14
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],  # y=15: bottom wall, exit at x=12
]

# Residential coords on town grid (where apartment entrance is)
RESIDENTIAL_TOWN_COORDS = {"x": 69.0, "y": 25.0}


def get_apartment_objmap_xy() -> List[List[int]]:
    """Return objmap indexed as [x][y] for A* compatibility with town pathfinder."""
    height = len(APARTMENT_OBJMAP)
    width = len(APARTMENT_OBJMAP[0]) if height > 0 else 0
    result = []
    for x in range(width):
        col = []
        for y in range(height):
            col.append(APARTMENT_OBJMAP[y][x])
        result.append(col)
    return result
