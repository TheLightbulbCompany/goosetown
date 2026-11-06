"""Small appearance input screen; not comprehensive semantic moderation."""

import re
import unicodedata

_UNSAFE = re.compile(
    r"https?://|www\.|<[^>]*>|```|[{}]|"
    r"\b(?:ignore|override|disregard)\s+(?:all\s+|any\s+)?"
    r"(?:instructions|(?:previous|prior|system)\s+(?:instructions|prompts?|messages?))\b|"
    r"\b(?:system\s*prompt|developer\s*message|execute\s+code|run\s+(?:command|script))\b",
    re.IGNORECASE,
)


class AppearanceRejected(ValueError):
    """Ask for a plain visual description instead of submitting to PixelLab."""


def validate_appearance(value: str) -> str:
    text = unicodedata.normalize("NFKC", value)
    if (
        not text.strip()
        or len(text) > 500
        or any(ord(c) < 32 and c not in "\r\n\t" for c in text)
        or _UNSAFE.search(text)
    ):
        raise AppearanceRejected(
            "Use a plain visual description under 500 characters, without links, code or instructions"
        )
    return text
