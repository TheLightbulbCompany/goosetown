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


class TownAgentWsManager:
    """In-memory registry of connected town agents."""

    def __init__(self):
        self._by_connection: dict[str, AgentConnection] = {}
        self._by_agent_name: dict[str, str] = {}  # agent_name -> connection_id

    def register(
        self,
        connection_id: str,
        user_id: str,
        agent_name: str,
        agent_id: str,
        instance_id: str,
    ):
        conn = AgentConnection(connection_id, user_id, agent_name, agent_id, instance_id)
        self._by_connection[connection_id] = conn
        self._by_agent_name[agent_name] = connection_id
        logger.info("Town agent registered: %s on connection %s", agent_name, connection_id)

    def unregister(self, connection_id: str):
        conn = self._by_connection.pop(connection_id, None)
        if conn:
            self._by_agent_name.pop(conn.agent_name, None)
            logger.info("Town agent unregistered: %s", conn.agent_name)

    def get_by_connection(self, connection_id: str) -> AgentConnection | None:
        return self._by_connection.get(connection_id)

    def get_agent_connection_id(self, agent_name: str) -> str | None:
        return self._by_agent_name.get(agent_name)

    def is_agent_connected(self, agent_name: str) -> bool:
        return agent_name in self._by_agent_name

    def connected_agents(self) -> list[AgentConnection]:
        return list(self._by_connection.values())


# Module-level singleton
_town_agent_ws_manager: TownAgentWsManager | None = None


def get_town_agent_ws_manager() -> TownAgentWsManager:
    global _town_agent_ws_manager
    if _town_agent_ws_manager is None:
        _town_agent_ws_manager = TownAgentWsManager()
    return _town_agent_ws_manager
