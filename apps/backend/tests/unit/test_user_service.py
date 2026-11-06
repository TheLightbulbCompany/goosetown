"""TDD: UserService manages per-user state for quotas and token revocation."""

import time

import pytest
from moto import mock_aws

from core.quota import QuotaExceeded


@pytest.fixture()
def users_table_resource():
    """Mock DynamoDB with a goosetown-users table.

    Mirrors the pattern in tests/conftest.py::dynamodb_tables — resets
    core.dynamo._client inside the mock_aws context so users_table()
    picks up the moto endpoint regardless of test ordering.  The table
    name follows core.dynamo.table_name("users") == "goosetown-users"
    (settings.DYNAMODB_TABLE_PREFIX is frozen to "goosetown" by conftest).
    """
    with mock_aws():
        import boto3

        import core.dynamo as dynamo_mod

        # Reset cached singleton so next users_table() call creates a
        # fresh client inside the moto context.
        dynamo_mod._client = None

        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName="goosetown-users",
            KeySchema=[{"AttributeName": "userId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "userId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield resource.Table("goosetown-users")

        # Restore the singleton so subsequent tests don't accidentally
        # inherit the expired mock client.
        dynamo_mod._client = None


def test_get_or_create_lazy_creates_user(users_table_resource):
    from core.services.user_service import UserService

    svc = UserService()
    user = svc.get_or_create("user_abc")
    assert user["userId"] == "user_abc"
    assert "createdAt" in user


def test_get_or_create_returns_existing_user(users_table_resource):
    from core.services.user_service import UserService

    svc = UserService()
    first = svc.get_or_create("user_abc")
    second = svc.get_or_create("user_abc")
    assert first["createdAt"] == second["createdAt"]


def test_revoke_tokens_bumps_timestamp(users_table_resource):
    from core.services.user_service import UserService

    svc = UserService()
    svc.get_or_create("user_abc")
    svc.revoke_tokens("user_abc")
    user = svc.get_or_create("user_abc")
    assert "tokenRevokedAt" in user
    assert int(user["tokenRevokedAt"]) > 0


def test_is_token_revoked_returns_true_for_pre_revoke_iat(users_table_resource):
    from core.services.user_service import UserService

    svc = UserService()
    svc.get_or_create("user_abc")
    iat_before = int(time.time())
    svc.revoke_tokens("user_abc")
    assert svc.is_token_revoked("user_abc", iat_before * 1000) is True


def test_is_token_revoked_returns_false_for_post_revoke_iat(users_table_resource):
    from core.services.user_service import UserService

    svc = UserService()
    svc.get_or_create("user_abc")
    svc.revoke_tokens("user_abc")
    iat_after = int(time.time()) + 5
    assert svc.is_token_revoked("user_abc", iat_after * 1000) is False


def test_is_token_revoked_false_when_never_revoked(users_table_resource):
    from core.services.user_service import UserService

    svc = UserService()
    svc.get_or_create("user_abc")
    assert svc.is_token_revoked("user_abc", int(time.time()) * 1000) is False


def test_consume_sprite_quota_counts_today(users_table_resource):
    from core.services.user_service import UserService, utc_day

    svc = UserService()
    svc.consume_sprite_quota("user_abc")
    svc.consume_sprite_quota("user_abc")
    user = svc.get_or_create("user_abc")
    assert user["spriteDay"] == utc_day()
    assert int(user["spriteCount"]) == 2
    assert svc.sprites_used_today("user_abc") == 2


def test_consume_sprite_quota_raises_at_limit(users_table_resource):
    from core.services.user_service import SPRITE_QUOTA_DAILY, UserService

    svc = UserService()
    svc.get_or_create("user_abc")
    for _ in range(SPRITE_QUOTA_DAILY):
        svc.consume_sprite_quota("user_abc")
    with pytest.raises(QuotaExceeded) as exc:
        svc.consume_sprite_quota("user_abc")
    assert exc.value.kind == "sprite_generation"
    assert exc.value.limit == SPRITE_QUOTA_DAILY
    assert exc.value.current == SPRITE_QUOTA_DAILY


def test_sprite_counter_resets_on_new_utc_day(users_table_resource):
    from core.services.user_service import SPRITE_QUOTA_DAILY, UserService

    svc = UserService()
    svc.get_or_create("user_abc")
    users_table_resource.update_item(
        Key={"userId": "user_abc"},
        UpdateExpression="SET spriteDay = :d, spriteCount = :n",
        ExpressionAttributeValues={":d": "2000-01-01", ":n": SPRITE_QUOTA_DAILY},
    )
    svc.consume_sprite_quota("user_abc")
    assert svc.sprites_used_today("user_abc") == 1


def test_legacy_sprite_list_counts_only_today(users_table_resource):
    """Rows from the old sliding-window code: today's entries carry over once, older ones don't."""
    from core.services.user_service import SPRITE_QUOTA_DAILY, SPRITE_QUOTA_WINDOW_MS, UserService

    svc = UserService()
    svc.get_or_create("user_abc")
    now_ms = int(time.time() * 1000)
    users_table_resource.update_item(
        Key={"userId": "user_abc"},
        UpdateExpression="SET sprite_jobs_24h = :jobs",
        ExpressionAttributeValues={
            ":jobs": [{"timestamp_ms": now_ms}] * 3 + [{"timestamp_ms": now_ms - SPRITE_QUOTA_WINDOW_MS - 1000}] * 5
        },
    )
    svc.consume_sprite_quota("user_abc")
    assert svc.sprites_used_today("user_abc") == 4
    assert "sprite_jobs_24h" not in svc.get_or_create("user_abc")

    # A legacy user already at today's limit stays blocked.
    users_table_resource.put_item(
        Item={"userId": "user_full", "sprite_jobs_24h": [{"timestamp_ms": now_ms}] * SPRITE_QUOTA_DAILY}
    )
    with pytest.raises(QuotaExceeded):
        svc.consume_sprite_quota("user_full")


def test_concurrent_sprite_requests_never_exceed_daily_limit(users_table_resource, atomic_moto_requests):
    """Fire 3x the limit at once: exactly SPRITE_QUOTA_DAILY succeed."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from core.services.user_service import SPRITE_QUOTA_DAILY, UserService

    n = SPRITE_QUOTA_DAILY * 3
    barrier = threading.Barrier(n)

    def attempt(_):
        barrier.wait()
        try:
            UserService().consume_sprite_quota("user_abc")
            return True
        except QuotaExceeded:
            return False

    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(attempt, range(n)))

    assert sum(results) == SPRITE_QUOTA_DAILY
    assert UserService().sprites_used_today("user_abc") == SPRITE_QUOTA_DAILY


def test_quota_constants():
    from core.services.user_service import AGENT_QUOTA, SPRITE_QUOTA_DAILY, SPRITE_QUOTA_WINDOW_MS

    assert AGENT_QUOTA == 5
    assert SPRITE_QUOTA_DAILY == 10
    assert SPRITE_QUOTA_WINDOW_MS == 24 * 60 * 60 * 1000
