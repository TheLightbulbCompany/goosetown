"""The per-user active-agent quota holds under concurrent requests.

``activeAgents`` on the users row moves in the same transaction as the agent
row's ``isActive`` flip, so N concurrent activations can't overshoot
AGENT_QUOTA and deactivations release each slot exactly once.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError

from core import dynamo
from core.quota import QuotaExceeded
from core.services.town_service import TownService
from core.services.user_service import AGENT_QUOTA, CONFLICT_ATTEMPTS

USER = "user-1"
pytestmark = pytest.mark.usefixtures("atomic_moto_requests")


def _counter() -> int:
    return int(dynamo.users_table().get_item(Key={"userId": USER})["Item"]["activeAgents"])


def _active_count() -> int:
    return len(TownService().get_active_agents_for_user(USER))


def _legacy_agent(name: str, active: bool) -> str:
    """An agent row written by pre-counter code (no activeAgents on the user)."""
    agent_id = f"id-{name}"
    dynamo.agents_table().put_item(
        Item={
            "id": agent_id,
            "userId": USER,
            "agentName": name,
            "isActive": "true" if active else "false",
            "joinedAt": "2026-01-01T00:00:00+00:00",
        }
    )
    return agent_id


def _race(fn, args) -> list:
    """Run fn(arg) for every arg at the same instant; return True/False per call."""
    barrier = threading.Barrier(len(args))

    def attempt(arg):
        barrier.wait()
        try:
            return fn(arg) is not False
        except QuotaExceeded:
            return False

    with ThreadPoolExecutor(max_workers=len(args)) as pool:
        return list(pool.map(attempt, args))


def test_concurrent_registrations_stop_exactly_at_quota(dynamodb_tables):
    results = _race(lambda i: TownService().opt_in(USER, f"agent-{i}", f"Agent {i}"), range(AGENT_QUOTA * 3))

    assert sum(results) == AGENT_QUOTA
    assert _active_count() == AGENT_QUOTA
    assert _counter() == AGENT_QUOTA


def test_concurrent_reactivations_stop_exactly_at_quota(dynamodb_tables):
    ids = [_legacy_agent(f"agent-{i}", active=False) for i in range(AGENT_QUOTA + 3)]

    results = _race(lambda agent_id: TownService().set_agent_active(agent_id, True), ids)

    assert sum(results) == AGENT_QUOTA
    assert _active_count() == AGENT_QUOTA
    assert _counter() == AGENT_QUOTA


def test_same_agent_reactivated_concurrently_takes_one_slot(dynamodb_tables):
    agent_id = _legacy_agent("solo", active=False)

    _race(lambda _: TownService().set_agent_active(agent_id, True), range(8))

    assert _counter() == 1


def test_same_agent_deactivated_concurrently_releases_one_slot(dynamodb_tables):
    svc = TownService()
    agents = [svc.opt_in(USER, f"agent-{i}", f"Agent {i}") for i in range(3)]

    _race(lambda _: TownService().set_agent_active(agents[0]["id"], False), range(8))

    assert _counter() == 2
    assert _active_count() == 2


def test_opt_out_frees_a_slot_and_opt_in_reactivation_is_gated(dynamodb_tables):
    svc = TownService()
    for i in range(AGENT_QUOTA):
        svc.opt_in(USER, f"agent-{i}", f"Agent {i}")

    assert svc.opt_out(USER, "agent-0") is True
    assert svc.opt_out(USER, "agent-0") is True  # repeat opt-out must not release twice
    assert _counter() == AGENT_QUOTA - 1

    svc.opt_in(USER, "agent-new", "New")  # takes the freed slot
    assert _counter() == AGENT_QUOTA

    # Re-registering the opted-out agent (the reactivation path) is now over quota.
    with pytest.raises(QuotaExceeded) as exc:
        svc.opt_in(USER, "agent-0", "Agent 0")
    assert exc.value.current == AGENT_QUOTA
    assert svc._get_town_agent(USER, "agent-0")["isActive"] == "false"


def test_legacy_user_counter_is_seeded_from_existing_active_agents(dynamodb_tables):
    for i in range(AGENT_QUOTA):
        _legacy_agent(f"old-{i}", active=True)
    _legacy_agent("old-inactive", active=False)

    with pytest.raises(QuotaExceeded):
        TownService().opt_in(USER, "agent-new", "New")
    assert _counter() == AGENT_QUOTA

    TownService().set_agent_active("id-old-0", False)
    TownService().opt_in(USER, "agent-new", "New")
    assert _counter() == AGENT_QUOTA
    assert _active_count() == AGENT_QUOTA


def test_deactivate_never_takes_counter_negative(dynamodb_tables):
    svc = TownService()
    agent = svc.opt_in(USER, "agent-a", "Agent A")
    # Simulate drift: the counter says 0 while the row is still active.
    dynamo.users_table().update_item(
        Key={"userId": USER}, UpdateExpression="SET activeAgents = :z", ExpressionAttributeValues={":z": 0}
    )

    assert svc.set_agent_active(agent["id"], False) is True
    assert svc.get_town_agent_by_id(agent["id"])["isActive"] == "false"
    assert _counter() == 0


# --- Real-DynamoDB contention (moto never raises it, so it's injected) ---


def _conflict() -> ClientError:
    return ClientError(
        {
            "Error": {"Code": "TransactionCanceledException", "Message": "conflict"},
            "CancellationReasons": [{"Code": "None"}, {"Code": "TransactionConflict"}, {"Code": "None"}],
        },
        "TransactWriteItems",
    )


def _conflicting_transactions(times: int):
    """Fail the next ``times`` TransactWriteItems with TransactionConflict, then pass through."""
    client = dynamo.agents_table().meta.client
    real = client.transact_write_items
    calls = {"n": 0}

    def fake(**kwargs):
        calls["n"] += 1
        if calls["n"] <= times:
            raise _conflict()
        return real(**kwargs)

    return patch.object(client, "transact_write_items", side_effect=fake), calls


def test_transient_conflict_is_retried(dynamodb_tables):
    agent_id = _legacy_agent("solo", active=False)
    patcher, calls = _conflicting_transactions(CONFLICT_ATTEMPTS - 1)
    with patcher:
        assert TownService().set_agent_active(agent_id, True) is True
    assert calls["n"] == CONFLICT_ATTEMPTS
    assert _counter() == 1


def test_persistent_conflict_maps_activation_to_quota_exceeded(dynamodb_tables):
    agent_id = _legacy_agent("solo", active=False)
    patcher, _ = _conflicting_transactions(99)
    with patcher:
        with pytest.raises(QuotaExceeded):
            TownService().set_agent_active(agent_id, True)
        with pytest.raises(QuotaExceeded):
            TownService().opt_in(USER, "agent-new", "New")
    assert _counter() == 0


def test_persistent_conflict_on_deactivation_reraises(dynamodb_tables):
    agent = TownService().opt_in(USER, "agent-a", "Agent A")
    patcher, _ = _conflicting_transactions(99)
    with patcher, pytest.raises(ClientError):
        TownService().set_agent_active(agent["id"], False)
    assert _counter() == 1  # nothing half-applied
