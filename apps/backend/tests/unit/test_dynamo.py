from core.dynamo import jobs_table, ledger_table, provider_subscriptions_table, table_name


def test_table_name_prefix():
    assert table_name("agents") == "goosetown-agents"


def test_table_name_custom():
    assert table_name("connections") == "goosetown-connections"


def test_table_name_jobs():
    assert table_name("jobs") == "goosetown-jobs"


def test_table_name_ledger():
    assert table_name("ledger") == "goosetown-ledger"


def test_jobs_table_returns_table_handle():
    """jobs_table() returns a boto3 Table object with the correct name."""
    table = jobs_table()
    assert table.name == "goosetown-jobs"


def test_ledger_table_returns_table_handle():
    """ledger_table() returns a boto3 Table object with the correct name."""
    table = ledger_table()
    assert table.name == "goosetown-ledger"


def test_table_name_provider_subscriptions():
    assert table_name("provider-subscriptions") == "goosetown-provider-subscriptions"


def test_provider_subscriptions_table_returns_table_handle():
    """provider_subscriptions_table() returns a boto3 Table object with the correct name."""
    table = provider_subscriptions_table()
    assert table.name == "goosetown-provider-subscriptions"
