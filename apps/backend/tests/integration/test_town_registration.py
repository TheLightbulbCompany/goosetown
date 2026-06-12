"""Integration test for the progressive sprite registration pipeline.

Tests that after Phase B (character creation + rotation fetch + static upload)
the agent record already has sprite_ready=True, sprite_url non-empty, and
onboarding_phase='static' — BEFORE any animations are downloaded.
"""

import asyncio
import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Minimal 1x1 white PNG for use as a fake rotation image.
# build_static_spritesheet_from_rotations calls Pillow, which needs a real PNG.
_MINIMAL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI6QAAAABJRU5ErkJggg=="
)


@pytest.mark.asyncio
async def test_registration_sets_static_phase_before_animations():
    """After Phase A (character creation) and Phase B (rotation fetch + static upload),
    the agent record must have sprite_ready=True, a non-empty sprite_url, and
    onboarding_phase='static' — BEFORE Phase C (queue_signup_animations) completes.

    We simulate a hung Phase C by making queue_signup_animations sleep for 30 s and
    cancelling with a 1.5 s timeout, then assert the Phase B state was already written.
    """
    from core.services.sprite_pipeline import _sprite_pipeline_task

    agent_id = "test-agent-id"

    # Four valid 1x1 PNGs — one per direction
    fake_rotations = {d: _MINIMAL_PNG for d in ("south", "west", "east", "north")}

    # Track every update_agent_state call keyed by agent_id
    captured_state: dict[str, dict] = {}

    def fake_update_agent_state(aid, **kwargs):
        captured_state.setdefault(str(aid), {}).update(kwargs)

    # Phase C blocker: an Event that never gets set, so queue_signup_animations hangs
    # until the test's asyncio.wait_for cancels the whole pipeline task.
    _phase_c_blocker = asyncio.Event()

    async def _hang_phase_c(*args, **kwargs):
        # Wait on a real event (not asyncio.sleep) so the instant-sleep patch
        # does not accidentally un-block Phase C.
        await _phase_c_blocker.wait()

    # Mock PixelLab service
    mock_pxl = MagicMock()
    mock_pxl.create_character = AsyncMock(return_value=("char-xyz", "job-1"))
    mock_pxl.get_job_status = AsyncMock(return_value={"status": "completed"})
    mock_pxl.fetch_all_rotations = AsyncMock(return_value=fake_rotations)
    # Phase C hangs forever so we can observe state after Phase B
    mock_pxl.queue_signup_animations = AsyncMock(side_effect=_hang_phase_c)
    mock_pxl.download_character_zip = AsyncMock(return_value=None)

    # Mock TownService
    mock_service = MagicMock()
    mock_service.update_agent_state.side_effect = fake_update_agent_state
    # The pipeline now reads display_name / appearance / char_id / status from
    # the ROW (the row is the source of truth — wrapper args are advisory).
    mock_service.get_town_agent_by_id.return_value = {
        "id": agent_id,
        "currentLocation": "plaza",
        "locationContext": "town",
        "displayName": "Test Agent",
        "appearance": "a pixel art warrior",
        "spriteStatus": "queued",
    }

    # Mock game server client and WS manager (agent not connected yet — that's fine)
    mock_gs_client = MagicMock()
    mock_gs_client.connect_agent = AsyncMock()
    mock_gs_client.update_agent_sprites = AsyncMock()

    fake_s3_key = f"sprites/{agent_id}/walk.png"

    # Patch everything that _sprite_pipeline_task imports or calls at runtime.
    # The function does lazy imports, so we patch at the source module level.
    # asyncio.sleep is patched to a no-op so the polling loops don't take real time.
    async def _instant_sleep(_secs=0):
        pass

    with (
        patch("core.services.pixellab_service.PixelLabService", return_value=mock_pxl),
        # `_sprite_pipeline_task` lives in `core.services.sprite_pipeline`.
        # TownService, get_game_server_client, and get_town_agent_ws_manager
        # are imported at the top of that module — so patch by that module's
        # namespace (patching where the NAME IS LOOKED UP, not where it's
        # defined, which is the standard unittest.mock rule).
        patch("core.services.sprite_pipeline.TownService", return_value=mock_service),
        patch("core.services.sprite_pipeline.get_game_server_client", return_value=mock_gs_client),
        # The pipeline no longer touches the ws_manager / game server
        # directly — backend-process effects go through LocalSpriteEventSink →
        # apply_sprite_update. Patch it at its source module (lazy import).
        patch(
            "core.services.sprite_update_apply.apply_sprite_update",
            new=AsyncMock(),
        ),
        patch(
            "core.services.sprite_storage.upload_sprite_to_s3",
            return_value=fake_s3_key,
        ),
        patch(
            "core.services.sprite_storage.upload_sprites_to_s3",
            return_value={},
        ),
        patch("routers.town._send_to_daemon", return_value=True),
        # asyncio.sleep is called inside the pipeline's polling loops.
        patch("core.services.sprite_pipeline.asyncio.sleep", side_effect=_instant_sleep),
        patch.dict(
            "os.environ",
            {
                "SPRITE_S3_BUCKET": "test-bucket",
                "SPRITE_CDN_URL": "https://cdn.example.com",
                "PIXELLAB_API_KEY": "test-key",
            },
        ),
    ):
        # Also set settings attributes directly in case they were already cached
        from core import config as _config_mod

        _config_mod.settings.PIXELLAB_API_KEY = "test-key"
        _config_mod.settings.SPRITE_S3_BUCKET = "test-bucket"
        _config_mod.settings.SPRITE_CDN_URL = "https://cdn.example.com"

        # Run the pipeline with a short timeout.
        # Phase A and B should complete well within 1.5 s.
        # Phase C (queue_signup_animations) will be cancelled by the TimeoutError.
        try:
            await asyncio.wait_for(
                _sprite_pipeline_task(agent_id, "Test Agent", "a pixel art warrior"),
                timeout=1.5,
            )
        except asyncio.TimeoutError:
            pass  # Expected — Phase C is hanging by design

    # ------------------------------------------------------------------
    # Assertions: Phase B must have written sprite_ready + onboarding_phase
    # ------------------------------------------------------------------
    final_state = captured_state.get(str(agent_id), {})

    assert final_state.get("sprite_ready") is True, (
        f"sprite_ready was not set to True after Phase B. Captured state: {final_state}"
    )

    sprite_url = final_state.get("sprite_url", "")
    assert sprite_url, f"sprite_url was empty after Phase B. Captured state: {final_state}"
    assert sprite_url.startswith("https://cdn.example.com"), f"sprite_url has unexpected prefix: {sprite_url!r}"

    assert final_state.get("onboarding_phase") == "static", (
        f"onboarding_phase was not 'static' after Phase B. Captured state: {final_state}"
    )
