"""LLM-backed narrative director: turn a WorldSnapshot + signals into a grounded
storyline injection (real cast validated) or nothing. See the design spec."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from worldgm.world_snapshot import Signal, WorldSnapshot

logger = logging.getLogger(__name__)

_MAX_SIGNALS = 5


@dataclass
class Injection:
    narrative: str
    cast: list[str]
    location: str | None


def _serialize_snapshot(snap: WorldSnapshot) -> str:
    lines = [f"Time: {snap.hour_utc:02d}:00 UTC", "Residents present:"]
    for r in snap.residents:
        bits = [f"- {r.name} at the {r.location}, feeling {r.mood_label}"]
        if r.persona:
            bits.append(f"({r.persona})")
        if r.activity and r.activity != "idle":
            bits.append(f"— {r.activity}")
        lines.append(" ".join(bits))
    if snap.relationships:
        lines.append("Relationships:")
        for rel in snap.relationships:
            lines.append(f"- {rel.a} & {rel.b}: {rel.rel_type} (affinity {rel.affinity})")
    if snap.recent_conversations:
        lines.append("Recently discussed:")
        for c in snap.recent_conversations[:5]:
            if c.topic:
                lines.append(f"- {', '.join(c.participants)}: {c.topic}")
    return "\n".join(lines)


def _real_names(snap: WorldSnapshot) -> set[str]:
    return {r.name for r in snap.residents}


def _parse(text: str) -> tuple[str | None, list[str], str]:
    """Parse 'LOCATION: x / CAST: a, b / NARRATIVE: ...' → (location, cast, narrative)."""
    location: str | None = None
    cast: list[str] = []
    narrative_parts: list[str] = []
    in_narrative = False
    for line in text.splitlines():
        upper = line.strip().upper()
        if upper.startswith("LOCATION:"):
            location = line.split(":", 1)[1].strip() or None
        elif upper.startswith("CAST:"):
            cast = [n.strip() for n in line.split(":", 1)[1].split(",") if n.strip()]
        elif upper.startswith("NARRATIVE:"):
            in_narrative = True
            narrative_parts.append(line.split(":", 1)[1].strip())
        elif in_narrative:
            narrative_parts.append(line.strip())
    return location, cast, " ".join(p for p in narrative_parts if p).strip()


def author_kickoff(model, snap: WorldSnapshot, signals: list[Signal]) -> Injection | None:
    """Author a grounded storyline kickoff, or None if the model declines or its
    cast contains no real residents (hallucination guard)."""
    if not snap.residents:
        return None
    hooks = "\n".join(f"- {s.note}" for s in signals[:_MAX_SIGNALS]) or "- (none; use the scene as-is)"
    prompt = (
        "You are the unseen game-master of a small AI town. You make it feel alive by "
        "introducing small, interesting, grounded happenings involving the REAL residents below.\n\n"
        f"{_serialize_snapshot(snap)}\n\n"
        f"Possible hooks you may use, combine, or ignore:\n{hooks}\n\n"
        "Author ONE short, vivid happening (2-3 sentences) featuring specific real residents by "
        "name. The cast may be together or apart; if apart, frame it as a letter, a rumor reaching "
        "them, or a memory surfacing. Only the residents listed above exist — do not invent any "
        "other named people, narrators, or visitors.\n"
        "Reply in EXACTLY this format:\n"
        "LOCATION: <one location or 'town'>\n"
        "CAST: <comma-separated real resident names>\n"
        "NARRATIVE: <2-3 sentences>\n"
        "If nothing interesting fits right now, reply with exactly: NOTHING"
    )
    text = model.sample_text(prompt, max_tokens=256)
    if "NOTHING" in text.upper() and len(text.strip()) < 40:
        return None
    location, cast, narrative = _parse(text)
    real = _real_names(snap)
    cast = [c for c in cast if c in real]
    if not cast or not narrative:
        logger.info("[director] kickoff rejected — no real cast or empty narrative")
        return None
    return Injection(narrative=narrative, cast=cast, location=location)


def author_beat(model, arc: dict, snap: WorldSnapshot) -> str | None:
    """Continue an active storyline from the fresh snapshot, or None when DONE."""
    prompt = (
        "Continue this town storyline.\n\n"
        f"Story so far:\n{arc.get('narrativeKickoff', '')}\n"
        f"{arc.get('lastBeatText', '')}\n\n"
        f"The town RIGHT NOW:\n{_serialize_snapshot(snap)}\n\n"
        "The residents may have responded since the last beat. Continue in ONE short paragraph "
        "reflecting what's actually happening now. Feature only the real residents above; do not "
        "introduce new named people. If the storyline has run its course, reply with exactly: DONE"
    )
    text = model.sample_text(prompt, max_tokens=256)
    if "DONE" in text.upper() and len(text.strip()) < 30:
        return None
    return text.strip() or None
