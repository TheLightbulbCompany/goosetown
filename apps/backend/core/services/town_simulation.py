"""GooseTown simulation engine.

Runs as an asyncio background task inside FastAPI. Manages agent movement,
state transitions, and proximity detection.

The simulation is THIN -- it does NOT make decisions for agents. It only:
- Interpolates movement toward target_x/target_y
- Detects arrivals
- Handles transition states (going_home -> sleeping)
- Detects inactive agents and sends them home
- Detects nearby agent pairs
"""

import asyncio
import logging
import math
import random
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple

from core.apartment_constants import APARTMENT_SPOTS, RESIDENTIAL_TOWN_COORDS
from core.services.town_pathfinding import find_path
from core.town_constants import TOWN_LOCATIONS

logger = logging.getLogger(__name__)

TICK_INTERVAL = 2.0  # seconds between simulation ticks
AGENT_SPEED = 1.2  # tiles per tick (~0.6 tiles/sec)
PROXIMITY_THRESHOLD = 3.0  # tiles to be "nearby"
INACTIVE_TIMEOUT = timedelta(minutes=5)  # send home after 5min without heartbeat


class TownSimulation:
    """Manages the GooseTown simulation loop."""

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        # Track which agent pairs were nearby last tick (frozenset of agent_name pairs)
        self._nearby_pairs: Set[frozenset] = set()
        # A* precomputed paths: agent_id -> list of (x, y) waypoints remaining
        self._agent_paths: Dict[str, List[Tuple[int, int]]] = {}
        # Pending cross-context destinations: agent_id -> final destination name
        self._pending_destinations: Dict[str, str] = {}

    async def start(self):
        """Start the simulation background task."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("GooseTown simulation started")

    async def stop(self):
        """Stop the simulation."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("GooseTown simulation stopped")

    async def _run_loop(self):
        """Main simulation tick loop."""
        while self._running:
            try:
                await self._tick()
            except Exception as e:
                logger.error(f"GooseTown tick error: {e}", exc_info=True)

            await asyncio.sleep(TICK_INTERVAL)

    async def _tick(self):
        """Execute one simulation tick.

        Steps:
        1. Move agents toward target_x/target_y
        2. Handle going_home -> sleeping transition
        3. Detect inactive agents -> send home
        4. Track nearby agent pairs
        """
        from core.services.town_service import TownService

        service = TownService()
        states = service.get_town_state()

        if not states:
            return

        now = datetime.now(timezone.utc)

        for agent_state in states:
            agent_id = agent_state["agent_id"]
            agent_name = agent_state["agent_name"]
            location_state = agent_state["location_state"] or "active"

            # Skip sleeping agents entirely
            if location_state == "sleeping":
                continue

            # Skip agents in a conversation (don't move them)
            if agent_state["current_conversation_id"] is not None:
                continue

            # --- Movement toward target_x/target_y using A* paths ---
            tx = agent_state["target_x"]
            ty = agent_state["target_y"]

            if tx is not None and ty is not None:
                # Compute A* path if we don't have one for this agent
                if agent_id not in self._agent_paths:
                    location_context = agent_state.get("location_context", "apartment")
                    path = find_path(
                        agent_state["position_x"],
                        agent_state["position_y"],
                        tx,
                        ty,
                        context=location_context,
                    )
                    if path and len(path) > 1:
                        # Skip the first waypoint (current position)
                        self._agent_paths[agent_id] = path[1:]
                    elif path and len(path) == 1:
                        self._agent_paths[agent_id] = path
                    else:
                        # No path found — move straight as fallback
                        logger.debug("No A* path for %s, using straight line", agent_name)
                        self._agent_paths[agent_id] = [(round(tx), round(ty))]

                # Follow the next waypoint in the path
                waypoints = self._agent_paths.get(agent_id, [])
                if waypoints:
                    wp_x, wp_y = float(waypoints[0][0]), float(waypoints[0][1])
                    new_x, new_y, wp_arrived = self._move_toward(
                        agent_state["position_x"],
                        agent_state["position_y"],
                        wp_x,
                        wp_y,
                        AGENT_SPEED,
                    )

                    if wp_arrived:
                        waypoints.pop(0)

                    arrived = wp_arrived and len(waypoints) == 0
                else:
                    new_x = agent_state["position_x"]
                    new_y = agent_state["position_y"]
                    arrived = True

                # Compute facing direction from movement delta
                dx = new_x - agent_state["position_x"]
                dy = new_y - agent_state["position_y"]
                move_dist = math.sqrt(dx * dx + dy * dy)

                update = {
                    "position_x": new_x,
                    "position_y": new_y,
                    "speed": AGENT_SPEED,
                }

                if move_dist > 0.001:
                    update["facing_x"] = dx / move_dist
                    update["facing_y"] = dy / move_dist

                if arrived:
                    update["target_x"] = None
                    update["target_y"] = None
                    update["speed"] = 0.0
                    update["current_activity"] = "idle"
                    self._agent_paths.pop(agent_id, None)

                    # Set current_location from target_location if provided
                    if agent_state["target_location"]:
                        update["current_location"] = agent_state["target_location"]
                        update["target_location"] = None

                    # Scatter within location bounds (town only, not during transitions or going_home)
                    arrived_location = update.get("current_location", agent_state.get("current_location"))
                    arrived_context = agent_state.get("location_context", "apartment")
                    if (
                        arrived_context == "town"
                        and arrived_location in TOWN_LOCATIONS
                        and location_state != "going_home"
                    ):
                        occupied = []
                        for other in states:
                            if other["agent_id"] != agent_id and other.get("current_location") == arrived_location:
                                occupied.append((other["position_x"], other["position_y"]))
                        scatter = self._pick_scatter_point(arrived_location, exclude_positions=occupied)
                        if scatter and (scatter[0] != new_x or scatter[1] != new_y):
                            update["target_x"] = scatter[0]
                            update["target_y"] = scatter[1]
                            update["target_location"] = None
                            update["current_activity"] = "walking"
                            update["speed"] = AGENT_SPEED
                            arrived = False

                    # Check for pending cross-context transition
                    pending = self._pending_destinations.pop(agent_id, None) if arrived else None
                    if pending:
                        cur_context = agent_state.get("location_context", "apartment")
                        if cur_context == "apartment":
                            # Arrived at exit -> transition to town
                            update["location_context"] = "town"
                            update["position_x"] = RESIDENTIAL_TOWN_COORDS["x"]
                            update["position_y"] = RESIDENTIAL_TOWN_COORDS["y"]
                            town_dest = TOWN_LOCATIONS.get(pending)
                            if town_dest:
                                update["target_x"] = float(town_dest["x"])
                                update["target_y"] = float(town_dest["y"])
                                update["target_location"] = pending
                                update["current_activity"] = "walking"
                                update["speed"] = AGENT_SPEED
                        else:
                            # Arrived at residential -> transition to apartment
                            update["location_context"] = "apartment"
                            update["position_x"] = float(APARTMENT_SPOTS["exit"]["x"])
                            update["position_y"] = float(APARTMENT_SPOTS["exit"]["y"])
                            apt_dest = APARTMENT_SPOTS.get(pending)
                            if apt_dest:
                                update["target_x"] = float(apt_dest["x"])
                                update["target_y"] = float(apt_dest["y"])
                                update["target_location"] = pending
                                update["current_activity"] = "walking"
                                update["speed"] = AGENT_SPEED

                    # Handle going_home -> sleeping transition
                    if arrived and location_state == "going_home" and not pending:
                        update["location_state"] = "sleeping"

                        from core.services.town_mood_engine import apply_event as _apply_mood_event
                        from core.services.town_mood_engine import parse_mood

                        _sleep_energy, _sleep_mood = _apply_mood_event(
                            "sleep",
                            energy=agent_state.get("energy", 50),
                            mood=parse_mood(agent_state.get("mood")),
                            traits=agent_state.get("traits", ""),
                        )
                        update["energy"] = _sleep_energy
                        update["mood"] = str(_sleep_mood)

                        logger.debug("Agent %s arrived home, now sleeping", agent_name)
                    elif arrived and not pending:
                        # Apply arrived_new_location mood/energy effect
                        from core.services.town_mood_engine import (
                            apply_event as _apply_mood_event,
                        )
                        from core.services.town_mood_engine import (
                            parse_mood as _parse_mood,
                        )

                        _arr_energy, _arr_mood = _apply_mood_event(
                            "arrived_new_location",
                            energy=agent_state.get("energy", 100),
                            mood=_parse_mood(agent_state.get("mood")),
                            traits=agent_state.get("traits", ""),
                        )
                        update["energy"] = _arr_energy
                        update["mood"] = str(_arr_mood)

                service.update_agent_state(agent_id, **update)

            # --- Inactive detection: send home if no heartbeat for 5min ---
            if location_state == "active":
                heartbeat = agent_state["last_heartbeat_at"]
                if heartbeat is None or (now - heartbeat) > INACTIVE_TIMEOUT:
                    cur_context = agent_state.get("location_context", "apartment")
                    self._agent_paths.pop(agent_id, None)
                    if cur_context == "town":
                        # In town -> walk to residential, then transition to apartment
                        self._pending_destinations[agent_id] = "bed_1"
                        service.update_agent_state(
                            agent_id,
                            location_state="going_home",
                            target_x=RESIDENTIAL_TOWN_COORDS["x"],
                            target_y=RESIDENTIAL_TOWN_COORDS["y"],
                            target_location="residence",
                            current_activity="walking",
                            speed=AGENT_SPEED,
                        )
                    else:
                        # Already in apartment -> walk to bed
                        service.update_agent_state(
                            agent_id,
                            location_state="going_home",
                            target_x=float(APARTMENT_SPOTS["bed_1"]["x"]),
                            target_y=float(APARTMENT_SPOTS["bed_1"]["y"]),
                            target_location="bed_1",
                            current_activity="walking",
                            speed=AGENT_SPEED,
                        )
                    logger.info(
                        "Agent %s inactive for >5min, sending home",
                        agent_name,
                    )

        # --- Proximity detection ---
        current_nearby = set()
        active_states = [
            s
            for s in states
            if (s["location_state"] or "active") != "sleeping" and s["current_conversation_id"] is None
        ]

        for i, a in enumerate(active_states):
            for b in active_states[i + 1 :]:
                # Only compare agents in the same coordinate space
                if a.get("location_context", "apartment") != b.get("location_context", "apartment"):
                    continue
                dist = self._calculate_distance(
                    a["position_x"],
                    a["position_y"],
                    b["position_x"],
                    b["position_y"],
                )
                if dist <= PROXIMITY_THRESHOLD:
                    pair = frozenset((a["agent_name"], b["agent_name"]))
                    current_nearby.add(pair)

        self._nearby_pairs = current_nearby


    @staticmethod
    def _pick_scatter_point(
        location_id: str,
        exclude_positions: list[tuple[float, float]] | None = None,
    ) -> tuple[float, float] | None:
        """Pick a random walkable tile within a location's bounds, avoiding occupied positions."""
        from core.services.town_pathfinding import is_walkable

        loc = TOWN_LOCATIONS.get(location_id)
        if not loc or "bounds" not in loc:
            return None

        bounds = loc["bounds"]
        candidates = []
        for x in range(bounds["x1"], bounds["x2"] + 1):
            for y in range(bounds["y1"], bounds["y2"] + 1):
                if is_walkable(x, y, context="town"):
                    if exclude_positions and (float(x), float(y)) in exclude_positions:
                        continue
                    candidates.append((float(x), float(y)))

        if not candidates:
            return None

        return random.choice(candidates)

    @staticmethod
    def _calculate_distance(x1: float, y1: float, x2: float, y2: float) -> float:
        """Euclidean distance between two points."""
        return math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)

    @staticmethod
    def _move_toward(
        current_x: float,
        current_y: float,
        target_x: float,
        target_y: float,
        speed: float,
    ) -> Tuple[float, float, bool]:
        """Move toward target at given speed. Returns (new_x, new_y, arrived)."""
        dx = target_x - current_x
        dy = target_y - current_y
        dist = math.sqrt(dx * dx + dy * dy)

        if dist <= speed:
            return target_x, target_y, True

        ratio = speed / dist
        return current_x + dx * ratio, current_y + dy * ratio, False

