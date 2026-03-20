"""WebSocket message handler — processes inbound agent messages from API Gateway.

API Gateway WebSocket $default route forwards agent messages here via VPC Link → NLB.
The request arrives as an HTTP POST with the WebSocket frame in the body and the
connectionId in a header.

Message types handled:
  - town_agent_connect: Agent connecting, send back current state
  - town_agent_act: Agent performing an action (move, chat, say, idle, end)
  - town_agent_sleep: Agent going to sleep
"""

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request

from core.apartment_constants import APARTMENT_SPOTS, RESIDENTIAL_TOWN_COORDS
from core.services.management_api_client import ManagementApiClient, ManagementApiClientError
from core.services.town_service import TownService
from core.services.ws_connection_service import WsConnectionService
from core.town_constants import TOWN_LOCATIONS

logger = logging.getLogger(__name__)
router = APIRouter()

# Lazy singleton for Management API client
_mgmt_client: Optional[ManagementApiClient] = None
_mgmt_client_failed = False


def _get_mgmt_client() -> Optional[ManagementApiClient]:
    global _mgmt_client, _mgmt_client_failed
    if _mgmt_client is not None:
        return _mgmt_client
    if _mgmt_client_failed:
        return None
    try:
        _mgmt_client = ManagementApiClient()
        return _mgmt_client
    except Exception:
        logger.debug("ManagementApiClient not available (expected in local dev)")
        _mgmt_client_failed = True
        return None


def _push_to_connection(connection_id: str, payload: dict) -> bool:
    """Push a message to a WebSocket connection. Returns True on success."""
    mgmt = _get_mgmt_client()
    if mgmt is None:
        logger.debug("No mgmt client, cannot push to %s", connection_id)
        return False
    try:
        return mgmt.send_message(connection_id, payload)
    except ManagementApiClientError as e:
        logger.warning("Failed to push to %s: %s", connection_id, e)
        return False


def _resolve_destination(destination: str, location_context: str) -> Optional[dict]:
    """Resolve a destination name to coordinates and metadata.

    Returns dict with x, y, location_context, current_location or None.
    """
    # Check town locations
    if destination in TOWN_LOCATIONS:
        loc = TOWN_LOCATIONS[destination]
        return {
            "x": loc["x"],
            "y": loc["y"],
            "location_context": "town",
            "current_location": destination,
        }

    # Check apartment spots
    if destination in APARTMENT_SPOTS:
        spot = APARTMENT_SPOTS[destination]
        return {
            "x": float(spot["x"]),
            "y": float(spot["y"]),
            "location_context": "apartment",
            "current_location": destination,
        }

    # Special: "residence" when in town means go to apartment entrance
    if destination == "home" or destination == "apartment":
        return {
            "x": RESIDENTIAL_TOWN_COORDS["x"],
            "y": RESIDENTIAL_TOWN_COORDS["y"],
            "location_context": "town",
            "current_location": "residence",
        }

    return None


def _lookup_agent_from_connection(
    conn_svc: WsConnectionService, service: TownService, connection_id: str
) -> Optional[dict]:
    """Look up agent state from a connectionId."""
    conn = conn_svc.get_by_connection_id(connection_id)
    if not conn:
        return None

    agent_name = conn.get("agentName")
    user_id = conn.get("userId")
    if not agent_name or not user_id:
        return None

    agent = service.get_agent_by_name(user_id, agent_name)
    return agent


@router.post("/ws/message")
async def handle_ws_message(
    request: Request,
    connectionid: Optional[str] = Header(None, alias="connectionId"),
):
    """Handle inbound WebSocket messages from agents via API Gateway $default route.

    API Gateway sends POST with:
    - Body: the raw WebSocket message (JSON string)
    - Header connectionId: the API Gateway connection ID
    """
    # Parse the body
    try:
        body = await request.json()
    except Exception:
        raw = await request.body()
        try:
            body = json.loads(raw)
        except Exception:
            raise HTTPException(400, "Invalid JSON body")

    msg_type = body.get("type", "")
    logger.info("WS message: type=%s, connectionId=%s", msg_type, connectionid)

    if msg_type == "town_agent_connect":
        return await _handle_agent_connect(body, connectionid)
    elif msg_type == "town_agent_act":
        return await _handle_agent_act(body, connectionid)
    elif msg_type == "town_agent_sleep":
        return await _handle_agent_sleep(body, connectionid)
    else:
        logger.warning("Unknown WS message type: %s", msg_type)
        return {"statusCode": 200}


