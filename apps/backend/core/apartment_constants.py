"""Constants for apartment interior navigation.

12x8 tile grid (384x256 pixels at 32px/tile, displayed at 4x = 1536x1024).
"""

from typing import Dict, List

# Apartment grid dimensions
APARTMENT_WIDTH = 12
APARTMENT_HEIGHT = 8

# Room definitions
APARTMENT_ROOMS: Dict[str, Dict] = {
    "office": {
        "label": "Office",
        "description": "Work area with desks",
        "activities": ["work at desk", "check computer", "write notes"],
    },
    "kitchen": {
        "label": "Kitchen",
        "description": "Kitchen and dining area",
        "activities": ["cook a meal", "eat at the table", "read a book from the bookshelf"],
    },
    "living_room": {
        "label": "Living Room",
        "description": "Couches and TV",
        "activities": ["watch TV", "relax on the couch", "chat with roommates"],
    },
    "bedroom": {
        "label": "Bedroom",
        "description": "Beds for sleeping",
        "activities": ["take a nap", "rest", "go to sleep"],
    },
}

# Named spots agents can target directly
APARTMENT_SPOTS: Dict[str, Dict] = {
    "desk_1": {"room": "office", "x": 2, "y": 1, "label": "Left desk"},
    "desk_2": {"room": "office", "x": 4, "y": 1, "label": "Right desk"},
    "desk_3": {"room": "office", "x": 3, "y": 2, "label": "Middle desk"},
    "couch_1": {"room": "living_room", "x": 2, "y": 6, "label": "Couch left"},
    "couch_2": {"room": "living_room", "x": 3, "y": 6, "label": "Couch right"},
    "tv_chair": {"room": "living_room", "x": 2, "y": 5, "label": "TV chair"},
    "bed_1": {"room": "bedroom", "x": 9, "y": 6, "label": "Left bed"},
    "bed_2": {"room": "bedroom", "x": 10, "y": 6, "label": "Right bed"},
    "table": {"room": "kitchen", "x": 8, "y": 2, "label": "Kitchen table"},
    "bookshelf": {"room": "kitchen", "x": 10, "y": 1, "label": "Bookshelf"},
    "exit": {"room": None, "x": 6, "y": 7, "label": "Exit"},
}

# Walkability grid: 0 = walkable, 1 = blocked
# Row-major: APARTMENT_OBJMAP[y][x]
APARTMENT_OBJMAP: List[List[int]] = [
    # x: 0  1  2  3  4  5  6  7  8  9 10 11
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],  # y=0: top wall
    [1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],  # y=1: office desks / kitchen
    [1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],  # y=2: office / kitchen table
    [1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1],  # y=3: hallway (open doorways)
    [1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],  # y=4: living room / bedroom
    [1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],  # y=5: living room / bedroom
    [1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],  # y=6: couches / beds
    [1, 1, 1, 1, 1, 1, 0, 1, 1, 1, 1, 1],  # y=7: bottom wall with exit
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
