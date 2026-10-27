import shutil
from pathlib import Path

import pytest

from enhanced_assistant import EnhancedQueryAssistant

REPO_DB = Path(__file__).resolve().parent.parent / "chinook.db"


@pytest.fixture
def assistant(tmp_path):
    # OpenAI and Chroma are replaced with fakes in conftest.py; the DB is a tmp copy.
    db = tmp_path / "chinook.db"
    shutil.copyfile(REPO_DB, db)
    return EnhancedQueryAssistant(str(db), "dummy-key")


def test_schema_loaded(assistant):
    assert "Table" in assistant.schema


def test_sample_queries(assistant):
    assert len(assistant.SAMPLE_QUERIES) == 10
