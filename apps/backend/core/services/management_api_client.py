"""Management API Client — pushes messages to WebSocket clients via API Gateway.

Uses the API Gateway Management API to send messages to connected WebSocket
clients. Handles the outbound direction of the WebSocket flow:

Architecture:
- INBOUND: Client WebSocket -> API Gateway -> Lambda ($connect/$disconnect)
                             -> HTTP integration -> Fargate (message routes)
- OUTBOUND: Fargate -> POST to Management API -> API Gateway -> Client WebSocket

The Management API endpoint URL is constructed from WS_API_ID + AWS_REGION:
  https://{api-id}.execute-api.{region}.amazonaws.com/{stage}
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
    """Client for API Gateway WebSocket Management API.

    Enables the backend to push messages to connected WebSocket clients
    and manage their connections.
    """

    def __init__(self, endpoint_url: str | None = None, region_name: str | None = None):
        """Initialize the management API client.

        Args:
            endpoint_url: Full management API endpoint URL. If not provided,
                         constructed from settings.WS_API_ID + settings.AWS_REGION.
            region_name: AWS region. If not provided, uses settings.AWS_REGION.

        Raises:
            ManagementApiClientError: If no endpoint URL can be determined.
        """
        if endpoint_url:
            self.endpoint_url = endpoint_url
        elif settings.WS_API_ID:
            region = region_name or settings.AWS_REGION
            stage = settings.WS_STAGE
            self.endpoint_url = f"https://{settings.WS_API_ID}.execute-api.{region}.amazonaws.com/{stage}"
        else:
            raise ManagementApiClientError(
                "No endpoint URL available. Set WS_API_ID environment variable "
                "or pass endpoint_url parameter."
            )

        region = region_name or settings.AWS_REGION
        self._client = boto3.client(
            "apigatewaymanagementapi",
            endpoint_url=self.endpoint_url,
            region_name=region,
        )

    def send_message(self, connection_id: str, payload: dict[str, Any]) -> bool:
        """Send a JSON message to a WebSocket client.

        Returns True if sent successfully, False if connection is gone.
        Raises ManagementApiClientError for other failures.
        """
        data = json.dumps(payload).encode("utf-8")

        try:
            self._client.post_to_connection(
                ConnectionId=connection_id,
                Data=data,
            )
            logger.debug("Sent message to %s: %d bytes", connection_id, len(data))
            return True

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "GoneException":
                logger.warning("Connection %s is gone", connection_id)
                return False
            error_msg = e.response.get("Error", {}).get("Message", str(e))
            logger.error("Failed to send to %s: %s", connection_id, error_msg)
            raise ManagementApiClientError(
                f"Failed to send message to connection {connection_id}: {error_msg}"
            ) from e

    def close_connection(self, connection_id: str) -> None:
        """Close a WebSocket connection.

        Raises ManagementApiClientError for failures other than already-gone.
        """
        try:
            self._client.delete_connection(ConnectionId=connection_id)
            logger.info("Closed connection %s", connection_id)
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "GoneException":
                logger.debug("Connection %s was already closed", connection_id)
                return
            error_msg = e.response.get("Error", {}).get("Message", str(e))
            logger.error("Failed to close %s: %s", connection_id, error_msg)
            raise ManagementApiClientError(
                f"Failed to close connection {connection_id}: {error_msg}"
            ) from e

    def send_to_multiple(
        self,
        connection_ids: list[str],
        payload: dict[str, Any],
    ) -> dict[str, bool]:
        """Send the same message to multiple connections.

        Returns a dict mapping connection_id -> success (True/False).
        Gone connections return False but don't raise.
        Other errors are logged but don't stop remaining sends.
        """
        data = json.dumps(payload).encode("utf-8")
        results = {}

        for conn_id in connection_ids:
            try:
                self._client.post_to_connection(
                    ConnectionId=conn_id,
                    Data=data,
                )
                results[conn_id] = True
            except ClientError as e:
                error_code = e.response.get("Error", {}).get("Code", "")
                if error_code == "GoneException":
                    results[conn_id] = False
                else:
                    logger.error("Failed to send to %s: %s", conn_id, e)
                    results[conn_id] = False

        return results
