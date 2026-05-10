"""Tests for location_sync."""

from unittest.mock import MagicMock


def test_sync_locations_updates_filter_dicts():
    from worldgm.location_sync import sync_locations

    town = MagicMock()
    town.get_active_agents.return_value = [
        {"id": "alice", "currentLocation": "cafe"},
        {"id": "bob", "currentLocation": "library"},
        {"id": "carol", "currentLocation": "cafe"},
    ]
    location_filter = MagicMock()
    location_filter._entity_locations = {}
    location_filter._location_to_entities = {}
    location_filter._entity_names = []

    import asyncio

    new = asyncio.run(sync_locations(town, location_filter, last_known={}))

    assert new == {"alice": "cafe", "bob": "library", "carol": "cafe"}
    assert location_filter._entity_locations == new
    assert location_filter._location_to_entities == {
        "cafe": {"alice", "carol"},
        "library": {"bob"},
    }
    assert set(location_filter._entity_names) == {"alice", "bob", "carol"}


def test_sync_locations_no_change_skips_update():
    """If locations haven't changed since last call, the filter is not touched."""
    from worldgm.location_sync import sync_locations

    town = MagicMock()
    town.get_active_agents.return_value = [
        {"id": "alice", "currentLocation": "cafe"},
    ]

    # Use a plain object so we can detect mutation via identity comparison.
    class _Filter:
        _entity_locations = {"alice": "cafe"}
        _location_to_entities = {"cafe": {"alice"}}
        _entity_names = ["alice"]

    location_filter = _Filter()
    original_locations_id = id(location_filter._entity_locations)

    import asyncio

    new = asyncio.run(sync_locations(town, location_filter, last_known={"alice": "cafe"}))
    assert new == {"alice": "cafe"}
    # The filter's dict should not have been replaced (same object identity).
    assert id(location_filter._entity_locations) == original_locations_id


def test_sync_locations_handles_unknown_location_default():
    """Agents with no currentLocation get 'unknown'."""
    from worldgm.location_sync import sync_locations

    town = MagicMock()
    town.get_active_agents.return_value = [
        {"id": "alice"},  # no currentLocation
        {"id": "bob", "currentLocation": ""},  # empty string
    ]
    location_filter = MagicMock()
    location_filter._entity_locations = {}
    location_filter._location_to_entities = {}
    location_filter._entity_names = []

    import asyncio

    new = asyncio.run(sync_locations(town, location_filter, last_known={}))
    assert new == {"alice": "unknown", "bob": "unknown"}
