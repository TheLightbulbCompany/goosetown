"""GooseTown simulation engine.

Runs as an asyncio background task inside FastAPI. Manages agent movement,
state transitions, and event dispatch to agent WebSocket connections.

The simulation is THIN -- it does NOT make decisions for agents. It only:
- Interpolates movement toward target_x/target_y
- Detects arrivals and pushes "arrived" events
- Detects nearby agents and pushes "nearby" events
- Handles transition states (going_home -> sleeping)
- Detects inactive agents and sends them home
- Broadcasts state to viewers
"""

import asyncio
import logging
import math
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Set, Tuple

from core.apartment_constants import APARTMENT_SPOTS, RESIDENTIAL_TOWN_COORDS
from core.services.town_pathfinding import find_path
from core.town_constants import TOWN_LOCATIONS

logger = logging.getLogger(__name__)

TICK_INTERVAL = 2.0  # seconds between simulation ticks
AGENT_SPEED = 1.2  # tiles per tick (~0.6 tiles/sec)
PROXIMITY_THRESHOLD = 3.0  # tiles to be "nearby"
INACTIVE_TIMEOUT = timedelta(minutes=5)  # send home after 5min without heartbeat
WORLD_UPDATE_INTERVAL = 5  # Push world_update every N ticks (N * TICK_INTERVAL seconds)
THINK_INTERVAL = 15.0  # seconds between autonomous think pings per agent


