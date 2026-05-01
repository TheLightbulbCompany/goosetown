"""TDD: plugin.json + marketplace.json are valid JSON with required fields."""

import json
from pathlib import Path

_REPO_ROOT = Path(__file__).parents[4]
_PLUGIN_JSON = (
    _REPO_ROOT / "apps" / "backend" / "data" / "goosetown-skill"
    / ".claude-plugin" / "plugin.json"
)
_MARKETPLACE_JSON = _REPO_ROOT / ".claude-plugin" / "marketplace.json"


def test_plugin_json_exists_and_has_required_fields():
    assert _PLUGIN_JSON.exists(), f"missing {_PLUGIN_JSON}"
    data = json.loads(_PLUGIN_JSON.read_text())
    assert data["name"] == "goosetown"
    assert "version" in data
    assert "description" in data
    assert "license" in data
    assert "repository" in data


def test_marketplace_json_lists_goosetown_plugin():
    assert _MARKETPLACE_JSON.exists(), f"missing {_MARKETPLACE_JSON}"
    data = json.loads(_MARKETPLACE_JSON.read_text())
    assert "plugins" in data
    plugin_names = [p["name"] for p in data["plugins"]]
    assert "goosetown" in plugin_names
    goosetown = next(p for p in data["plugins"] if p["name"] == "goosetown")
    assert goosetown["source"] == "./apps/backend/data/goosetown-skill"


def test_plugin_and_marketplace_versions_match():
    plugin = json.loads(_PLUGIN_JSON.read_text())
    marketplace = json.loads(_MARKETPLACE_JSON.read_text())
    market_plugin = next(p for p in marketplace["plugins"] if p["name"] == "goosetown")
    assert plugin["version"] == market_plugin["version"], (
        "plugin.json and marketplace.json version drift — keep them in sync"
    )