async def _handle_agent_connect(body: dict, connection_id: Optional[str]):
    """Handle town_agent_connect — agent declaring it's online."""
    agent_name = body.get("agent_name")
    token = body.get("token")

    if not agent_name or not token:
        if connection_id:
            _push_to_connection(connection_id, {
                "type": "town_event",
                "event": "error",
                "message": "Missing agent_name or token",
            })
        return {"statusCode": 200}

    # Verify token and look up agent
    from core.town_token import verify_town_token

    payload = verify_town_token(token)
    if payload is None:
        if connection_id:
            _push_to_connection(connection_id, {
                "type": "town_event",
                "event": "error",
                "message": "Invalid token",
            })
        return {"statusCode": 200}

    user_id = payload.get("uid")
    service = TownService()
    agent = service.get_agent_by_name(user_id, agent_name)

    if not agent:
        if connection_id:
            _push_to_connection(connection_id, {
                "type": "town_event",
                "event": "error",
                "message": f"Agent '{agent_name}' not found",
            })
        return {"statusCode": 200}

    # Update heartbeat
    now_iso = datetime.now(timezone.utc).isoformat()
    service.update_agent_state(
        agent["id"],
        last_heartbeat_at=now_iso,
        location_state="active",
    )

    # Push connected event with current state
    if connection_id:
        _push_to_connection(connection_id, {
            "type": "town_event",
            "event": "connected",
            "agent": {
                "agent_name": agent.get("agentName"),
                "display_name": agent.get("displayName"),
                "position": [
                    agent.get("positionX", 0),
                    agent.get("positionY", 0),
                ],
                "location": agent.get("currentLocation"),
                "location_context": agent.get("locationContext", "apartment"),
                "activity": agent.get("currentActivity", "idle"),
                "mood": agent.get("mood"),
                "energy": agent.get("energy", 100),
            },
        })

    logger.info("Agent %s connected (connectionId=%s)", agent_name, connection_id)
    return {"statusCode": 200}


async def _handle_agent_act(body: dict, connection_id: Optional[str]):
    """Handle town_agent_act — agent performing an action."""
    if not connection_id:
        return {"statusCode": 200}

    conn_svc = WsConnectionService()
    service = TownService()
    agent = _lookup_agent_from_connection(conn_svc, service, connection_id)

    if not agent:
        _push_to_connection(connection_id, {
            "type": "town_event",
            "event": "error",
            "message": "Agent not found for this connection",
        })
        return {"statusCode": 200}

    action = body.get("action")
    agent_id = agent["id"]
    agent_name = agent.get("agentName")
    now_iso = datetime.now(timezone.utc).isoformat()

    # Update heartbeat on every action
    service.update_agent_state(agent_id, last_heartbeat_at=now_iso)

    if action == "move":
        return _handle_move(body, agent, service, connection_id)
    elif action == "chat":
        return _handle_chat(body, agent, service, conn_svc, connection_id)
    elif action == "say":
        return _handle_say(body, agent, service, conn_svc, connection_id)
    elif action == "idle":
        return _handle_idle(body, agent, service, connection_id)
    elif action == "end_conversation":
        return _handle_end_conversation(body, agent, service, conn_svc, connection_id)
    else:
        _push_to_connection(connection_id, {
            "type": "town_event",
            "event": "error",
            "message": f"Unknown action: {action}",
        })
        return {"statusCode": 200}


def _handle_move(body: dict, agent: dict, service: TownService, connection_id: str):
    """Handle move action — set target coordinates for simulation to interpolate."""
    destination = body.get("destination", "")
    agent_id = agent["id"]
    location_context = agent.get("locationContext", "apartment")

    resolved = _resolve_destination(destination, location_context)
    if not resolved:
        _push_to_connection(connection_id, {
            "type": "town_event",
            "event": "error",
            "message": f"Unknown destination: {destination}",
        })
        return {"statusCode": 200}

    # If crossing context boundary (apartment → town or vice versa),
    # agent needs to go to the transition point first
    target_context = resolved["location_context"]

    service.update_agent_state(
        agent_id,
        target_x=resolved["x"],
        target_y=resolved["y"],
        target_location=resolved["current_location"],
        current_activity="walking",
        location_context=target_context,
    )

    _push_to_connection(connection_id, {
        "type": "town_event",
        "event": "act_ok",
        "action": "move",
        "destination": destination,
    })

    logger.info("Agent %s moving to %s", agent.get("agentName"), destination)
    return {"statusCode": 200}


