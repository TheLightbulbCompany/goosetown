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
