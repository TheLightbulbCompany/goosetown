"""Tests for core.services.sprite_jobs — SQS producer for durable sprite jobs.

Test strategy
-------------
- Patch ``core.services.sprite_jobs._sqs`` (the lazy-init factory) to return a
  pre-configured MagicMock, bypassing all boto3/AWS wiring.
- Patch ``core.services.town_service.TownService`` to control claim_sprite_job
  return values without touching DynamoDB.
- An autouse fixture resets the module-level ``_sqs_client`` and
  ``_queue_url_cache`` singletons between tests so state can't leak.

Behaviours under test
---------------------
1. enqueue_signup claim=True → SQS send, returns True, correct body schema.
2. enqueue_signup claim=False → no SQS send, returns False.
3. enqueue_signup force=True → skips claim entirely, sends, returns True.
4. enqueue_animation → sends with correct body, no claim, returns True.
5. SQS send raises → returns False (never raises), logs via logger.exception.
6. IN_PROGRESS_STATUSES constant is exported with the correct set of values.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_module_state():
    """Reset lazy singletons before every test so state can't leak."""
    import core.services.sprite_jobs as sj

    sj._sqs_client = None
    sj._queue_url_cache = None
    yield
    sj._sqs_client = None
    sj._queue_url_cache = None


@pytest.fixture()
def mock_sqs_client():
    """Return a MagicMock SQS client pre-configured with a get_queue_url response."""
    client = MagicMock()
    client.get_queue_url.return_value = {"QueueUrl": "https://sqs.us-east-1.amazonaws.com/123/sprite-jobs"}
    client.send_message.return_value = {"MessageId": "abc-123"}
    return client


@pytest.fixture()
def patched_sqs(mock_sqs_client):
    """Patch the module-level _sqs factory to return mock_sqs_client."""
    with patch("core.services.sprite_jobs._sqs", return_value=mock_sqs_client):
        yield mock_sqs_client


# ---------------------------------------------------------------------------
# Behaviour 1: enqueue_signup happy path
# ---------------------------------------------------------------------------


class TestEnqueueSignupHappyPath:
    def test_returns_true_when_claim_succeeds(self, patched_sqs):
        """enqueue_signup returns True when claim_sprite_job returns True."""
        with patch("core.services.town_service.TownService") as MockTownService:
            MockTownService.return_value.claim_sprite_job.return_value = True

            from core.services.sprite_jobs import enqueue_signup

            result = enqueue_signup("a-1", "Ada", "a goose in a suit")

        assert result is True

    def test_calls_sqs_send_message(self, patched_sqs):
        """A single SQS send_message call is made when the claim succeeds."""
        with patch("core.services.town_service.TownService") as MockTownService:
            MockTownService.return_value.claim_sprite_job.return_value = True

            from core.services.sprite_jobs import enqueue_signup

            enqueue_signup("a-1", "Ada", "a goose in a suit")

        patched_sqs.send_message.assert_called_once()

    def test_body_has_schema_1(self, patched_sqs):
        """Message body must carry schema=1."""
        with patch("core.services.town_service.TownService") as MockTownService:
            MockTownService.return_value.claim_sprite_job.return_value = True

            from core.services.sprite_jobs import enqueue_signup

            enqueue_signup("a-1", "Ada", "a goose in a suit")

        body = json.loads(patched_sqs.send_message.call_args.kwargs["MessageBody"])
        assert body["schema"] == 1

    def test_body_kind_is_signup(self, patched_sqs):
        """Message body kind must be 'signup'."""
        with patch("core.services.town_service.TownService") as MockTownService:
            MockTownService.return_value.claim_sprite_job.return_value = True

            from core.services.sprite_jobs import enqueue_signup

            enqueue_signup("a-1", "Ada", "a goose in a suit")

        body = json.loads(patched_sqs.send_message.call_args.kwargs["MessageBody"])
        assert body["kind"] == "signup"

    def test_body_carries_agent_fields(self, patched_sqs):
        """agent_id, display_name, appearance, and enqueued_at are all present."""
        with patch("core.services.town_service.TownService") as MockTownService:
            MockTownService.return_value.claim_sprite_job.return_value = True

            from core.services.sprite_jobs import enqueue_signup

            enqueue_signup("a-1", "Ada", "a goose in a suit")

        body = json.loads(patched_sqs.send_message.call_args.kwargs["MessageBody"])
        assert body["agent_id"] == "a-1"
        assert body["display_name"] == "Ada"
        assert body["appearance"] == "a goose in a suit"
        assert isinstance(body["enqueued_at"], int)

    def test_claim_is_called_with_agent_id(self, patched_sqs):
        """claim_sprite_job must be called with the exact agent_id."""
        with patch("core.services.town_service.TownService") as MockTownService:
            mock_svc = MockTownService.return_value
            mock_svc.claim_sprite_job.return_value = True

            from core.services.sprite_jobs import enqueue_signup

            enqueue_signup("a-1", "Ada", "a goose in a suit")

        mock_svc.claim_sprite_job.assert_called_once_with("a-1")

    def test_send_targets_correct_queue_url(self, patched_sqs):
        """QueueUrl passed to send_message must match get_queue_url response."""
        with patch("core.services.town_service.TownService") as MockTownService:
            MockTownService.return_value.claim_sprite_job.return_value = True

            from core.services.sprite_jobs import enqueue_signup

            enqueue_signup("a-1", "Ada", "a goose in a suit")

        assert (
            patched_sqs.send_message.call_args.kwargs["QueueUrl"]
            == "https://sqs.us-east-1.amazonaws.com/123/sprite-jobs"
        )


