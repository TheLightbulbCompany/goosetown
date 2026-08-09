import json
import logging
import os
import urllib.request
import urllib.error

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["CONNECTIONS_TABLE"])
BACKEND_URL = os.environ.get("BACKEND_URL", "").rstrip("/")
# Name of the Secrets Manager secret holding the backend shared secret (#427).
WS_LAMBDA_SECRET_NAME = os.environ.get("WS_LAMBDA_SECRET_NAME", "")

_secrets_client = boto3.client("secretsmanager")
_secret_cache = {}


def _ws_lambda_secret():
    """Fetch (and cache for the container lifetime) the backend shared secret."""
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
    logger.info("Disconnect: %s", connection_id)

    table.delete_item(Key={"connectionId": connection_id})

    # Notify backend to clean up agent from game server and DynamoDB
    if BACKEND_URL:
        payload = json.dumps({"connectionId": connection_id}).encode("utf-8")
        url = f"{BACKEND_URL}/api/v1/town/ws-disconnect"
        headers = {"Content-Type": "application/json"}
        secret = _ws_lambda_secret()
        if secret:
            headers["x-ws-lambda-secret"] = secret
        req = urllib.request.Request(
            url, data=payload,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                logger.info("Backend cleanup: %s", resp.status)
        except Exception as e:
            logger.warning("Backend cleanup failed: %s", e)

    return {"statusCode": 200, "body": "Disconnected"}
