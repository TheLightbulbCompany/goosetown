from unittest.mock import MagicMock

from worldgm.residents.personas import Persona
from worldgm.residents.provision import ResidentConfig, provision_resident


def _persona():
    return Persona(
        name="tobias",
        display_name="Tobias",
        personality="Lighthouse keeper.",
        appearance="Old man, mustard raincoat.",
        traits=("gruff", "nostalgic"),
        hangout="plaza",
    )


def test_provision_creates_row_mints_token_and_builds_config():
    fake_ts = MagicMock()
    fake_ts.create_resident.return_value = {"id": "agent-uuid-123"}
    fake_mint = MagicMock(return_value="signed.token.value")

    cfg = provision_resident(
        _persona(),
        town_service=fake_ts,
        mint_token=fake_mint,
        ws_url="wss://ws-dev",
    )

    assert isinstance(cfg, ResidentConfig)
    assert cfg.token == "signed.token.value"
    assert cfg.agent_name == "tobias"
    assert cfg.agent_id == "agent-uuid-123"
    assert "/residents/" in str(cfg.workspace) and str(cfg.workspace).endswith("tobias")

    fake_ts.create_resident.assert_called_once()
    # token minted for the system user with the agent id as instance id:
    fake_mint.assert_called_once_with("system:resident", "agent-uuid-123")


def test_provision_create_resident_called_with_persona_fields():
    fake_ts = MagicMock()
    fake_ts.create_resident.return_value = {"id": "agent-uuid-456"}
    fake_mint = MagicMock(return_value="tok")

    persona = _persona()
    provision_resident(persona, town_service=fake_ts, mint_token=fake_mint, ws_url="wss://x")

    fake_ts.create_resident.assert_called_once_with(
        name=persona.name,
        display_name=persona.display_name,
        personality=persona.personality,
        appearance=persona.appearance,
        traits=list(persona.traits),
    )


def test_provision_ws_url_falls_back_to_env_var(monkeypatch):
    monkeypatch.setenv("TOWN_WS_URL", "wss://from-env")

    fake_ts = MagicMock()
    fake_ts.create_resident.return_value = {"id": "agent-uuid-789"}
    fake_mint = MagicMock(return_value="tok")

    cfg = provision_resident(
        _persona(),
        town_service=fake_ts,
        mint_token=fake_mint,
        ws_url=None,
    )

    assert cfg.ws_url == "wss://from-env"


def test_provision_ws_url_empty_string_when_env_unset(monkeypatch):
    monkeypatch.delenv("TOWN_WS_URL", raising=False)

    fake_ts = MagicMock()
    fake_ts.create_resident.return_value = {"id": "agent-uuid-000"}
    fake_mint = MagicMock(return_value="tok")

    cfg = provision_resident(
        _persona(),
        town_service=fake_ts,
        mint_token=fake_mint,
        ws_url=None,
    )

    assert cfg.ws_url == ""


def test_provision_state_dir_is_under_workspace():
    fake_ts = MagicMock()
    fake_ts.create_resident.return_value = {"id": "agent-uuid-abc"}
    fake_mint = MagicMock(return_value="tok")

    cfg = provision_resident(
        _persona(),
        town_service=fake_ts,
        mint_token=fake_mint,
        ws_url="wss://ws-dev",
    )

    assert cfg.state_dir == cfg.workspace / ".state"
