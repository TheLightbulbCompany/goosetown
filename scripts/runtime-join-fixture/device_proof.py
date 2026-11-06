"""Run device-route/connect acceptance with an isolated released Lightbulb signer.

Reuses the regression's real FastAPI routes, Ed25519 verification and DynamoDB
transactions. Clerk identity, AWS stores and game-server transport are synthetic.
The signing key and signatures come exclusively from the supplied release binary.
"""

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "apps/backend"), str(ROOT / "apps/goosetown-cli/src")]
os.environ.update(
    AWS_ACCESS_KEY_ID="testing",
    AWS_SECRET_ACCESS_KEY="testing",
    AWS_SESSION_TOKEN="testing",
    AWS_DEFAULT_REGION="us-east-1",
    AWS_EC2_METADATA_DISABLED="true",
    ENVIRONMENT="dev",
    DYNAMODB_TABLE_PREFIX="goosetown",
    PIXELLAB_API_KEY="",
    SPRITE_S3_BUCKET="",
    SPRITE_CDN_URL="",
)


def run(binary):
    import contextlib
    import io

    import boto3
    import pytest
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from goosetown import core as cli_core
    from goosetown import runtime_join
    from goosetown.member_binding import ManagedMember, Member
    from moto import mock_aws

    from core import dynamo, town_token
    from core.config import settings
    from core.services.town_service import TownService
    from routers import town as town_routes
    from routers.runtime_session import verify
    from tests.conftest import _create_tables
    from tests.unit.test_device_join import (
        device,
        test_scoped_device_binds_and_arrives_only_after_its_proven_connection,
    )

    binary = Path(binary).resolve(strict=True)
    version = subprocess.check_output([str(binary), "--version"], text=True).strip()
    assert version == "lightbulb 0.9.14", version
    api = "https://api.goosetown.thelightbulb.company/api/v1"
    original_which, original_prove, original_mint = shutil.which, Member.prove, town_token.sign_town_token
    with (
        mock_aws(),
        tempfile.TemporaryDirectory(prefix="gt-device-release-") as temp,
        pytest.MonkeyPatch.context() as mp,
    ):
        isolated = Path(temp)
        workspace = isolated / ".goosetown/agents/lucky"
        for name, value in {
            "HOME": str(isolated),
            "LIGHTBULB_API": "http://127.0.0.1:9",
            "LIGHTBULB_CONNECT_DEBUG_NO_DEFAULT_RELAYS": "1",
            "LIGHTBULB_CONNECT_DEBUG_NO_TELEMETRY": "1",
        }.items():
            mp.setenv(name, value)
        mp.delenv("AGENT_DIR", raising=False)
        mp.setattr(shutil, "which", lambda name: str(binary) if name == "lightbulb" else original_which(name))
        mp.setattr("core.services.sqs_drainer.create_agent_queue", lambda _: None)
        signed, scoped_tokens = [], []

        def tracked_prove(member, payload):
            result = original_prove(member, payload)
            signed.append(True)
            return result

        def tracked_mint(*args, **kwargs):
            result = original_mint(*args, **kwargs)
            if kwargs.get("actor_id"):
                scoped_tokens.append(result)
            return result

        mp.setattr(Member, "prove", tracked_prove)
        mp.setattr(town_token, "sign_town_token", tracked_mint)
        dynamo._client = None
        db = boto3.resource("dynamodb", region_name="us-east-1")
        _create_tables(db)
        settings.RUNTIME_BINDING_ENABLED = True
        settings.RUNTIME_SESSION_ENFORCEMENT_ENABLED = True
        settings.RUNTIME_BINDING_ORIGIN = api.removesuffix("/api/v1")
        settings.PIXELLAB_API_KEY = ""
        with ManagedMember(workspace, api) as member:
            member.check_support(api)
            raw_key = base64.b32decode(member.identity().upper() + "====")

            class ReleasedSigner:
                def public_key(self):
                    return self

                def public_bytes(self, *_):
                    return raw_key

                def sign(self, payload):
                    signature = member.prove(base64.urlsafe_b64encode(payload).decode().rstrip("="))
                    return base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))

            fixture = device.__wrapped__(db, mp)
            with patch.object(Ed25519PrivateKey, "generate", return_value=ReleasedSigner()):
                test_scoped_device_binds_and_arrives_only_after_its_proven_connection(fixture, mp)
            assert len(signed) == 2
        # The original real member process has stopped. Resume must boot the
        # same persisted member home and acquire a NEW session before launch.
        client = fixture[0]
        token = scoped_tokens[0]
        actor = town_token.verify_town_token(token)["aid"]
        old_jti = town_routes.get_town_agent_ws_manager().get_by_agent_id(actor).runtime_session_claims["jti"]
        dynamo.connections_table().delete_item(Key={"connectionId": "fresh-connection"})
        (workspace / "GOOSETOWN.md").write_text(
            f"agent: lucky\ntoken: {token}\napi_url: {api}\n"
            f"ws_url: wss://example.invalid\nworkspace_path: {workspace}\n"
        )
        (isolated / ".goosetown/active-agent").write_text("another-profile\n")
        state = isolated / "lucky-state"
        state.mkdir()
        alarm = state / "alarm.json"
        alarm.write_text(json.dumps({"wake_time": "00:00", "timezone": "UTC"}))
        mp.setattr(cli_core, "state_dir", lambda _: state)

        def post(url, credential, payload):
            response = client.post(
                url.removeprefix(api), headers={"Authorization": "Bearer " + credential}, json=payload
            )
            if response.status_code != 200:
                raise runtime_join.EnrollmentError("Backend denied the current actor binding or activity")
            return response.json()

        mp.setattr(runtime_join, "_post", post)
        started = []

        def launch(config, home, session):
            claims = verify(session["runtime_session_token"])
            assert config["agent"] == "lucky" and config["token"] == token
            assert home == workspace and session["managed"] is True
            assert claims["actor_id"] == actor and claims["jti"] != old_jti
            started.append(True)
            return {"connected": True}

        mp.setattr(cli_core, "_start_daemon", launch)
        with contextlib.redirect_stdout(io.StringIO()):
            assert cli_core.cmd_daemon_resume("lucky") == 0
        assert len(signed) == 3 and started == [True] and not alarm.exists()
        TownService().set_agent_active(actor, False)
        alarm.write_text(json.dumps({"wake_time": "00:00", "timezone": "UTC"}))
        denied = io.StringIO()
        with contextlib.redirect_stdout(denied):
            assert cli_core.cmd_daemon_resume("lucky") == 1
        assert "Owner action required" in denied.getvalue()
        assert alarm.exists() and len(started) == 1 and len(signed) == 3
        print(
            json.dumps(
                {
                    "result": "PASS",
                    "lightbulb_version": version,
                    "binary": str(binary),
                    "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                    "real": [
                        "released Lightbulb member",
                        "UDS identity/capability/prove RPC",
                        "device start/approve/redeem routes",
                        "Ed25519 verification",
                        "single-use enrollment transactions",
                        "backend connect handler",
                        "active daemon-resume restarts same member home and proves fresh session",
                    ],
                    "synthetic": ["Clerk owner", "Moto AWS", "game-server transport", "Goose daemon process launch"],
                    "checks": [
                        "actor-scoped credential",
                        "replayed challenge denied",
                        "arrival only after this proof+connect",
                        "raw other-owner apartment command denied",
                        "Exchange/upload authority denied",
                        "active resume uses a fresh proven session",
                        "inactive resume denied and alarm retained",
                    ],
                    "signatures": len(signed),
                    "home": "isolated temporary directory, removed on exit",
                },
                indent=2,
            )
        )
        dynamo._client = None


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--lightbulb", required=True, help="Verified isolated released Lightbulb binary")
    run(parser.parse_args().lightbulb)
