import json
import logging
import os
import urllib.request
import urllib.error

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

BACKEND_URL = os.environ["BACKEND_URL"].rstrip("/")
# Name of the Secrets Manager secret holding the backend shared secret (#427).
WS_LAMBDA_SECRET_NAME = os.environ.get("WS_LAMBDA_SECRET_NAME", "")

_secrets_client = boto3.client("secretsmanager")
_secret_cache = {}


def _ws_lambda_secret():
    """Fetch (and cache for the container lifetime) the backend shared secret.

    Cached at module scope so warm invocations don't re-hit Secrets Manager.
    A fetch failure returns "" — the backend then answers 403 and we log it,
    which is the correct fail-closed direction for an auth credential.
    """
    if not WS_LAMBDA_SECRET_NAME:
        return ""
    if WS_LAMBDA_SECRET_NAME not in _secret_cache:
        try:
            resp = _secrets_client.get_secret_value(SecretId=WS_LAMBDA_SECRET_NAME)
            _secret_cache[WS_LAMBDA_SECRET_NAME] = resp["SecretString"]
        except Exception as e:
            logger.error("Failed to fetch WS lambda secret: %s", e)
            return ""
    return _secret_cache[WS_LAMBDA_SECRET_NAME]

def handler(event, context):
    connection_id = event["requestContext"]["connectionId"]
    authorizer_ctx = event.get("requestContext", {}).get("authorizer", {})
    principal_id = authorizer_ctx.get("principalId", "unknown")
    body = event.get("body", "")

    logger.info("Default route: connection=%s principal=%s body=%s", connection_id, principal_id, body[:200])

    payload = json.dumps({
        "connectionId": connection_id,
        "principalId": principal_id,
        "body": json.loads(body) if body else {},
    }).encode("utf-8")

    url = f"{BACKEND_URL}/api/v1/town/ws-message"
    headers = {"Content-Type": "application/json"}
    secret = _ws_lambda_secret()
    if secret:
        headers["x-ws-lambda-secret"] = secret
    req = urllib.request.Request(
        url,
        data=payload,
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp_body = resp.read().decode("utf-8")
            logger.info("Backend responded: %s %s", resp.status, resp_body[:200])
        return {"statusCode": 200}
    except urllib.error.HTTPError as e:
        logger.error("Backend HTTP error: %s %s", e.code, e.read().decode("utf-8")[:500])
        return {"statusCode": 500}
    except Exception as e:
        logger.error("Backend request failed: %s", e)
        return {"statusCode": 500}