def _handle_chat(
    body: dict, agent: dict, service: TownService,
    conn_svc: WsConnectionService, connection_id: str,
):
    """Handle chat action — start a conversation with another agent."""
    target_name = body.get("target", "")
    message = body.get("message", "")
    agent_id = agent["id"]
    agent_display = agent.get("displayName", agent.get("agentName", "Unknown"))

    if not target_name or not message:
        _push_to_connection(connection_id, {
            "type": "town_event",
            "event": "error",
            "message": "Missing target or message for chat",
        })
        return {"statusCode": 200}

    # Find target agent — scan active agents for the target name
    all_agents = service.get_active_agents()
    target_agent = None
    for a in all_agents:
        if a.get("agentName") == target_name:
            target_agent = a
            break

    if not target_agent:
        _push_to_connection(connection_id, {
            "type": "town_event",
            "event": "error",
            "message": f"Agent '{target_name}' not found or not active",
        })
        return {"statusCode": 200}

    target_id = target_agent["id"]

    # Create conversation
    conv_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()

    from core import dynamo
    conv_table = dynamo.conversations_table()
    conv_table.put_item(Item={
        "id": conv_id,
        "participantAId": agent_id,
        "participantBId": target_id,
        "location": agent.get("currentLocation", "unknown"),
        "turnCount": 1,
        "publicLog": [{"speaker": agent_display, "text": message}],
        "status": "active",
        "startedAt": now_iso,
        "updatedAt": now_iso,
    })

    # Update both agents' conversation state
    service.update_agent_state(agent_id, current_conversation_id=conv_id)
    service.update_agent_state(target_id, current_conversation_id=conv_id)

    # Ack to sender
    _push_to_connection(connection_id, {
        "type": "town_event",
        "event": "act_ok",
        "action": "chat",
        "conv_id": conv_id,
    })

    # Push invite to target agent
    target_conn_id = conn_svc.get_agent_connection_id(target_name)
    if target_conn_id:
        _push_to_connection(target_conn_id, {
            "type": "town_event",
            "event": "conversation_invite",
            "conv_id": conv_id,
            "from": agent_display,
            "from_agent_name": agent.get("agentName"),
            "message": message,
        })

    logger.info("Agent %s started chat with %s (conv=%s)", agent.get("agentName"), target_name, conv_id)
    return {"statusCode": 200}


def _handle_say(
    body: dict, agent: dict, service: TownService,
    conn_svc: WsConnectionService, connection_id: str,
):
    """Handle say action — send a message in an existing conversation."""
    conv_id = body.get("conv_id", "")
    message = body.get("message", "")
    agent_id = agent["id"]
    agent_display = agent.get("displayName", agent.get("agentName", "Unknown"))

    if not conv_id or not message:
        _push_to_connection(connection_id, {
            "type": "town_event",
            "event": "error",
            "message": "Missing conv_id or message for say",
        })
        return {"statusCode": 200}

    # Get conversation
    from core import dynamo
    conv_table = dynamo.conversations_table()
    resp = conv_table.get_item(Key={"id": conv_id})
    conv = resp.get("Item")

    if not conv:
        _push_to_connection(connection_id, {
            "type": "town_event",
            "event": "error",
            "message": f"Conversation {conv_id} not found",
        })
        return {"statusCode": 200}

    # Add message to log
    public_log = conv.get("publicLog", [])
    public_log.append({"speaker": agent_display, "text": message})
    now_iso = datetime.now(timezone.utc).isoformat()

    conv_table.update_item(
        Key={"id": conv_id},
        UpdateExpression="SET publicLog = :log, turnCount = :tc, updatedAt = :now",
        ExpressionAttributeValues={
            ":log": public_log,
            ":tc": len(public_log),
            ":now": now_iso,
        },
    )

    # Ack to sender
    _push_to_connection(connection_id, {
        "type": "town_event",
        "event": "act_ok",
        "action": "say",
        "conv_id": conv_id,
    })

    # Push message to other participant
    other_id = conv.get("participantBId") if conv.get("participantAId") == agent_id else conv.get("participantAId")
    if other_id:
        other_agent = service.get_town_agent_by_id(other_id)
        if other_agent:
            other_name = other_agent.get("agentName")
            other_conn_id = conn_svc.get_agent_connection_id(other_name)
            if other_conn_id:
                _push_to_connection(other_conn_id, {
                    "type": "town_event",
                    "event": "conversation_message",
                    "conv_id": conv_id,
                    "from": agent_display,
                    "from_agent_name": agent.get("agentName"),
                    "text": message,
                    "turn": len(public_log),
                })

    return {"statusCode": 200}


def _handle_idle(body: dict, agent: dict, service: TownService, connection_id: str):
    """Handle idle action — set activity description."""
    activity = body.get("activity", "idle")
    service.update_agent_state(agent["id"], current_activity=activity)

    _push_to_connection(connection_id, {
        "type": "town_event",
        "event": "act_ok",
        "action": "idle",
        "activity": activity,
    })

    return {"statusCode": 200}


