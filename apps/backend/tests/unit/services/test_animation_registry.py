"""Template/v3 split: only walk+idle are templates; everything else is v3.
Every activity_registry animation resolves to a registry key."""

from core.services.pixellab_service import ANIMATION_REGISTRY, SIGNUP_ANIMATIONS


def test_signup_animations_is_walk_only():
    """Fast/slow split (task #40): signup generates create + walk ONLY.
    idle/talking generate lazily (activity layer / conversation hook)."""
    assert set(SIGNUP_ANIMATIONS.keys()) == {"walk"}


def test_only_walk_idle_are_templates():
    templates = {k for k, v in ANIMATION_REGISTRY.items() if v["type"] == "template"}
    assert templates == {"walk", "idle"}


def test_unused_templates_removed():
    for gone in ("run", "crouch", "pick_up"):
        assert gone not in ANIMATION_REGISTRY


def test_drink_is_now_v3():
    assert ANIMATION_REGISTRY["drink"]["type"] == "custom"
    assert ANIMATION_REGISTRY["drink"].get("prompt")


def test_every_activity_animation_resolves():
    # Collect every animation referenced by activity_registry and assert each
    # is a key in ANIMATION_REGISTRY. ADJUST the import + collection below to the
    # REAL symbol in core/services/activity_registry.py (read that file first).
    from core.services import activity_registry as ar

    used = set()
    for obj in vars(ar).values():
        # activities may be a dict of Activity or a list — collect any object
        # exposing a string .animation attribute
        candidates = obj.values() if isinstance(obj, dict) else (obj if isinstance(obj, (list, tuple, set)) else [])
        for a in candidates:
            anim = getattr(a, "animation", None)
            if isinstance(anim, str):
                used.add(anim)
    assert used, "no activity animations discovered — fix the collection logic"
    for name in used:
        assert name in ANIMATION_REGISTRY, f"activity animation {name!r} missing from ANIMATION_REGISTRY"
