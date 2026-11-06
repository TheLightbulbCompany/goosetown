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
    import boto3
    import pytest
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from goosetown.member_binding import ManagedMember
    from moto import mock_aws

    from core import dynamo
    from core.config import settings
    from tests.conftest import _create_tables
    from tests.unit.test_device_join import (
        device,
        test_scoped_device_binds_and_arrives_only_after_its_proven_connection,
    )

    binary = Path(binary).resolve(strict=True)
    version = subprocess.check_output([str(binary), "--version"], text=True).strip()
    assert version == "lightbulb 0.9.14", version
    api = "https://api.goosetown.thelightbulb.company/api/v1"
    original_which = shutil.which
    with (
        mock_aws(),
        tempfile.TemporaryDirectory(prefix="gt-device-release-") as temp,
        pytest.MonkeyPatch.context() as mp,
    ):
        isolated = Path(temp)
        dynamo._client = None
        db = boto3.resource("dynamodb", region_name="us-east-1")
        _create_tables(db)
        settings.RUNTIME_BINDING_ENABLED = True
        settings.RUNTIME_SESSION_ENFORCEMENT_ENABLED = True
        settings.RUNTIME_BINDING_ORIGIN = api.removesuffix("/api/v1")
        settings.PIXELLAB_API_KEY = ""
        with (
            patch.dict(
                os.environ,
                {
                    "HOME": str(isolated),
                    "LIGHTBULB_API": "http://127.0.0.1:9",
                    "LIGHTBULB_CONNECT_DEBUG_NO_DEFAULT_RELAYS": "1",
                    "LIGHTBULB_CONNECT_DEBUG_NO_TELEMETRY": "1",
                },
            ),
            patch(
                "shutil.which", side_effect=lambda name: str(binary) if name == "lightbulb" else original_which(name)
            ),
            patch("core.services.sqs_drainer.create_agent_queue", return_value=None),
            ManagedMember(isolated / "town-home", api) as member,
        ):
            member.check_support(api)
            key = member.identity()
            raw_key = base64.b32decode(key.upper() + "====")
            signed = []

            class ReleasedSigner:
                def public_key(self):
                    return self

                def public_bytes(self, *_):
                    return raw_key

                def sign(self, payload):
                    # The released member validates the exact canonical GooseTown
                    # challenge and audience before signing with its private key.
                    signature = member.prove(base64.urlsafe_b64encode(payload).decode().rstrip("="))
                    signed.append(True)
                    return base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))

            fixture = device.__wrapped__(db, mp)
            with patch.object(Ed25519PrivateKey, "generate", return_value=ReleasedSigner()):
                test_scoped_device_binds_and_arrives_only_after_its_proven_connection(fixture, mp)
            assert len(signed) == 2
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
                        ],
                        "synthetic": ["Clerk owner", "Moto AWS", "game-server transport"],
                        "checks": [
                            "actor-scoped credential",
                            "replayed challenge denied",
                            "arrival only after this proof+connect",
                            "raw other-owner apartment command denied",
                            "Exchange/upload authority denied",
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
