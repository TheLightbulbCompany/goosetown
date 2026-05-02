import json
import logging
import os
import urllib.request
import urllib.error

logger = logging.getLogger()
logger.setLevel(logging.INFO)

BACKEND_URL = os.environ["BACKEND_URL"].rstrip("/")

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
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
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
