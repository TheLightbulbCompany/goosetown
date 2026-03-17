"""Shared constants for the GooseTown simulation.

Location coordinates can be overridden by exporting from the Godot map editor.
Game design data (labels, activities, bounds) stays hardcoded here.
"""

import copy
import json
import logging
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

_BASE_TOWN_LOCATIONS: Dict[str, Dict] = {
    "plaza": {
        "x": 18.0,
        "y": 12.0,
        "label": "Plaza",
        "type": "area",
        "activities": ["walk around", "sit at the fountain", "chat with nearby agents"],
        "bounds": {"x1": 14, "y1": 10, "x2": 22, "y2": 16},
    },
    "library": {
        "x": 25.0,
        "y": 15.0,
        "label": "Library",
        "type": "point",
        "activities": ["browse shelves", "read a book", "study quietly"],
    },
    "cafe": {
        "x": 22.0,
        "y": 20.0,
        "label": "Cafe",
        "type": "point",
        "activities": ["order a drink", "sit at a table", "chat with nearby agents"],
    },
    "activity_center": {
        "x": 16.0,
        "y": 16.0,
        "label": "Activity Center",
        "type": "point",
        "activities": ["admire the flowers", "sit on a bench", "relax", "play sports"],
    },
    "residence": {
        "x": 30.0,
        "y": 8.0,
        "label": "Residence",
        "type": "point",
        "activities": ["go inside your apartment"],
    },
}


def _merge_exported_locations(base: Dict[str, Dict]) -> Dict[str, Dict]:
    """Merge exported coordinates from locations.json into base locations dict.

    Only overwrites x, y for locations that exist in the base dict.
    Preserves label, type, activities, bounds.
    """
    locations_path = Path(__file__).parent.parent / "data" / "goosetown" / "locations.json"
    if not locations_path.exists():
        logger.debug("No exported locations.json found, using hardcoded coordinates")
        return base

    try:
        with open(locations_path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read locations.json: %s", e)
        return base

    exported = data.get("locations", {})
    exported_at = data.get("exported_at", "unknown")
    updated = 0

    for name, coords in exported.items():
        if name in base:
            base[name]["x"] = float(coords.get("x", base[name]["x"]))
            base[name]["y"] = float(coords.get("y", base[name]["y"]))
            updated += 1
        else:
            logger.debug("Exported location '%s' not in TOWN_LOCATIONS, skipping", name)

    if updated > 0:
        logger.info(
            "Merged %d location coordinates from Godot export (exported_at=%s)",
            updated,
            exported_at,
        )

    return base


TOWN_LOCATIONS: Dict[str, Dict] = _merge_exported_locations(copy.deepcopy(_BASE_TOWN_LOCATIONS))
