"""Pydantic discriminated-union schema for WebSocket messages forwarded from
API Gateway to ``POST /ws-message``.

Replaces the previous untyped ``dict`` body on ``WsMessageRequest``. FastAPI
validates the payload at the request boundary; handlers receive a typed
object and pattern-match on the concrete class.

Adding a new message type:
    1. Add a Pydantic class with ``type: Literal["..."]``.
    2. Add it to the ``WsMessage`` union.
    3. Add a handler branch in ``routers.town.handle_ws_message``.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Action bodies (nested inside TownAgentAct)
# ---------------------------------------------------------------------------


class MoveAction(BaseModel):
    action: Literal["move"]
    target: str


class ChatAction(BaseModel):
    action: Literal["chat"]
    target_agent_id: str


class SayAction(BaseModel):
    action: Literal["say"]
    text: str = Field(max_length=500)


class IdleAction(BaseModel):
    action: Literal["idle"]


class EndConversationAction(BaseModel):
    action: Literal["end"]


class ContextTransitionAction(BaseModel):
    action: Literal["context_transition"]
    target_context: Literal["town", "apartment"]


class ActivityAction(BaseModel):
    action: Literal["activity"]
    activity_id: str


AgentActionBody = Annotated[
    Union[
        MoveAction,
        ChatAction,
        SayAction,
        IdleAction,
        EndConversationAction,
        ContextTransitionAction,
        ActivityAction,
    ],
    Field(discriminator="action"),
]


# ---------------------------------------------------------------------------
# Top-level messages
# ---------------------------------------------------------------------------


class TownAgentConnect(BaseModel):
    type: Literal["town_agent_connect"]
    token: str
    connection_id: str


class TownAgentAct(BaseModel):
    type: Literal["town_agent_act"]
    connection_id: str
    body: AgentActionBody


class TownDisconnect(BaseModel):
    type: Literal["town_disconnect"]
    connection_id: str
    alarm: str | None = None


class ViewerSubscribe(BaseModel):
    type: Literal["town_subscribe"]
    connection_id: str


WsMessage = Annotated[
    Union[TownAgentConnect, TownAgentAct, TownDisconnect, ViewerSubscribe],
    Field(discriminator="type"),
]
