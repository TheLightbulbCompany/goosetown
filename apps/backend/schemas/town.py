"""Pydantic schemas for GooseTown API."""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class TownOptInRequest(BaseModel):
    """Request to register an agent in GooseTown."""

    agent_name: str = Field(..., min_length=1, max_length=50, pattern="^[a-zA-Z0-9_-]+$")
    display_name: str = Field(..., min_length=1, max_length=100)
    personality_summary: Optional[str] = Field(None, max_length=200)
    avatar_config: Optional[dict] = None


class TownOptOutRequest(BaseModel):
    """Request to remove an agent from GooseTown."""

    agent_name: str = Field(..., min_length=1, max_length=50, pattern="^[a-zA-Z0-9_-]+$")


class TownAgentResponse(BaseModel):
    """Public info about a town agent."""

    id: str
    user_id: str
    agent_name: str
    display_name: str
    avatar_url: Optional[str] = None
    avatar_config: Optional[dict] = None
    personality_summary: Optional[str] = None
    home_location: Optional[str] = None
    is_active: bool
    joined_at: Optional[str] = None

    class Config:
        from_attributes = True


class TownAgentStateResponse(BaseModel):
    """Current state of a town agent (position, activity, mood)."""

    agent_id: str
    display_name: str
    current_location: Optional[str] = None
    current_activity: Optional[str] = None
    target_location: Optional[str] = None
    position_x: float
    position_y: float
    mood: Optional[str] = None
    energy: int
    status_message: Optional[str] = None


class TownStateResponse(BaseModel):
    """Full town state snapshot."""

    agents: List[TownAgentStateResponse]
    timestamp: datetime


class ConversationTurn(BaseModel):
    """A single turn in a conversation."""

    speaker: str
    text: str


class TownConversationResponse(BaseModel):
    """Public conversation log.

    ``participants`` is the source of truth for who's in the conversation —
    populated from the conversation row's ``participants`` list and
    resolved to display names. ``participant_a`` / ``participant_b`` are
    kept for backward compatibility with clients that haven't migrated to
    reading ``participants``.
    """

    id: str
    participant_a: str
    participant_b: str
    participants: List[str] = []
    location: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    turn_count: int
    topic_summary: Optional[str] = None
    public_log: List[ConversationTurn]

    class Config:
        from_attributes = True


class TownConversationsListResponse(BaseModel):
    """List of recent conversations."""

    conversations: List[TownConversationResponse]


class HumanSayRequest(BaseModel):
    """A logged-in user's message into an ongoing resident conversation.

    The speaker identity is derived from the authenticated user server-side,
    NOT from this body — there is deliberately no ``speaker`` field, so a
    visitor cannot impersonate a resident. The 280-char bound matches
    ``conversation_service.HUMAN_MESSAGE_MAX_CHARS`` and is enforced again in
    the service layer; the pydantic bound here turns an over-long body into a
    422 at the boundary.
    """

    message: str = Field(..., min_length=1, max_length=280)


class HumanSayResponse(BaseModel):
    """Acknowledgement that a user's turn was appended and broadcast."""

    ok: bool
    conv_id: str
    turn: int


class ProfileResponse(BaseModel):
    """The current user's town profile.

    ``nickname`` is ``None`` when the user hasn't picked a public-safe handle
    yet — the frontend uses that to decide whether to prompt before letting
    them speak in a conversation.
    """

    nickname: Optional[str] = None


class NicknameRequest(BaseModel):
    """A user's chosen public-safe town nickname.

    The pydantic bound (2–20 chars) gives a fast boundary rejection; the
    character-class rules (letters/digits/spaces/``_-`` only, no leading or
    trailing space, no email-like strings) are enforced in the router via
    ``town_service.validate_nickname`` so the same rule governs every writer.
    """

    nickname: str = Field(..., min_length=2, max_length=20)


class ApartmentAgentState(BaseModel):
    """Agent state for the apartment view."""

    agent_id: str
    agent_name: str
    display_name: str
    character: Optional[str] = None
    location_context: Optional[str] = "apartment"
    current_location: Optional[str] = None
    current_activity: Optional[str] = None
    mood: str = "neutral"
    energy: int = 100
    status_message: Optional[str] = None
    position_x: float = 0.0
    position_y: float = 0.0
    speed: float = 0.0
    facing_x: float = 0.0
    facing_y: float = 1.0
    current_spot: Optional[str] = None
    is_active: bool = True
    sprite_url: Optional[str] = None
    currentGoal: Optional[str] = None
    wallet_balance: int = 0
    trust_tier: str = "newcomer"


class WorldEventEntry(BaseModel):
    """A single public world event from the WorldGM feed."""

    eventId: str
    timestamp: str  # ISO 8601
    tag: str
    summary: Optional[str] = None
    location: Optional[str] = None


class ApartmentActivityEvent(BaseModel):
    """Activity event in the apartment feed."""

    agent_name: str
    display_name: str
    event_type: str
    description: str
    location: Optional[str] = None
    timestamp: datetime


class ApartmentResponse(BaseModel):
    """Full apartment view response."""

    agents: List[ApartmentAgentState]
    activity: List[ApartmentActivityEvent]
