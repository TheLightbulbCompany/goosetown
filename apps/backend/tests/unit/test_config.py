"""Tests for GooseTown backend configuration.

Reload-based env-override tests. INVARIANT (documented at length in
test_config_sprite_worker.py): after each test, ``core.config.settings`` must
be the *exact same object* (``is``) it was before the test ran. Other modules
(e.g. ``routers.landing``) bind ``settings`` at import time, so leaving a
fresh instance behind makes later ``monkeypatch.setattr`` calls patch an
object nobody reads.
"""

from __future__ import annotations

import importlib

import pytest

import core.config as _config_mod


def test_session_enforcement_requires_binding_even_in_dev(monkeypatch):
    settings = _config_mod.Settings()
    monkeypatch.setattr(settings, "ENVIRONMENT", "dev")
    monkeypatch.setattr(settings, "RUNTIME_SESSION_ENFORCEMENT_ENABLED", True)
    monkeypatch.setattr(settings, "RUNTIME_BINDING_ENABLED", False)
    with pytest.raises(RuntimeError, match="requires RUNTIME_BINDING_ENABLED"):
        settings.validate()
    monkeypatch.setattr(settings, "RUNTIME_BINDING_ENABLED", True)
    settings.validate()


@pytest.fixture
def reload_config():
    """Callable that reloads core.config; restores singleton identity on exit."""
    original = _config_mod.settings

    def _reload():
        return importlib.reload(_config_mod)

    yield _reload
    importlib.reload(_config_mod)
    _config_mod.settings = original


def test_resident_memory_flags_default_off(monkeypatch, reload_config):
    """Test that RESIDENT_MEMORY_ENABLED and RESIDENT_MEMORIES_TABLE default to off/empty."""
    monkeypatch.delenv("RESIDENT_MEMORY_ENABLED", raising=False)
    monkeypatch.delenv("RESIDENT_MEMORIES_TABLE", raising=False)
    config = reload_config()
    assert config.settings.RESIDENT_MEMORY_ENABLED is False
    assert config.settings.RESIDENT_MEMORIES_TABLE == ""


def test_resident_memory_flags_enable(monkeypatch, reload_config):
    """Test that RESIDENT_MEMORY_ENABLED and RESIDENT_MEMORIES_TABLE can be set via env."""
    monkeypatch.setenv("RESIDENT_MEMORY_ENABLED", "true")
    monkeypatch.setenv("RESIDENT_MEMORIES_TABLE", "goosetown-dev-memories")
    config = reload_config()
    assert config.settings.RESIDENT_MEMORY_ENABLED is True
    assert config.settings.RESIDENT_MEMORIES_TABLE == "goosetown-dev-memories"


def test_social_goals_and_lore_flags_default_off(monkeypatch, reload_config):
    """Test that RESIDENT_SOCIAL_GOALS_ENABLED and TOWN_LORE_ENABLED default to False."""
    monkeypatch.delenv("RESIDENT_SOCIAL_GOALS_ENABLED", raising=False)
    monkeypatch.delenv("TOWN_LORE_ENABLED", raising=False)
    config = reload_config()
    assert config.settings.RESIDENT_SOCIAL_GOALS_ENABLED is False
    assert config.settings.TOWN_LORE_ENABLED is False


def test_social_goals_and_lore_flags_enable(monkeypatch, reload_config):
    """Test that RESIDENT_SOCIAL_GOALS_ENABLED and TOWN_LORE_ENABLED can be set via env."""
    monkeypatch.setenv("RESIDENT_SOCIAL_GOALS_ENABLED", "true")
    monkeypatch.setenv("TOWN_LORE_ENABLED", "true")
    config = reload_config()
    assert config.settings.RESIDENT_SOCIAL_GOALS_ENABLED is True
    assert config.settings.TOWN_LORE_ENABLED is True