class TownSimulation:
    """Manages the GooseTown simulation loop."""

    def __init__(self, notify_fn: Optional[Callable] = None):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._notify_fn = notify_fn
        # Track which agent pairs were nearby last tick (frozenset of agent_name pairs)
        self._nearby_pairs: Set[frozenset] = set()
        # Lazy-initialized management API client for pushing events to agents
        self._mgmt_client = None
        self._mgmt_client_failed = False
        # Lazy-initialized connection service
        self._conn_service = None
        # A* precomputed paths: agent_id -> list of (x, y) waypoints remaining
        self._agent_paths: Dict[str, List[Tuple[int, int]]] = {}
        # Pending cross-context destinations: agent_id -> final destination name
        self._pending_destinations: Dict[str, str] = {}
        # Tick counter for periodic world updates
        self._tick_count: int = 0
        # Per-agent timestamps for autonomous think pings
        self._last_think_at: Dict[str, float] = {}

    def _get_mgmt_client(self):
        """Lazily create ManagementApiClient. Returns None if unavailable."""
        if self._mgmt_client is not None:
            return self._mgmt_client
        if self._mgmt_client_failed:
            return None
        try:
            from core.services.management_api_client import ManagementApiClient

            self._mgmt_client = ManagementApiClient()
            return self._mgmt_client
        except Exception:
            logger.debug("ManagementApiClient not available (expected in local dev)")
            self._mgmt_client_failed = True
            return None

    def _get_connection_service(self):
        """Get or create the WsConnectionService."""
        if self._conn_service is None:
            from core.services.ws_connection_service import WsConnectionService

            self._conn_service = WsConnectionService()
        return self._conn_service

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

    def set_notify_fn(self, fn: Callable):
        """Set the WebSocket push callback (called after state changes)."""
        self._notify_fn = fn

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
        1. Wake alarm: wake sleeping agents whose alarm has fired
        2. Move agents toward target_x/target_y
        3. Detect arrivals -> push "arrived" event
        4. Handle going_home -> sleeping transition
        5. Detect inactive agents -> send home (only if disconnected)
        6. Detect proximity changes -> push "nearby" events
        7. Periodic world_update push to connected agents
        8. Broadcast state to viewers
        """
        from core.services.town_service import TownService

        service = TownService()
        states = service.get_town_state()

        if not states:
            # Still call notify_fn even with no agents
            if self._notify_fn:
                try:
                    self._notify_fn()
                except Exception as e:
                    logger.debug(f"WS notify failed: {e}")
            return

        now = datetime.now(timezone.utc)
        conn_svc = self._get_connection_service()

        # Collect events to push after DB updates
        events_to_push: list[tuple[str, dict]] = []  # (agent_name, payload)

        for agent_state in states:
            agent_id = agent_state["agent_id"]
            agent_name = agent_state["agent_name"]
            location_state = agent_state["location_state"] or "active"

            # --- Wake alarm: wake sleeping agents whose alarm has fired ---
            if location_state == "sleeping" and conn_svc.is_agent_connected(agent_name):
                wake_at = agent_state.get("wake_at")
                if wake_at and now >= wake_at:
                    service.update_agent_state(
                        agent_id,
                        location_state="active",
                        wake_at=None,
                        wake_timezone=None,
                    )
                    events_to_push.append(
                        (
                            agent_name,
                            {
                                "type": "town_event",
                                "event": "wake",
                                "message": "Your alarm went off. You just woke up in GooseTown.",
                            },
                        )
                    )
                    logger.info("Agent %s alarm fired, waking up", agent_name)

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

                        # Push "arrived" event to connected agent
                        events_to_push.append(
                            (
                                agent_name,
                                {
                                    "type": "town_event",
                                    "event": "arrived",
                                    "location": update.get(
                                        "current_location",
                                        agent_state["current_location"],
                                    ),
                                    "position": {"x": new_x, "y": new_y},
                                },
                            )
                        )

                service.update_agent_state(agent_id, **update)

            # --- Inactive detection: send home if no heartbeat for 5min ---
            # Only send home if agent is NOT connected (connected agents may just
            # have stale heartbeats due to WS-only communication)
            if location_state == "active" and not conn_svc.is_agent_connected(agent_name):
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

                    # Only push event for NEW proximity (not already nearby last tick)
                    if pair not in self._nearby_pairs:
                        # Push to agent a if connected
                        events_to_push.append(
                            (
                                a["agent_name"],
                                {
                                    "type": "town_event",
                                    "event": "nearby",
                                    "nearby_agent": b["agent_name"],
                                    "nearby_display_name": b["display_name"],
                                    "distance": round(dist, 2),
                                },
                            )
                        )
                        # Push to agent b if connected
                        events_to_push.append(
                            (
                                b["agent_name"],
                                {
                                    "type": "town_event",
                                    "event": "nearby",
                                    "nearby_agent": a["agent_name"],
                                    "nearby_display_name": a["display_name"],
                                    "distance": round(dist, 2),
                                },
                            )
                        )

        self._nearby_pairs = current_nearby

        # --- Push events to connected agents ---
        self._push_agent_events(conn_svc, events_to_push)

        # --- World update push (every N ticks) ---
        self._tick_count += 1
        if self._tick_count % WORLD_UPDATE_INTERVAL == 0:
            # Bulk-load relationships for enrichment
            relationships_cache = {}
            try:
                rel_service = TownService()
                rel_rows = rel_service.get_all_relationships()
                for r in rel_rows:
                    key = frozenset([str(r["agent_a_id"]), str(r["agent_b_id"])])
                    relationships_cache[key] = r
            except Exception:
                pass  # graceful degradation
            self._push_world_updates(conn_svc, states, relationships_cache)

        # Push updated state to WebSocket viewers
        if self._notify_fn:
            try:
                self._notify_fn()
            except Exception as e:
                logger.debug(f"WS notify failed: {e}")

    def _push_agent_events(
        self,
        conn_svc,
        events: list[tuple[str, dict]],
    ):
        """Push events to connected agents via Management API.

        Best-effort: logs errors but never crashes the tick.
        """
        if not events:
            return

        mgmt = self._get_mgmt_client()
        if mgmt is None:
            return

        for agent_name, payload in events:
            connection_id = conn_svc.get_agent_connection_id(agent_name)
            if connection_id is None:
                continue
            try:
                mgmt.send_message(connection_id, payload)
            except Exception as e:
                logger.debug("Failed to push event to agent %s: %s", agent_name, e)

    def _push_world_updates(self, conn_svc, states, relationships_cache=None):
        """Push world_update to each connected agent with full world context."""
        mgmt = self._get_mgmt_client()
        if mgmt is None:
            return

        if relationships_cache is None:
            relationships_cache = {}

        # Build location occupancy map
        location_agents: dict[str, list[str]] = {}
        for s in states:
            loc = s.get("current_location")
            if loc:
                location_agents.setdefault(loc, []).append(s.get("display_name", s["agent_name"]))

        for agent_state in states:
            agent_name = agent_state["agent_name"]
            conn_id = conn_svc.get_agent_connection_id(agent_name)
            if conn_id is None:
                continue

            if (agent_state.get("location_state") or "active") == "sleeping":
                continue

            nearby = []
            for other in states:
                if other["agent_name"] == agent_name:
                    continue
                if (other.get("location_state") or "active") == "sleeping":
                    continue
                dist = self._calculate_distance(
                    agent_state["position_x"],
                    agent_state["position_y"],
                    other["position_x"],
                    other["position_y"],
                )
                if dist <= PROXIMITY_THRESHOLD * 2:
                    nearby.append(
                        {
                            "name": other.get("display_name", other["agent_name"]),
                            "agent_name": other["agent_name"],
                            "position": [
                                round(other["position_x"], 1),
                                round(other["position_y"], 1),
                            ],
                            "activity": other.get("current_activity", "idle"),
                            "distance": round(dist, 1),
                        }
                    )

            # Enrich nearby agents with personality and relationship data
            if nearby:
                agent_id = str(agent_state["agent_id"])
                for n in nearby:
                    n_agent_name = n.get("agent_name")
                    for other_state in states:
                        if other_state["agent_name"] == n_agent_name:
                            n["personality"] = other_state.get("personality_summary") or ""
                            n_agent_id = str(other_state["agent_id"])
                            rel_key = frozenset([agent_id, n_agent_id])
                            rel_data = relationships_cache.get(rel_key)
                            if rel_data:
                                n["relationship_type"] = rel_data.get("relationship_type", "stranger")
                                n["interaction_count"] = rel_data.get("interaction_count", 0)
                                n["last_topic"] = rel_data.get("last_topic")
                            break

            payload = {
                "type": "world_update",
                "you": {
                    "position": [
                        round(agent_state["position_x"], 1),
                        round(agent_state["position_y"], 1),
                    ],
                    "current_location": agent_state.get("current_location"),
                    "location_context": agent_state.get("location_context", "apartment"),
                    "mood": agent_state.get("mood"),
                    "energy": agent_state.get("energy"),
                    "activity": agent_state.get("current_activity", "idle"),
                },
                "nearby_agents": nearby,
                "locations": [
                    {
                        "name": loc_data["label"],
                        "id": loc_id,
                        "position": [loc_data["x"], loc_data["y"]],
                        "agents_here": location_agents.get(loc_id, []),
                    }
                    for loc_id, loc_data in TOWN_LOCATIONS.items()
                ],
                "active_conversations": [],
            }

            # Generate context summary for agent's TOWN_STATUS.md
            payload["context_summary"] = self._build_context_summary(agent_state, nearby)

            # Add think ping if enough time has elapsed and agent is idle
            now_ts = time.time()
            last_think = self._last_think_at.get(agent_name, 0)
            is_walking = agent_state.get("target_x") is not None
            is_sleeping = (agent_state.get("location_state") or "active") == "sleeping"

            if (now_ts - last_think >= THINK_INTERVAL) and not is_walking and not is_sleeping:
                payload["think"] = True
                self._last_think_at[agent_name] = now_ts

            try:
                mgmt.send_message(conn_id, payload)
            except Exception as e:
                logger.debug("Failed to push world_update to %s: %s", agent_name, e)

    @staticmethod
    def _build_context_summary(
        agent_state: dict,
        nearby: list[dict],
        pending_messages: list | None = None,
    ) -> str:
        """Build a markdown context summary for an agent's TOWN_STATUS.md."""
        from core.apartment_constants import APARTMENT_ROOMS, APARTMENT_SPOTS
        from core.services.town_mood_engine import mood_label, parse_mood

        location_context = agent_state.get("location_context", "apartment")
        current_location = agent_state.get("current_location", "unknown")
        activity = agent_state.get("current_activity", "idle")
        mood_int = parse_mood(agent_state.get("mood"))
        mood_str = mood_label(mood_int)
        energy = agent_state.get("energy")
        energy_str = f"{energy}%" if energy is not None else "unknown"

        # Resolve location label and activities
        if location_context == "town":
            loc_data = TOWN_LOCATIONS.get(current_location, {})
            loc_label = loc_data.get("label", current_location)
            activities = loc_data.get("activities", ["walk around", "chat with nearby agents"])
        else:
            # In apartment — find room from spot or use location directly
            spot_data = APARTMENT_SPOTS.get(current_location, {})
            room_name = spot_data.get("room", current_location) if spot_data else current_location
            room_data = APARTMENT_ROOMS.get(room_name, {})
            loc_label = room_data.get("label", current_location)
            activities = room_data.get("activities", ["relax", "chat with roommates"])

        context_label = "town" if location_context == "town" else "apartment"

        lines = [
            "# GooseTown Status",
            "",
            f"**Location:** {loc_label} ({context_label})",
            f"**Activity:** {activity}",
            f"**Mood:** {mood_str} | **Energy:** {energy_str}",
            "",
        ]

        # Nearby agents
        if nearby:
            lines.append("**Nearby:**")
            for n in nearby:
                name = n.get("name", n.get("agent_name", "someone"))
                dist = n.get("distance", "?")
                n_activity = n.get("activity", "idle")
                personality = n.get("personality", "")
                line = f"- {name} ({dist} tiles away, {n_activity})"
                if personality:
                    line += f' — "{personality}"'
                lines.append(line)

                # Show relationship details when meaningful
                rel_type = n.get("relationship_type")
                interaction_count = n.get("interaction_count", 0)
                if rel_type and rel_type != "stranger" and interaction_count > 0:
                    last_topic = n.get("last_topic")
                    rel_line = f"  Relationship: {rel_type} (talked {interaction_count} times"
                    if last_topic:
                        rel_line += f", last topic: {last_topic}"
                    rel_line += ")"
                    lines.append(rel_line)
        else:
            lines.append("**Nearby:** no one")
        lines.append("")

        # Available activities
        lines.append(f"**Available here:** {', '.join(activities)}")
        lines.append("")

        # Pending messages
        msg_count = len(pending_messages) if pending_messages else 0
        if msg_count > 0:
            lines.append(f"**Pending messages:** {msg_count} — run town_check to read them")
        else:
            lines.append("**Pending messages:** None")

        return "\n".join(lines)

    def _pick_random_location(self, exclude: Optional[str] = None) -> str:
        """Pick a random town location, excluding current."""
        choices = [loc for loc in TOWN_LOCATIONS if loc != exclude]
        return random.choice(choices)

    @staticmethod
    def _conversation_probability(affinity: int) -> float:
        """Calculate conversation probability based on relationship affinity.

        Strangers (0): 15%
        Acquaintances (25): ~30%
        Friends (50): ~45%
        Close friends (75): ~60%
        Best friends (100): ~70%
        """
        base = 0.15
        bonus = (affinity / 100.0) * 0.55
        return min(0.70, base + bonus)

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
