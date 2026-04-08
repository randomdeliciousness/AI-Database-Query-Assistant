import pytest
from enhanced_assistant import EnhancedQueryAssistant

@pytest.fixture
def assistant():
    return EnhancedQueryAssistant("chinook.db", "dummy-key")  # mocked in real tests

def test_schema_loaded(assistant):
    assert "Table" in assistant.schema

# Add more tests as needed
