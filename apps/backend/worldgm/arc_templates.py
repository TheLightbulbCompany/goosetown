"""Curated narrative arc seed prompts.

Each template is a 1-2 line scenario seed. The narrative resolver expands
the seed into a kickoff event, and subsequent beats follow naturally from
the agents' responses. Templates can be tagged ``tension`` / ``friendship``
/ ``mystery`` etc. to bias selection by stance pairs.

Mostly hand-written. New templates land here directly; no admin UI in v1.
"""

from __future__ import annotations

from typing import Any

ARC_TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "traveling_merchant",
        "kickoff_prompt": (
            "A traveling merchant has set up a colorful stall outside the library, "
            "selling unusual trinkets and exotic foods. They claim to have come "
            "from across the mountains."
        ),
        "duration_hours": 24,
        "preferred_location": "library",
        "tags": ["mystery"],
    },
    {
        "id": "mysterious_note",
        "kickoff_prompt": (
            "Someone left a mysterious handwritten note pinned to the bulletin board. "
            "The handwriting looks deliberate but the message is cryptic."
        ),
        "duration_hours": 48,
        "preferred_location": "plaza",
        "tags": ["mystery"],
    },
    {
        "id": "lost_dog",
        "kickoff_prompt": ("A small dog with no collar wanders into the cafe, sits down, and refuses to leave."),
        "duration_hours": 12,
        "preferred_location": "cafe",
        "tags": ["friendship"],
    },
    {
        "id": "rumor",
        "kickoff_prompt": (
            "An overheard rumor spreads through the plaza: someone in town is keeping "
            "a secret about why they came here."
        ),
        "duration_hours": 36,
        "preferred_location": "plaza",
        "tags": ["tension"],
    },
    {
        "id": "weather_storm",
        "kickoff_prompt": (
            "Dark clouds roll in over the town. A heavy storm is clearly coming. "
            "Agents indoors stay; agents outdoors should consider moving."
        ),
        "duration_hours": 4,
        "preferred_location": None,
        "tags": ["weather"],
    },
    {
        "id": "festival_announcement",
        "kickoff_prompt": (
            "An old hand-written poster appears in the plaza announcing a small "
            "town festival three days from now. No one remembers planning it."
        ),
        "duration_hours": 72,
        "preferred_location": "plaza",
        "tags": ["friendship"],
    },
    {
        "id": "unexpected_visitor",
        "kickoff_prompt": (
            "A traveler arrives at the residence asking after one specific agent "
            "by name. They claim to be an old acquaintance."
        ),
        "duration_hours": 24,
        "preferred_location": "residence",
        "tags": ["mystery", "tension"],
    },
    {
        "id": "missing_object",
        "kickoff_prompt": (
            "A small but valuable-looking object has gone missing from the activity center. "
            "Several agents were nearby earlier."
        ),
        "duration_hours": 24,
        "preferred_location": "activity_center",
        "tags": ["tension"],
    },
    {
        "id": "abandoned_book",
        "kickoff_prompt": (
            "An old leather-bound book has been left open on a library table. "
            "It's been there for hours, opened to the same page."
        ),
        "duration_hours": 24,
        "preferred_location": "library",
        "tags": ["mystery"],
    },
    {
        "id": "shared_meal",
        "kickoff_prompt": ("The cafe owner declares free coffee for the next hour to celebrate a personal milestone."),
        "duration_hours": 2,
        "preferred_location": "cafe",
        "tags": ["friendship"],
    },
]


def find_template(template_id: str) -> dict[str, Any] | None:
    for t in ARC_TEMPLATES:
        if t["id"] == template_id:
            return t
    return None


def templates_by_tag(tag: str) -> list[dict[str, Any]]:
    return [t for t in ARC_TEMPLATES if tag in t.get("tags", [])]
