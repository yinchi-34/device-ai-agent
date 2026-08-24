from tests.conftest import _is_test_database_name


def test_destructive_test_setup_only_accepts_test_database_names():
    assert _is_test_database_name("device_ai_agent_test") is True
    assert _is_test_database_name("feature_branch_test") is True
    assert _is_test_database_name("device_ai_agent") is False
    assert _is_test_database_name("production") is False
