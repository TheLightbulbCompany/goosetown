"""
Management API Client - pushes messages to WebSocket clients via API Gateway.

This client uses the API Gateway Management API to send messages to connected
WebSocket clients. It handles the outbound direction of the WebSocket flow:

Architecture:
- INBOUND: Client WebSocket -> API Gateway -> HTTP POST -> VPC Link -> ALB -> Fargate
- OUTBOUND: Fargate -> POST to Management API -> API Gateway -> Client WebSocket

The Management API endpoint URL format:
https://{api-id}.execute-api.{region}.amazonaws.com/{stage}

Usage:
    client = ManagementApiClient()

    # Send a message to a WebSocket client
    success = client.send_message(connection_id, {"type": "message", "content": "Hello"})

    # Close a WebSocket connection
    client.close_connection(connection_id)
"""

import json
import logging
from typing import Any

import boto3
from botocore.exceptions import ClientError

from core.config import settings

logger = logging.getLogger(__name__)


class ManagementApiClientError(Exception):
    """Base exception for management API client errors."""


class ManagementApiClient:
    """
    Client for API Gateway WebSocket Management API.

    Enables the backend to push messages to connected WebSocket clients
    and manage their connections.
    """

    def __init__(self, endpoint_url: str | None = None, region_name: str | None = None):
        """
        Initialize the management API client.

        Args:
            endpoint_url: Management API endpoint URL. If not provided, uses
                         settings.WS_MANAGEMENT_API_URL.
                         Format: https://{api-id}.execute-api.{region}.amazonaws.com/{stage}
            region_name: AWS region. If not provided, uses settings.AWS_REGION.

        Raises:
            ManagementApiClientError: If no endpoint URL is configured.
        """
        self.endpoint_url = endpoint_url or settings.WS_MANAGEMENT_API_URL

        if not self.endpoint_url:
            raise ManagementApiClientError(
                "No endpoint URL provided. Set WS_MANAGEMENT_API_URL environment "
                "variable or pass endpoint_url parameter."
            )

        region = region_name or settings.AWS_REGION
        self._client = boto3.client(
            "apigatewaymanagementapi",
            endpoint_url=self.endpoint_url,
            region_name=region,
        )

    def send_message(self, connection_id: str, payload: dict[str, Any]) -> bool:
        """
        Send a JSON message to a WebSocket client.

        Args:
            connection_id: API Gateway WebSocket connection ID
            payload: Dictionary to send (will be JSON encoded)

        Returns:
            True if message was sent successfully, False if connection is gone.

        Raises:
            ManagementApiClientError: If sending fails for reasons other than
                                     a gone connection.
        """
        data = json.dumps(payload).encode("utf-8")

        try:
            self._client.post_to_connection(
                ConnectionId=connection_id,
                Data=data,
            )
            logger.debug(
                "Sent message to connection %s: %d bytes",
                connection_id,
                len(data),
            )
            return True

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")

            if error_code == "GoneException":
                logger.warning(
                    "Connection %s is gone, message not delivered",
                    connection_id,
                )
                return False

            logger.error(
                "Failed to send message to connection %s: %s",
                connection_id,
                e.response.get("Error", {}).get("Message", str(e)),
            )
            raise ManagementApiClientError(
                f"Failed to send message to connection {connection_id}: "
                f"{e.response.get('Error', {}).get('Message', str(e))}"
            ) from e

    def close_connection(self, connection_id: str) -> None:
        """
        Close a WebSocket connection.

        Args:
            connection_id: API Gateway WebSocket connection ID

        Raises:
            ManagementApiClientError: If closing fails for reasons other than
                                     an already-gone connection.
        """
        try:
            self._client.delete_connection(ConnectionId=connection_id)
            logger.info("Closed connection %s", connection_id)

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")

            if error_code == "GoneException":
                logger.debug(
                    "Connection %s was already closed",
                    connection_id,
                )
                return

            logger.error(
                "Failed to close connection %s: %s",
                connection_id,
                e.response.get("Error", {}).get("Message", str(e)),
            )
            raise ManagementApiClientError(
                f"Failed to close connection {connection_id}: {e.response.get('Error', {}).get('Message', str(e))}"
            ) from e
