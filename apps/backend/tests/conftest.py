"""Shared test fixtures for GooseTown backend tests."""

import os

import boto3
import pytest
from moto import mock_aws

# Force DynamoDB table prefix for tests
os.environ["DYNAMODB_TABLE_PREFIX"] = "goosetown"
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"
os.environ["TOWN_TOKEN_SECRET"] = "test-secret"
os.environ["ENVIRONMENT"] = "dev"


def _create_tables(dynamodb):
    """Create all DynamoDB tables with GSIs for tests."""
    dynamodb.create_table(
        TableName="goosetown-agents",
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "id", "AttributeType": "S"},
            {"AttributeName": "userId", "AttributeType": "S"},
            {"AttributeName": "agentName", "AttributeType": "S"},
            {"AttributeName": "instanceId", "AttributeType": "S"},
            {"AttributeName": "isActive", "AttributeType": "S"},
            {"AttributeName": "joinedAt", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "userId-agentName-index",
                "KeySchema": [
                    {"AttributeName": "userId", "KeyType": "HASH"},
                    {"AttributeName": "agentName", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "instanceId-index",
                "KeySchema": [{"AttributeName": "instanceId", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "isActive-joinedAt-index",
                "KeySchema": [
                    {"AttributeName": "isActive", "KeyType": "HASH"},
                    {"AttributeName": "joinedAt", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    dynamodb.create_table(
        TableName="goosetown-instances",
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "id", "AttributeType": "S"},
            {"AttributeName": "userId", "AttributeType": "S"},
            {"AttributeName": "townToken", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "userId-index",
                "KeySchema": [{"AttributeName": "userId", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "townToken-index",
                "KeySchema": [{"AttributeName": "townToken", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    dynamodb.create_table(
        TableName="goosetown-relationships",
        KeySchema=[
            {"AttributeName": "agentAId", "KeyType": "HASH"},
            {"AttributeName": "agentBId", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "agentAId", "AttributeType": "S"},
            {"AttributeName": "agentBId", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    dynamodb.create_table(
        TableName="goosetown-conversations",
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "id", "AttributeType": "S"},
            {"AttributeName": "status", "AttributeType": "S"},
            {"AttributeName": "startedAt", "AttributeType": "S"},
            {"AttributeName": "participantAId", "AttributeType": "S"},
            {"AttributeName": "endedAt", "AttributeType": "S"},
            {"AttributeName": "updatedAt", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "status-startedAt-index",
                "KeySchema": [
                    {"AttributeName": "status", "KeyType": "HASH"},
                    {"AttributeName": "startedAt", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "participantA-endedAt-index",
                "KeySchema": [
                    {"AttributeName": "participantAId", "KeyType": "HASH"},
                    {"AttributeName": "endedAt", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "status-updatedAt-index",
                "KeySchema": [
                    {"AttributeName": "status", "KeyType": "HASH"},
                    {"AttributeName": "updatedAt", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def dynamodb_tables():
    """Create mock DynamoDB tables for testing."""
    with mock_aws():
        # Reset the cached client so dynamo module picks up mock
        import core.dynamo as dynamo_mod

        dynamo_mod._client = None

        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        _create_tables(dynamodb)

        yield dynamodb

        dynamo_mod._client = None