# ---------------------------------------------------------------------------
# Behaviour 2: claim returns False → skip send
# ---------------------------------------------------------------------------


class TestEnqueueSignupClaimFalse:
    def test_returns_false_when_claim_fails(self, patched_sqs):
        """enqueue_signup returns False when claim_sprite_job returns False."""
        with patch("core.services.town_service.TownService") as MockTownService:
            MockTownService.return_value.claim_sprite_job.return_value = False

            from core.services.sprite_jobs import enqueue_signup

            result = enqueue_signup("a-1", "Ada", "a goose in a suit")

        assert result is False

    def test_no_sqs_send_when_claim_fails(self, patched_sqs):
        """No SQS message must be sent when claim_sprite_job returns False."""
        with patch("core.services.town_service.TownService") as MockTownService:
            MockTownService.return_value.claim_sprite_job.return_value = False

            from core.services.sprite_jobs import enqueue_signup

            enqueue_signup("a-1", "Ada", "a goose in a suit")

        patched_sqs.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# Behaviour 3: force=True skips claim
# ---------------------------------------------------------------------------


class TestEnqueueSignupForce:
    def test_force_skips_claim_and_sends(self, patched_sqs):
        """force=True must send without calling claim_sprite_job."""
        with patch("core.services.town_service.TownService") as MockTownService:
            from core.services.sprite_jobs import enqueue_signup

            result = enqueue_signup("a-1", "Ada", "a goose in a suit", force=True)

        MockTownService.return_value.claim_sprite_job.assert_not_called()
        patched_sqs.send_message.assert_called_once()
        assert result is True

    def test_force_body_kind_is_signup(self, patched_sqs):
        """force=True still produces a 'signup' kind message."""
        with patch("core.services.town_service.TownService"):
            from core.services.sprite_jobs import enqueue_signup

            enqueue_signup("a-1", "Ada", "a goose in a suit", force=True)

        body = json.loads(patched_sqs.send_message.call_args.kwargs["MessageBody"])
        assert body["kind"] == "signup"
        assert body["agent_id"] == "a-1"


# ---------------------------------------------------------------------------
# Behaviour 4: enqueue_animation
# ---------------------------------------------------------------------------


