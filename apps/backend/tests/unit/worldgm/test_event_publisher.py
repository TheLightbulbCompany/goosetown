"""Tests for event_publisher."""

from unittest.mock import MagicMock, patch


@patch("worldgm.event_publisher.dynamo.world_events_table")
@patch("worldgm.event_publisher._events_client")
def test_emit_world_event_publishes_base_and_per_witness(mock_eb, mock_table):
    from worldgm.event_publisher import emit_world_event

    eb = MagicMock()
    mock_eb.return_value = eb

    event_id = emit_world_event(
        tag="weather.storm",
        detail={"intensity": "heavy"},
        witnesses=["alice", "bob"],
        location="plaza",
        summary="A storm rolls in",
    )

    assert event_id  # returns a uuid
    # 1 base + 2 per-witness = 3 entries
    all_entries = []
    for call in eb.put_events.call_args_list:
        all_entries.extend(call.kwargs["Entries"])
    assert len(all_entries) == 3
    detail_types = sorted(e["DetailType"] for e in all_entries)
    assert detail_types == ["personal.alice.observed", "personal.bob.observed", "weather.storm"]
    # All entries reference the same eventId
    import json

    eid_set = {json.loads(e["Detail"])["eventId"] for e in all_entries}
    assert len(eid_set) == 1


@patch("worldgm.event_publisher.dynamo.world_events_table")
@patch("worldgm.event_publisher._events_client")
def test_emit_world_event_persists_to_table(mock_eb, mock_table):
    from worldgm.event_publisher import emit_world_event

    mock_eb.return_value = MagicMock()
    emit_world_event(
        tag="ambient.cat_spotted",
        detail={},
        witnesses=["alice"],
        summary="A cat appears",
    )
    mock_table.return_value.put_item.assert_called_once()
    item = mock_table.return_value.put_item.call_args.kwargs["Item"]
    assert item["tag"] == "ambient.cat_spotted"
    assert item["summary"] == "A cat appears"
    assert item["witnesses"] == ["alice"]


@patch("worldgm.event_publisher.dynamo.world_events_table")
@patch("worldgm.event_publisher._events_client")
def test_emit_world_event_default_wake_false_for_ambient(mock_eb, mock_table):
    from worldgm.event_publisher import emit_world_event

    mock_eb.return_value = MagicMock()
    emit_world_event(tag="ambient.cat_spotted", detail={}, witnesses=["alice"])
    item = mock_table.return_value.put_item.call_args.kwargs["Item"]
    assert item["wake"] is False


@patch("worldgm.event_publisher.dynamo.world_events_table")
@patch("worldgm.event_publisher._events_client")
def test_emit_world_event_default_wake_true_for_personal(mock_eb, mock_table):
    from worldgm.event_publisher import emit_world_event

    mock_eb.return_value = MagicMock()
    emit_world_event(tag="personal.alice.opportunity", detail={}, witnesses=["alice"])
    item = mock_table.return_value.put_item.call_args.kwargs["Item"]
    assert item["wake"] is True


@patch("worldgm.event_publisher.dynamo.world_events_table")
@patch("worldgm.event_publisher._events_client")
def test_emit_world_event_batches_per_10(mock_eb, mock_table):
    from worldgm.event_publisher import emit_world_event

    eb = MagicMock()
    mock_eb.return_value = eb
    # 1 base + 15 witnesses = 16 entries → 2 batches (10, 6)
    witnesses = [f"agent{i}" for i in range(15)]
    emit_world_event(tag="ambient.x", detail={}, witnesses=witnesses)
    assert eb.put_events.call_count == 2
