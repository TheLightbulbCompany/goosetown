"""Durable sprite-pipeline settings exist with correct defaults and respect env overrides.

Uses importlib.reload to test env-var overrides without polluting the global
settings singleton.

INVARIANT: after each reload-based test completes (pass or fail), both of the
following must hold:
  1. ``core.config.settings`` is the *exact same object* (``is``) that existed
     before the test ran.
  2. os.environ is unpatched (guaranteed by ``patch.dict`` as a context manager).

This matters because other modules (e.g. ``routers.landing``) do
``from core.config import settings`` at import time and hold a permanent
reference to the original object.  A reload creates a *new* Settings instance
at ``core.config.settings``; any subsequent ``monkeypatch.setattr`` call that
targets ``core.config.settings.<attr>`` patches the new object while the router
still reads from the original, causing silent mis-patches and spurious 503s in
the landing-endpoint tests.

The fix: after every reload-under-test we reload again with clean env (to
regenerate correct defaults) and then rebind ``core.config.settings = original``
so the module-level name and every other module's reference agree on the same
object.  See ``_reload_with_env`` below.
"""

from __future__ import annotations

import importlib
import os
from contextlib import contextmanager
from unittest.mock import patch

import core.config as _config_mod


@contextmanager
def _reload_with_env(strip: frozenset[str] = frozenset(), **env_overrides: str):
    """Yield a fresh Settings *class* built under a controlled environment.

    Builds the patched env as: current os.environ minus *strip* keys, with
    *env_overrides* merged on top.  After the ``with`` block exits (success or
    exception), ``core.config.settings`` is restored to the exact same object
    it was before this call.

    INVARIANT guaranteed on exit: ``core.config.settings is original``.
    """
    original = _config_mod.settings

    patched_env = {k: v for k, v in os.environ.items() if k not in strip}
    patched_env.update(env_overrides)

    try:
        with patch.dict(os.environ, patched_env, clear=True):
            reloaded = importlib.reload(_config_mod)
            yield reloaded.Settings
    finally:
        # Reload with real env so class-level defaults are regenerated cleanly,
        # then restore singleton identity so other modules' references stay valid.
        importlib.reload(_config_mod)
        _config_mod.settings = original  # invariant: core.config.settings IS original


# ── Default-value tests (no reload needed — read from the live singleton) ─────


def test_activity_animation_generation_default():
    """ACTIVITY_ANIMATION_GENERATION defaults to True (opt-in)."""
    from core.config import settings

    assert settings.ACTIVITY_ANIMATION_GENERATION is True


def test_sprite_worker_max_concurrent_pipelines_default():
    """SPRITE_WORKER_MAX_CONCURRENT_PIPELINES defaults to 2."""
    from core.config import settings

    assert settings.SPRITE_WORKER_MAX_CONCURRENT_PIPELINES == 2


def test_backend_internal_url_default():
    """BACKEND_INTERNAL_URL defaults to empty string."""
    from core.config import settings

    assert settings.BACKEND_INTERNAL_URL == ""


# ── Queue name default requires env control (ENVIRONMENT drives the default) ──


def test_sprite_jobs_queue_name_default_dev():
    """SPRITE_JOBS_QUEUE_NAME defaults to goosetown-dev-sprite-jobs when ENVIRONMENT=dev."""
    strip = frozenset({"SPRITE_JOBS_QUEUE_NAME", "ENVIRONMENT"})
    with _reload_with_env(strip=strip, ENVIRONMENT="dev") as Fresh:
        assert Fresh.SPRITE_JOBS_QUEUE_NAME == "goosetown-dev-sprite-jobs"


# ── Override tests (env-var overrides respected) ───────────────────────────────


def test_activity_animation_generation_override_false():
    """ACTIVITY_ANIMATION_GENERATION='false' disables the flag."""
    strip = frozenset({"ACTIVITY_ANIMATION_GENERATION"})
    with _reload_with_env(strip=strip, ACTIVITY_ANIMATION_GENERATION="false") as Fresh:
        assert Fresh.ACTIVITY_ANIMATION_GENERATION is False


def test_sprite_jobs_queue_name_override():
    """SPRITE_JOBS_QUEUE_NAME respects its env var."""
    strip = frozenset({"SPRITE_JOBS_QUEUE_NAME"})
    with _reload_with_env(strip=strip, SPRITE_JOBS_QUEUE_NAME="my-custom-queue") as Fresh:
        assert Fresh.SPRITE_JOBS_QUEUE_NAME == "my-custom-queue"


def test_sprite_worker_max_concurrent_pipelines_override():
    """SPRITE_WORKER_MAX_CONCURRENT_PIPELINES respects its env var."""
    strip = frozenset({"SPRITE_WORKER_MAX_CONCURRENT_PIPELINES"})
    with _reload_with_env(strip=strip, SPRITE_WORKER_MAX_CONCURRENT_PIPELINES="5") as Fresh:
        assert Fresh.SPRITE_WORKER_MAX_CONCURRENT_PIPELINES == 5


def test_backend_internal_url_override():
    """BACKEND_INTERNAL_URL respects its env var."""
    strip = frozenset({"BACKEND_INTERNAL_URL"})
    with _reload_with_env(strip=strip, BACKEND_INTERNAL_URL="http://backend.internal:8000") as Fresh:
        assert Fresh.BACKEND_INTERNAL_URL == "http://backend.internal:8000"


# ── Invariant smoke test ───────────────────────────────────────────────────────


def test_settings_singleton_identity_preserved_after_overrides():
    """INVARIANT: core.config.settings is the same object after reload-based tests.

    Explicitly verifies the contract described in this module's docstring:
    after _reload_with_env exits, core.config.settings IS the original singleton
    so that other modules' ``from core.config import settings`` references remain
    valid and subsequent monkeypatch calls target the right object.
    """
    original = _config_mod.settings

    with _reload_with_env(ACTIVITY_ANIMATION_GENERATION="false"):
        pass  # exercise the reload path

    assert _config_mod.settings is original, (
        "core.config.settings was replaced — other modules holding the original "
        "reference will silently read stale values (test-pollution bug)"
    )
