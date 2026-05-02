"""TDD: POST /landing/signup proxies to Resend with validation + honeypot."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from core.services.resend_client import ResendError


@pytest.fixture
def test_client():
    from main import app

    return TestClient(app)


def test_signup_returns_200_on_success(test_client, monkeypatch):
    monkeypatch.setattr("core.config.settings.RESEND_API_KEY", "test-key")
    monkeypatch.setattr("core.config.settings.RESEND_AUDIENCE_ID", "aud-123")
    with patch("routers.landing.ResendClient") as mock_cls:
        mock_cls.return_value.add_contact = AsyncMock(return_value=None)
        response = test_client.post(
            "/api/v1/landing/signup",
            json={"email": "user@example.com"},
        )
    assert response.status_code == 200
    mock_cls.return_value.add_contact.assert_awaited_once_with("user@example.com")


def test_signup_returns_400_on_invalid_email(test_client, monkeypatch):
    monkeypatch.setattr("core.config.settings.RESEND_API_KEY", "test-key")
    monkeypatch.setattr("core.config.settings.RESEND_AUDIENCE_ID", "aud-123")
    response = test_client.post(
        "/api/v1/landing/signup",
        json={"email": "not-an-email"},
    )
    assert response.status_code == 400
    assert "email" in str(response.json().get("detail", "")).lower()


def test_signup_drops_honeypot_silently(test_client, monkeypatch):
    """Bots that fill the hidden honeypot field get a 200 with no Resend call."""
    monkeypatch.setattr("core.config.settings.RESEND_API_KEY", "test-key")
    monkeypatch.setattr("core.config.settings.RESEND_AUDIENCE_ID", "aud-123")
    with patch("routers.landing.ResendClient") as mock_cls:
        mock_cls.return_value.add_contact = AsyncMock(return_value=None)
        response = test_client.post(
            "/api/v1/landing/signup",
            json={"email": "user@example.com", "honeypot": "i-am-a-bot"},
        )
    assert response.status_code == 200
    mock_cls.return_value.add_contact.assert_not_awaited()


def test_signup_returns_503_on_resend_5xx(test_client, monkeypatch):
    monkeypatch.setattr("core.config.settings.RESEND_API_KEY", "test-key")
    monkeypatch.setattr("core.config.settings.RESEND_AUDIENCE_ID", "aud-123")
    with patch("routers.landing.ResendClient") as mock_cls:
        mock_cls.return_value.add_contact = AsyncMock(side_effect=ResendError(503, "Service Unavailable"))
        response = test_client.post(
            "/api/v1/landing/signup",
            json={"email": "user@example.com"},
        )
    assert response.status_code == 503


def test_signup_returns_400_on_resend_4xx(test_client, monkeypatch):
    monkeypatch.setattr("core.config.settings.RESEND_API_KEY", "test-key")
    monkeypatch.setattr("core.config.settings.RESEND_AUDIENCE_ID", "aud-123")
    with patch("routers.landing.ResendClient") as mock_cls:
        mock_cls.return_value.add_contact = AsyncMock(side_effect=ResendError(400, "Invalid email"))
        response = test_client.post(
            "/api/v1/landing/signup",
            json={"email": "user@example.com"},
        )
    assert response.status_code == 400


def test_signup_returns_503_on_misconfigured_backend(test_client, monkeypatch):
    """If RESEND_API_KEY or RESEND_AUDIENCE_ID is unset, return 503."""
    monkeypatch.setattr("core.config.settings.RESEND_API_KEY", "")
    monkeypatch.setattr("core.config.settings.RESEND_AUDIENCE_ID", "aud-123")
    response = test_client.post(
        "/api/v1/landing/signup",
        json={"email": "user@example.com"},
    )
    assert response.status_code == 503


def test_landing_healthz_returns_200(test_client):
    response = test_client.get("/api/v1/landing/healthz")
    assert response.status_code == 200
