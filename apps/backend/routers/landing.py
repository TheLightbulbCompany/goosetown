"""Public landing-page endpoints. No auth — protected by WAF rate limits + honeypot."""

import logging
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.config import settings
from core.services.resend_client import ResendClient, ResendError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/landing", tags=["landing"])

# Conservative-but-permissive email regex. Real validation = SMTP MX lookup,
# but for a launch-list signup we just need to filter blatant garbage.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SignupRequest(BaseModel):
    email: str = Field(..., max_length=320)
    honeypot: str | None = None


@router.post("/signup", status_code=200)
async def signup(req: SignupRequest):
    # Honeypot: if filled, return 200 silently (don't signal to the bot).
    if req.honeypot:
        logger.info("landing signup honeypot triggered")
        return {"status": "ok"}

    # Email format
    if not _EMAIL_RE.match(req.email):
        raise HTTPException(status_code=400, detail="Invalid email")

    if not settings.RESEND_API_KEY or not settings.RESEND_AUDIENCE_ID:
        # Misconfigured backend — fail-closed with a clear server log.
        logger.error("RESEND_API_KEY or RESEND_AUDIENCE_ID is unset; landing signup disabled")
        raise HTTPException(status_code=503, detail="Signup temporarily unavailable")

    client = ResendClient(
        api_key=settings.RESEND_API_KEY,
        audience_id=settings.RESEND_AUDIENCE_ID,
    )

    try:
        result = await client.add_contact(req.email)
    except ResendError as exc:
        if 400 <= exc.status < 500:
            raise HTTPException(status_code=400, detail="Could not add to list") from exc
        raise HTTPException(status_code=503, detail="Signup temporarily unavailable") from exc

    return {"status": result}  # "created" | "already_subscribed"


@router.get("/healthz", status_code=200)
async def healthz():
    """Trivial health check; no auth, used by external monitors."""
    return {"status": "ok"}
