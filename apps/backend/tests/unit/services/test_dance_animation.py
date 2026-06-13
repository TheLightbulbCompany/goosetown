"""Tests for the 'dance' custom animation registry entry (showcase video, 2026-06-11)."""

from core.services.pixellab_service import (
    _LOOP_SUFFIX,
    ANIMATION_REGISTRY,
    CUSTOM_ANIMATIONS,
    SIGNUP_ANIMATIONS,
)


def test_dance_registered_as_custom():
    entry = ANIMATION_REGISTRY["dance"]
    assert entry["type"] == "custom"
    assert entry["frames"] == 8
    assert entry["fps"] == 8.0
    assert "dance" in CUSTOM_ANIMATIONS


def test_dance_not_auto_generated_at_signup():
    assert ANIMATION_REGISTRY["dance"]["auto_generate"] is False
    assert "dance" not in SIGNUP_ANIMATIONS


def test_dance_prompt_declares_loop():
    # animate-with-text-v3 has no loop flag; loop intent must live in the prompt
    # (_LOOP_SUFFIX convention in pixellab_service.py).
    assert ANIMATION_REGISTRY["dance"]["prompt"].endswith(_LOOP_SUFFIX)