def _handle_end_conversation(
    body: dict, agent: dict, service: TownService,
    conn_svc: WsConnectionService, connection_id: str,
):
    """Handle end_conversation action — close an active conversation."""
    conv_id = body.get("conv_id", "")
    agent_id = agent["id"]

    if not conv_id:
        _push_to_connection(connection_id, {
            "type": "town_event",
            "event": "error",
            "message": "Missing conv_id for end_conversation",
        })
        return {"statusCode": 200}

    from core import dynamo
    conv_table = dynamo.conversations_table()
    resp = conv_table.get_item(Key={"id": conv_id})
    conv = resp.get("Item")

    if not conv:
        _push_to_connection(connection_id, {
            "type": "town_event",
            "event": "error",
            "message": f"Conversation {conv_id} not found",
        })
        return {"statusCode": 200}

    now_iso = datetime.now(timezone.utc).isoformat()

    # Mark conversation as ended
    conv_table.update_item(
        Key={"id": conv_id},
        UpdateExpression="SET #s = :ended, endedAt = :now, updatedAt = :now",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":ended": "ended", ":now": now_iso},
    )

    # Clear conversation ID from both participants
    service.update_agent_state(agent_id, current_conversation_id=None)

    other_id = conv.get("participantBId") if conv.get("participantAId") == agent_id else conv.get("participantAId")
    if other_id:
        service.update_agent_state(other_id, current_conversation_id=None)

        # Update relationship
        _update_relationship(service, agent_id, other_id, conv)

        # Notify other participant
        other_agent = service.get_town_agent_by_id(other_id)
        if other_agent:
            other_name = other_agent.get("agentName")
            other_conn_id = conn_svc.get_agent_connection_id(other_name)
            if other_conn_id:
                _push_to_connection(other_conn_id, {
                    "type": "town_event",
                    "event": "conversation_ended",
                    "conv_id": conv_id,
                })

    # Ack to sender
    _push_to_connection(connection_id, {
        "type": "town_event",
        "event": "act_ok",
        "action": "end_conversation",
        "conv_id": conv_id,
    })

    logger.info("Conversation %s ended by %s", conv_id, agent.get("agentName"))
    return {"statusCode": 200}


def _update_relationship(service: TownService, agent_a_id: str, agent_b_id: str, conv: dict):
    """Update the relationship between two agents after a conversation."""
    from core import dynamo

    # Canonical order
    a_id, b_id = sorted([agent_a_id, agent_b_id])
    table = dynamo.relationships_table()

    # Get existing relationship
    resp = table.get_item(Key={"agentAId": a_id, "agentBId": b_id})
    existing = resp.get("Item", {})

    interaction_count = int(existing.get("interactionCount", 0)) + 1
    now_iso = datetime.now(timezone.utc).isoformat()

    # Determine relationship type based on interaction count
    if interaction_count >= 10:
        rel_type = "close_friend"
    elif interaction_count >= 5:
        rel_type = "friend"
    elif interaction_count >= 2:
        rel_type = "acquaintance"
    else:
        rel_type = "acquaintance"

    # Get last topic from conversation log
    public_log = conv.get("publicLog", [])
    last_topic = public_log[-1].get("text", "")[:100] if public_log else None

    table.put_item(Item={
        "agentAId": a_id,
        "agentBId": b_id,
        "relationshipType": rel_type,
        "interactionCount": interaction_count,
        "lastTopic": last_topic,
        "lastInteractionAt": now_iso,
    })


async def _handle_agent_sleep(body: dict, connection_id: Optional[str]):
    """Handle town_agent_sleep — agent going to sleep."""
    if not connection_id:
        return {"statusCode": 200}

    wake_time = body.get("wake_time", "")
    tz = body.get("timezone", "UTC")

    conn_svc = WsConnectionService()
    service = TownService()
    agent = _lookup_agent_from_connection(conn_svc, service, connection_id)

    if not agent:
        _push_to_connection(connection_id, {
            "type": "town_event",
            "event": "error",
            "message": "Agent not found for this connection",
        })
        return {"statusCode": 200}

    agent_id = agent["id"]

    # Set agent to going_home state — simulation will handle movement to bed
    service.update_agent_state(
        agent_id,
        location_state="going_home",
        wake_at=wake_time,
        wake_timezone=tz,
        current_activity="going home",
    )

    _push_to_connection(connection_id, {
        "type": "town_event",
        "event": "sleep_ok",
        "wake_time": wake_time,
        "timezone": tz,
    })

    logger.info("Agent %s going to sleep, wake at %s %s", agent.get("agentName"), wake_time, tz)
    return {"statusCode": 200}
