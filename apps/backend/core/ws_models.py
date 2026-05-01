"""Pydantic schema for WebSocket message bodies forwarded from API Gateway
to ``POST /ws-message``.

The body shape is flat: ``{type, ...fields}``. For ``town_agent_act``, the
``action`` field further specifies which fields are required. Validation
catches malformed input at the request boundary before it reaches the
dispatch ladder in ``routers.town.handle_ws_message``.

Adding a new message type:
    1. Add a Pydantic class with ``type: Literal["..."]``.
    2. Add it to the ``WsMessage`` union.
    3. Add a handler branch in ``routers.town.handle_ws_message``.

Adding a new agent action:
    1. Extend the ``Literal`` in ``TownAgentAct.action``.
    2. Add per-action required-field logic in the ``_check_action_fields``
       ``model_validator``.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, model_validator

AgentAction = Literal[
    "move",
    "chat",
    "say",
    "idle",
    "end_conversation",
    "join_conversation",
    "context_transition",
    "activity",
    "reply_owner",
]


class TownAgentConnect(BaseModel):
    type: Literal["town_agent_connect"]
    token: str
    agent_name: str | None = None


class TownAgentAct(BaseModel):
    type: Literal["town_agent_act"]
    action: AgentAction

    # Per-action fields (all optional at parse time; model_validator enforces).
    destination: str | None = None  # move
    target: str | None = None  # chat
    conv_id: str | None = None  # say, end (handler tolerates empty string)
    message: str | None = Field(default=None, max_length=500)  # chat (optional), say
    target_context: Literal["town", "apartment"] | None = None  # context_transition
    activity_id: str | None = None  # activity
    text: str | None = Field(default=None, max_length=2000)  # reply_owner
    ref_message_id: str | None = None  # reply_owner (optional back-reference)

    @model_validator(mode="after")
    def _check_action_fields(self) -> "TownAgentAct":
        if self.action == "move" and not self.destination:
            raise ValueError("move action requires 'destination'")
        if self.action == "chat" and not self.target:
            raise ValueError("chat action requires 'target'")
        if self.action == "say" and not self.message:
            raise ValueError("say action requires 'message'")
        if self.action == "context_transition" and not self.target_context:
            raise ValueError("context_transition action requires 'target_context'")
        if self.action == "activity" and not self.activity_id:
            raise ValueError("activity action requires 'activity_id'")
        if self.action == "reply_owner" and not self.text:
            raise ValueError("reply_owner action requires 'text'")
        return self


class TownDisconnect(BaseModel):
    type: Literal["town_disconnect"]
    alarm: str | None = None


class TownAgentSleep(BaseModel):
    """Clean-leave signal the daemon emits just before closing the socket.

    Distinct from a raw connection drop: $disconnect fires for both, but
    the backend needs to tell them apart so error-path disconnects don't
    silently teleport the agent into apartment/bed.
    """

    type: Literal["town_agent_sleep"]
    wake_time: str | None = None
    timezone: str | None = None


class TownAgentKeepalive(BaseModel):
    """Lightweight keepalive frame. The daemon sends one every 120s to keep
    AWS API Gateway's 10-minute idle clock from closing the WebSocket; this
    class keeps those frames from 422'ing under strict validation."""

    type: Literal["ping"]


class TownSubscribe(BaseModel):
    type: Literal["town_subscribe"]


WsMessage = Annotated[
    Union[
        TownAgentConnect,
        TownAgentAct,
        TownDisconnect,
        TownAgentSleep,
        TownAgentKeepalive,
        TownSubscribe,
    ],
    Field(discriminator="type"),
]
