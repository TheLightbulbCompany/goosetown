"""Tests for ANIMATION_REGISTRY auto_generate filtering."""

from core.services.pixellab_service import (
    ANIMATION_REGISTRY,
    SIGNUP_ANIMATIONS,
)


def test_signup_animations_contains_only_walk():
    # Fast/slow split (task #40): signup generates walk only.
    # idle/talking generate lazily (activity layer / conversation-start hook).
    assert set(SIGNUP_ANIMATIONS.keys()) == {"walk"}


def test_all_registry_entries_have_auto_generate_flag():
    for name, config in ANIMATION_REGISTRY.items():
        assert "auto_generate" in config, f"{name} missing auto_generate flag"
        assert isinstance(config["auto_generate"], bool)


def test_only_signup_animations_have_auto_generate_true():
    # Fast/slow split (task #40): only walk is auto-generated at signup.
    auto = {name for name, cfg in ANIMATION_REGISTRY.items() if cfg["auto_generate"]}
    assert auto == {"walk"}


def test_walk_is_template_type():
    assert ANIMATION_REGISTRY["walk"]["type"] == "template"
    assert ANIMATION_REGISTRY["walk"]["template_id"] == "walk"


def test_idle_is_template_type_with_breathing_idle_template():
    assert ANIMATION_REGISTRY["idle"]["type"] == "template"
    assert ANIMATION_REGISTRY["idle"]["template_id"] == "breathing-idle"


def test_talking_is_custom_v2():
    assert ANIMATION_REGISTRY["talking"]["type"] == "custom"
    assert "prompt" in ANIMATION_REGISTRY["talking"]
