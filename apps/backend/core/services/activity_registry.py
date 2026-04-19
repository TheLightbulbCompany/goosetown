"""Activity registry for GooseTown.

Defines every timed activity an agent can perform at a location.
Consumers call ``get_activities_for_location`` to build the action
menu and ``get_activity`` to validate an inbound ``town_act`` command.

Effect application follows the GDD formulas:
- Mood and energy changes are applied at *completion*, not at start.
- Personality trait bonuses (introvert/extrovert) are applied here,
  independently of the existing ``town_mood_engine`` named-event table.
- ``apply_activity_effect`` returns the new (energy, mood) pair and a
  boolean indicating whether the activity was *interrupted* because
  energy ran out before completion.

Apartment location alias
------------------------
All apartment-interior spot names (desk, couch, bed, …) are treated as
the single canonical location key ``"apartment"`` so that every
apartment activity is available anywhere inside the apartment.
"""

from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# Apartment spots that all map to the "apartment" location key
# ---------------------------------------------------------------------------

APARTMENT_SPOT_NAMES: frozenset[str] = frozenset(
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
    """Normalise a raw location string to the registry key.

    Apartment spot names (desk, couch, bed, …) all map to ``"apartment"``
    so that the activity registry does not need to enumerate every spot.
    """
    if location in APARTMENT_SPOT_NAMES:
        return "apartment"
    return location


# ---------------------------------------------------------------------------
# Activity dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Activity:
    """A timed activity an agent can perform at a specific location.

    Attributes:
        name: Unique snake_case identifier, also the ``town_act`` argument.
        location: Canonical registry location key (e.g. ``"library"``).
        duration_seconds: How long the activity runs before completion.
        mood_delta: Mood change applied on completion (clamped to -50..+50).
        energy_delta: Energy change applied on completion (clamped to 0..100).
        currency_delta: Currency change applied on completion (economy Phase 5).
        introvert_bonus: Additional mood for agents with introversion > 0.5.
        extrovert_bonus: Additional mood for agents with introversion <= 0.5.
        animation: Animation state name broadcast in ``world_state``.
        description: Short human-readable label shown above the agent.
        agent_prompt: Instruction for the agent LLM — what to *do* during
            this activity beyond the mechanical effect.
        spot: For apartment activities, the Marker2D node name the agent must
            walk to before the timer starts (e.g. ``"stove"``, ``"bed"``).
            ``None`` for town-location activities where the agent is already
            standing at the location marker.
        location_context: ``"town"`` or ``"apartment"``.  Used to enforce
            context-gating: agents in town cannot start apartment activities
            and vice versa, regardless of what ``currentLocation`` says.
    """

    name: str
    location: str
    duration_seconds: int
    mood_delta: int
    energy_delta: int
    currency_delta: int = 0
    introvert_bonus: int = 0
    extrovert_bonus: int = 0
    animation: str = "idle"
    description: str = ""
    agent_prompt: str = ""
    spot: Optional[str] = None
    location_context: str = "town"


# ---------------------------------------------------------------------------
# Registry data
# ---------------------------------------------------------------------------

_ACTIVITIES: list[Activity] = [
    # --- Plaza ---
    Activity(
        name="people_watch",
        location="plaza",
        duration_seconds=10,
        mood_delta=3,
        energy_delta=-2,
        animation="sitting",
        description="People-watching",
        agent_prompt=(
            "You are sitting at the plaza fountain watching the town go by. "
            "Observe who is around and think about what their day might be like. "
            "You may write a brief reflection in TOWN_LIFE.md if something strikes you."
        ),
    ),
    Activity(
        name="perform",
        location="plaza",
        duration_seconds=30,
        mood_delta=6,
        energy_delta=-6,
        currency_delta=3,
        animation="performing",
        description="Performing",
        agent_prompt=(
            "You are busking at the plaza — performing for passersby. "
            "Decide what you are performing (music, poetry, a short speech, juggling). "
            "Write what you performed in TOWN_LIFE.md as a journal entry."
        ),
    ),
    Activity(
        name="rest",
        location="plaza",
        duration_seconds=8,
        mood_delta=1,
        energy_delta=3,
        animation="idle",
        description="Resting",
        agent_prompt=(
            "You are resting by the fountain. Take a moment to catch your breath "
            "and observe the town. No need to write anything — just be present."
        ),
    ),
    # --- Library ---
    Activity(
        name="read",
        location="library",
        duration_seconds=15,
        mood_delta=4,
        energy_delta=-3,
        introvert_bonus=2,
        animation="reading",
        description="Reading",
        agent_prompt=(
            "You are reading at the library. Pick a topic that genuinely interests you. "
            "Look up or reason about something real on that topic, then write a short "
            "summary of what you learned in TOWN_LIFE.md under a 'Reading Notes' section."
        ),
    ),
    Activity(
        name="study",
        location="library",
        duration_seconds=20,
        mood_delta=2,
        energy_delta=-5,
        animation="reading",
        description="Studying",
        agent_prompt=(
            "You are studying at the library. Work through a problem or question you have "
            "been thinking about. Use your tools or reasoning skills to make progress. "
            "Write your conclusions in TOWN_LIFE.md."
        ),
    ),
    Activity(
        name="write_journal",
        location="library",
        duration_seconds=10,
        mood_delta=3,
        energy_delta=-2,
        introvert_bonus=1,
        animation="writing",
        description="Writing in journal",
        agent_prompt=(
            "You are writing in your personal journal at the library. "
            "Reflect on recent events in GooseTown, relationships, goals, or how you are feeling. "
            "Append an entry to TOWN_LIFE.md dated today."
        ),
    ),
    # --- Cafe ---
    Activity(
        name="order_coffee",
        location="cafe",
        duration_seconds=5,
        mood_delta=3,
        energy_delta=6,
        currency_delta=-1,
        animation="drink",
        description="Getting coffee",
        agent_prompt=(
            "You are ordering a coffee at the café. "
            "Decide what kind of coffee (or tea, or other drink) matches your mood today "
            "and write a one-line note in TOWN_LIFE.md."
        ),
    ),
    Activity(
        name="sit_and_chat",
        location="cafe",
        duration_seconds=15,
        mood_delta=4,
        energy_delta=-2,
        extrovert_bonus=2,
        animation="sitting",
        description="Sitting and chatting",
        agent_prompt=(
            "You are sitting at the café enjoying the atmosphere and potentially chatting "
            "with whoever is nearby. If someone is nearby, consider starting a conversation "
            "with ``town_act chat``. Otherwise, enjoy the ambient sounds and people-watch."
        ),
    ),
    Activity(
        name="work_on_laptop",
        location="cafe",
        duration_seconds=20,
        mood_delta=1,
        energy_delta=-5,
        currency_delta=3,
        animation="working",
        description="Working on laptop",
        agent_prompt=(
            "You are working on your laptop at the café. Use your actual skills and tools "
            "to do something productive — write code, draft a document, analyse data, "
            "or work on a project. Log what you accomplished in TOWN_LIFE.md."
        ),
    ),
    # --- Activity Center ---
    Activity(
        name="exercise",
        location="activity_center",
        duration_seconds=15,
        mood_delta=4,
        energy_delta=10,
        animation="exercising",
        description="Exercising",
        agent_prompt=(
            "You are exercising at the activity center. "
            "Decide what workout you are doing and how it is going. "
            "Note your energy level before and after in TOWN_LIFE.md."
        ),
    ),
    Activity(
        name="play_game",
        location="activity_center",
        duration_seconds=20,
        mood_delta=5,
        energy_delta=-3,
        animation="gaming",
        description="Playing a game",
        agent_prompt=(
            "You are playing a game at the activity center. "
            "Choose a game (board game, arcade, card game) and play a round. "
            "Write a brief match report in TOWN_LIFE.md."
        ),
    ),
    Activity(
        name="garden",
        location="activity_center",
        duration_seconds=15,
        mood_delta=5,
        energy_delta=-2,
        introvert_bonus=2,
        animation="gardening",
        description="Gardening",
        agent_prompt=(
            "You are tending the community garden at the activity center. "
            "Decide what you are planting or caring for. "
            "Write a short note about the garden's progress in TOWN_LIFE.md."
        ),
    ),
    # --- Bank ---
    Activity(
        name="check_balance",
        location="bank",
        duration_seconds=5,
        mood_delta=0,
        energy_delta=-1,
        animation="idle",
        description="Checking balance",
        agent_prompt=(
            "You are at the bank checking your account. "
            "Note your current balance in TOWN_LIFE.md and decide if you need to earn more "
            "or if you have enough to spend on something."
        ),
    ),
    Activity(
        name="browse_listings",
        location="bank",
        duration_seconds=10,
        mood_delta=1,
        energy_delta=-2,
        animation="reading",
        description="Browsing job listings",
        agent_prompt=(
            "You are browsing the job listings board at the bank. "
            "Look through what work is available in town. "
            "Note any interesting opportunities in TOWN_LIFE.md."
        ),
    ),
    # --- Residence ---
    Activity(
        name="sit_outside",
        location="residence",
        duration_seconds=10,
        mood_delta=3,
        energy_delta=2,
        animation="sitting",
        description="Sitting outside",
        agent_prompt=(
            "You are sitting on the steps of the apartment building. "
            "Watch the town from here and decompress before heading home. "
            "If someone passes by, you might say hello."
        ),
    ),
    # --- Apartment ---
    # All apartment activities carry location_context="apartment".
    # The backend rejects attempts to start these from town, and the daemon
    # filters them out of the town action menu.
    # Each also has a `spot` field — the Marker2D node name the game server
    # navigates the agent to before the activity timer starts.
    Activity(
        name="sleep",
        location="apartment",
        location_context="apartment",
        duration_seconds=30,
        mood_delta=8,
        energy_delta=100,  # clamped to 100 via min(100, current + 100)
        animation="sleeping",
        description="Sleeping",
        spot="bed",
        agent_prompt=(
            "You are sleeping in your bed. Rest completely. "
            "When you wake, you may write a brief dream or morning thought in TOWN_LIFE.md."
        ),
    ),
    Activity(
        name="work_at_desk",
        location="apartment",
        location_context="apartment",
        duration_seconds=20,
        mood_delta=1,
        energy_delta=-5,
        currency_delta=3,
        animation="working",
        description="Working at desk",
        spot="desk",
        agent_prompt=(
            "You are working at your desk in your apartment. "
            "Use your tools and skills to do real, productive work — "
            "write code, analyse something, draft a report. "
            "Log what you accomplished in TOWN_LIFE.md."
        ),
    ),
    Activity(
        name="cook",
        location="apartment",
        location_context="apartment",
        duration_seconds=15,
        mood_delta=4,
        energy_delta=5,
        currency_delta=-1,
        animation="cooking",
        description="Cooking",
        spot="stove",
        agent_prompt=(
            "You are cooking a meal in your apartment kitchen. "
            "Look up or invent a recipe that fits what you would realistically make. "
            "Write the recipe and how it turned out in TOWN_LIFE.md."
        ),
    ),
    Activity(
        name="watch_tv",
        location="apartment",
        location_context="apartment",
        duration_seconds=15,
        mood_delta=3,
        energy_delta=-1,
        animation="sitting",
        description="Watching TV",
        spot="couch",
        agent_prompt=(
            "You are relaxing in front of the TV in your apartment. "
            "Decide what you are watching and whether you enjoy it. "
            "Write a short note about what you watched in TOWN_LIFE.md."
        ),
    ),
    Activity(
        name="read_book",
        location="apartment",
        location_context="apartment",
        duration_seconds=15,
        mood_delta=4,
        energy_delta=-2,
        introvert_bonus=2,
        animation="reading",
        description="Reading a book",
        spot="bookshelf",
        agent_prompt=(
            "You are reading a book in your apartment. "
            "Choose a book you would genuinely enjoy. "
            "Reason about or look up something from that book and write your thoughts "
            "in TOWN_LIFE.md under a 'Currently Reading' section."
        ),
    ),
    Activity(
        name="listen_to_music",
        location="apartment",
        location_context="apartment",
        duration_seconds=15,
        mood_delta=5,
        energy_delta=2,
        introvert_bonus=1,
        animation="sitting",
        description="Listening to music",
        spot="record_player",
        agent_prompt=(
            "You are listening to music on the record player. "
            "Decide what you are listening to. If you have access to a music service "
            "(like Spotify), you can ask the user to play something. "
            "Write about the mood the music creates in TOWN_LIFE.md."
        ),
    ),
    Activity(
        name="watch_aquarium",
        location="apartment",
        location_context="apartment",
        duration_seconds=10,
        mood_delta=4,
        energy_delta=3,
        introvert_bonus=1,
        animation="sitting",
        description="Watching the aquarium",
        spot="aquarium",
        agent_prompt=(
            "You are watching the fish in the aquarium. "
            "Let your mind wander. Reflect on how you are feeling about life in GooseTown, "
            "your goals, and your relationships. Write a reflective entry in TOWN_LIFE.md."
        ),
    ),
    Activity(
        name="nap",
        location="apartment",
        location_context="apartment",
        duration_seconds=12,
        mood_delta=3,
        energy_delta=40,
        animation="sleeping",
        description="Napping",
        spot="bed",
        agent_prompt=(
            "You are taking a short nap. Rest briefly and recover some energy. "
            "When you wake, note how you feel in TOWN_LIFE.md if anything comes to mind."
        ),
    ),
]

# ---------------------------------------------------------------------------
# Lookup structures (built once at import time)
# ---------------------------------------------------------------------------

ACTIVITY_REGISTRY: dict[str, Activity] = {a.name: a for a in _ACTIVITIES}

# Maps canonical location key -> list of activities available there
LOCATION_ACTIVITIES: dict[str, list[Activity]] = {}
for _activity in _ACTIVITIES:
    LOCATION_ACTIVITIES.setdefault(_activity.location, []).append(_activity)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_activity(name: str) -> Optional[Activity]:
    """Return the ``Activity`` for the given name, or ``None`` if unknown."""
    return ACTIVITY_REGISTRY.get(name)


def get_context_mismatch_message(activity: "Activity", agent_location_context: str) -> Optional[str]:
    """Return a human-readable rejection message if the agent is in the wrong context.

    Returns ``None`` when there is no mismatch (the activity is allowed here).

    Args:
        activity: The requested ``Activity``.
        agent_location_context: The agent's current context — ``"town"`` or
            ``"apartment"``.  Sourced from ``locationContext`` on the agent DB record.

    Returns:
        A guidance string explaining what the agent should do instead, or
        ``None`` if the contexts match and the activity is permitted.
    """
    if activity.location_context == agent_location_context:
        return None
    if activity.location_context == "apartment":
        return "You need to go to your apartment first. Use `town_act move residence` to go home."
    # activity.location_context == "town"
    return "You need to go to town first. Use `town_act move exit` to leave your apartment."


def get_activities_for_location(location: str) -> list[Activity]:
    """Return all activities available at a location.

    The ``location`` argument is first normalised via ``canonical_location``
    so apartment spot names (``"desk"``, ``"couch"``, …) all return the
    apartment activity list.
    """
    key = canonical_location(location)
    return LOCATION_ACTIVITIES.get(key, [])


def apply_activity_effect(
    activity: Activity,
    current_energy: int,
    current_mood: int,
    traits: str,
    elapsed_seconds: float,
) -> tuple[int, int, bool]:
    """Compute the final (energy, mood) after an activity completes or is interrupted.

    Args:
        activity: The ``Activity`` being completed.
        current_energy: Agent's energy before the effect (0–100).
        current_mood: Agent's mood before the effect (-50–+50).
        traits: Comma-separated trait string from the agent record.
        elapsed_seconds: How many seconds actually elapsed. Used to scale
            the mood effect proportionally when the activity is interrupted
            (energy would drop to 0 before the full duration). Energy cost
            is the full delta because the agent worked until exhaustion.

    Returns:
        A ``(new_energy, new_mood, interrupted)`` tuple.
        ``interrupted`` is ``True`` when energy would have gone below 0.
    """
    trait_set = {t.strip().lower() for t in traits.split(",") if t.strip()} if traits else set()

    # Determine personality bonus
    introversion = 1.0 if "introvert" in trait_set else 0.0
    mood_bonus = 0
    if introversion > 0.5 and activity.introvert_bonus:
        mood_bonus = activity.introvert_bonus
    elif introversion <= 0.5 and activity.extrovert_bonus:
        mood_bonus = activity.extrovert_bonus

    # Determine whether the activity would exhaust the agent
    projected_energy = current_energy + activity.energy_delta
    interrupted = projected_energy < 0

    if interrupted:
        # Partial mood effect proportional to elapsed / total duration
        completion_ratio = min(1.0, elapsed_seconds / max(1, activity.duration_seconds))
        base_mood = int(round((activity.mood_delta + mood_bonus) * completion_ratio))
        new_energy = 0
    else:
        base_mood = activity.mood_delta + mood_bonus
        new_energy = min(100, max(0, projected_energy))

    new_mood = min(50, max(-50, current_mood + base_mood))
    return new_energy, new_mood, interrupted
