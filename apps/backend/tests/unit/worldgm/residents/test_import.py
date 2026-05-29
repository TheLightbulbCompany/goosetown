def test_can_import_town_daemon():
    from goosetown.daemon import TownDaemon  # noqa: F401

    assert hasattr(TownDaemon, "run")
    assert hasattr(TownDaemon, "handle_tool_command")
