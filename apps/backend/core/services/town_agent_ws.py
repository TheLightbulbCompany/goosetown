"""Track WebSocket connections for GooseTown agents.

Maps API Gateway connection IDs to agent info, enabling the simulation
to push events to specific agents over WebSocket.
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class AgentConnection:
    connection_id: str
    user_id: str
    agent_name: str
    agent_id: str  # UUID as string
    instance_id: str  # UUID as string
    location_context: str = "town"
    exhausted_rejections: int = 0  # count of acts rejected while energy=0


class TownAgentWsManager:
    """In-memory registry of connected town agents."""

    def __init__(self):
        self._by_connection: dict[str, AgentConnection] = {}
        self._by_agent_name: dict[str, str] = {}  # agent_name -> connection_id
        self._by_agent_id: dict[str, str] = {}  # agent_id (UUID) -> connection_id

    def register(
        self,
        connection_id: str,
        user_id: str,
        agent_name: str,
        agent_id: str,
        instance_id: str,
    ):
        """Register an agent WebSocket connection.

        Args:
            connection_id: API Gateway WebSocket connection ID.
            user_id: Clerk user ID owning this agent.
            agent_name: Machine-readable agent name.
            agent_id: Agent UUID from DynamoDB.
            instance_id: Town instance UUID.
        """
        # Clean up stale connection if agent is re-registering (e.g., after backend restart)
        old_conn_id = self._by_agent_id.get(agent_id)
        if old_conn_id and old_conn_id != connection_id:
            self._by_connection.pop(old_conn_id, None)
            logger.debug("Cleaned up stale connection %s for agent %s", old_conn_id, agent_name)

        conn = AgentConnection(connection_id, user_id, agent_name, agent_id, instance_id)
        self._by_connection[connection_id] = conn
        self._by_agent_name[agent_name] = connection_id
        self._by_agent_id[agent_id] = connection_id
        logger.info("Town agent registered: %s on connection %s", agent_name, connection_id)

    def unregister(self, connection_id: str):
        """Remove a connection from all indexes.

        Args:
            connection_id: API Gateway WebSocket connection ID.
        """
        conn = self._by_connection.pop(connection_id, None)
        if conn:
            self._by_agent_name.pop(conn.agent_name, None)
            self._by_agent_id.pop(conn.agent_id, None)
            logger.info("Town agent unregistered: %s", conn.agent_name)

    def get_by_connection(self, connection_id: str) -> AgentConnection | None:
        """Return the connection record for an API Gateway connection ID."""
        return self._by_connection.get(connection_id)

    def get_by_agent_id(self, agent_id: str) -> AgentConnection | None:
        """Return the connection record for an agent UUID, or None if not connected.

        Args:
            agent_id: Agent UUID from DynamoDB.
        """
        conn_id = self._by_agent_id.get(agent_id)
        if conn_id is None:
            return None
        return self._by_connection.get(conn_id)

    def get_agent_connection_id(self, agent_name: str) -> str | None:
        """Return the API Gateway connection ID for a given agent machine name."""
        return self._by_agent_name.get(agent_name)

    def is_agent_connected(self, agent_name: str) -> bool:
        """Return True if an agent with this machine name is currently connected."""
        return agent_name in self._by_agent_name

    def update_context(self, agent_id: str, context: str) -> None:
        """Update the location_context for an agent.

        Uses the two-step lookup: agent_id -> connection_id -> AgentConnection.

        Args:
            agent_id: Agent UUID from DynamoDB.
            context: New location context (e.g. "town" or "apartment").
        """
        conn_id = self._by_agent_id.get(agent_id)
        if conn_id is not None:
            conn = self._by_connection.get(conn_id)
            if conn:
                conn.location_context = context

    def connected_agents(self) -> list[AgentConnection]:
        """Return all currently registered agent connections."""
        return list(self._by_connection.values())


# Module-level singleton
_town_agent_ws_manager: TownAgentWsManager | None = None


def get_town_agent_ws_manager() -> TownAgentWsManager:
    global _town_agent_ws_manager
    if _town_agent_ws_manager is None:
        _town_agent_ws_manager = TownAgentWsManager()
    return _town_agent_ws_manager
