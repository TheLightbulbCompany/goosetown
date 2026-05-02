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

def handler(event, context):
    connection_id = event["requestContext"]["connectionId"]
    logger.info("Disconnect: %s", connection_id)

    table.delete_item(Key={"connectionId": connection_id})

    # Notify backend to clean up agent from game server and DynamoDB
    if BACKEND_URL:
        payload = json.dumps({"connectionId": connection_id}).encode("utf-8")
        url = f"{BACKEND_URL}/api/v1/town/ws-disconnect"
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                logger.info("Backend cleanup: %s", resp.status)
        except Exception as e:
            logger.warning("Backend cleanup failed: %s", e)

    return {"statusCode": 200, "body": "Disconnected"}
