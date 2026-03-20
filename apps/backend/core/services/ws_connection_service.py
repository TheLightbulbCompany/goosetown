"""WebSocket connection tracking service (DynamoDB-backed).

Replaces the old in-memory TownAgentWsManager. Stores connection state in
DynamoDB so it survives Fargate task restarts and works across multiple tasks.

The $connect/$disconnect Lambda handlers write to the same table — this
service provides read access for the simulation engine running in Fargate.

Table schema:
  PK: connectionId (S)
  GSI agentName-index: agentName (S)
  GSI connectionType-index: connectionType (S)
  TTL: ttl (N)
"""

import logging
import time
from typing import Optional

from boto3.dynamodb.conditions import Key

from core import dynamo

logger = logging.getLogger(__name__)

# Connections auto-expire after 24 hours if not cleaned up by $disconnect
CONNECTION_TTL_SECONDS = 86400


class WsConnectionService:
    """DynamoDB-backed WebSocket connection tracking."""

    def register(
        self,
        connection_id: str,
        user_id: str,
        connection_type: str,
        agent_name: Optional[str] = None,
    ) -> None:
        """Register a WebSocket connection.

        Called by the $connect Lambda or during testing. Stores connection
        metadata in DynamoDB with a TTL for automatic cleanup.
        """
        table = dynamo.connections_table()
        now = int(time.time())
        item = {
            "connectionId": connection_id,
            "userId": user_id,
            "connectionType": connection_type,
            "connectedAt": str(now),
            "ttl": now + CONNECTION_TTL_SECONDS,
        }
        if agent_name:
            item["agentName"] = agent_name
        table.put_item(Item=item)
        logger.debug("Registered connection %s (type=%s, agent=%s)", connection_id, connection_type, agent_name)

    def unregister(self, connection_id: str) -> None:
        """Remove a WebSocket connection.

        Called by the $disconnect Lambda or during testing.
        """
        table = dynamo.connections_table()
        table.delete_item(Key={"connectionId": connection_id})
        logger.debug("Unregistered connection %s", connection_id)

    def get_by_connection_id(self, connection_id: str) -> Optional[dict]:
        """Look up a connection by its ID."""
        table = dynamo.connections_table()
        resp = table.get_item(Key={"connectionId": connection_id})
        return resp.get("Item")

    def is_agent_connected(self, agent_name: str) -> bool:
        """Check if an agent has an active WebSocket connection."""
        return self.get_agent_connection_id(agent_name) is not None

    def get_agent_connection_id(self, agent_name: str) -> Optional[str]:
        """Get the connectionId for a connected agent, or None."""
        table = dynamo.connections_table()
        resp = table.query(
            IndexName="agentName-index",
            KeyConditionExpression=Key("agentName").eq(agent_name),
        )
        items = resp.get("Items", [])
        if not items:
            return None
        # Return the most recent connection if multiple exist
        if len(items) > 1:
            items.sort(key=lambda x: x.get("connectedAt", "0"), reverse=True)
        return items[0]["connectionId"]

    def get_viewer_connection_ids(self) -> list[str]:
        """Get all viewer connection IDs for broadcast."""
        table = dynamo.connections_table()
        resp = table.query(
            IndexName="connectionType-index",
            KeyConditionExpression=Key("connectionType").eq("viewer"),
        )
        return [item["connectionId"] for item in resp.get("Items", [])]

    def get_all_agent_connections(self) -> list[dict]:
        """Get all agent connections (for world_update broadcast)."""
        table = dynamo.connections_table()
        resp = table.query(
            IndexName="connectionType-index",
            KeyConditionExpression=Key("connectionType").eq("agent"),
        )
        return resp.get("Items", [])
