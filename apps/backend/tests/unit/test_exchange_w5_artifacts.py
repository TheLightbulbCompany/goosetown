"""Tests for Task W5 — S3 artifact delivery.

Covers:
- ``POST /town/exchange/upload-url``: authz (caller must own the provider
  agent), job-status gating, filename sanitization, size cap, presign args.
- ``exchange_service.deliver_job`` with ``artifact_key``: prefix validation
  (``bad_artifact``), missing object (``artifact_missing``), size cap, happy
  path with a moto S3 bucket (artifactKey persisted, ``job_delivered`` event
  carries ``artifact_url`` + ``artifact_name``).
- ``ws_models.TownAgentAct`` validator: deliverable OR artifact_key.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import boto3
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from core.services import exchange_service as ex

TEST_USER_ID = "w5-user-1"
OTHER_USER_ID = "w5-user-2"
FAKE_TOKEN = "fake-w5-token"  # nosec B105: test fixture token, not a credential
BUCKET = "goosetown-test-sprites"


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


def _town_token_override():
    return (TEST_USER_ID, FAKE_TOKEN)


@pytest.fixture
def s3_bucket(dynamodb_tables, monkeypatch):
    """Create a moto S3 bucket and point settings at it."""
    from core.config import settings

    monkeypatch.setattr(settings, "SPRITE_S3_BUCKET", BUCKET)
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=BUCKET)
    return s3


@pytest.fixture
def client(dynamodb_tables, s3_bucket):
    """TestClient with town_token auth bypassed."""
    from main import app
    from routers.town import get_town_token_user

    app.dependency_overrides[get_town_token_user] = _town_token_override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_town_token_user, None)


@pytest.fixture
def mailbox():
    mb = MagicMock()
    with patch("core.services.exchange_service.get_daemon_mailbox", return_value=mb):
        yield mb


@pytest.fixture(autouse=True)
def _clean_timers():
    yield
    for task in list(ex._timers.values()):
        task.cancel()
    ex._timers.clear()


def _seed_agent(db, agent_id: str, name: str, user_id: str = TEST_USER_ID) -> None:
    db.Table("goosetown-agents").put_item(
        Item={
            "id": agent_id,
            "userId": user_id,
            "agentName": name,
            "displayName": name,
            "isActive": "true",
            "currentLocation": "bank",
            "energy": 100,
            "mood": 0,
            "traits": "",
        }
    )


def _seed_job(db, job_id: str, status: str = "AWARDED", provider_id: str = "prov-1") -> None:
    db.Table("goosetown-jobs").put_item(
        Item={
            "id": job_id,
            "status": status,
            "title": "Build a thing",
            "description": "desc",
            "maxPrice": 50,
            "agreedPrice": 40,
            "consumerId": "cons-1",
            "consumerName": "carol",
            "providerId": provider_id,
            "providerName": "pete",
            "createdAt": "2026-06-12T10:00:00+00:00",
            "updatedAt": "2026-06-12T10:00:00+00:00",
            "bids": {},
        }
    )


def _conn(agent_id: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(agent_id=agent_id, agent_name=name, location_context="town", connection_id="c1")


# ---------------------------------------------------------------------------
# POST /town/exchange/upload-url
# ---------------------------------------------------------------------------


class TestUploadUrlEndpoint:
    def _post(self, client, **overrides):
        body = {"job_id": "job-1", "filename": "report.pdf", "content_length": 1024}
        body.update(overrides)
        return client.post("/api/v1/town/exchange/upload-url", json=body)

    def test_happy_path_returns_post_upload_and_key(self, client, dynamodb_tables):
        _seed_agent(dynamodb_tables, "prov-1", "pete")
        _seed_job(dynamodb_tables, "job-1")

        resp = self._post(client)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["method"] == "POST"
        assert data["artifact_key"] == "artifacts/job-1/report.pdf"
        assert data["max_bytes"] == ex.ARTIFACT_MAX_BYTES
        assert "url" in data["upload"]
        assert "fields" in data["upload"]
        assert data["upload"]["fields"]["key"] == "artifacts/job-1/report.pdf"

    def test_presign_called_with_content_length_range(self, client, dynamodb_tables):
        _seed_agent(dynamodb_tables, "prov-1", "pete")
        _seed_job(dynamodb_tables, "job-1")

        fake_client = MagicMock()
        fake_client.generate_presigned_post.return_value = {"url": "https://s3", "fields": {}}
        with patch("core.services.exchange_service._s3_client", return_value=fake_client):
            resp = self._post(client)
        assert resp.status_code == 200
        kwargs = fake_client.generate_presigned_post.call_args.kwargs
        assert kwargs["Bucket"] == BUCKET
        assert kwargs["Key"] == "artifacts/job-1/report.pdf"
        assert ["content-length-range", 1, ex.ARTIFACT_MAX_BYTES] in kwargs["Conditions"]
        assert kwargs["ExpiresIn"] == ex.ARTIFACT_UPLOAD_EXPIRES_SEC

    def test_wrong_user_403(self, client, dynamodb_tables):
        """Provider agent belongs to a different user → the token can't presign."""
        _seed_agent(dynamodb_tables, "prov-1", "pete", user_id=OTHER_USER_ID)
        _seed_job(dynamodb_tables, "job-1")

        resp = self._post(client)
        assert resp.status_code == 403

    def test_consumer_owner_cannot_presign(self, client, dynamodb_tables):
        """Owning the consumer is NOT enough — only the provider's owner uploads."""
        _seed_agent(dynamodb_tables, "cons-1", "carol", user_id=TEST_USER_ID)
        _seed_agent(dynamodb_tables, "prov-1", "pete", user_id=OTHER_USER_ID)
        _seed_job(dynamodb_tables, "job-1")

        resp = self._post(client)
        assert resp.status_code == 403

    def test_job_not_found_404(self, client, dynamodb_tables):
        resp = self._post(client, job_id="job-missing")
        assert resp.status_code == 404

    def test_job_not_awarded_409(self, client, dynamodb_tables):
        _seed_agent(dynamodb_tables, "prov-1", "pete")
        _seed_job(dynamodb_tables, "job-1", status="OPEN")

        resp = self._post(client)
        assert resp.status_code == 409

    @pytest.mark.parametrize("bad", [".hidden", "no spaces.txt", "ümlaut.png", "###", "a" * 129])
    def test_bad_filename_400(self, client, dynamodb_tables, bad):
        _seed_agent(dynamodb_tables, "prov-1", "pete")
        _seed_job(dynamodb_tables, "job-1")

        resp = self._post(client, filename=bad)
        assert resp.status_code == 400

    def test_path_traversal_filename_reduced_to_basename(self, client, dynamodb_tables):
        _seed_agent(dynamodb_tables, "prov-1", "pete")
        _seed_job(dynamodb_tables, "job-1")

        resp = self._post(client, filename="../../etc/passwd")
        assert resp.status_code == 200
        assert resp.json()["artifact_key"] == "artifacts/job-1/passwd"

    @pytest.mark.parametrize("size", [0, -1, ex.ARTIFACT_MAX_BYTES + 1])
    def test_content_length_out_of_range_422(self, client, dynamodb_tables, size):
        _seed_agent(dynamodb_tables, "prov-1", "pete")
        _seed_job(dynamodb_tables, "job-1")

        resp = self._post(client, content_length=size)
        assert resp.status_code == 422

    def test_no_bucket_configured_503(self, client, dynamodb_tables, monkeypatch):
        from core.config import settings

        monkeypatch.setattr(settings, "SPRITE_S3_BUCKET", "")
        _seed_agent(dynamodb_tables, "prov-1", "pete")
        _seed_job(dynamodb_tables, "job-1")

        resp = self._post(client)
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Filename sanitization unit coverage
# ---------------------------------------------------------------------------


