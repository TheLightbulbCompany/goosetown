"""Tests for Task W5 — S3 artifact delivery (CLI side).

Covers:
- ``act deliver --file=...``: small UTF-8 file stays on the inline text path;
  big or binary files go through ``_upload_artifact`` and the payload carries
  ``artifact_key`` + the short text note.
- ``_upload_artifact``: stubs urllib end-to-end (upload-url request, then the
  presigned multipart POST to S3) and asserts both request bodies.
- Daemon ``_on_job_delivered`` with ``artifact_url``: downloads into
  ``<workspace>/JOBS/{job_id}/{name}`` and references the path in
  TOWN_JOBS.md; on download failure renders the URL with a note.
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from goosetown.core import _upload_artifact, cmd_act
from goosetown.daemon import TownDaemon

FAKE_CONFIG = {
    "agent": "tester",
    "token": "tok-123",
    "api_url": "https://api.example/api/v1",
}


def _run_cmd_act(
    action: str, extra_args: list[str], config: dict | None = None
) -> dict:
    """Build an argparse.Namespace, run cmd_act, return captured payload."""
    captured: list[dict] = []

    def fake_ipc(sock_path, msg):
        captured.append(msg)
        return {"status": "ok"}

    ns = argparse.Namespace(action=action, args=extra_args)

    with (
        patch("goosetown.core.load_config", return_value=config or FAKE_CONFIG),
        patch("goosetown.core.state_dir", return_value=Path("/tmp/fake")),
        patch("goosetown.core._ensure_daemon"),
        patch("goosetown.core._ensure_websockets"),
        patch("goosetown.core._require_websockets"),
        patch("goosetown.core.ipc_send", side_effect=fake_ipc),
        patch("goosetown.core._out"),
    ):
        cmd_act(ns)

    assert captured, "ipc_send was never called"
    return captured[0]["payload"]


class _FakeResponse:
    """Minimal context-manager stand-in for urlopen's response."""

    def __init__(self, body: bytes):
        self._body = body

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            body, self._body = self._body, b""
            return body
        chunk, self._body = self._body[:n], self._body[n:]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# act deliver --file: inline vs upload path
# ---------------------------------------------------------------------------


class TestDeliverFileRouting:
    def test_small_utf8_file_stays_inline(self, tmp_path: Path):
        f = tmp_path / "haiku.txt"
        f.write_text("plaza fountain hums")

        with patch("goosetown.core._upload_artifact") as upload:
            payload = _run_cmd_act("deliver", ["job-1", f"--file={f}"])

        upload.assert_not_called()
        assert payload == {
            "action": "deliver_job",
            "job_id": "job-1",
            "deliverable": "plaza fountain hums",
        }

    def test_large_utf8_file_uses_upload_path(self, tmp_path: Path):
        f = tmp_path / "big.txt"
        f.write_text("x" * 16385)  # one byte over the inline cap

        with patch(
            "goosetown.core._upload_artifact",
            return_value="artifacts/job-1/big.txt",
        ) as upload:
            payload = _run_cmd_act("deliver", ["job-1", f"--file={f}"])

        upload.assert_called_once()
        args = upload.call_args.args
        assert args[1] == "job-1"
        assert args[2] == "big.txt"
        assert args[3] == b"x" * 16385
        assert payload["artifact_key"] == "artifacts/job-1/big.txt"
        assert payload["deliverable"] == "Delivered file: big.txt (16385 bytes)"

    def test_small_binary_file_uses_upload_path(self, tmp_path: Path):
        f = tmp_path / "img.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff\xfe")  # not valid UTF-8

        with patch(
            "goosetown.core._upload_artifact",
            return_value="artifacts/job-1/img.png",
        ) as upload:
            payload = _run_cmd_act("deliver", ["job-1", f"--file={f}"])

        upload.assert_called_once()
        assert payload["artifact_key"] == "artifacts/job-1/img.png"
        assert payload["deliverable"].startswith("Delivered file: img.png")

    def test_inline_text_path_unchanged(self):
        payload = _run_cmd_act("deliver", ["job-1", "done,", "see", "notes"])
        assert payload == {
            "action": "deliver_job",
            "job_id": "job-1",
            "deliverable": "done, see notes",
        }
        assert "artifact_key" not in payload


# ---------------------------------------------------------------------------
# _upload_artifact: stubbed HTTP layer
# ---------------------------------------------------------------------------


