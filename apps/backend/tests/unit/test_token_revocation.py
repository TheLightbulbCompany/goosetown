"""TDD: verify_town_token rejects tokens with iat < user.tokenRevokedAt."""

import time

import pytest
from moto import mock_aws


@pytest.fixture()
def users_table_resource():
    """Mock DynamoDB with a goosetown-users table.

    Mirrors the pattern in test_user_service.py — resets core.dynamo._client
    inside the mock_aws context so users_table() picks up the moto endpoint
    regardless of test ordering.
    """
    with mock_aws():
        import boto3

        import core.dynamo as dynamo_mod

        dynamo_mod._client = None

        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName="goosetown-users",
            KeySchema=[{"AttributeName": "userId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "userId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield resource.Table("goosetown-users")

        dynamo_mod._client = None


def test_verify_passes_when_user_has_never_revoked(users_table_resource):
    from core.services.user_service import UserService
    from core.town_token import sign_town_token, verify_town_token

    svc = UserService()
    svc.get_or_create("user_abc")

    token = sign_town_token(user_id="user_abc", instance_id="inst_1")
    payload = verify_town_token(token)
    assert payload is not None
    assert payload["uid"] == "user_abc"


def test_verify_rejects_token_issued_before_revoke(users_table_resource):
    from core.services.user_service import UserService
    from core.town_token import sign_town_token, verify_town_token

    svc = UserService()
    svc.get_or_create("user_abc")

    iat_old = int(time.time()) - 60
    token = sign_town_token(user_id="user_abc", instance_id="inst_1", issued_at=iat_old)

    svc.revoke_tokens("user_abc")

    result = verify_town_token(token)
    assert result is None, "verify_town_token must return None for tokens issued before revoke"


def test_verify_passes_for_token_issued_after_revoke(users_table_resource):
    from core.services.user_service import UserService
    from core.town_token import sign_town_token, verify_town_token

    svc = UserService()
    svc.get_or_create("user_abc")

    svc.revoke_tokens("user_abc")

    # Issue a fresh token 2 seconds after the revoke timestamp to be safe
    iat_after = int(time.time()) + 2
    token = sign_town_token(user_id="user_abc", instance_id="inst_1", issued_at=iat_after)
    from unittest.mock import patch

    with patch("core.town_token.time.time", return_value=iat_after):
        payload = verify_town_token(token)
    assert payload is not None
    assert payload["uid"] == "user_abc"
