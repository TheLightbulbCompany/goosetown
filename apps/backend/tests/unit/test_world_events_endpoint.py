"""Tests for GET /town/world-events endpoint."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Unauthenticated TestClient — /town/world-events is a public endpoint."""
    from main import app

    yield TestClient(app)


# ---------------------------------------------------------------------------
# Basic happy-path
# ---------------------------------------------------------------------------


@patch("core.dynamo.world_events_table")
def test_world_events_returns_public_events(mock_table_fn, client):
    mock_table_fn.return_value.query.return_value = {
        "Items": [
            {
                "eventId": "e1",
                "timestamp": "2026-05-10T10:00:00Z",
                "tag": "weather.storm",
                "summary": "rain begins",
                "envBucket": "dev",
            },
            {
                "eventId": "e2",
                "timestamp": "2026-05-10T09:00:00Z",
                "tag": "ambient.cat",
                "summary": "cat at cafe",
                "envBucket": "dev",
            },
        ]
    }
    resp = client.get("/api/v1/town/world-events")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert body[0]["tag"] == "weather.storm"
    assert body[0]["summary"] == "rain begins"
    assert body[1]["tag"] == "ambient.cat"


# ---------------------------------------------------------------------------
# personal.* filtering
# ---------------------------------------------------------------------------


@patch("core.dynamo.world_events_table")
def test_world_events_filters_personal_events(mock_table_fn, client):
    mock_table_fn.return_value.query.return_value = {
        "Items": [
            {"eventId": "e1", "timestamp": "t1", "tag": "weather.storm", "envBucket": "dev"},
            {
                "eventId": "e2",
                "timestamp": "t2",
                "tag": "personal.alice.opportunity",
                "envBucket": "dev",
            },
            {"eventId": "e3", "timestamp": "t3", "tag": "ambient.cat", "envBucket": "dev"},
        ]
    }
    resp = client.get("/api/v1/town/world-events")
    assert resp.status_code == 200
    body = resp.json()
    tags = [e["tag"] for e in body]
    assert "personal.alice.opportunity" not in tags
    assert "weather.storm" in tags
    assert "ambient.cat" in tags
    assert len(body) == 2


# ---------------------------------------------------------------------------
# Limit capping
# ---------------------------------------------------------------------------


@patch("core.dynamo.world_events_table")
def test_world_events_caps_limit_at_200(mock_table_fn, client):
    mock_table_fn.return_value.query.return_value = {"Items": []}
    resp = client.get("/api/v1/town/world-events?limit=99999")
    assert resp.status_code == 200
    # over-fetch is limit*2; cap is 200 → DDB Limit must be ≤ 400
    call_kwargs = mock_table_fn.return_value.query.call_args.kwargs
    assert call_kwargs["Limit"] <= 400


@patch("core.dynamo.world_events_table")
def test_world_events_clamps_limit_min_to_1(mock_table_fn, client):
    mock_table_fn.return_value.query.return_value = {"Items": []}
    resp = client.get("/api/v1/town/world-events?limit=0")
    assert resp.status_code == 200
    call_kwargs = mock_table_fn.return_value.query.call_args.kwargs
    # min(max(0,1),200)*2 = 2
    assert call_kwargs["Limit"] >= 2


# ---------------------------------------------------------------------------
# Public endpoint — no Clerk auth required
# ---------------------------------------------------------------------------


@patch("core.dynamo.world_events_table")
def test_world_events_no_auth_required(mock_table_fn, client):
    """Confirm the endpoint works without any Authorization header."""
    mock_table_fn.return_value.query.return_value = {"Items": []}
    # No auth header — should still succeed
    resp = client.get("/api/v1/town/world-events")
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------


@patch("core.dynamo.world_events_table")
def test_world_events_response_shape(mock_table_fn, client):
    """Each returned entry has the correct fields."""
    mock_table_fn.return_value.query.return_value = {
        "Items": [
            {
                "eventId": "evt-abc",
                "timestamp": "2026-05-10T12:00:00Z",
                "tag": "arc.chapter_open",
                "summary": "A new chapter begins",
                "location": "plaza",
                "envBucket": "dev",
            }
        ]
    }
    resp = client.get("/api/v1/town/world-events")
    assert resp.status_code == 200
    entry = resp.json()[0]
    assert entry["eventId"] == "evt-abc"
    assert entry["timestamp"] == "2026-05-10T12:00:00Z"
    assert entry["tag"] == "arc.chapter_open"
    assert entry["summary"] == "A new chapter begins"
    assert entry["location"] == "plaza"


@patch("core.dynamo.world_events_table")
def test_world_events_optional_fields_none(mock_table_fn, client):
    """summary and location are optional — absent from DDB item means null in response."""
    mock_table_fn.return_value.query.return_value = {
        "Items": [
            {
                "eventId": "evt-min",
                "timestamp": "2026-05-10T08:00:00Z",
                "tag": "system.tick",
                "envBucket": "dev",
            }
        ]
    }
    resp = client.get("/api/v1/town/world-events")
    assert resp.status_code == 200
    entry = resp.json()[0]
    assert entry["summary"] is None
    assert entry["location"] is None
