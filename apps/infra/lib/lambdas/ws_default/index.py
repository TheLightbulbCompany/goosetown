"""WebSocket $default route handler.

Processes inbound agent messages and writes to DynamoDB.
Pushes responses back via the API Gateway Management API.

Message types: town_agent_connect, town_agent_act, town_agent_sleep
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Tables
dynamodb = boto3.resource("dynamodb")
connections_table = dynamodb.Table(os.environ["CONNECTIONS_TABLE"])
agents_table = dynamodb.Table(os.environ["AGENTS_TABLE"])
conversations_table = dynamodb.Table(os.environ["CONVERSATIONS_TABLE"])
relationships_table = dynamodb.Table(os.environ["RELATIONSHIPS_TABLE"])

# Secrets client for town token verification
secrets_client = boto3.client("secretsmanager")
_secret_cache = {}

# Management API client (lazy init)
_mgmt_client = None

# Location constants (duplicated from backend to keep Lambda self-contained)
TOWN_LOCATIONS = {
    "plaza": {"x": 18.0, "y": 12.0},
    "library": {"x": 25.0, "y": 15.0},
    "cafe": {"x": 22.0, "y": 20.0},
    "activity_center": {"x": 16.0, "y": 16.0},
    "residence": {"x": 30.0, "y": 8.0},
}

APARTMENT_SPOTS = {
    "desk_1": {"x": 2, "y": 1},
    "desk_2": {"x": 4, "y": 1},
    "desk_3": {"x": 3, "y": 2},
    "couch_1": {"x": 2, "y": 6},
    "couch_2": {"x": 3, "y": 6},
    "tv_chair": {"x": 2, "y": 5},
    "bed_1": {"x": 9, "y": 6},
    "bed_2": {"x": 10, "y": 6},
    "table": {"x": 8, "y": 2},
    "bookshelf": {"x": 10, "y": 1},
    "exit": {"x": 6, "y": 7},
}

RESIDENTIAL_TOWN_COORDS = {"x": 69.0, "y": 25.0}


def _get_secret(name):
    if name not in _secret_cache:
        resp = secrets_client.get_secret_value(SecretId=name)
        _secret_cache[name] = resp["SecretString"]
    return _secret_cache[name]


def _b64url_decode(s):
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


def _verify_town_token(token):
    """Verify HMAC-signed town token. Returns payload dict or None."""
    parts = token.split(".")
    if len(parts) != 2:
        return None
    payload_b64, sig_b64 = parts
    try:
        secret = _get_secret(os.environ["TOWN_TOKEN_SECRET"])
        expected_sig = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest()
        actual_sig = _b64url_decode(sig_b64)
    except Exception:
        return None
    if not hmac.compare_digest(expected_sig, actual_sig):
        return None
    try:
        payload = json.loads(_b64url_decode(payload_b64))
        if "uid" not in payload or "iid" not in payload:
            return None
        return payload
    except Exception:
        return None


def _get_mgmt_client(domain_name, stage):
    global _mgmt_client
    if _mgmt_client is None:
        endpoint = f"https://{domain_name}/{stage}"
        _mgmt_client = boto3.client(
            "apigatewaymanagementapi",
            endpoint_url=endpoint,
        )
    return _mgmt_client


def _push(mgmt, connection_id, payload):
    """Send a message to a WebSocket connection. Returns True on success."""
    try:
        mgmt.post_to_connection(
            ConnectionId=connection_id,
            Data=json.dumps(payload).encode("utf-8"),
        )
        return True
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "GoneException":
            logger.warning("Connection %s is gone", connection_id)
        else:
            logger.error("Failed to push to %s: %s", connection_id, e)
        return False


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _to_decimal(val):
    """Convert float to Decimal for DynamoDB."""
    if isinstance(val, float):
        return Decimal(str(val))
    return val


def _get_agent_by_name(user_id, agent_name):
    """Look up active agent by userId + agentName."""
    resp = agents_table.query(
        IndexName="userId-agentName-index",
        KeyConditionExpression=boto3.dynamodb.conditions.Key("userId").eq(user_id)
        & boto3.dynamodb.conditions.Key("agentName").eq(agent_name),
    )
    items = resp.get("Items", [])
    for item in items:
        if item.get("isActive") == "true":
            return item
    return None


def _get_agent_by_connection(connection_id):
    """Look up agent from a connectionId via connections table."""
    resp = connections_table.get_item(Key={"connectionId": connection_id})
    conn = resp.get("Item")
    if not conn:
        return None
    agent_name = conn.get("agentName")
    user_id = conn.get("userId")
    if not agent_name or not user_id:
        return None
    return _get_agent_by_name(user_id, agent_name)


def _update_agent(agent_id, **kwargs):
    """Update agent state fields in DynamoDB."""
    field_map = {
        "last_heartbeat_at": "lastHeartbeatAt",
        "location_state": "locationState",
        "target_x": "targetX",
        "target_y": "targetY",
        "target_location": "targetLocation",
        "current_activity": "currentActivity",
        "location_context": "locationContext",
        "current_conversation_id": "currentConversationId",
        "wake_at": "wakeAt",
        "wake_timezone": "wakeTimezone",
    }
    parts, vals, names = [], {}, {}
    for i, (k, v) in enumerate(kwargs.items()):
        dk = field_map.get(k, k)
        pv, pn = f":v{i}", f"#n{i}"
        parts.append(f"{pn} = {pv}")
        vals[pv] = _to_decimal(v)
        names[pn] = dk
    if not parts:
        return
    agents_table.update_item(
        Key={"id": agent_id},
        UpdateExpression="SET " + ", ".join(parts),
        ExpressionAttributeValues=vals,
        ExpressionAttributeNames=names,
    )


def _resolve_destination(destination):
    """Resolve destination name to coordinates."""
    if destination in TOWN_LOCATIONS:
        loc = TOWN_LOCATIONS[destination]
        return {"x": loc["x"], "y": loc["y"], "ctx": "town", "loc": destination}
    if destination in APARTMENT_SPOTS:
        spot = APARTMENT_SPOTS[destination]
        return {"x": float(spot["x"]), "y": float(spot["y"]), "ctx": "apartment", "loc": destination}
    if destination in ("home", "apartment"):
        return {"x": RESIDENTIAL_TOWN_COORDS["x"], "y": RESIDENTIAL_TOWN_COORDS["y"], "ctx": "town", "loc": "residence"}
    return None


def _get_agent_connection_id(agent_name):
    """Find connectionId for an agent."""
    resp = connections_table.query(
        IndexName="agentName-index",
        KeyConditionExpression=boto3.dynamodb.conditions.Key("agentName").eq(agent_name),
    )
    items = resp.get("Items", [])
    if not items:
        return None
    if len(items) > 1:
        items.sort(key=lambda x: x.get("connectedAt", "0"), reverse=True)
    return items[0]["connectionId"]


def handler(event, context):
    connection_id = event["requestContext"]["connectionId"]
    domain = event["requestContext"]["domainName"]
    stage = event["requestContext"]["stage"]
    body_str = event.get("body", "{}")

    try:
        body = json.loads(body_str)
    except Exception:
        logger.warning("Invalid JSON body: %s", body_str[:200])
        return {"statusCode": 200}

    msg_type = body.get("type", "")
    logger.info("WS message: type=%s conn=%s", msg_type, connection_id)

    mgmt = _get_mgmt_client(domain, stage)

    if msg_type == "town_agent_connect":
        _handle_connect(mgmt, body, connection_id)
    elif msg_type == "town_agent_act":
        _handle_act(mgmt, body, connection_id)
    elif msg_type == "town_agent_sleep":
        _handle_sleep(mgmt, body, connection_id)
    else:
        logger.warning("Unknown message type: %s", msg_type)

    return {"statusCode": 200}


def _handle_connect(mgmt, body, connection_id):
    agent_name = body.get("agent_name")
    token = body.get("token")

    if not agent_name or not token:
        _push(mgmt, connection_id, {"type": "town_event", "event": "error", "message": "Missing agent_name or token"})
        return

    payload = _verify_town_token(token)
    if not payload:
        _push(mgmt, connection_id, {"type": "town_event", "event": "error", "message": "Invalid token"})
        return

    user_id = payload.get("uid")
    agent = _get_agent_by_name(user_id, agent_name)
    if not agent:
        _push(mgmt, connection_id, {"type": "town_event", "event": "error", "message": f"Agent '{agent_name}' not found"})
        return

    _update_agent(agent["id"], last_heartbeat_at=_now_iso(), location_state="active")

    _push(mgmt, connection_id, {
        "type": "town_event",
        "event": "connected",
        "agent": {
            "agent_name": agent.get("agentName"),
            "display_name": agent.get("displayName"),
            "position": [float(agent.get("positionX", 0)), float(agent.get("positionY", 0))],
            "location": agent.get("currentLocation"),
            "location_context": agent.get("locationContext", "apartment"),
            "activity": agent.get("currentActivity", "idle"),
            "mood": agent.get("mood"),
            "energy": int(agent.get("energy", 100)),
        },
    })
    logger.info("Agent %s connected", agent_name)


def _handle_act(mgmt, body, connection_id):
    agent = _get_agent_by_connection(connection_id)
    if not agent:
        _push(mgmt, connection_id, {"type": "town_event", "event": "error", "message": "Agent not found"})
        return

    action = body.get("action")
    agent_id = agent["id"]
    _update_agent(agent_id, last_heartbeat_at=_now_iso())

    if action == "move":
        dest = body.get("destination", "")
        resolved = _resolve_destination(dest)
        if not resolved:
            _push(mgmt, connection_id, {"type": "town_event", "event": "error", "message": f"Unknown destination: {dest}"})
            return
        _update_agent(agent_id,
            target_x=resolved["x"], target_y=resolved["y"],
            target_location=resolved["loc"], current_activity="walking",
            location_context=resolved["ctx"])
        _push(mgmt, connection_id, {"type": "town_event", "event": "act_ok", "action": "move", "destination": dest})

    elif action == "chat":
        _handle_chat(mgmt, body, agent, connection_id)

    elif action == "say":
        _handle_say(mgmt, body, agent, connection_id)

    elif action == "idle":
        activity = body.get("activity", "idle")
        _update_agent(agent_id, current_activity=activity)
        _push(mgmt, connection_id, {"type": "town_event", "event": "act_ok", "action": "idle", "activity": activity})

    elif action == "end_conversation":
        _handle_end_conversation(mgmt, body, agent, connection_id)

    else:
        _push(mgmt, connection_id, {"type": "town_event", "event": "error", "message": f"Unknown action: {action}"})


def _handle_chat(mgmt, body, agent, connection_id):
    target_name = body.get("target", "")
    message = body.get("message", "")
    if not target_name or not message:
        _push(mgmt, connection_id, {"type": "town_event", "event": "error", "message": "Missing target or message"})
        return

    # Find target agent by scanning active agents
    resp = agents_table.query(
        IndexName="isActive-joinedAt-index",
        KeyConditionExpression=boto3.dynamodb.conditions.Key("isActive").eq("true"),
    )
    target_agent = None
    for a in resp.get("Items", []):
        if a.get("agentName") == target_name:
            target_agent = a
            break

    if not target_agent:
        _push(mgmt, connection_id, {"type": "town_event", "event": "error", "message": f"Agent '{target_name}' not found"})
        return

    conv_id = str(uuid.uuid4())
    now = _now_iso()
    display = agent.get("displayName", agent.get("agentName", "Unknown"))

    conversations_table.put_item(Item={
        "id": conv_id,
        "participantAId": agent["id"],
        "participantBId": target_agent["id"],
        "location": agent.get("currentLocation", "unknown"),
        "turnCount": 1,
        "publicLog": [{"speaker": display, "text": message}],
        "status": "active",
        "startedAt": now,
        "updatedAt": now,
    })

    _update_agent(agent["id"], current_conversation_id=conv_id)
    _update_agent(target_agent["id"], current_conversation_id=conv_id)

    _push(mgmt, connection_id, {"type": "town_event", "event": "act_ok", "action": "chat", "conv_id": conv_id})

    target_conn = _get_agent_connection_id(target_name)
    if target_conn:
        _push(mgmt, target_conn, {
            "type": "town_event", "event": "conversation_invite",
            "conv_id": conv_id, "from": display,
            "from_agent_name": agent.get("agentName"), "message": message,
        })


def _handle_say(mgmt, body, agent, connection_id):
    conv_id = body.get("conv_id", "")
    message = body.get("message", "")
    if not conv_id or not message:
        _push(mgmt, connection_id, {"type": "town_event", "event": "error", "message": "Missing conv_id or message"})
        return

    resp = conversations_table.get_item(Key={"id": conv_id})
    conv = resp.get("Item")
    if not conv:
        _push(mgmt, connection_id, {"type": "town_event", "event": "error", "message": f"Conversation {conv_id} not found"})
        return

    display = agent.get("displayName", agent.get("agentName", "Unknown"))
    log = conv.get("publicLog", [])
    log.append({"speaker": display, "text": message})
    now = _now_iso()

    conversations_table.update_item(
        Key={"id": conv_id},
        UpdateExpression="SET publicLog = :log, turnCount = :tc, updatedAt = :now",
        ExpressionAttributeValues={":log": log, ":tc": len(log), ":now": now},
    )

    _push(mgmt, connection_id, {"type": "town_event", "event": "act_ok", "action": "say", "conv_id": conv_id})

    other_id = conv.get("participantBId") if conv.get("participantAId") == agent["id"] else conv.get("participantAId")
    if other_id:
        other_resp = agents_table.get_item(Key={"id": other_id})
        other = other_resp.get("Item")
        if other:
            other_conn = _get_agent_connection_id(other.get("agentName"))
            if other_conn:
                _push(mgmt, other_conn, {
                    "type": "town_event", "event": "conversation_message",
                    "conv_id": conv_id, "from": display,
                    "from_agent_name": agent.get("agentName"),
                    "text": message, "turn": len(log),
                })


def _handle_end_conversation(mgmt, body, agent, connection_id):
    conv_id = body.get("conv_id", "")
    if not conv_id:
        _push(mgmt, connection_id, {"type": "town_event", "event": "error", "message": "Missing conv_id"})
        return

    resp = conversations_table.get_item(Key={"id": conv_id})
    conv = resp.get("Item")
    if not conv:
        _push(mgmt, connection_id, {"type": "town_event", "event": "error", "message": f"Conversation {conv_id} not found"})
        return

    now = _now_iso()
    conversations_table.update_item(
        Key={"id": conv_id},
        UpdateExpression="SET #s = :ended, endedAt = :now, updatedAt = :now",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":ended": "ended", ":now": now},
    )

    _update_agent(agent["id"], current_conversation_id=None)

    other_id = conv.get("participantBId") if conv.get("participantAId") == agent["id"] else conv.get("participantAId")
    if other_id:
        _update_agent(other_id, current_conversation_id=None)

        # Update relationship
        a_id, b_id = sorted([agent["id"], other_id])
        rel_resp = relationships_table.get_item(Key={"agentAId": a_id, "agentBId": b_id})
        existing = rel_resp.get("Item", {})
        count = int(existing.get("interactionCount", 0)) + 1
        rel_type = "close_friend" if count >= 10 else "friend" if count >= 5 else "acquaintance"
        log = conv.get("publicLog", [])
        last_topic = log[-1].get("text", "")[:100] if log else None
        relationships_table.put_item(Item={
            "agentAId": a_id, "agentBId": b_id,
            "relationshipType": rel_type, "interactionCount": count,
            "lastTopic": last_topic, "lastInteractionAt": now,
        })

        other_resp = agents_table.get_item(Key={"id": other_id})
        other = other_resp.get("Item")
        if other:
            other_conn = _get_agent_connection_id(other.get("agentName"))
            if other_conn:
                _push(mgmt, other_conn, {"type": "town_event", "event": "conversation_ended", "conv_id": conv_id})

    _push(mgmt, connection_id, {"type": "town_event", "event": "act_ok", "action": "end_conversation", "conv_id": conv_id})


def _handle_sleep(mgmt, body, connection_id):
    agent = _get_agent_by_connection(connection_id)
    if not agent:
        _push(mgmt, connection_id, {"type": "town_event", "event": "error", "message": "Agent not found"})
        return

    wake_time = body.get("wake_time", "")
    tz = body.get("timezone", "UTC")

    _update_agent(agent["id"],
        location_state="going_home",
        wake_at=wake_time, wake_timezone=tz,
        current_activity="going home")

    _push(mgmt, connection_id, {"type": "town_event", "event": "sleep_ok", "wake_time": wake_time, "timezone": tz})
    logger.info("Agent %s sleeping, wake at %s %s", agent.get("agentName"), wake_time, tz)
