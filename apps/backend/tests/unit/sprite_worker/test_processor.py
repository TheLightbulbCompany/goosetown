"""Tests for sprite_worker.processor."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sprite_worker.processor as processor
from sprite_worker.processor import Outcome

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_active():
    """Ensure _active is clean before and after every test."""
    processor._active.clear()
    yield
    processor._active.clear()


def _make_sink():
    sink = MagicMock()
    sink.progress = AsyncMock()
    sink.sprite_updated = AsyncMock()
    return sink


# ---------------------------------------------------------------------------
# Malformed / invalid messages -> DELETE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_json_returns_delete():
    outcome = await processor.process_message("not json {{{", sink=_make_sink())
    assert outcome is Outcome.DELETE


@pytest.mark.asyncio
async def test_non_dict_json_returns_delete():
    """JSON that parses but isn't a dict (e.g. the number 42) -> DELETE."""
    outcome = await processor.process_message("42", sink=_make_sink())
    assert outcome is Outcome.DELETE


@pytest.mark.asyncio
async def test_unknown_kind_returns_delete():
    import json

    body = json.dumps({"kind": "unknown", "agent_id": "abc-123"})
    outcome = await processor.process_message(body, sink=_make_sink())
    assert outcome is Outcome.DELETE


@pytest.mark.asyncio
async def test_missing_agent_id_returns_delete():
    import json

    body = json.dumps({"kind": "signup"})
    outcome = await processor.process_message(body, sink=_make_sink())
    assert outcome is Outcome.DELETE


@pytest.mark.asyncio
async def test_missing_kind_returns_delete():
    import json

    body = json.dumps({"agent_id": "abc-123"})
    outcome = await processor.process_message(body, sink=_make_sink())
    assert outcome is Outcome.DELETE


# ---------------------------------------------------------------------------
# Signup success -> DELETE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signup_success_returns_delete():
    import json

    body = json.dumps({"kind": "signup", "agent_id": "agent-uuid-001"})
    sink = _make_sink()

    with patch("sprite_worker.processor.run_signup_pipeline", new_callable=AsyncMock) as mock_pipeline:
        outcome = await processor.process_message(body, sink=sink)

    assert outcome is Outcome.DELETE
    mock_pipeline.assert_awaited_once_with(
        "agent-uuid-001",
        sink=sink,
        fallback_display_name="",
        fallback_appearance="",
    )


# ---------------------------------------------------------------------------
# Signup message fields forwarded as fallback kwargs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signup_message_fields_forwarded_as_fallback_kwargs():
    """display_name and appearance in the SQS message body must be forwarded
    to run_signup_pipeline as fallback_display_name / fallback_appearance."""
    import json

    body = json.dumps(
        {
            "kind": "signup",
            "agent_id": "agent-uuid-010",
            "display_name": "Goose McAgent",
            "appearance": "a pixel-art goose in a business suit",
        }
    )
    sink = _make_sink()

    with patch("sprite_worker.processor.run_signup_pipeline", new_callable=AsyncMock) as mock_pipeline:
        outcome = await processor.process_message(body, sink=sink)

    assert outcome is Outcome.DELETE
    mock_pipeline.assert_awaited_once_with(
        "agent-uuid-010",
        sink=sink,
        fallback_display_name="Goose McAgent",
        fallback_appearance="a pixel-art goose in a business suit",
    )


# ---------------------------------------------------------------------------
# Transient failure -> RELEASE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_runtime_error_returns_release():
    import json

    body = json.dumps({"kind": "signup", "agent_id": "agent-uuid-002"})
    sink = _make_sink()

    with patch(
        "sprite_worker.processor.run_signup_pipeline",
        new_callable=AsyncMock,
        side_effect=RuntimeError("PixelLab timed out"),
    ):
        outcome = await processor.process_message(body, sink=sink)

    assert outcome is Outcome.RELEASE


