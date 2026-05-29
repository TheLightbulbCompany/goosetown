from dataclasses import dataclass


@dataclass(frozen=True)
class Persona:
    name: str  # slug, e.g. "tobias"
    display_name: str  # e.g. "Tobias"
    personality: str
    appearance: str
    traits: tuple[str, ...]
    hangout: str  # a valid town location the resident gravitates to
