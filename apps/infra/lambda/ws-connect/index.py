import json
import logging
import os
import time

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["CONNECTIONS_TABLE"])

def handler(event, context):
    connection_id = event["requestContext"]["connectionId"]
    # Extract userId from authorizer context if available
    authorizer_ctx = event.get("requestContext", {}).get("authorizer", {})
    principal_id = authorizer_ctx.get("principalId", "unknown")
    query_params = event.get("queryStringParameters") or {}
    connection_type = query_params.get("type", "viewer")

    logger.info("Connect: %s (principal=%s, type=%s)", connection_id, principal_id, connection_type)

    table.put_item(Item={
        "connectionId": connection_id,
        "userId": principal_id,
        "connectionType": connection_type,
        "connectedAt": str(int(time.time())),
    })

    return {"statusCode": 200, "body": "Connected"}
