"""queue_animation builds the right characters/animations request per type and
returns (directions, job_ids) — one batched call, all directions, no rotation fetch."""

from unittest.mock import MagicMock, patch

import pytest

from core.services.pixellab_service import PixelLabService


def _ok_resp(body):
    r = MagicMock()
    r.status_code = 200
    r.json = MagicMock(return_value=body)
    r.raise_for_status = MagicMock()
    return r


@pytest.mark.asyncio
async def test_queue_animation_template_walk():
    service = PixelLabService(api_key="stub")
    captured = {}

    async def fake_request(method, url, *, json=None, timeout=30.0, retries=4):
        captured["url"] = url
        captured["json"] = json
        return _ok_resp(
            {"directions": ["south", "west", "east", "north"], "background_job_ids": ["j1", "j2", "j3", "j4"]}
        )

    with patch.object(service, "_request", side_effect=fake_request):
        dirs, jobs = await service.queue_animation("char-1", "walk")

    assert captured["url"].endswith("/characters/animations")
    assert captured["json"]["mode"] == "template"
    assert captured["json"]["template_animation_id"] == "walk"
    assert captured["json"]["directions"] == ["south", "west", "east", "north"]
    assert "action_description" not in captured["json"]
    assert len(jobs) == 4


@pytest.mark.asyncio
async def test_queue_animation_v3_talking():
    service = PixelLabService(api_key="stub")
    captured = {}

    async def fake_request(method, url, *, json=None, timeout=30.0, retries=4):
        captured["json"] = json
        return _ok_resp(
            {"directions": ["south", "west", "east", "north"], "background_job_ids": ["j1", "j2", "j3", "j4"]}
        )

    with patch.object(service, "_request", side_effect=fake_request):
        dirs, jobs = await service.queue_animation("char-1", "talking")

    assert captured["json"]["mode"] == "v3"
    assert captured["json"]["action_description"]
    assert captured["json"]["enhance_prompt"] is True
    assert captured["json"]["frame_count"] >= 4
    assert "template_animation_id" not in captured["json"]
    assert len(jobs) == 4


@pytest.mark.asyncio
async def test_queue_animation_unknown_returns_empty():
    service = PixelLabService(api_key="stub")
    dirs, jobs = await service.queue_animation("char-1", "not_a_real_animation")
    assert dirs == [] and jobs == []
