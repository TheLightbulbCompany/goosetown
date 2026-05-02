"""Activity catalogue — mirrors backend activity_registry.py.

Keep in sync with apps/backend/core/services/activity_registry.py.
"""

_APARTMENT_SPOTS: frozenset = frozenset(
    {
        "desk",
        "chair",
        "couch",
        "coffee_table",
        "bookshelf",
        "rug",
        "aquarium",
        "record_player",
        "bed",
        "coat_rack",
        "shoe_rack",
        "fridge",
        "stove",
        "exit",
        "apartment",
    }
)


def canonical_location(location: str) -> str:
    """Normalise an apartment spot name to the 'apartment' registry key."""
    return "apartment" if location in _APARTMENT_SPOTS else location


# Each entry: dict with keys name, description, duration_seconds, agent_prompt
LOCATION_ACTIVITIES: dict[str, list[dict]] = {
    "plaza": [
        {
            "name": "people_watch",
            "description": "People-watching",
            "duration_seconds": 10,
            "agent_prompt": "Sit by the fountain and observe the town. Write a brief reflection in TOWN_LIFE.md if inspired.",
        },
        {
            "name": "perform",
            "description": "Performing (busking)",
            "duration_seconds": 30,
            "agent_prompt": "Perform for passersby. Decide what you perform and write about it in TOWN_LIFE.md.",
        },
        {
            "name": "rest",
            "description": "Resting by the fountain",
            "duration_seconds": 8,
            "agent_prompt": "Rest by the fountain and catch your breath. Just be present — no need to write anything.",
        },
    ],
    "library": [
        {
            "name": "read",
            "description": "Reading",
            "duration_seconds": 15,
            "agent_prompt": "Pick a topic that interests you. Research it and write your findings in TOWN_LIFE.md.",
        },
        {
            "name": "study",
            "description": "Studying",
            "duration_seconds": 20,
            "agent_prompt": "Work through a problem using your tools. Write conclusions in TOWN_LIFE.md.",
        },
        {
            "name": "write_journal",
            "description": "Writing in journal",
            "duration_seconds": 10,
            "agent_prompt": "Reflect on recent events. Append an entry to TOWN_LIFE.md dated today.",
        },
    ],
    "cafe": [
        {
            "name": "order_coffee",
            "description": "Getting coffee",
            "duration_seconds": 5,
            "agent_prompt": "Order a drink. Note what you chose and why in TOWN_LIFE.md.",
        },
        {
            "name": "sit_and_chat",
            "description": "Sitting and chatting",
            "duration_seconds": 15,
            "agent_prompt": "Enjoy the atmosphere. If someone is nearby, consider starting a conversation.",
        },
        {
            "name": "work_on_laptop",
            "description": "Working on laptop",
            "duration_seconds": 20,
            "agent_prompt": "Use your skills and tools to do real work. Log what you accomplished in TOWN_LIFE.md.",
        },
    ],
    "activity_center": [
        {
            "name": "exercise",
            "description": "Exercising",
            "duration_seconds": 15,
            "agent_prompt": "Decide what workout you are doing. Note energy before/after in TOWN_LIFE.md.",
        },
        {
            "name": "play_game",
            "description": "Playing a game",
            "duration_seconds": 20,
            "agent_prompt": "Choose a game and play a round. Write a brief match report in TOWN_LIFE.md.",
        },
        {
            "name": "garden",
            "description": "Gardening",
            "duration_seconds": 15,
            "agent_prompt": "Tend the community garden. Write a short note about the garden in TOWN_LIFE.md.",
        },
    ],
    "bank": [
        {
            "name": "check_balance",
            "description": "Checking balance",
            "duration_seconds": 5,
            "agent_prompt": "Check your account. Note your balance in TOWN_LIFE.md.",
        },
        {
            "name": "browse_listings",
            "description": "Browsing job listings",
            "duration_seconds": 10,
            "agent_prompt": "Look through available work. Note interesting opportunities in TOWN_LIFE.md.",
        },
    ],
    "residence": [
        {
            "name": "sit_outside",
            "description": "Sitting outside the apartment building",
            "duration_seconds": 10,
            "agent_prompt": "Sit on the steps and decompress. Watch the town go by. Say hello if someone passes.",
        },
    ],
    "apartment": [
        {
            "name": "sleep",
            "description": "Sleeping (walk to bed)",
            "duration_seconds": 30,
            "agent_prompt": "Sleep in your bed. Write a morning thought in TOWN_LIFE.md when you wake.",
        },
        {
            "name": "nap",
            "description": "Napping (walk to bed)",
            "duration_seconds": 12,
            "agent_prompt": "Take a short nap. Note how you feel when you wake in TOWN_LIFE.md.",
        },
        {
            "name": "work_at_desk",
            "description": "Working at desk",
            "duration_seconds": 20,
            "agent_prompt": "Do real work at your desk using your tools. Log what you accomplished in TOWN_LIFE.md.",
        },
        {
            "name": "cook",
            "description": "Cooking (walk to stove)",
            "duration_seconds": 15,
            "agent_prompt": "Cook a meal. Look up a recipe and write how it turned out in TOWN_LIFE.md.",
        },
        {
            "name": "watch_tv",
            "description": "Watching TV (walk to couch)",
            "duration_seconds": 15,
            "agent_prompt": "Relax in front of the TV. Write a note about what you watched in TOWN_LIFE.md.",
        },
        {
            "name": "read_book",
            "description": "Reading a book (walk to bookshelf)",
            "duration_seconds": 15,
            "agent_prompt": "Read a book you enjoy. Write thoughts in TOWN_LIFE.md under 'Currently Reading'.",
        },
        {
            "name": "listen_to_music",
            "description": "Listening to music (walk to record player)",
            "duration_seconds": 15,
            "agent_prompt": "Put on a record. Write about the mood the music creates in TOWN_LIFE.md.",
        },
        {
            "name": "watch_aquarium",
            "description": "Watching the aquarium",
            "duration_seconds": 10,
            "agent_prompt": "Watch the fish and reflect. Write a reflective entry in TOWN_LIFE.md.",
        },
    ],
}
