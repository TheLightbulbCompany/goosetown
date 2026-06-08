"""Tests for POST /town/admin/recomposite-sprites (dev-only sprite re-composite backfill)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from main import app

    yield TestClient(app)


@patch("core.config.settings")
def test_recomposite_is_dev_only(mock_settings, client):
    mock_settings.ENVIRONMENT = "prod"
    resp = client.post("/api/v1/town/admin/recomposite-sprites")
    assert resp.status_code == 403


@patch("routers.town.resume_sprite_generation", new_callable=AsyncMock)
@patch("core.dynamo.agents_table")
@patch("core.config.settings")
def test_recomposite_queues_only_agents_with_a_pixellab_character(mock_settings, mock_table, mock_resume, client):
    mock_settings.ENVIRONMENT = "dev"
    mock_table.return_value.scan.return_value = {
        "Items": [
            {"id": "a1", "pixellabCharacterId": "char1", "displayName": "Alice"},
            {"id": "a2"},  # no character — filtered out, not queued
        ]
    }
    resp = client.post("/api/v1/town/admin/recomposite-sprites")
    assert resp.status_code == 200
    assert resp.json() == {"queued": 1}


# ─── full regen (single agent, costs credits) ──────────────────────────────


@patch("core.config.settings")
def test_regenerate_is_dev_only(mock_settings, client):
    mock_settings.ENVIRONMENT = "prod"
    resp = client.post("/api/v1/town/admin/regenerate-sprite/abc")
    assert resp.status_code == 403


@patch("routers.town.resume_sprite_generation", new_callable=AsyncMock)
@patch("routers.town.TownService")
@patch("core.config.settings")
def test_regenerate_404_when_agent_missing(mock_settings, mock_ts, mock_resume, client):
    mock_settings.ENVIRONMENT = "dev"
    mock_ts.return_value.get_town_agent_by_id.return_value = None
    resp = client.post("/api/v1/town/admin/regenerate-sprite/nope")
    assert resp.status_code == 404


@patch("routers.town.resume_sprite_generation", new_callable=AsyncMock)
@patch("routers.town.TownService")
@patch("core.config.settings")
def test_regenerate_kicks_off_for_valid_agent(mock_settings, mock_ts, mock_resume, client):
    mock_settings.ENVIRONMENT = "dev"
    mock_ts.return_value.get_town_agent_by_id.return_value = {
        "id": "510b22c3",
        "displayName": "Sherlock Holmes",
        "appearance": "deerstalker cap, Inverness cape",
    }
    resp = client.post("/api/v1/town/admin/regenerate-sprite/510b22c3")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "regenerating"
    assert body["display_name"] == "Sherlock Holmes"
