"""Periodic DDB -> LocationBasedFilter sync.

Reads active agents from DynamoDB and updates Concordia's LocationBasedFilter
internal location dict. The filter has no built-in DDB integration; we update
it directly because its state is just two dicts.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

logger = logging.getLogger(__name__)


def _invert_locations(entity_locations: dict[str, str]) -> dict[str, set[str]]:
    inverted: dict[str, set[str]] = defaultdict(set)
    for entity, location in entity_locations.items():
        inverted[location].add(entity)
    return dict(inverted)


async def sync_locations(
    town_service: Any,
    location_filter: Any,
    last_known: dict[str, str],
) -> dict[str, str]:
    """Pull active agents' currentLocation from DDB and update the filter.

    Returns the new locations dict (caller stores it as ``last_known`` for next call).

    The filter's ``_entity_locations`` and ``_location_to_entities`` are private-by-convention
    but mutable; we update them directly because ``LocationBasedFilter.extract_locations_from_narrative``
    parses GM output text -- we don't have GM output, we have authoritative DDB state.
    """
    agents = town_service.get_active_agents()
    new_locations = {a["id"]: a.get("currentLocation") or "unknown" for a in agents}
    if new_locations != last_known:
        # Update the filter's internal dicts in place
        location_filter._entity_locations = new_locations
        location_filter._location_to_entities = _invert_locations(new_locations)
        # Also update entity_names so new agents show up in the filter's reported state
        location_filter._entity_names = list(new_locations.keys())
        logger.info(
            "Locations updated: %d agents tracked across %d locations",
            len(new_locations),
            len(location_filter._location_to_entities),
        )
    return new_locations
