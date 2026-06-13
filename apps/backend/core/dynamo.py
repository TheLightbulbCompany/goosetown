"""DynamoDB client and table operations for GooseTown."""

import boto3

from core.config import settings

_client = None


def get_client():
    global _client
    if _client is None:
        _client = boto3.resource("dynamodb", region_name=settings.AWS_REGION)
    return _client


def table_name(name: str) -> str:
    return f"{settings.DYNAMODB_TABLE_PREFIX}-{name}"


def agents_table():
    return get_client().Table(table_name("agents"))


def instances_table():
    return get_client().Table(table_name("instances"))


def relationships_table():
    return get_client().Table(table_name("relationships"))


def conversations_table():
    return get_client().Table(table_name("conversations"))


def connections_table():
    return get_client().Table(table_name("connections"))


def owner_chats_table():
    return get_client().Table(table_name("owner-chats"))


def users_table():
    return get_client().Table(table_name("users"))


def completed_goals_table():
    """DynamoDB table handle for completed_goals."""
    return get_client().Table(table_name("completed-goals"))


def worldgm_arcs_table():
    """DynamoDB table handle for worldgm-arcs (WorldGM narrative arc state)."""
    return get_client().Table(table_name("worldgm-arcs"))


def world_events_table():
    """DynamoDB table handle for world-events (durable event log for Tier-1 feed)."""
    return get_client().Table(table_name("world-events"))


def jobs_table():
    """DynamoDB table handle for Exchange jobs."""
    return get_client().Table(table_name("jobs"))


def ledger_table():
    """DynamoDB table handle for the Exchange crumb ledger (append-only)."""
    return get_client().Table(table_name("ledger"))


def provider_subscriptions_table():
    """DynamoDB table handle for the ProviderDirectory membership projection (ADR-0008).

    PK: category (S) — AEX category slug (or wildcard prefix)
    SK: providerSort (S) — f"{trustScore:08.4f}#{providerId}" enables trust-desc query
        via ScanIndexForward=False without a scan.
    GSI providerId-index (PK: providerId) — find-and-delete all rows for a provider on
        register/unregister without a full table scan.
    """
    return get_client().Table(table_name("provider-subscriptions"))
