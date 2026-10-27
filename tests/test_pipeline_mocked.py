"""End-to-end pipeline tests with the model and vector store mocked.

The fake OpenAI client (conftest.py) returns scripted SQL, so no API key, model
call or network access is needed. Every test runs on a tmp_path copy of
chinook.db.
"""

import hashlib
import os
import shutil
import socket
from pathlib import Path

import pandas as pd
import pytest
from conftest import FakeChromaClient, FakeOpenAI, NetworkBlockedError

from enhanced_assistant import EnhancedQueryAssistant
from sql_guard import UnsafeSQLError

REPO_DB = Path(__file__).resolve().parent.parent / "chinook.db"


def _snapshot(db: Path):
    digest = hashlib.sha256(db.read_bytes()).hexdigest()
    return digest, sorted(p.name for p in db.parent.iterdir())


@pytest.fixture
def tmp_db(tmp_path):
    db = tmp_path / "chinook.db"
    shutil.copyfile(REPO_DB, db)
    return db


@pytest.fixture
def assistant(tmp_db):
    return EnhancedQueryAssistant(str(tmp_db), "dummy-key")


def _model(assistant):
    return assistant.client.completions


def test_model_and_store_are_fakes(assistant):
    assert isinstance(assistant.client, FakeOpenAI)
    assert isinstance(assistant.chroma_client, FakeChromaClient)
    assert "OPENAI_API_KEY" not in os.environ


def test_network_is_blocked():
    with pytest.raises(NetworkBlockedError):
        socket.create_connection(("api.openai.com", 443), timeout=1)


def test_mocked_select_returns_dataframe(assistant, tmp_db):
    before = _snapshot(tmp_db)
    _model(assistant).sql = "SELECT GenreId, Name FROM Genre ORDER BY GenreId LIMIT 3"
    df, similar = assistant.execute_query("List three genres")
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == ["GenreId", "Name"]
    assert df["GenreId"].tolist() == [1, 2, 3]
    assert df.attrs["truncated"] is False
    assert similar == []
    assert _snapshot(tmp_db) == before

    call = _model(assistant).calls[-1]
    assert call["model"] == "gpt-4o"
    assert "List three genres" in call["messages"][-1]["content"]

    stored = assistant.collection
    assert stored.documents == ["List three genres"]
    assert stored.metadatas[0]["sql_query"] == _model(assistant).sql
    assert stored.metadatas[0]["row_count"] == 3


def test_history_is_returned_on_next_query(assistant):
    _model(assistant).sql = "SELECT COUNT(*) AS n FROM Customer"
    assistant.execute_query("How many customers?")
    df, similar = assistant.execute_query("Count customers")
    assert df["n"].tolist() == [59]
    assert similar[0]["natural_query"] == "How many customers?"
    assert similar[0]["sql_query"] == "SELECT COUNT(*) AS n FROM Customer"


def test_mocked_drop_table_is_blocked(assistant, tmp_db):
    before = _snapshot(tmp_db)
    _model(assistant).sql = "DROP TABLE Genre"
    with pytest.raises(UnsafeSQLError):
        assistant.execute_query("Remove the genre table")
    assert _snapshot(tmp_db) == before
    assert assistant.collection.documents == []
