import sys
import os
import pytest

# Fix import path for GitHub Actions + local runs
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from enhanced_assistant import EnhancedQueryAssistant


@pytest.fixture
def assistant():
    # Use a dummy key; real tests would mock OpenAI
    return EnhancedQueryAssistant("chinook.db", "dummy-key")


def test_schema_loaded(assistant):
    assert "Table" in assistant.schema


def test_sample_queries(assistant):
    assert len(assistant.SAMPLE_QUERIES) == 10