class TestSanitizeArtifactFilename:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("report.pdf", "report.pdf"),
            ("a/b/c/report.pdf", "report.pdf"),
            ("..\\..\\evil.exe", "evil.exe"),
            ("under_score-dash.PNG", "under_score-dash.PNG"),
            ("a" * 128, "a" * 128),
        ],
    )
    def test_valid(self, raw, expected):
        assert ex.sanitize_artifact_filename(raw) == expected

    @pytest.mark.parametrize("raw", ["", ".env", "..", "sp ace.txt", "naïve.txt", "a" * 129, "x/"])
    def test_invalid(self, raw):
        assert ex.sanitize_artifact_filename(raw) is None


# ---------------------------------------------------------------------------
# deliver_job with artifact_key
# ---------------------------------------------------------------------------


class TestDeliverJobArtifact:
    async def test_wrong_prefix_rejected_bad_artifact(self, dynamodb_tables, s3_bucket, mailbox):
        _seed_agent(dynamodb_tables, "prov-1", "pete")
        _seed_job(dynamodb_tables, "job-1")

        result = await ex.deliver_job(_conn("prov-1", "pete"), "job-1", "", artifact_key="artifacts/job-OTHER/x.bin")
        assert result["status"] == "rejected"
        assert result["reason"] == "bad_artifact"

    async def test_unsafe_filename_in_key_rejected(self, dynamodb_tables, s3_bucket, mailbox):
        _seed_agent(dynamodb_tables, "prov-1", "pete")
        _seed_job(dynamodb_tables, "job-1")

        result = await ex.deliver_job(
            _conn("prov-1", "pete"), "job-1", "", artifact_key="artifacts/job-1/../sprites/walk.png"
        )
        assert result["status"] == "rejected"
        assert result["reason"] == "bad_artifact"

    async def test_missing_object_rejected_artifact_missing(self, dynamodb_tables, s3_bucket, mailbox):
        _seed_agent(dynamodb_tables, "prov-1", "pete")
        _seed_job(dynamodb_tables, "job-1")

        result = await ex.deliver_job(_conn("prov-1", "pete"), "job-1", "", artifact_key="artifacts/job-1/ghost.bin")
        assert result["status"] == "rejected"
        assert result["reason"] == "artifact_missing"

    async def test_oversize_object_rejected(self, dynamodb_tables, s3_bucket, mailbox):
        _seed_agent(dynamodb_tables, "prov-1", "pete")
        _seed_job(dynamodb_tables, "job-1")
        with patch("core.services.exchange_service.head_artifact", return_value=(ex.ARTIFACT_MAX_BYTES + 1, None)):
            result = await ex.deliver_job(_conn("prov-1", "pete"), "job-1", "", artifact_key="artifacts/job-1/big.bin")
        assert result["status"] == "rejected"
        assert result["reason"] == "bad_artifact"

    async def test_happy_path_stores_key_and_notifies_with_url(self, dynamodb_tables, s3_bucket, mailbox):
        _seed_agent(dynamodb_tables, "prov-1", "pete")
        _seed_job(dynamodb_tables, "job-1")
        s3_bucket.put_object(Bucket=BUCKET, Key="artifacts/job-1/report.pdf", Body=b"x" * 100)

        result = await ex.deliver_job(
            _conn("prov-1", "pete"),
            "job-1",
            "Delivered file: report.pdf (100 bytes)",
            artifact_key="artifacts/job-1/report.pdf",
        )
        assert result["status"] == "ok", result

        job = dynamodb_tables.Table("goosetown-jobs").get_item(Key={"id": "job-1"})["Item"]
        assert job["status"] == "DELIVERED"
        assert job["artifactKey"] == "artifacts/job-1/report.pdf"

        # Consumer event carries the presigned GET + name + text note.
        consumer_events = [c.args[1] for c in mailbox.notify.call_args_list if c.args[0] == "cons-1"]
        delivered = next(e for e in consumer_events if e["event"] == "job_delivered")
        assert delivered["artifact_name"] == "report.pdf"
        assert "artifacts/job-1/report.pdf" in delivered["artifact_url"]
        assert delivered["deliverable"] == "Delivered file: report.pdf (100 bytes)"

    async def test_text_only_delivery_has_no_artifact_fields(self, dynamodb_tables, s3_bucket, mailbox):
        _seed_agent(dynamodb_tables, "prov-1", "pete")
        _seed_job(dynamodb_tables, "job-1")

        result = await ex.deliver_job(_conn("prov-1", "pete"), "job-1", "plain text work")
        assert result["status"] == "ok"

        consumer_events = [c.args[1] for c in mailbox.notify.call_args_list if c.args[0] == "cons-1"]
        delivered = next(e for e in consumer_events if e["event"] == "job_delivered")
        assert "artifact_url" not in delivered
        assert "artifact_name" not in delivered
        job = dynamodb_tables.Table("goosetown-jobs").get_item(Key={"id": "job-1"})["Item"]
        assert "artifactKey" not in job


