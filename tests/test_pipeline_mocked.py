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
from conftest import (
    FakeChromaClient,
    FakeCompletions,
    FakeOpenAI,
    NetworkBlockedError,
)

import mcp_server
from config import DEFAULT_MODEL_ID, ModelConfig
from enhanced_assistant import EnhancedQueryAssistant, is_truncated
from sql_guard import GuardConfig, UnsafeSQLError

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
    assert call["model"] == assistant.model_config.model_id == DEFAULT_MODEL_ID
    assert "List three genres" in call["messages"][-1]["content"]

    stored = assistant.collection
    assert stored.documents == ["List three genres"]
    assert stored.metadatas[0]["sql_query"] == _model(assistant).sql
    assert stored.metadatas[0]["row_count"] == 3
    assert stored.metadatas[0]["model_id"] == DEFAULT_MODEL_ID
    assert stored.metadatas[0]["model_provider"] == "openai"


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


def test_default_model_is_gpt_4o(assistant):
    # Default matches the behaviour before C3 (model="gpt-4o" was hard-coded).
    assert DEFAULT_MODEL_ID == "gpt-4o"
    _model(assistant).sql = "SELECT 1 AS x"
    assistant.execute_query("one")
    assert _model(assistant).calls[-1]["model"] == "gpt-4o"


def test_model_override_via_env(monkeypatch, tmp_db):
    monkeypatch.setenv("ADQ_MODEL_ID", "test-model-env")
    a = EnhancedQueryAssistant(str(tmp_db), "dummy-key")
    assert a.model_config == ModelConfig("openai", "test-model-env")
    _model(a).sql = "SELECT 1 AS x"
    a.execute_query("one")
    assert _model(a).calls[-1]["model"] == "test-model-env"
    assert a.collection.metadatas[-1]["model_id"] == "test-model-env"


def test_model_override_via_argument(tmp_db):
    a = EnhancedQueryAssistant(
        str(tmp_db), "dummy-key", model_config=ModelConfig(model_id="test-model-arg")
    )
    _model(a).sql = "SELECT 1 AS x"
    a.execute_query("one")
    assert _model(a).calls[-1]["model"] == "test-model-arg"


def test_model_id_logged_at_startup(tmp_db, caplog):
    with caplog.at_level("INFO", logger="enhanced_assistant"):
        EnhancedQueryAssistant(str(tmp_db), "dummy-key")
    assert "model provider=openai id=gpt-4o" in caplog.text
    assert "dummy-key" not in caplog.text


def _capped(tmp_db, cap):
    return EnhancedQueryAssistant(
        str(tmp_db),
        "dummy-key",
        guard_config=GuardConfig(step_budget=1_000_000, row_cap=cap, timeout_s=60.0),
    )


# Genre has 25 rows in the committed chinook.db.
GENRES = "SELECT GenreId FROM Genre ORDER BY GenreId"


def test_truncated_true_when_rows_exceed_cap(tmp_db):
    a = _capped(tmp_db, 5)
    _model(a).sql = GENRES
    df, _ = a.execute_query("all genres")
    assert df["GenreId"].tolist() == [1, 2, 3, 4, 5]
    assert is_truncated(df) is True
    assert df.attrs["row_cap"] == 5
    assert a.collection.metadatas[-1]["truncated"] is True


@pytest.mark.parametrize("cap", [25, 26, 1000])
def test_truncated_false_at_or_under_cap(tmp_db, cap):
    a = _capped(tmp_db, cap)
    _model(a).sql = GENRES
    df, _ = a.execute_query("all genres")
    assert len(df) == 25
    assert is_truncated(df) is False
    assert a.collection.metadatas[-1]["truncated"] is False


def test_truncated_row_cap_from_env(monkeypatch, tmp_db):
    monkeypatch.setenv("ADQ_SQL_ROW_CAP", "3")
    a = EnhancedQueryAssistant(str(tmp_db), "dummy-key")
    _model(a).sql = GENRES
    df, _ = a.execute_query("all genres")
    assert len(df) == 3
    assert is_truncated(df) is True


# The JSON-RPC result body is built by mcp_server.build_query_result from the
# assistant's (results, similar) return value. The endpoint itself is not run
# here: execute_query calls asyncio.run, which fails inside a running event loop
# (pre-existing, reported for Review, not changed in C3).
def test_api_reports_truncated(tmp_db):
    a = _capped(tmp_db, 4)
    _model(a).sql = GENRES
    result = mcp_server.build_query_result(*a.execute_query("all genres"))
    assert result["row_count"] == 4
    assert len(result["results"]) == 4
    assert result["truncated"] is True


def test_api_reports_not_truncated(tmp_db):
    a = _capped(tmp_db, 100)
    _model(a).sql = GENRES
    result = mcp_server.build_query_result(*a.execute_query("all genres"))
    assert result["row_count"] == 25
    assert result["truncated"] is False


def test_response_model_is_logged(monkeypatch, tmp_db, caplog):
    # Q12: the alias stays the default; the model the API reports is logged.
    monkeypatch.setattr(FakeCompletions, "response_model", "gpt-4o-2099-01-01")
    a = EnhancedQueryAssistant(str(tmp_db), "test-not-a-key-log")
    _model(a).sql = "SELECT 1 AS x"
    with caplog.at_level("INFO", logger="enhanced_assistant"):
        a.execute_query("one")
    assert _model(a).calls[-1]["model"] == "gpt-4o"
    assert "model response id=gpt-4o-2099-01-01" in caplog.text
    assert "test-not-a-key-log" not in caplog.text
