from unittest.mock import AsyncMock, patch

import httpx
import pytest

from core.config import settings
from core.dynamo import users_table
from core.services.appearance_validation import AppearanceRejected, validate_appearance
from core.services.pixellab_quota import PixelLabQuotaDenied, reserve_request
from core.services.pixellab_service import PixelLabService


@pytest.fixture
def limits(monkeypatch, dynamodb_tables):
    monkeypatch.setattr(settings, "PIXELLAB_REQUESTS_PER_DAY", 3)
    monkeypatch.setattr(settings, "PIXELLAB_REQUESTS_PER_OWNER_PER_DAY", 2)


def test_global_and_owner_reservations_are_atomic(limits):
    reserve_request("alice")
    reserve_request("alice")
    with pytest.raises(PixelLabQuotaDenied):
        reserve_request("alice")
    reserve_request("bob")  # failed owner reservation did not consume global slot
    with pytest.raises(PixelLabQuotaDenied):
        reserve_request("carol")
    assert sum(int(x["requests"]) for x in users_table().scan()["Items"] if "requests" in x) == 6


def test_missing_owner_and_disabled_config_deny_without_writes(limits, monkeypatch):
    with pytest.raises(PixelLabQuotaDenied):
        reserve_request(None)
    monkeypatch.setattr(settings, "PIXELLAB_REQUESTS_PER_DAY", 0)
    with pytest.raises(PixelLabQuotaDenied):
        reserve_request("alice")
    assert users_table().scan()["Items"] == []


@pytest.mark.asyncio
async def test_guard_is_before_http_and_every_ambiguous_post_retry_is_counted(limits):
    response = httpx.Response(200, json={}, request=httpx.Request("POST", "https://example.invalid"))
    client = AsyncMock()
    client.request.side_effect = [httpx.ReadTimeout("unknown outcome"), response]
    service = PixelLabService(api_key="synthetic", owner_id="alice")
    with (
        patch("core.services.pixellab_service._get_pixellab_client", return_value=client),
        patch("core.services.pixellab_service.asyncio.sleep", new_callable=AsyncMock),
    ):
        await service._request("POST", "https://example.invalid")
        assert client.request.await_count == 2
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
            await PixelLabService(api_key="synthetic", owner_id="alice")._request("POST", "https://example.invalid")
    client.request.assert_not_awaited()


@pytest.mark.parametrize(
    "text",
    ["https://example.com", "<script>x</script>", "ignore previous instructions", "```python", "x" * 501, "a\x00b"],
)
def test_rejects_nonvisual_input(text):
    with pytest.raises(AppearanceRejected):
        validate_appearance(text)


def test_plain_visual_description():
    assert validate_appearance("A goose with a blue scarf and glasses") == "A goose with a blue scarf and glasses"


@pytest.mark.asyncio
async def test_readonly_fetch_does_not_need_quota_or_owner():
    response = httpx.Response(200, json={}, request=httpx.Request("GET", "https://example.invalid"))
    client = AsyncMock()
    client.request.return_value = response
    with (
        patch("core.services.pixellab_quota.users_table", side_effect=RuntimeError("outage")),
        patch("core.services.pixellab_service._get_pixellab_client", return_value=client),
    ):
        assert await PixelLabService(api_key="synthetic")._request("GET", "https://example.invalid") is response
    client.request.assert_awaited_once()


@pytest.mark.asyncio
async def test_worker_keeps_queued_avatar_without_charging_or_error_page():
    import sprite_worker.processor as processor

    sink = AsyncMock()
    with (
        patch.object(processor, "run_signup_pipeline", side_effect=PixelLabQuotaDenied("limit")),
        patch.object(processor, "TownService") as town,
    ):
        result = await processor.process_message('{"kind":"signup","agent_id":"agent"}', sink=sink)
    assert result is processor.Outcome.BUDGET_PAUSE
    town.return_value.update_agent_state.assert_called_once_with("agent", avatarState="queued")
    sink.progress.assert_awaited_once()


def test_budget_pause_retains_job_without_credit_alarm():
    from unittest.mock import MagicMock

    import sprite_worker.__main__ as worker
    from sprite_worker.processor import Outcome

    sqs = MagicMock()
    with patch.object(worker, "_emit_credit_metric") as metric:
        worker._apply_outcome(sqs, "queue", {"ReceiptHandle": "receipt", "Body": "body"}, Outcome.BUDGET_PAUSE)
    sqs.send_message.assert_called_once_with(QueueUrl="queue", MessageBody="body", DelaySeconds=900)
    sqs.delete_message.assert_called_once_with(QueueUrl="queue", ReceiptHandle="receipt")
    metric.assert_not_called()
