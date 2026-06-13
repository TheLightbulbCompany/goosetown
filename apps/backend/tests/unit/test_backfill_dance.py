"""Tests for backfill_dance agent selection (pure function, no AWS)."""

import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "backfill_dance",
    pathlib.Path(__file__).parents[2] / "scripts" / "backfill_dance.py",
)
backfill_dance = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill_dance)


def _agent(**kw):
    base = {"id": "a-1", "displayName": "A", "pixellabCharacterId": "c-1", "spriteAnimations": {}}
    base.update(kw)
    return base


def test_skips_agents_without_character_id():
    agents = [_agent(pixellabCharacterId=None), _agent(id="a-2", pixellabCharacterId="c-2")]
    out = backfill_dance.agents_needing_animation(agents)
    assert [a["id"] for a in out] == ["a-2"]


def test_skips_agents_that_already_have_dance():
    agents = [
        _agent(spriteAnimations={"dance": {"url": "u", "frameCount": 8, "fps": 8}}),
        _agent(id="a-2", spriteAnimations={"walk": {}}),
    ]
    out = backfill_dance.agents_needing_animation(agents)
    assert [a["id"] for a in out] == ["a-2"]


def test_handles_missing_sprite_animations_attr():
    out = backfill_dance.agents_needing_animation([{"id": "a-3", "pixellabCharacterId": "c"}])
    assert [a["id"] for a in out] == ["a-3"]


def test_animation_parameter_overrides_default():
    agents = [
        _agent(spriteAnimations={"walk": {"url": "u", "frameCount": 6, "fps": 8}}),
        _agent(id="a-2", spriteAnimations={"dance": {"url": "u", "frameCount": 8, "fps": 8}}),
    ]
    out = backfill_dance.agents_needing_animation(agents, animation="walk")
    assert [a["id"] for a in out] == ["a-2"]


def test_in_progress_statuses_mirror_sprite_jobs():
    from core.services.sprite_jobs import IN_PROGRESS_STATUSES

    assert backfill_dance.SPRITE_IN_PROGRESS_STATUSES == IN_PROGRESS_STATUSES


def test_skips_agents_with_sprite_job_in_flight():
    agents = [
        _agent(spriteStatus="animating"),
        _agent(id="a-2", spriteStatus="complete"),
        _agent(id="a-3"),
    ]
    out = backfill_dance.agents_needing_animation(agents)
    assert [a["id"] for a in out] == ["a-2", "a-3"]
