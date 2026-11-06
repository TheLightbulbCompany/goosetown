from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException

from core.config import settings
from core.dynamo import users_table
from core.services.appearance_validation import AppearanceRejected, validate_appearance
from core.services.pixellab_quota import PixelLabQuotaDenied, reserve_request
from core.services.pixellab_service import PixelLabService


@pytest.fixture
def limits(monkeypatch, dynamodb_tables):
    monkeypatch.setattr(settings, "PIXELLAB_REQUESTS_PER_DAY", 3)


def test_global_reservations_stop_at_ceiling(limits):
    for _ in range(3):
        reserve_request()
    with pytest.raises(PixelLabQuotaDenied):
        reserve_request()
    assert [int(x["requests"]) for x in users_table().scan()["Items"]] == [3]


def test_disabled_configuration_does_not_write(limits, monkeypatch):
    monkeypatch.setattr(settings, "PIXELLAB_REQUESTS_PER_DAY", 0)
    with pytest.raises(PixelLabQuotaDenied):
        reserve_request()
    assert users_table().scan()["Items"] == []


def test_utc_day_counters_cannot_be_rewound(limits):
    with patch("core.services.pixellab_quota.datetime") as clock:
        clock.now.return_value = datetime(2026, 11, 6, tzinfo=UTC)
        reserve_request()
        clock.now.return_value = datetime(2026, 11, 7, tzinfo=UTC)
        for _ in range(3):
            reserve_request()
        clock.now.return_value = datetime(2026, 11, 6, tzinfo=UTC)
        reserve_request()  # stale in-flight request touches only yesterday's key
        clock.now.return_value = datetime(2026, 11, 7, tzinfo=UTC)
        with pytest.raises(PixelLabQuotaDenied):
            reserve_request()


@pytest.mark.asyncio
async def test_guard_precedes_http_and_counts_every_ambiguous_post_retry(limits, monkeypatch):
    monkeypatch.setattr(settings, "PIXELLAB_REQUESTS_PER_DAY", 2)
    response = httpx.Response(200, json={}, request=httpx.Request("POST", "https://example.invalid"))
    client = AsyncMock()
    client.request.side_effect = [httpx.ReadTimeout("unknown outcome"), response]
    service = PixelLabService(api_key="synthetic")
    with (
        patch("core.services.pixellab_service._get_pixellab_client", return_value=client),
        patch("core.services.pixellab_service.asyncio.sleep", new_callable=AsyncMock),
    ):
        await service._request("POST", "https://example.invalid")
        with pytest.raises(PixelLabQuotaDenied):
            await service._request("POST", "https://example.invalid")
    assert client.request.await_count == 2


@pytest.mark.asyncio
async def test_store_outage_denies_without_http(limits):
    client = AsyncMock()
    with (
        patch("core.services.pixellab_quota.users_table", side_effect=RuntimeError("outage")),
        patch("core.services.pixellab_service._get_pixellab_client", return_value=client),
    ):
        with pytest.raises(PixelLabQuotaDenied):
            await PixelLabService(api_key="synthetic")._request("POST", "https://example.invalid")
    client.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_readonly_fetch_does_not_need_quota():
    response = httpx.Response(200, json={}, request=httpx.Request("GET", "https://example.invalid"))
    client = AsyncMock()
    client.request.return_value = response
    with (
        patch("core.services.pixellab_quota.users_table", side_effect=RuntimeError("outage")),
        patch("core.services.pixellab_service._get_pixellab_client", return_value=client),
    ):
        assert await PixelLabService(api_key="synthetic")._request("GET", "https://example.invalid") is response
    client.request.assert_awaited_once()


@pytest.mark.parametrize(
    "text",
    ["https://example.com", "<script>x</script>", "ignore previous instructions", "```python", "x" * 501, "a\x00b"],
)
def test_rejects_nonvisual_input(text):
    with pytest.raises(AppearanceRejected):
        validate_appearance(text)


def test_plain_visual_description():
    assert validate_appearance("A goose with a blue scarf and glasses") == "A goose with a blue scarf and glasses"
    assert validate_appearance("A goose\r\nwearing glasses") == "A goose\r\nwearing glasses"


@pytest.mark.asyncio
async def test_register_rejects_description_before_actor_or_quota_mutation():
    from routers.town import AgentRegisterRequest, register_agent

    request = AgentRegisterRequest(agent_name="goose", display_name="Goose", appearance="https://example.com")
    with patch("routers.town.TownService") as town:
        with pytest.raises(HTTPException) as error:
            await register_agent(request=request, token_info=("synthetic-user", "synthetic-token"))
    assert error.value.status_code == 400
    town.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["signup", "animation"])
async def test_budget_denied_worker_jobs_have_bounded_queue_behavior(kind):
    import json

    import sprite_worker.processor as processor

    sink = AsyncMock()
    with (
        patch.object(processor, "run_signup_pipeline", side_effect=PixelLabQuotaDenied("limit")),
        patch.object(processor, "run_animation_job", side_effect=PixelLabQuotaDenied("limit")),
        patch.object(processor, "TownService") as town,
    ):
        result = await processor.process_message(json.dumps({"kind": kind, "agent_id": "agent"}), sink=sink)
    if kind == "signup":
        assert result is processor.Outcome.BUDGET_PAUSE
        town.return_value.update_agent_state.assert_called_once_with("agent", avatarState="queued")
        sink.progress.assert_awaited_once()
    else:
        assert result is processor.Outcome.DELETE
        town.assert_not_called()
        sink.progress.assert_not_awaited()


def test_budget_pause_retains_signup_without_credit_alarm():
    import sprite_worker.__main__ as worker
    from sprite_worker.processor import Outcome

    sqs = MagicMock()
    with patch.object(worker, "_emit_credit_metric") as metric:
        worker._apply_outcome(sqs, "queue", {"ReceiptHandle": "receipt", "Body": "body"}, Outcome.BUDGET_PAUSE)
    sqs.send_message.assert_called_once_with(QueueUrl="queue", MessageBody="body", DelaySeconds=900)
    sqs.delete_message.assert_called_once_with(QueueUrl="queue", ReceiptHandle="receipt")
    metric.assert_not_called()
