"""Smoke test for the WorldGM service bootstrap.

Verifies that _bootstrap_gm() constructs all required GmState fields and that
the minimal LocationBasedFilter / NarrativeEventResolution stand-ins are
correctly wired.

Concordia v2.x API deviation:
  - LocationBasedFilter and NarrativeEventResolution are minimal local
    dataclasses (not Concordia ContextComponents) because the plan-assumed
    contrib components don't exist in the installed gdm-concordia.
  - The Concordia GameMaster object (gm) is deferred to Phase C3.
    generic.GameMaster.build() raises ValueError when entities=[], so we
    cannot build it at startup before any GooseTown agents have connected.
    state.gm is None at bootstrap; Phase C3 assigns it.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


@patch("worldgm.bedrock_model.boto3.client")
@patch("core.services.town_service.TownService")
def test_bootstrap_gm_constructs_components(mock_town, mock_boto):
    from worldgm.__main__ import LocationBasedFilter, NarrativeEventResolution, _bootstrap_gm

    mock_boto.return_value = MagicMock()
    mock_town.return_value = MagicMock()

    state = _bootstrap_gm()

    assert state.model_lite is not None
    assert state.model_micro is not None
    assert isinstance(state.location_filter, LocationBasedFilter)
    assert isinstance(state.narrative_resolver, NarrativeEventResolution)
    # gm is intentionally None at bootstrap — deferred to Phase C3.
    assert state.gm is None
    assert state.town_service is not None
    assert state.last_known_locations == {}


@patch("worldgm.bedrock_model.boto3.client")
@patch("core.services.town_service.TownService")
def test_location_filter_starts_empty(mock_town, mock_boto):
    """LocationBasedFilter initialises with empty dicts."""
    from worldgm.__main__ import _bootstrap_gm

    mock_boto.return_value = MagicMock()
    mock_town.return_value = MagicMock()

    state = _bootstrap_gm()
    lf = state.location_filter

    assert lf._entity_locations == {}
    assert lf._location_to_entities == {}
    assert lf._entity_names == []
    assert lf.entities_at("plaza") == set()
    assert lf.location_of("no-one") == "unknown"


@patch("worldgm.bedrock_model.boto3.client")
@patch("core.services.town_service.TownService")
def test_narrative_resolver_uses_model_lite(mock_town, mock_boto):
    """NarrativeEventResolution.resolve delegates to model_lite.sample_text."""
    from worldgm.__main__ import _bootstrap_gm

    mock_client = MagicMock()
    mock_client.converse.return_value = {"output": {"message": {"content": [{"text": "A narrative event occurred."}]}}}
    mock_boto.return_value = mock_client
    mock_town.return_value = MagicMock()

    state = _bootstrap_gm()
    result = state.narrative_resolver.resolve("Something happens in the plaza.")

    assert result == "A narrative event occurred."
    assert mock_client.converse.called
