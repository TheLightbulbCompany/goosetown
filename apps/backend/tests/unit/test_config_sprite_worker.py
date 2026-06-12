"""Durable sprite-pipeline settings exist with correct defaults and respect env overrides.

Uses importlib.reload to test env-var overrides without polluting the global
settings singleton — mirrors the pattern established for this config module.

Each reload-based test restores the module to its original state afterwards so
the module-level ``settings`` singleton that other test modules hold references
to stays consistent across the full suite.
"""

from __future__ import annotations

import importlib
import os
from unittest.mock import patch


def test_activity_animation_generation_default():
    """ACTIVITY_ANIMATION_GENERATION defaults to True (opt-in)."""
    from core.config import settings

    assert settings.ACTIVITY_ANIMATION_GENERATION is True


def test_sprite_jobs_queue_name_default_dev():
    """SPRITE_JOBS_QUEUE_NAME defaults to goosetown-dev-sprite-jobs when ENVIRONMENT is dev."""
    import core.config as config_mod

    # Strip both vars so we exercise the default expression cleanly, then restore.
    strip = {"SPRITE_JOBS_QUEUE_NAME", "ENVIRONMENT"}
    env = {k: v for k, v in os.environ.items() if k not in strip}
    env["ENVIRONMENT"] = "dev"
    try:
        with patch.dict(os.environ, env, clear=True):
            reloaded = importlib.reload(config_mod)
            assert reloaded.Settings.SPRITE_JOBS_QUEUE_NAME == "goosetown-dev-sprite-jobs"
    finally:
        importlib.reload(config_mod)


def test_sprite_worker_max_concurrent_pipelines_default():
    """SPRITE_WORKER_MAX_CONCURRENT_PIPELINES defaults to 2."""
    from core.config import settings

    assert settings.SPRITE_WORKER_MAX_CONCURRENT_PIPELINES == 2


def test_backend_internal_url_default():
    """BACKEND_INTERNAL_URL defaults to empty string."""
    from core.config import settings

    assert settings.BACKEND_INTERNAL_URL == ""


def test_activity_animation_generation_override_false():
    """ACTIVITY_ANIMATION_GENERATION='false' disables the flag."""
    import core.config as config_mod

    env = {k: v for k, v in os.environ.items() if k != "ACTIVITY_ANIMATION_GENERATION"}
    env["ACTIVITY_ANIMATION_GENERATION"] = "false"
    try:
        with patch.dict(os.environ, env, clear=True):
            reloaded = importlib.reload(config_mod)
            assert reloaded.Settings.ACTIVITY_ANIMATION_GENERATION is False
    finally:
        importlib.reload(config_mod)


def test_sprite_jobs_queue_name_override():
    """SPRITE_JOBS_QUEUE_NAME respects its env var."""
    import core.config as config_mod

    env = {k: v for k, v in os.environ.items() if k != "SPRITE_JOBS_QUEUE_NAME"}
    env["SPRITE_JOBS_QUEUE_NAME"] = "my-custom-queue"
    try:
        with patch.dict(os.environ, env, clear=True):
            reloaded = importlib.reload(config_mod)
            assert reloaded.Settings.SPRITE_JOBS_QUEUE_NAME == "my-custom-queue"
    finally:
        importlib.reload(config_mod)


def test_sprite_worker_max_concurrent_pipelines_override():
    """SPRITE_WORKER_MAX_CONCURRENT_PIPELINES respects its env var."""
    import core.config as config_mod

    env = {k: v for k, v in os.environ.items() if k != "SPRITE_WORKER_MAX_CONCURRENT_PIPELINES"}
    env["SPRITE_WORKER_MAX_CONCURRENT_PIPELINES"] = "5"
    try:
        with patch.dict(os.environ, env, clear=True):
            reloaded = importlib.reload(config_mod)
            assert reloaded.Settings.SPRITE_WORKER_MAX_CONCURRENT_PIPELINES == 5
    finally:
        importlib.reload(config_mod)


def test_backend_internal_url_override():
    """BACKEND_INTERNAL_URL respects its env var."""
    import core.config as config_mod

    env = {k: v for k, v in os.environ.items() if k != "BACKEND_INTERNAL_URL"}
    env["BACKEND_INTERNAL_URL"] = "http://backend.internal:8000"
    try:
        with patch.dict(os.environ, env, clear=True):
            reloaded = importlib.reload(config_mod)
            assert reloaded.Settings.BACKEND_INTERNAL_URL == "http://backend.internal:8000"
    finally:
        importlib.reload(config_mod)
