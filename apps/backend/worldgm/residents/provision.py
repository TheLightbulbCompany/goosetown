import os
from dataclasses import dataclass
from pathlib import Path

from core.services.town_service import RESIDENT_USER_ID
from worldgm.residents.personas import Persona

# Default base dir for resident workspaces inside the worldgm container.
_DEFAULT_BASE_DIR = Path("/tmp/residents")


@dataclass
class ResidentConfig:
    agent_id: str
    agent_name: str
    token: str
    ws_url: str
    workspace: Path  # TownDaemon workspace_path — holds TOWN_*.md
    state_dir: Path  # TownDaemon state_dir — holds state.json/pid/sock


def provision_resident(
    persona: Persona,
    *,
    town_service,
    mint_token,
    ws_url: str | None = None,
    base_dir: Path = _DEFAULT_BASE_DIR,
) -> ResidentConfig:
    """Create the resident row, mint its token, build its daemon config.

    ``town_service`` and ``mint_token`` are injected for testability:
      - town_service: a TownService-like object exposing create_resident(...)
      - mint_token: a callable (user_id, instance_id) -> token
                    (production: sign_town_token)

    Directories are NOT created here — the manager in Task 5 will mkdir them.
    """
    agent = town_service.create_resident(
        name=persona.name,
        display_name=persona.display_name,
        personality=persona.personality,
        appearance=persona.appearance,
        traits=list(persona.traits),
    )
    agent_id = agent["id"]
    token = mint_token(RESIDENT_USER_ID, agent_id)
    workspace = base_dir / persona.name
    return ResidentConfig(
        agent_id=agent_id,
        agent_name=persona.name,
        token=token,
        ws_url=ws_url if ws_url is not None else os.environ.get("TOWN_WS_URL", ""),
        workspace=workspace,
        state_dir=workspace / ".state",
    )
