"""Tests for GooseTown backend configuration."""

import importlib


def test_resident_memory_flags_default_off(monkeypatch):
    """Test that RESIDENT_MEMORY_ENABLED and RESIDENT_MEMORIES_TABLE default to off/empty."""
    monkeypatch.delenv("RESIDENT_MEMORY_ENABLED", raising=False)
    monkeypatch.delenv("RESIDENT_MEMORIES_TABLE", raising=False)
    from core import config
    importlib.reload(config)
    assert config.settings.RESIDENT_MEMORY_ENABLED is False
    assert config.settings.RESIDENT_MEMORIES_TABLE == ""


def test_resident_memory_flags_enable(monkeypatch):
    """Test that RESIDENT_MEMORY_ENABLED and RESIDENT_MEMORIES_TABLE can be set via env."""
    monkeypatch.setenv("RESIDENT_MEMORY_ENABLED", "true")
    monkeypatch.setenv("RESIDENT_MEMORIES_TABLE", "goosetown-dev-memories")
    from core import config
    importlib.reload(config)
    assert config.settings.RESIDENT_MEMORY_ENABLED is True
    assert config.settings.RESIDENT_MEMORIES_TABLE == "goosetown-dev-memories"