class TestUploadArtifact:
    def test_happy_path_two_requests(self):
        requests: list = []

        def fake_urlopen(req, timeout=None):
            requests.append(req)
            if len(requests) == 1:
                return _FakeResponse(
                    json.dumps(
                        {
                            "method": "POST",
                            "upload": {
                                "url": "https://bucket.s3.example/",
                                "fields": {
                                    "key": "artifacts/job-9/data.bin",
                                    "policy": "p",
                                    "x-amz-signature": "s",
                                },
                            },
                            "artifact_key": "artifacts/job-9/data.bin",
                        }
                    ).encode()
                )
            return _FakeResponse(b"")  # S3 presigned POST → 204 empty body

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            key = _upload_artifact(FAKE_CONFIG, "job-9", "data.bin", b"\x00\x01\x02")

        assert key == "artifacts/job-9/data.bin"
        assert len(requests) == 2

        # First request: upload-url with Bearer token + JSON body.
        first = requests[0]
        assert first.full_url == "https://api.example/api/v1/town/exchange/upload-url"
        assert first.get_header("Authorization") == "Bearer tok-123"
        body = json.loads(first.data.decode())
        assert body == {"job_id": "job-9", "filename": "data.bin", "content_length": 3}

        # Second request: multipart POST to the presigned URL — policy fields
        # before the file part, file bytes present.
        second = requests[1]
        assert second.full_url == "https://bucket.s3.example/"
        assert second.get_header("Content-type").startswith(
            "multipart/form-data; boundary="
        )
        raw = second.data
        assert b'name="key"' in raw
        assert b"artifacts/job-9/data.bin" in raw
        assert b'name="file"; filename="data.bin"' in raw
        assert b"\x00\x01\x02" in raw
        assert raw.index(b'name="policy"') < raw.index(b'name="file"')

    def test_oversize_file_dies_before_any_request(self):
        with (
            patch("urllib.request.urlopen") as urlopen,
            pytest.raises(SystemExit),
        ):
            _upload_artifact(
                FAKE_CONFIG, "job-9", "huge.bin", b"x" * (5 * 1024 * 1024 + 1)
            )
        urlopen.assert_not_called()

    def test_missing_config_dies(self):
        with pytest.raises(SystemExit):
            _upload_artifact({"agent": "tester"}, "job-9", "f.bin", b"x")

    def test_upload_url_http_error_dies(self):
        import urllib.error

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url,
                403,
                "Forbidden",
                hdrs=None,
                fp=io.BytesIO(b'{"detail":"nope"}'),
            )

        with (
            patch("urllib.request.urlopen", side_effect=fake_urlopen),
            pytest.raises(SystemExit),
        ):
            _upload_artifact(FAKE_CONFIG, "job-9", "f.bin", b"x")


# ---------------------------------------------------------------------------
# Daemon: download on job_delivered
# ---------------------------------------------------------------------------


@pytest.fixture
def daemon(tmp_path: Path) -> TownDaemon:
    state_dir = tmp_path / "state"
    workspace = tmp_path / "workspace"
    state_dir.mkdir()
    workspace.mkdir()
    return TownDaemon(
        ws_url="wss://example.invalid",
        token="t",
        agent_name="testagent",
        state_dir=state_dir,
        workspace_path=workspace,
    )


def _jobs_md(daemon: TownDaemon) -> str:
    return (daemon._workspace_path / "TOWN_JOBS.md").read_text()


def _events(daemon: TownDaemon) -> str:
    return (daemon._workspace_path / "TOWN_EVENTS.md").read_text()


class TestDaemonArtifactDownload:
    def _delivered_event(self, **extra) -> dict:
        return {
            "event": "job_delivered",
            "job_id": "job-7",
            "title": "Make a logo",
            "deliverable": "Delivered file: logo.png (2048 bytes)",
            "provider_id": "p1",
            "consumer_id": "c1",
            **extra,
        }

    def test_downloads_artifact_to_jobs_dir(self, daemon: TownDaemon):
        payload = b"\x89PNG fake bytes"

        def fake_urlopen(url, timeout=None):
            assert timeout == 30
            return _FakeResponse(payload)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            daemon._handle_event(
                self._delivered_event(
                    artifact_url="https://s3.example/presigned",
                    artifact_name="logo.png",
                )
            )

        target = daemon._workspace_path / "JOBS" / "job-7" / "logo.png"
        assert target.read_bytes() == payload
        assert f"Artifact file: {target}" in _jobs_md(daemon)
        assert "File saved to" in _events(daemon)

    def test_artifact_name_sanitized_client_side(self, daemon: TownDaemon):
        with patch("urllib.request.urlopen", return_value=_FakeResponse(b"data")):
            daemon._handle_event(
                self._delivered_event(
                    artifact_url="https://s3.example/presigned",
                    artifact_name="../../.bashrc",
                )
            )

        # basename → ".bashrc" → leading dots stripped → "bashrc"
        target = daemon._workspace_path / "JOBS" / "job-7" / "bashrc"
        assert target.exists()

    def test_download_failure_renders_url_with_note(self, daemon: TownDaemon):
        def fake_urlopen(url, timeout=None):
            raise OSError("connection refused")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            daemon._handle_event(
                self._delivered_event(
                    artifact_url="https://s3.example/presigned",
                    artifact_name="logo.png",
                )
            )

        md = _jobs_md(daemon)
        assert "download failed" in md
        assert "https://s3.example/presigned" in md
        assert "download failed" in _events(daemon)

    def test_midstream_failure_leaves_no_partial_file(self, daemon: TownDaemon):
        """A failure mid-download must not strand a truncated artifact file."""

        class _BrokenResponse(_FakeResponse):
            def __init__(self):
                super().__init__(b"first chunk of bytes")
                self._reads = 0

            def read(self, n: int = -1) -> bytes:
                self._reads += 1
                if self._reads == 1:
                    return b"first chunk"
                raise OSError("connection reset mid-stream")

        with patch("urllib.request.urlopen", return_value=_BrokenResponse()):
            daemon._handle_event(
                self._delivered_event(
                    artifact_url="https://s3.example/presigned",
                    artifact_name="logo.png",
                )
            )

        job_dir = daemon._workspace_path / "JOBS" / "job-7"
        assert not (job_dir / "logo.png").exists()
        assert not (job_dir / "logo.png.part").exists()
        md = _jobs_md(daemon)
        assert "download failed" in md
        assert "https://s3.example/presigned" in md

    def test_text_only_delivery_unchanged(self, daemon: TownDaemon):
        daemon._handle_event(self._delivered_event(deliverable="plain text work"))
        md = _jobs_md(daemon)
        assert "plain text work" in md
        assert "Artifact" not in md
        assert not (daemon._workspace_path / "JOBS").exists()