# ---------------------------------------------------------------------------
# Version-pinning: consumer download is pinned to the delivery-time S3 version
# ---------------------------------------------------------------------------


@pytest.fixture
def versioned_s3_bucket(dynamodb_tables, monkeypatch):
    """S3 bucket with versioning enabled — simulates the production sprite bucket."""
    from core.config import settings

    monkeypatch.setattr(settings, "SPRITE_S3_BUCKET", BUCKET)
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=BUCKET)
    s3.put_bucket_versioning(
        Bucket=BUCKET,
        VersioningConfiguration={"Status": "Enabled"},
    )
    return s3


class TestArtifactVersionPinning:
    """Upload presigns outlive delivery (15 min); the consumer's GET must be
    pinned to the S3 version captured at deliver time so post-delivery overwrites
    are invisible (bucket is versioned per storage-stack.ts)."""

    async def test_versioned_bucket_pins_download_to_delivery_version(
        self, dynamodb_tables, versioned_s3_bucket, mailbox
    ):
        """Upload v1, deliver (captures v1 VersionId), overwrite with v2.
        The presigned URL must reference the v1 VersionId, not the current HEAD."""
        _seed_agent(dynamodb_tables, "prov-1", "pete")
        _seed_job(dynamodb_tables, "job-1")

        # Upload v1 and record its VersionId.
        put_v1 = versioned_s3_bucket.put_object(Bucket=BUCKET, Key="artifacts/job-1/data.bin", Body=b"version-one")
        v1_id = put_v1["VersionId"]

        # Capture the presign call so we can inspect the Params.
        captured: list[dict] = []
        real_presign = ex.presign_artifact_download

        def _spy(key, version_id=None):
            captured.append({"key": key, "version_id": version_id})
            return real_presign(key, version_id=version_id)

        with patch("core.services.exchange_service.presign_artifact_download", side_effect=_spy):
            result = await ex.deliver_job(
                _conn("prov-1", "pete"),
                "job-1",
                "delivered",
                artifact_key="artifacts/job-1/data.bin",
            )

        assert result["status"] == "ok", result

        # Overwrite with v2 AFTER delivery (simulates provider re-uploading).
        versioned_s3_bucket.put_object(Bucket=BUCKET, Key="artifacts/job-1/data.bin", Body=b"version-two")

        # Presign was called with the v1 VersionId.
        assert len(captured) == 1
        assert captured[0]["version_id"] == v1_id

        # artifactVersionId persisted on the job row.
        job = dynamodb_tables.Table("goosetown-jobs").get_item(Key={"id": "job-1"})["Item"]
        assert job.get("artifactVersionId") == v1_id

        # Consumer event artifact_url contains the version query param.
        # Moto uses "versionId=" in presigned URLs; real S3 uses "VersionId=".
        # Case-insensitive check covers both environments.
        consumer_events = [c.args[1] for c in mailbox.notify.call_args_list if c.args[0] == "cons-1"]
        delivered = next(e for e in consumer_events if e["event"] == "job_delivered")
        url = delivered["artifact_url"]
        assert "versionid=" in url.lower()

    async def test_unversioned_bucket_omits_version_id_no_crash(self, dynamodb_tables, s3_bucket, mailbox):
        """On an unversioned bucket (no VersionId in HeadObject), delivery succeeds
        and the presigned URL has no VersionId param — no crash."""
        _seed_agent(dynamodb_tables, "prov-1", "pete")
        _seed_job(dynamodb_tables, "job-1")
        s3_bucket.put_object(Bucket=BUCKET, Key="artifacts/job-1/plain.txt", Body=b"hello")

        captured: list[dict] = []
        real_presign = ex.presign_artifact_download

        def _spy(key, version_id=None):
            captured.append({"key": key, "version_id": version_id})
            return real_presign(key, version_id=version_id)

        with patch("core.services.exchange_service.presign_artifact_download", side_effect=_spy):
            result = await ex.deliver_job(
                _conn("prov-1", "pete"),
                "job-1",
                "plain text",
                artifact_key="artifacts/job-1/plain.txt",
            )

        assert result["status"] == "ok", result
        # version_id must be None (unversioned bucket returns no VersionId).
        assert len(captured) == 1
        assert captured[0]["version_id"] is None

        # No artifactVersionId on the job row.
        job = dynamodb_tables.Table("goosetown-jobs").get_item(Key={"id": "job-1"})["Item"]
        assert "artifactVersionId" not in job

        # URL is still valid (no VersionId= query param injected).
        consumer_events = [c.args[1] for c in mailbox.notify.call_args_list if c.args[0] == "cons-1"]
        delivered = next(e for e in consumer_events if e["event"] == "job_delivered")
        assert "artifact_url" in delivered

    def test_presign_artifact_download_includes_version_id_in_params(self, monkeypatch):
        """Unit-test presign_artifact_download: when version_id is supplied,
        it is forwarded in the S3 Params dict."""
        from core.config import settings

        monkeypatch.setattr(settings, "SPRITE_S3_BUCKET", BUCKET)
        fake_s3 = MagicMock()
        fake_s3.generate_presigned_url.return_value = "https://s3.example.com/signed"

        with patch("core.services.exchange_service._s3_client", return_value=fake_s3):
            ex.presign_artifact_download("artifacts/job-1/x.bin", version_id="v-abc123")

        call_kwargs = fake_s3.generate_presigned_url.call_args
        params = call_kwargs[1]["Params"] if call_kwargs[1] else call_kwargs[0][1]["Params"]
        assert params.get("VersionId") == "v-abc123"

    def test_presign_artifact_download_no_version_id_omits_param(self, monkeypatch):
        """Without a version_id, VersionId must NOT appear in the Params dict."""
        from core.config import settings

        monkeypatch.setattr(settings, "SPRITE_S3_BUCKET", BUCKET)
        fake_s3 = MagicMock()
        fake_s3.generate_presigned_url.return_value = "https://s3.example.com/signed"

        with patch("core.services.exchange_service._s3_client", return_value=fake_s3):
            ex.presign_artifact_download("artifacts/job-1/x.bin")

        call_kwargs = fake_s3.generate_presigned_url.call_args
        params = call_kwargs[1]["Params"] if call_kwargs[1] else call_kwargs[0][1]["Params"]
        assert "VersionId" not in params


# ---------------------------------------------------------------------------
# ws_models validator
# ---------------------------------------------------------------------------


class TestWsModelValidation:
    def _act(self, **kw):
        from core.ws_models import TownAgentAct

        return TownAgentAct(type="town_agent_act", **kw)

    def test_deliver_with_artifact_key_only_is_valid(self):
        act = self._act(action="deliver_job", job_id="j1", artifact_key="artifacts/j1/a.bin")
        assert act.artifact_key == "artifacts/j1/a.bin"

    def test_deliver_with_deliverable_only_is_valid(self):
        act = self._act(action="deliver_job", job_id="j1", deliverable="text")
        assert act.deliverable == "text"

    def test_deliver_with_neither_raises(self):
        with pytest.raises(ValidationError, match="deliverable.*artifact_key|artifact_key"):
            self._act(action="deliver_job", job_id="j1")

    def test_artifact_key_on_other_action_raises(self):
        with pytest.raises(ValidationError, match="artifact_key"):
            self._act(action="bid", job_id="j1", price=5, artifact_key="artifacts/j1/a.bin")

    def test_artifact_key_max_length(self):
        with pytest.raises(ValidationError):
            self._act(action="deliver_job", job_id="j1", artifact_key="a" * 513)
