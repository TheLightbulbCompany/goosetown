"""Tests for Management API client (WebSocket push to API Gateway)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from core.services.management_api_client import ManagementApiClient, ManagementApiClientError


class TestInit:
    def test_constructs_endpoint_from_api_id(self):
        from core.config import settings

        old_api_id = settings.WS_API_ID
        old_stage = settings.WS_STAGE
        old_region = settings.AWS_REGION
        try:
            settings.WS_API_ID = "abc123"
            settings.WS_STAGE = "dev"
            settings.AWS_REGION = "us-east-1"
            client = ManagementApiClient()
            assert client.endpoint_url == "https://abc123.execute-api.us-east-1.amazonaws.com/dev"
        finally:
            settings.WS_API_ID = old_api_id
            settings.WS_STAGE = old_stage
            settings.AWS_REGION = old_region

    def test_raises_when_no_api_id(self):
        from core.config import settings

        old_val = settings.WS_API_ID
        try:
            settings.WS_API_ID = ""
            with pytest.raises(ManagementApiClientError, match="WS_API_ID"):
                ManagementApiClient()
        finally:
            settings.WS_API_ID = old_val

    def test_accepts_explicit_endpoint(self):
        client = ManagementApiClient(endpoint_url="https://custom.example.com/stage")
        assert client.endpoint_url == "https://custom.example.com/stage"


class TestSendMessage:
    def _make_client(self):
        client = ManagementApiClient(endpoint_url="https://fake.example.com/dev")
        client._client = MagicMock()
        return client

    def test_send_message_success(self):
        client = self._make_client()
        client._client.post_to_connection = MagicMock(return_value={})

        result = client.send_message("conn-123", {"type": "test", "data": "hello"})
        assert result is True

        client._client.post_to_connection.assert_called_once()
        call_kwargs = client._client.post_to_connection.call_args[1]
        assert call_kwargs["ConnectionId"] == "conn-123"
        payload = json.loads(call_kwargs["Data"].decode("utf-8"))
        assert payload == {"type": "test", "data": "hello"}

    def test_send_message_gone_returns_false(self):
        from botocore.exceptions import ClientError

        client = self._make_client()
        client._client.post_to_connection = MagicMock(
            side_effect=ClientError(
                {"Error": {"Code": "GoneException", "Message": "Connection gone"}},
                "PostToConnection",
            )
        )

        result = client.send_message("conn-gone", {"type": "test"})
        assert result is False

    def test_send_message_other_error_raises(self):
        from botocore.exceptions import ClientError

        client = self._make_client()
        client._client.post_to_connection = MagicMock(
            side_effect=ClientError(
                {"Error": {"Code": "InternalError", "Message": "Something broke"}},
                "PostToConnection",
            )
        )

        with pytest.raises(ManagementApiClientError, match="Something broke"):
            client.send_message("conn-err", {"type": "test"})


class TestCloseConnection:
    def _make_client(self):
        client = ManagementApiClient(endpoint_url="https://fake.example.com/dev")
        client._client = MagicMock()
        return client

    def test_close_connection_success(self):
        client = self._make_client()
        client._client.delete_connection = MagicMock(return_value={})
        client.close_connection("conn-123")
        client._client.delete_connection.assert_called_once_with(ConnectionId="conn-123")

    def test_close_already_gone_is_safe(self):
        from botocore.exceptions import ClientError

        client = self._make_client()
        client._client.delete_connection = MagicMock(
            side_effect=ClientError(
                {"Error": {"Code": "GoneException", "Message": "Already gone"}},
                "DeleteConnection",
            )
        )
        # Should not raise
        client.close_connection("conn-gone")

    def test_close_other_error_raises(self):
        from botocore.exceptions import ClientError

        client = self._make_client()
        client._client.delete_connection = MagicMock(
            side_effect=ClientError(
                {"Error": {"Code": "InternalError", "Message": "Something broke"}},
                "DeleteConnection",
            )
        )
        with pytest.raises(ManagementApiClientError):
            client.close_connection("conn-err")


class TestSendToMultiple:
    def _make_client(self):
        client = ManagementApiClient(endpoint_url="https://fake.example.com/dev")
        client._client = MagicMock()
        return client

    def test_send_to_multiple_returns_results(self):
        client = self._make_client()
        client._client.post_to_connection = MagicMock(return_value={})

        results = client.send_to_multiple(
            ["conn-1", "conn-2", "conn-3"],
            {"type": "world_update"},
        )
        assert results == {"conn-1": True, "conn-2": True, "conn-3": True}
        assert client._client.post_to_connection.call_count == 3

    def test_send_to_multiple_handles_gone_connections(self):
        from botocore.exceptions import ClientError

        client = self._make_client()

        def side_effect(**kwargs):
            if kwargs["ConnectionId"] == "conn-2":
                raise ClientError(
                    {"Error": {"Code": "GoneException", "Message": "Gone"}},
                    "PostToConnection",
                )
            return {}

        client._client.post_to_connection = MagicMock(side_effect=side_effect)

        results = client.send_to_multiple(
            ["conn-1", "conn-2", "conn-3"],
            {"type": "world_update"},
        )
        assert results == {"conn-1": True, "conn-2": False, "conn-3": True}
