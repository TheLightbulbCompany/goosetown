"""Smoke tests for the canonical SKILL.md."""

from pathlib import Path

_SKILL_MD = Path(__file__).parents[2] / "data" / "goosetown-skill" / "skills" / "goosetown" / "SKILL.md"


def test_skill_md_under_100_lines():
    """Anthropic best practice: SKILL.md is loaded into context every session
    it activates. ≤100 lines keeps the per-session token cost bounded."""
    line_count = len(_SKILL_MD.read_text().splitlines())
    assert line_count <= 100, f"SKILL.md is {line_count} lines; target ≤100"


def test_skill_md_has_required_frontmatter_fields():
    """name + description are required for plugin marketplace discovery."""
    text = _SKILL_MD.read_text()
    assert text.startswith("---\n")
    fm_end = text.index("\n---\n", 4)
    frontmatter = text[4:fm_end]
    for field in ("name:", "description:", "license:"):
        assert field in frontmatter, f"Missing frontmatter field: {field}"


def test_skill_md_frontmatter_drops_invalid_openclaw_fields():
    """OpenClaw's schema doesn't accept `pip` under requires, or `heartbeat`
    at the skill level. These must be in references/openclaw.md as prose,
    not in frontmatter where they're silently ignored."""
    text = _SKILL_MD.read_text()
    fm_end = text.index("\n---\n", 4)
    frontmatter = text[4:fm_end]
    # If openclaw block is present, must not have pip or heartbeat keys
    if "openclaw" in frontmatter:
        # Crude but effective: parse the frontmatter lines individually
        for line in frontmatter.splitlines():
            stripped = line.strip()
            assert not stripped.startswith("pip:"), f"OpenClaw doesn't recognize 'pip' under requires: {line}"
            assert not stripped.startswith("heartbeat:"), f"OpenClaw heartbeat is agent-level, not skill-level: {line}"


def test_skill_md_install_steps_reference_real_commands():
    """SKILL.md install instructions must reference commands that exist
    after the install.sh sunset (codex P2 regression)."""
    text = _SKILL_MD.read_text()
    # The deleted shell script must not appear in install instructions
    assert "bash goosetown/setup-hooks.sh" not in text, (
        "SKILL.md still references deleted install.sh — replace with "
        "'python3 goosetown_cli.py setup-hooks'"
    )
    assert "bash goosetown/install.sh" not in text, (
        "SKILL.md still references deleted install.sh"
    )
    # If hooks are mentioned, the canonical CLI form must be there
    if "setup-hooks" in text:
        assert "goosetown_cli.py setup-hooks" in text or "goosetown setup-hooks" in text, (
            "setup-hooks mentioned without the canonical CLI invocation"
        )