# ---------------------------------------------------------------------------
# Credit exhaustion -> CREDIT_PAUSE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_credit_error_returns_credit_pause():
    import json

    from core.services.pixellab_service import PixelLabCreditError

    body = json.dumps({"kind": "signup", "agent_id": "agent-uuid-003"})
    sink = _make_sink()

    with patch(
        "sprite_worker.processor.run_signup_pipeline",
        new_callable=AsyncMock,
        side_effect=PixelLabCreditError("402 out of credits"),
    ):
        outcome = await processor.process_message(body, sink=sink)

    assert outcome is Outcome.CREDIT_PAUSE


# ---------------------------------------------------------------------------
# Duplicate in-flight -> RELEASE without calling pipeline, guard intact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_in_flight_returns_release_without_pipeline():
    """When another coroutine holds the _active guard for the same agent,
    a second call should RELEASE without invoking the pipeline at all, AND
    the original guard entry must remain in _active after the duplicate returns."""
    import json

    agent_id = "agent-uuid-004"
    processor._active.add(agent_id)  # simulate owner in flight

    body = json.dumps({"kind": "signup", "agent_id": agent_id})
    sink = _make_sink()

    with patch("sprite_worker.processor.run_signup_pipeline", new_callable=AsyncMock) as mock_pipeline:
        outcome = await processor.process_message(body, sink=sink)

    assert outcome is Outcome.RELEASE
    mock_pipeline.assert_not_awaited()
    # Owner's guard must still be there — the duplicate must not discard it.
    assert agent_id in processor._active


# ---------------------------------------------------------------------------
# Animation kind routes to run_animation_job with anim_name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_animation_kind_calls_run_animation_job():
    import json

    agent_id = "agent-uuid-005"
    body = json.dumps({"kind": "animation", "agent_id": agent_id, "anim_name": "idle"})
    sink = _make_sink()

    with patch("sprite_worker.processor.run_animation_job", new_callable=AsyncMock) as mock_anim:
        outcome = await processor.process_message(body, sink=sink)

    assert outcome is Outcome.DELETE
    mock_anim.assert_awaited_once_with(agent_id, "idle", sink=sink)


@pytest.mark.asyncio
async def test_animation_kind_missing_anim_name_uses_empty_string():
    """anim_name defaults to '' when absent from the message."""
    import json

    agent_id = "agent-uuid-006"
    body = json.dumps({"kind": "animation", "agent_id": agent_id})
    sink = _make_sink()

    with patch("sprite_worker.processor.run_animation_job", new_callable=AsyncMock) as mock_anim:
        outcome = await processor.process_message(body, sink=sink)

    assert outcome is Outcome.DELETE
    mock_anim.assert_awaited_once_with(agent_id, "", sink=sink)


# ---------------------------------------------------------------------------
# Guard always discarded after own run (success and failure)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guard_discarded_after_success():
    import json

    agent_id = "agent-uuid-007"
    body = json.dumps({"kind": "signup", "agent_id": agent_id})

    with patch("sprite_worker.processor.run_signup_pipeline", new_callable=AsyncMock):
        await processor.process_message(body, sink=_make_sink())

    assert agent_id not in processor._active


@pytest.mark.asyncio
async def test_guard_discarded_after_failure():
    import json

    agent_id = "agent-uuid-008"
    body = json.dumps({"kind": "signup", "agent_id": agent_id})

    with patch(
        "sprite_worker.processor.run_signup_pipeline",
        new_callable=AsyncMock,
        side_effect=RuntimeError("boom"),
    ):
        await processor.process_message(body, sink=_make_sink())

    assert agent_id not in processor._active


@pytest.mark.asyncio
async def test_guard_discarded_after_credit_error():
    import json

    from core.services.pixellab_service import PixelLabCreditError

    agent_id = "agent-uuid-009"
    body = json.dumps({"kind": "signup", "agent_id": agent_id})

    with patch(
        "sprite_worker.processor.run_signup_pipeline",
        new_callable=AsyncMock,
        side_effect=PixelLabCreditError("402"),
    ):
        await processor.process_message(body, sink=_make_sink())

    assert agent_id not in processor._active
