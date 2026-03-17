"""Tests for town_pathfinding with walkability.json loading."""

from unittest.mock import patch

from core.services.town_pathfinding import _load_objmap_from_walkability, find_path


def _make_walkability_grid(width=5, height=5, blocked=None):
    """Create a minimal walkability grid payload."""
    grid = [[0] * height for _ in range(width)]
    for x, y in blocked or []:
        grid[x][y] = -1
    return {
        "width": width,
        "height": height,
        "tile_size": 32,
        "exported_at": "2026-03-14T00:00:00Z",
        "grid": grid,
    }


def test_load_walkability_all_walkable():
    data = _make_walkability_grid(3, 3)
    grid = _load_objmap_from_walkability(data)
    assert len(grid) == 3
    assert len(grid[0]) == 3
    assert grid[0][0] == 0
    assert grid[2][2] == 0


def test_load_walkability_with_blocked():
    data = _make_walkability_grid(3, 3, blocked=[(1, 1)])
    grid = _load_objmap_from_walkability(data)
    assert grid[1][1] == -1
    assert grid[0][0] == 0


def test_find_path_on_exported_grid():
    """A* should work on an exported grid the same as the old grid."""
    data = _make_walkability_grid(5, 5, blocked=[(2, 0), (2, 1), (2, 2)])
    grid = _load_objmap_from_walkability(data)
    with patch("core.services.town_pathfinding._objmap", grid):
        path = find_path(0, 0, 4, 0)
        assert path is not None
        assert path[0] == (0, 0)
        assert path[-1] == (4, 0)
        # Path must go around the wall
        assert all(p != (2, 0) and p != (2, 1) and p != (2, 2) for p in path)


def test_find_path_blocked_returns_none():
    """Completely walled off → no path."""
    data = _make_walkability_grid(3, 3, blocked=[(1, 0), (1, 1), (1, 2)])
    grid = _load_objmap_from_walkability(data)
    with patch("core.services.town_pathfinding._objmap", grid):
        path = find_path(0, 0, 2, 2)
        assert path is None