class TestEnqueueAnimation:
    def test_returns_true(self, patched_sqs):
        """enqueue_animation returns True on a successful send."""
        from core.services.sprite_jobs import enqueue_animation

        result = enqueue_animation("a-1", "talking")

        assert result is True

    def test_sends_one_sqs_message(self, patched_sqs):
        """Exactly one SQS message is sent."""
        from core.services.sprite_jobs import enqueue_animation

        enqueue_animation("a-1", "talking")

        patched_sqs.send_message.assert_called_once()

    def test_body_schema_1_kind_animation(self, patched_sqs):
        """Body must have schema=1 and kind='animation'."""
        from core.services.sprite_jobs import enqueue_animation

        enqueue_animation("a-1", "talking")

        body = json.loads(patched_sqs.send_message.call_args.kwargs["MessageBody"])
        assert body["schema"] == 1
        assert body["kind"] == "animation"

    def test_body_carries_agent_id_and_anim_name(self, patched_sqs):
        """agent_id, anim_name, and enqueued_at are all present."""
        from core.services.sprite_jobs import enqueue_animation

        enqueue_animation("a-1", "talking")

        body = json.loads(patched_sqs.send_message.call_args.kwargs["MessageBody"])
        assert body["agent_id"] == "a-1"
        assert body["anim_name"] == "talking"
        assert isinstance(body["enqueued_at"], int)

    def test_no_claim_call_for_animation(self, patched_sqs):
        """enqueue_animation must never call claim_sprite_job."""
        with patch("core.services.town_service.TownService") as MockTownService:
            from core.services.sprite_jobs import enqueue_animation

            enqueue_animation("a-1", "talking")

        MockTownService.return_value.claim_sprite_job.assert_not_called()


# ---------------------------------------------------------------------------
# Behaviour 5: SQS send raises → returns False, logs
# ---------------------------------------------------------------------------


class TestSqsSendFailure:
    def test_returns_false_on_sqs_exception(self, patched_sqs):
        """_send must catch any exception from SQS and return False."""
        patched_sqs.send_message.side_effect = Exception("network timeout")

        from core.services.sprite_jobs import enqueue_animation

        result = enqueue_animation("a-1", "talking")

        assert result is False

    def test_never_raises_on_sqs_exception(self, patched_sqs):
        """Exceptions from SQS must never propagate to the caller."""
        patched_sqs.send_message.side_effect = RuntimeError("unexpected AWS error")

        from core.services.sprite_jobs import enqueue_animation

        # Should not raise
        enqueue_animation("a-1", "talking")

    def test_logs_exception_on_sqs_failure(self, patched_sqs):
        """logger.exception must be called when SQS send fails."""
        patched_sqs.send_message.side_effect = Exception("connection refused")

        with patch("core.services.sprite_jobs.logger") as mock_logger:
            from core.services.sprite_jobs import enqueue_animation

            enqueue_animation("a-1", "talking")

        mock_logger.exception.assert_called_once()

    def test_signup_returns_false_on_sqs_exception(self, patched_sqs):
        """enqueue_signup also returns False (never raises) when SQS send fails."""
        patched_sqs.send_message.side_effect = Exception("timeout")

        with patch("core.services.town_service.TownService") as MockTownService:
            MockTownService.return_value.claim_sprite_job.return_value = True

            from core.services.sprite_jobs import enqueue_signup

            result = enqueue_signup("a-1", "Ada", "a goose in a suit")

        assert result is False


# ---------------------------------------------------------------------------
# Behaviour 6: IN_PROGRESS_STATUSES constant
# ---------------------------------------------------------------------------


class TestInProgressStatuses:
    def test_constant_is_exported(self):
        """IN_PROGRESS_STATUSES must be importable from the module."""
        from core.services.sprite_jobs import IN_PROGRESS_STATUSES

        assert IN_PROGRESS_STATUSES is not None

    def test_constant_contains_expected_values(self):
        """IN_PROGRESS_STATUSES must contain the canonical set of in-flight states."""
        from core.services.sprite_jobs import IN_PROGRESS_STATUSES

        assert set(IN_PROGRESS_STATUSES) == {
            "queued",
            "creating",
            "building_static",
            "static_ready",
            "animating",
            "downloading",
        }

    def test_constant_is_a_tuple(self):
        """IN_PROGRESS_STATUSES should be a tuple (immutable, hashable)."""
        from core.services.sprite_jobs import IN_PROGRESS_STATUSES

        assert isinstance(IN_PROGRESS_STATUSES, tuple)
