from core.dynamo import table_name


def test_table_name_prefix():
    assert table_name("agents") == "goosetown-agents"


def test_table_name_custom():
    assert table_name("connections") == "goosetown-connections"
