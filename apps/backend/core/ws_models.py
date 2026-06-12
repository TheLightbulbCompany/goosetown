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
    "set_goal",
    "complete_goal",
    "describe_stance",
    # Exchange verbs (ADR-0007)
    "post_job",
    "bid",
    "cancel_job",
    "deliver_job",
    "review_job",
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
    # reply_owner (max 2000); set_goal (max 500, enforced in validator)
    text: str | None = Field(default=None, max_length=2000)
    ref_message_id: str | None = None  # reply_owner (optional back-reference)
    reflection: str | None = Field(default=None, max_length=2000)  # complete_goal
    target_agent_id: str | None = Field(default=None, max_length=128)  # describe_stance

    # Exchange verbs (ADR-0007)
    job_id: str | None = Field(default=None, max_length=64)
    title: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=4096)
    max_price: int | None = Field(default=None, ge=1, le=1_000_000)
    strategy: Literal["balanced", "lowest_price", "best_quality"] | None = None
    price: int | None = Field(default=None, ge=0, le=1_000_000)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    pitch: str | None = Field(default=None, max_length=500)
    deliverable: str | None = Field(default=None, max_length=16384)
    accept: bool | None = None
    reason: str | None = Field(default=None, max_length=500)
    # W3: success criteria and per-item verdicts
    success_criteria: list[str] | None = None
    criteria_verdicts: list[bool] | None = None

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
        if self.action == "set_goal":
            if not self.text:
                raise ValueError("'set_goal' requires 'text'")
            if len(self.text) > 500:
                raise ValueError("'set_goal' text must be <= 500 chars")
        if self.action == "complete_goal" and not self.reflection:
            raise ValueError("'complete_goal' requires 'reflection'")
        if self.action == "describe_stance":
            if not self.target_agent_id:
                raise ValueError("'describe_stance' requires 'target_agent_id'")
            if not self.text:
                raise ValueError("'describe_stance' requires 'text'")
            if len(self.text) > 500:
                raise ValueError("'describe_stance' text must be <= 500 chars")
        if self.action == "post_job":
            if not self.title:
                raise ValueError("'post_job' requires 'title'")
            if not self.description:
                raise ValueError("'post_job' requires 'description'")
            if self.max_price is None:
                raise ValueError("'post_job' requires 'max_price'")
        if self.action in ("bid", "cancel_job", "deliver_job", "review_job") and not self.job_id:
            raise ValueError(f"'{self.action}' requires 'job_id'")
        if self.action == "bid" and self.price is None:
            raise ValueError("'bid' requires 'price'")
        if self.action == "deliver_job" and not self.deliverable:
            raise ValueError("'deliver_job' requires 'deliverable'")
        if self.action == "review_job" and self.accept is None:
            raise ValueError("'review_job' requires 'accept'")
        # W3: success_criteria only valid with post_job
        if self.success_criteria is not None:
            if self.action != "post_job":
                raise ValueError("'success_criteria' is only valid with 'post_job'")
            if len(self.success_criteria) > 8:
                raise ValueError("'success_criteria' must have at most 8 items")
            for item in self.success_criteria:
                stripped = item.strip()
                if not stripped:
                    raise ValueError("Each success criterion must be non-empty")
                if len(stripped) > 200:
                    raise ValueError("Each success criterion must be <= 200 characters")
        # W3: criteria_verdicts only valid with review_job
        if self.criteria_verdicts is not None and self.action != "review_job":
            raise ValueError("'criteria_verdicts' is only valid with 'review_job'")
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
