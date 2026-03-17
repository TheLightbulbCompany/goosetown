"""Tests for town_constants location loading."""

import copy
import json
from pathlib import Path
from unittest.mock import mock_open, patch


def test_locations_merge_coordinates():
    """Exported coordinates should overwrite hardcoded x/y but keep label/activities."""
    locations_json = json.dumps(
        {
            "exported_at": "2026-03-14T00:00:00Z",
            "locations": {
                "library": {"x": 20.0, "y": 18.0, "pixel_x": 640, "pixel_y": 576},
            },
        }
    )

    from core.town_constants import _BASE_TOWN_LOCATIONS, _merge_exported_locations

    base = copy.deepcopy(_BASE_TOWN_LOCATIONS)

    with patch("builtins.open", mock_open(read_data=locations_json)):
        with patch.object(Path, "exists", return_value=True):
            merged = _merge_exported_locations(base)

    assert merged["library"]["x"] == 20.0
    assert merged["library"]["y"] == 18.0
    assert merged["library"]["label"] == "Library"
    assert "activities" in merged["library"]


def test_locations_fallback_when_no_file():
    """When locations.json doesn't exist, use hardcoded coordinates."""
    from core.town_constants import _BASE_TOWN_LOCATIONS, _merge_exported_locations

    base = copy.deepcopy(_BASE_TOWN_LOCATIONS)

    with patch.object(Path, "exists", return_value=False):
        merged = _merge_exported_locations(base)

    assert merged["library"]["x"] == _BASE_TOWN_LOCATIONS["library"]["x"]


def test_unknown_location_in_export_ignored():
    """Export locations not in TOWN_LOCATIONS are ignored."""
    locations_json = json.dumps(
        {
            "exported_at": "2026-03-14T00:00:00Z",
            "locations": {
                "mystery_building": {"x": 10.0, "y": 10.0, "pixel_x": 320, "pixel_y": 320},
            },
        }
    )

    from core.town_constants import _BASE_TOWN_LOCATIONS, _merge_exported_locations

    base = copy.deepcopy(_BASE_TOWN_LOCATIONS)

    with patch("builtins.open", mock_open(read_data=locations_json)):
        with patch.object(Path, "exists", return_value=True):
            merged = _merge_exported_locations(base)

    assert "mystery_building" not in merged
