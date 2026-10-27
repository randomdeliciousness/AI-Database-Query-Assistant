"""Audit log (ADQ-1b C6): row schema, literal masking, keyed hashes, and
fail-closed startup. Every audit log here comes from chinook queries only.
Keys are random per run (secrets.token_hex) and live in memory only.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import uuid
from pathlib import Path

import pytest
from conftest import FakeCompletions, FakeOpenAI
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

import audit_log
import mcp_server
import streamlit_app
from audit_log import (
    CHINOOK_SHA256,
    KEYGEN_COMMAND,
    MISSING_KEY_MESSAGE,
    OUTCOME_ONLY_WARNING,
    RAW_TEXT_REFUSED_MESSAGE,
    AuditConfigError,
    AuditLogger,
    load_audit_config,
    mask_sql,
    parse_session_id,
    schema_identifiers,
)
from enhanced_assistant import EnhancedQueryAssistant
from sql_guard import GuardConfig, GuardError, UnsafeSQLError

REPO = Path(__file__).resolve().parent.parent
SMITH_SQL = "SELECT FirstName, Email FROM Customer WHERE LastName = 'Smith'"
SMITH_QUESTION = "Which customer has the last name Smith?"
COMMON_KEYS = {
    "timestamp",
    "request_id",
    "session_id",
    "log_mode",
    "outcome",
    "block_reason",
    "row_count",
    "truncated",
    "steps",
    "model_provider",
    "model_id",
    "response_model",
    "error_class",
}
HASH_KEYS = {
    "question_hmac",
    "executed_sql_hmac",
    "executed_sql_redacted",
    "error_hmac",
    "error_code",
}
TEXT_KEYS = {"question", "executed_sql", "error_message", "error_code"}


# --- helpers ----------------------------------------------------------------


def _audit_path() -> Path:
    return Path(os.environ["ADQ_AUDIT_PATH"])


def _rows() -> list[dict]:
    path = _audit_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _log_text() -> str:
    path = _audit_path()
    return path.read_text() if path.exists() else ""


@pytest.fixture
def tmp_db(tmp_path):
    db = tmp_path / "chinook.db"
    shutil.copyfile(REPO / "chinook.db", db)
    return db


@pytest.fixture
def key(monkeypatch):
    value = secrets.token_hex(32)
    monkeypatch.setenv("ADQ_AUDIT_HMAC_KEY", value)
    return value


def _modified_copy(tmp_path, name="chinook.db"):
    """A chinook copy with one synthetic change: no longer the bundled DB."""
    d = tmp_path / "modified"
    d.mkdir()
    db = d / name
    shutil.copyfile(REPO / "chinook.db", db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE Genre SET Name = 'Fake Name' WHERE GenreId = 1")
    conn.commit()
    conn.close()
    return db


def _assistant(db, cap=100, **kw):
    return EnhancedQueryAssistant(
        str(db),
        "test-not-a-key",
        guard_config=GuardConfig(step_budget=1_000_000, row_cap=cap, timeout_s=60.0),
        **kw,
    )


def _ask(a, sql, question="q", **labels):
    a.client.completions.sql = sql
    return a.execute_query(question, **labels)


def _hmac(key, text):
    return hmac.new(key.encode(), text.encode(), hashlib.sha256).hexdigest()


# --- masker -----------------------------------------------------------------

NAMES = schema_identifiers(REPO / "chinook.db")


def test_schema_identifiers_are_chinook_tables_and_columns():
    assert {"customer", "lastname", "genre", "invoice", "total"} <= NAMES


def test_mask_single_quoted_string():
    masked = mask_sql(SMITH_SQL, NAMES)
    assert masked == "SELECT FirstName, Email FROM Customer WHERE LastName = ?"
    assert "Smith" not in masked


def test_mask_double_quoted_non_schema_token():
    # The guard now rejects this (DQS off), so the masker is tested directly.
    masked = mask_sql('SELECT * FROM Customer WHERE LastName = "Smith"', NAMES)
    assert masked == "SELECT * FROM Customer WHERE LastName = ?"


def test_mask_doubled_quote_escape():
    masked = mask_sql("SELECT * FROM Customer WHERE LastName = 'O''Brien'", NAMES)
    assert masked == "SELECT * FROM Customer WHERE LastName = ?"
    assert "Brien" not in masked


def test_mask_drops_comments_and_their_literals():
    sql = "SELECT Name /* 'Smith' 42 */ FROM Genre -- WHERE Name = 'Rock' 7\n LIMIT 3"
    masked = mask_sql(sql, NAMES)
    for leak in ("Smith", "42", "Rock", "7", "/*", "--"):
        assert leak not in masked
    assert masked.split() == ["SELECT", "Name", "FROM", "Genre", "LIMIT", "?"]


def test_mask_keeps_schema_quoted_identifiers():
    sql = 'SELECT "LastName", [FirstName], `Email` FROM "Customer"'
    assert mask_sql(sql, NAMES) == sql


def test_mask_non_schema_bracketed_and_quoted_tokens():
    masked = mask_sql('SELECT [secret], "nope", `x` FROM Customer', NAMES)
    assert masked == "SELECT ?, ?, ? FROM Customer"


def test_mask_numbers_hex_exponent_blob():
    sql = (
        "SELECT * FROM Invoice WHERE Total > 1.5e-3 OR Total < 2E10 OR "
        "InvoiceId = 0x1F OR Total = .5 OR Total = 12 OR x'0A0B' = X'FF'"
    )
    masked = mask_sql(sql, NAMES)
    assert masked == (
        "SELECT * FROM Invoice WHERE Total > ? OR Total < ? OR "
        "InvoiceId = ? OR Total = ? OR Total = ? OR ? = ?"
    )


def test_mask_keeps_digits_inside_identifiers():
    assert mask_sql("SELECT t1.Name FROM Genre t1", NAMES) == (
        "SELECT t1.Name FROM Genre t1"
    )


def test_mask_unterminated_tokens_mask_the_rest():
    assert mask_sql("SELECT 'Smith", NAMES) == "SELECT ?"
    assert mask_sql('SELECT "LastName', NAMES) == "SELECT ?"
    assert mask_sql("SELECT 1 /* 'Smith'", NAMES) == "SELECT ?  "


# --- keyed-hash rows ----------------------------------------------------------


def test_answered_row_schema_and_masking(tmp_db, key):
    a = _assistant(tmp_db)
    df, _ = _ask(a, SMITH_SQL, SMITH_QUESTION)
    rows = _rows()
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == COMMON_KEYS | HASH_KEYS
    assert row["log_mode"] == "keyed_hash"
    assert row["outcome"] == "answered"
    assert row["block_reason"] is None
    assert row["row_count"] == 1
    assert row["truncated"] is False
    assert isinstance(row["steps"], int) and row["steps"] >= 0
    assert row["model_id"] == "gpt-4o"
    assert row["executed_sql_redacted"] == (
        "SELECT FirstName, Email FROM Customer WHERE LastName = ?"
    )
    assert uuid.UUID(row["request_id"]).version == 4
    assert df.attrs["request_id"] == row["request_id"]
    assert "Smith" not in _log_text()


def test_no_result_rows_or_cell_values_logged(tmp_db, key):
    a = _assistant(tmp_db)
    df, _ = _ask(a, SMITH_SQL, "first name and email of customer 17")
    assert df.values.tolist() == [["Jack", "jacksmith@microsoft.com"]]
    text = _log_text()
    assert "Jack" not in text
    assert "jacksmith" not in text
    assert "microsoft" not in text


def test_question_is_keyed_hash_not_plain_sha256(tmp_db, key):
    a = _assistant(tmp_db)
    _ask(a, SMITH_SQL, SMITH_QUESTION)
    text = _log_text()
    row = _rows()[0]
    assert SMITH_QUESTION not in text
    assert hashlib.sha256(SMITH_QUESTION.encode()).hexdigest() not in text
    assert hashlib.sha256(SMITH_SQL.encode()).hexdigest() not in text
    assert row["question_hmac"] == _hmac(key, SMITH_QUESTION)
    assert row["executed_sql_hmac"] == _hmac(key, SMITH_SQL)


def test_blocked_delete_keeps_redacted_sql(tmp_db, key):
    a = _assistant(tmp_db)
    with pytest.raises(UnsafeSQLError):
        _ask(a, "DELETE FROM Invoice WHERE Total > 5", "delete big invoices")
    row = _rows()[0]
    assert row["outcome"] == "blocked"
    assert row["block_reason"] == "text_check"
    assert row["row_count"] is None
    assert row["truncated"] is None
    assert row["executed_sql_redacted"] == "DELETE FROM Invoice WHERE Total > ?"
    assert row["executed_sql_hmac"] == _hmac(key, "DELETE FROM Invoice WHERE Total > 5")


def test_outcome_values(tmp_db, key):
    a = _assistant(tmp_db, cap=5)
    _ask(a, "SELECT Name FROM Genre WHERE GenreId <= 3")
    _ask(a, "SELECT Name FROM Genre")
    with pytest.raises(GuardError):
        _ask(a, "SELECT * FROM pragma_table_info('Genre')")
    with pytest.raises(sqlite3.OperationalError):
        _ask(a, "SELECT NoSuchColumn FROM Genre")
    rows = _rows()
    assert [r["outcome"] for r in rows] == ["answered", "truncated", "blocked", "error"]
    answered, truncated, blocked, error = rows
    assert (answered["row_count"], answered["truncated"]) == (3, False)
    assert (truncated["row_count"], truncated["truncated"]) == (5, True)
    assert blocked["block_reason"] == "authorizer"
    assert blocked["row_count"] is None and error["row_count"] is None
    assert error["block_reason"] is None and answered["block_reason"] is None
    assert error["executed_sql_redacted"] == "SELECT NoSuchColumn FROM Genre"


def test_step_budget_block_records_steps(tmp_db, key):
    a = EnhancedQueryAssistant(
        str(tmp_db),
        "test-not-a-key",
        guard_config=GuardConfig(step_budget=20_000, row_cap=100, timeout_s=60.0),
    )
    with pytest.raises(GuardError):
        _ask(a, "SELECT count(*) FROM Track a, Track b")
    row = _rows()[0]
    assert row["outcome"] == "blocked"
    assert row["block_reason"] == "step_budget"
    assert row["steps"] > 20_000


def test_model_error_is_logged_without_sql(tmp_db, key, monkeypatch):
    a = _assistant(tmp_db)

    def _boom(**kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(a.client.completions, "parse", _boom)
    with pytest.raises(RuntimeError):
        a.execute_query("how many genres")
    row = _rows()[0]
    assert row["outcome"] == "error"
    assert row["executed_sql_hmac"] is None
    assert row["executed_sql_redacted"] is None


def test_response_model_and_append_only_0600(tmp_db, key, monkeypatch):
    monkeypatch.setattr(FakeCompletions, "response_model", "gpt-4o-2099-01-01")
    a = _assistant(tmp_db)
    _ask(a, "SELECT 1 AS x")
    first = _audit_path().read_text()
    _ask(a, "SELECT 2 AS x")
    text = _audit_path().read_text()
    assert text.startswith(first)
    assert len(text.splitlines()) == 2
    assert _rows()[1]["response_model"] == "gpt-4o-2099-01-01"
    assert stat.S_IMODE(_audit_path().stat().st_mode) == 0o600


def test_env_key_wins_over_secrets(tmp_db, key):
    other = secrets.token_hex(32)
    cfg = load_audit_config(tmp_db, {"hmac_key": other})
    a = _assistant(tmp_db, audit_config=cfg)
    _ask(a, SMITH_SQL, SMITH_QUESTION)
    assert _rows()[0]["question_hmac"] == _hmac(key, SMITH_QUESTION)


def test_secrets_key_used_without_env(tmp_db):
    from_secrets = secrets.token_hex(32)
    cfg = load_audit_config(tmp_db, {"hmac_key": from_secrets})
    assert cfg.mode == "keyed_hash"
    a = _assistant(tmp_db, audit_config=cfg)
    _ask(a, SMITH_SQL, SMITH_QUESTION)
    assert _rows()[0]["question_hmac"] == _hmac(from_secrets, SMITH_QUESTION)


# --- session and request ids -------------------------------------------------


def test_parse_session_id():
    sid = str(uuid.uuid4())
    assert parse_session_id(sid) == sid
    assert parse_session_id(sid.upper()) == sid
    for bad in (None, "", "not-a-uuid", "12345", "x" * 36):
        assert parse_session_id(bad) is None


@pytest.fixture
def api(monkeypatch, tmp_db, key):
    a = _assistant(tmp_db)
    a.client.completions.sql = SMITH_SQL
    monkeypatch.setattr(mcp_server, "_assistant", a)
    with TestClient(mcp_server.app) as client:
        yield client


def _rpc(client, headers=None):
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "method": "natural_language_query",
            "params": {"natural_query": SMITH_QUESTION},
            "id": 3,
        },
        headers=headers or {},
    )


def test_api_returns_uuid4_request_id_and_logs_valid_session(api):
    sid = str(uuid.uuid4())
    body = _rpc(api, {"X-Session-ID": sid}).json()
    rid = body["result"]["request_id"]
    assert uuid.UUID(rid).version == 4
    row = _rows()[-1]
    assert row["request_id"] == rid
    assert row["session_id"] == sid


def test_api_ignores_invalid_session_header(api):
    body = _rpc(api, {"X-Session-ID": "session-of-qa@example.com"}).json()
    row = _rows()[-1]
    assert row["request_id"] == body["result"]["request_id"]
    assert row["session_id"] is None
    assert "example.com" not in _log_text()


def test_api_error_carries_request_id(api):
    mcp_server._assistant.client.completions.sql = "DELETE FROM Genre"
    body = _rpc(api).json()
    rid = body["error"]["data"]["request_id"]
    assert uuid.UUID(rid).version == 4
    row = _rows()[-1]
    assert (row["request_id"], row["outcome"]) == (rid, "blocked")


def test_api_refuses_to_start_without_key_on_non_chinook(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_server, "DB_PATH", str(_modified_copy(tmp_path)))
    with pytest.raises(AuditConfigError, match="ADQ_AUDIT_HMAC_KEY"):
        with TestClient(mcp_server.app):
            pass


# --- fail-closed startup -----------------------------------------------------


def test_no_key_chinook_logs_outcome_only_with_warning(tmp_db, caplog):
    with caplog.at_level("WARNING", logger="audit_log"):
        a = _assistant(tmp_db)
    assert OUTCOME_ONLY_WARNING in caplog.text
    assert a.audit.config.mode == "outcome_only"
    _ask(a, SMITH_SQL, SMITH_QUESTION)
    with pytest.raises(GuardError):
        _ask(a, "DELETE FROM Invoice WHERE Total > 5")
    rows = _rows()
    assert [r["outcome"] for r in rows] == ["answered", "blocked"]
    for row in rows:
        assert set(row) == COMMON_KEYS
        assert not (set(row) & (HASH_KEYS | TEXT_KEYS))
    text = _log_text()
    assert "Smith" not in text and "Customer" not in text and "Invoice" not in text


def test_no_key_non_chinook_refuses_to_start(tmp_path):
    db = _modified_copy(tmp_path, "sales.db")
    with pytest.raises(AuditConfigError) as excinfo:
        _assistant(db)
    message = str(excinfo.value)
    assert "Set ADQ_AUDIT_HMAC_KEY or [audit] hmac_key in .streamlit/secrets.toml" in (
        message
    )
    assert KEYGEN_COMMAND in message
    assert FakeOpenAI.instances == []
    assert _rows() == []


def test_modified_copy_named_chinook_refuses_to_start(tmp_path):
    db = _modified_copy(tmp_path, "chinook.db")
    with pytest.raises(AuditConfigError):
        load_audit_config(db)


def test_renamed_identical_copy_counts_as_chinook(tmp_path):
    db = tmp_path / "renamed.sqlite"
    shutil.copyfile(REPO / "chinook.db", db)
    cfg = load_audit_config(db)
    assert cfg.db_is_chinook is True
    assert cfg.mode == "outcome_only"
    assert hashlib.sha256(db.read_bytes()).hexdigest() == CHINOOK_SHA256


def test_with_key_non_chinook_starts_in_keyed_hash_mode(tmp_path, key):
    cfg = load_audit_config(_modified_copy(tmp_path))
    assert (cfg.mode, cfg.db_is_chinook) == ("keyed_hash", False)


@pytest.mark.parametrize("length", [8, 16, 31])
def test_short_key_rejected(tmp_db, monkeypatch, length):
    short = secrets.token_hex(32)[:length]
    monkeypatch.setenv("ADQ_AUDIT_HMAC_KEY", short)
    with pytest.raises(AuditConfigError) as excinfo:
        load_audit_config(tmp_db)
    assert short not in str(excinfo.value)
    monkeypatch.delenv("ADQ_AUDIT_HMAC_KEY")
    with pytest.raises(AuditConfigError):
        load_audit_config(tmp_db, {"hmac_key": short})


def test_32_byte_key_accepted(tmp_db, monkeypatch):
    monkeypatch.setenv("ADQ_AUDIT_HMAC_KEY", secrets.token_hex(16))
    assert load_audit_config(tmp_db).mode == "keyed_hash"


def test_raw_text_on_non_chinook_refuses_to_start(tmp_path, key, monkeypatch):
    db = _modified_copy(tmp_path)
    monkeypatch.setenv("ADQ_AUDIT_RAW_TEXT", "1")
    with pytest.raises(AuditConfigError, match="chinook"):
        load_audit_config(db)
    monkeypatch.delenv("ADQ_AUDIT_RAW_TEXT")
    with pytest.raises(AuditConfigError, match="chinook"):
        load_audit_config(db, {"raw_text": True})


def test_raw_text_invalid_value_refuses(tmp_db, monkeypatch):
    monkeypatch.setenv("ADQ_AUDIT_RAW_TEXT", "maybe")
    with pytest.raises(AuditConfigError):
        load_audit_config(tmp_db)


def test_raw_text_on_chinook_logs_raw_text_without_key(tmp_db, monkeypatch):
    monkeypatch.setenv("ADQ_AUDIT_RAW_TEXT", "true")
    a = _assistant(tmp_db)
    assert a.audit.config.mode == "raw_text"
    _ask(a, SMITH_SQL, SMITH_QUESTION)
    row = _rows()[0]
    assert set(row) == COMMON_KEYS | TEXT_KEYS
    assert row["question"] == SMITH_QUESTION
    assert row["executed_sql"] == SMITH_SQL


def test_no_switch_turns_auditing_off(tmp_db, monkeypatch):
    for name in ("ADQ_AUDIT", "ADQ_AUDIT_DISABLED", "ADQ_AUDIT_OFF"):
        monkeypatch.setenv(name, "1")
    a = _assistant(tmp_db)
    _ask(a, "SELECT 1 AS x")
    assert len(_rows()) == 1


# --- the key never leaks -----------------------------------------------------


def test_key_never_in_logs_errors_history_or_output(tmp_db, key, caplog, capsys):
    with caplog.at_level("DEBUG"):
        a = _assistant(tmp_db, cap=2)
        _ask(a, SMITH_SQL, SMITH_QUESTION)
        _ask(a, "SELECT Name FROM Genre")
        errors = []
        for sql in ("DELETE FROM Genre", "SELECT NoSuchColumn FROM Genre"):
            try:
                _ask(a, sql)
            except Exception as e:
                errors.append(repr(e) + str(e))
    out = capsys.readouterr()
    haystacks = [
        caplog.text,
        out.out,
        out.err,
        *errors,
        _log_text(),
        repr(a.audit),
        repr(a.audit.config),
        json.dumps(a.collection.metadatas),
        json.dumps(a.collection.documents),
    ]
    assert len(errors) == 2
    for text in haystacks:
        assert key not in text


def test_audit_logger_repr_hides_key(tmp_db, key):
    cfg = load_audit_config(tmp_db)
    assert key not in repr(cfg)
    assert key not in repr(AuditLogger(cfg))


# --- Streamlit -------------------------------------------------------------


@pytest.fixture
def app(tmp_path, monkeypatch):
    shutil.copyfile(REPO / "chinook.db", tmp_path / "chinook.db")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(FakeCompletions, "default_sql", SMITH_SQL)
    at = AppTest.from_file(str(REPO / "streamlit_app.py"), default_timeout=30)
    at.secrets["openai"] = {"api_key": "test-not-a-key"}
    return at


def test_app_logs_with_uuid_session(app, key):
    app.run()
    app.text_input(key="query").input(SMITH_QUESTION).run()
    assert not app.exception
    rows = _rows()
    assert len(rows) == 1
    assert uuid.UUID(rows[0]["session_id"]).version == 4
    assert rows[0]["session_id"] == app.session_state["session_id"]
    assert rows[0]["log_mode"] == "keyed_hash"


def test_app_key_from_secrets(app):
    from_secrets = secrets.token_hex(32)
    app.secrets["audit"] = {"hmac_key": from_secrets}
    app.run()
    app.text_input(key="query").input(SMITH_QUESTION).run()
    assert _rows()[0]["question_hmac"] == _hmac(from_secrets, SMITH_QUESTION)


def test_app_refuses_to_start_on_non_chinook_without_key(app, tmp_path):
    conn = sqlite3.connect(tmp_path / "chinook.db")
    conn.execute("UPDATE Genre SET Name = 'Fake Name' WHERE GenreId = 1")
    conn.commit()
    conn.close()
    app.run()
    assert not app.exception
    assert len(app.error) == 1
    assert app.error[0].value.startswith(
        "Set ADQ_AUDIT_HMAC_KEY or [audit] hmac_key in .streamlit/secrets.toml"
    )
    assert KEYGEN_COMMAND in app.error[0].value
    assert len(app.text_input) == 0
    assert FakeOpenAI.instances == []


# --- SQLite error text never reaches logs outside raw-text mode -------------

# Each raises a sqlite3 error whose message quotes "Smith". No identifier
# contains "Smith", so any "Smith" in a log could only come from the message.
SMITH_ERRORS = [
    "SELECT NoSuchColumn FROM Customer WHERE LastName = 'Smith' 'Smith'",
    "SELECT * FROM Customer WHERE LastName 'Smith'",
    'SELECT * FROM Customer WHERE LastName = "Smith"',
]


def _smith_error(a, sql):
    with pytest.raises(sqlite3.Error) as excinfo:
        _ask(a, sql, "smith error")
    assert "Smith" in str(excinfo.value)  # the test is meaningful
    return excinfo.value


def _assert_no_smith(caplog, capsys):
    out = capsys.readouterr()
    for text in (_log_text(), caplog.text, out.out, out.err):
        assert "Smith" not in text


@pytest.mark.parametrize("sql", SMITH_ERRORS)
def test_error_text_keyed_mode_class_code_and_hmac(tmp_db, key, caplog, capsys, sql):
    with caplog.at_level("DEBUG"):
        a = _assistant(tmp_db)
        err = _smith_error(a, sql)
    row = _rows()[0]
    assert row["outcome"] == "error"
    assert row["error_class"] == type(err).__name__ == "OperationalError"
    assert row["error_code"] == err.sqlite_errorcode
    assert row["error_hmac"] == _hmac(key, str(err))
    assert "error_message" not in row
    _assert_no_smith(caplog, capsys)


@pytest.mark.parametrize("sql", SMITH_ERRORS)
def test_error_text_outcome_only_class_only(tmp_db, caplog, capsys, sql):
    with caplog.at_level("DEBUG"):
        a = _assistant(tmp_db)
        _smith_error(a, sql)
    row = _rows()[0]
    assert a.audit.config.mode == "outcome_only"
    assert row["error_class"] == "OperationalError"
    assert not ({"error_code", "error_hmac", "error_message"} & set(row))
    assert set(row) == COMMON_KEYS
    _assert_no_smith(caplog, capsys)


def test_error_text_raw_mode_logs_message(tmp_db, monkeypatch):
    monkeypatch.setenv("ADQ_AUDIT_RAW_TEXT", "1")
    a = _assistant(tmp_db)
    err = _smith_error(a, SMITH_ERRORS[0])
    row = _rows()[0]
    assert row["error_message"] == str(err)
    assert "Smith" in row["error_message"]


def test_blocked_rows_carry_class_not_message(tmp_db, key):
    a = _assistant(tmp_db)
    with pytest.raises(GuardError) as excinfo:
        _ask(a, "SELECT * FROM pragma_table_info('Genre')")
    row = _rows()[0]
    assert row["error_class"] == "UnsafeSQLError"
    assert row["error_hmac"] == _hmac(key, str(excinfo.value))
    assert str(excinfo.value) not in _log_text()


@pytest.mark.parametrize("keyed", [True, False])
def test_api_error_log_line_has_no_message_text(
    monkeypatch, tmp_db, caplog, capsys, keyed
):
    if keyed:
        monkeypatch.setenv("ADQ_AUDIT_HMAC_KEY", secrets.token_hex(32))
    a = _assistant(tmp_db)
    a.client.completions.sql = SMITH_ERRORS[0]
    monkeypatch.setattr(mcp_server, "_assistant", a)
    with caplog.at_level("DEBUG"), TestClient(mcp_server.app) as client:
        body = _rpc(client).json()
    # The caller may get its own error text back; server-side logs may not.
    assert "Smith" in body["error"]["message"]
    assert "query failed request_id=" in caplog.text
    assert "error_class=OperationalError" in caplog.text
    assert ("error_hmac=" in caplog.text) is keyed
    _assert_no_smith(caplog, capsys)


def test_non_chinook_dqs_error_not_logged(tmp_path, key, monkeypatch, caplog, capsys):
    db = _modified_copy(tmp_path)
    with caplog.at_level("DEBUG"):
        a = _assistant(db)
        assert a.audit.config.db_is_chinook is False
        with pytest.raises(sqlite3.OperationalError, match='no such column: "Smith"'):
            _ask(a, 'SELECT * FROM Customer WHERE LastName = "Smith"')
        a.client.completions.sql = 'SELECT * FROM Customer WHERE LastName = "Smith"'
        monkeypatch.setattr(mcp_server, "_assistant", a)
        monkeypatch.setattr(mcp_server, "DB_PATH", str(db))
        with TestClient(mcp_server.app) as client:
            body = _rpc(client).json()
    assert body["error"] is not None
    row = _rows()[0]
    assert (row["outcome"], row["error_class"]) == ("error", "OperationalError")
    _assert_no_smith(caplog, capsys)


def test_non_chinook_writes_nothing_to_chroma(tmp_path, key):
    a = _assistant(_modified_copy(tmp_path))
    df, _ = _ask(a, "SELECT Name FROM Genre WHERE GenreId = 2", "genre two")
    assert len(df) == 1
    assert a.collection.documents == []
    assert a.collection.metadatas == []
    assert _rows()[0]["outcome"] == "answered"


def test_chinook_still_writes_chroma_history(tmp_db, key):
    a = _assistant(tmp_db)
    _ask(a, "SELECT Name FROM Genre WHERE GenreId = 2", "genre two")
    assert a.collection.documents == ["genre two"]


def test_app_outcome_only_caption_shown(app):
    app.run()
    assert not app.exception
    captions = [c.value for c in app.caption]
    assert streamlit_app.OUTCOME_ONLY_CAPTION in captions
    assert streamlit_app.OUTCOME_ONLY_CAPTION == (
        "Audit log: outcomes only. "
        "Set ADQ_AUDIT_HMAC_KEY to also log hashed questions and masked SQL."
    )


def test_app_outcome_only_caption_hidden_in_keyed_mode(app, key):
    app.run()
    assert not app.exception
    assert streamlit_app.OUTCOME_ONLY_CAPTION not in [c.value for c in app.caption]


# --- per-connection DB identity (size, mtime_ns, inode; re-hash on change) ---

OLD_NS = 1_600_000_000 * 10**9  # fixed past mtime, so any write changes it


def _startup_db(tmp_path):
    d = tmp_path / "live"
    d.mkdir()
    db = d / "chinook.db"
    shutil.copyfile(REPO / "chinook.db", db)
    os.utime(db, ns=(OLD_NS, OLD_NS))
    return db


def _modified_bytes(tmp_path):
    return _modified_copy(tmp_path, "src.db").read_bytes()


def _swap_same_inode(db, data):
    """Different file contents, same size and inode (written in place)."""
    assert len(data) == db.stat().st_size
    with open(db, "r+b") as f:
        f.write(data)


def _swap_new_inode(db, data, same_mtime=True):
    """Replace the file (new inode); optionally keep size and mtime equal."""
    before = db.stat()
    tmp = db.with_name(db.name + ".new")
    tmp.write_bytes(data)
    if same_mtime:
        os.utime(tmp, ns=(before.st_atime_ns, before.st_mtime_ns))
    os.replace(tmp, db)
    after = db.stat()
    assert after.st_ino != before.st_ino
    if same_mtime:
        assert (after.st_size, after.st_mtime_ns) == (
            before.st_size,
            before.st_mtime_ns,
        )


def _modify_in_place(db):
    conn = sqlite3.connect(db)
    conn.execute("UPDATE Genre SET Name = 'Fake Name' WHERE GenreId = 1")
    conn.commit()
    conn.close()


SWAPS = {
    "same_size_same_inode": lambda db, tp: _swap_same_inode(db, _modified_bytes(tp)),
    "new_inode_same_size_mtime": lambda db, tp: _swap_new_inode(
        db, _modified_bytes(tp)
    ),
    "modified_in_place": lambda db, tp: _modify_in_place(db),
}


# A swapped file is refused with the same class and message as at startup.
REFUSAL = {"outcome_only": MISSING_KEY_MESSAGE, "raw_text": RAW_TEXT_REFUSED_MESSAGE}


@pytest.mark.parametrize("mode", ["outcome_only", "raw_text"])
def test_swap_refusal_matches_startup_refusal(tmp_path, monkeypatch, mode):
    if mode == "raw_text":
        monkeypatch.setenv("ADQ_AUDIT_RAW_TEXT", "1")
    db = _startup_db(tmp_path)
    a = _assistant(db)
    with pytest.raises(AuditConfigError) as at_startup:
        load_audit_config(_modified_copy(tmp_path))
    _swap_new_inode(db, (tmp_path / "modified" / "chinook.db").read_bytes())
    with pytest.raises(AuditConfigError) as on_connect:
        _ask(a, "SELECT 1 AS x")
    assert type(on_connect.value) is type(at_startup.value) is AuditConfigError
    assert str(on_connect.value) == str(at_startup.value) == REFUSAL[mode]


@pytest.fixture
def hash_calls(monkeypatch):
    calls = []
    real = audit_log.file_sha256

    def spy(path):
        calls.append(Path(path))
        return real(path)

    monkeypatch.setattr(audit_log, "file_sha256", spy)
    return calls


def test_unchanged_file_is_not_rehashed(tmp_path, hash_calls):
    a = _assistant(_startup_db(tmp_path))
    assert len(hash_calls) == 1  # startup only
    for _ in range(3):
        _ask(a, "SELECT Name FROM Genre WHERE GenreId = 2")
    assert len(hash_calls) == 1
    assert [r["outcome"] for r in _rows()] == ["answered"] * 3


@pytest.mark.parametrize("mode", ["outcome_only", "raw_text"])
@pytest.mark.parametrize("swap", list(SWAPS))
def test_swap_fails_closed_without_key(
    tmp_path, monkeypatch, hash_calls, caplog, mode, swap
):
    if mode == "raw_text":
        monkeypatch.setenv("ADQ_AUDIT_RAW_TEXT", "1")
    db = _startup_db(tmp_path)
    a = _assistant(db)
    assert a.audit.config.mode == mode
    _ask(a, SMITH_SQL, SMITH_QUESTION)
    SWAPS[swap](db, tmp_path)
    n = len(hash_calls)
    calls_before = len(a.client.completions.calls)
    with caplog.at_level("DEBUG"), pytest.raises(AuditConfigError) as excinfo:
        _ask(a, SMITH_SQL, SMITH_QUESTION)
    assert type(excinfo.value) is AuditConfigError
    assert str(excinfo.value) == REFUSAL[mode]
    assert len(hash_calls) == n + 1  # re-hashed exactly once
    assert len(a.client.completions.calls) == calls_before  # no model call
    row = _rows()[-1]
    assert row["outcome"] == "error"
    assert row["error_class"] == "AuditConfigError"
    assert set(row) == COMMON_KEYS  # class only: no question, SQL or message
    assert "Smith" not in _log_text().splitlines()[-1]
    assert "Smith" not in caplog.text
    # Still refused on the next query (no re-hash: the file did not change).
    with pytest.raises(AuditConfigError, match=re.escape(REFUSAL[mode])):
        _ask(a, "SELECT 1 AS x")
    assert len(hash_calls) == n + 1


@pytest.mark.parametrize("swap", list(SWAPS))
def test_swap_in_keyed_mode_continues_without_chroma(tmp_path, key, swap):
    db = _startup_db(tmp_path)
    a = _assistant(db)
    _ask(a, "SELECT Name FROM Genre WHERE GenreId = 2", "genre two")
    assert a.collection.documents == ["genre two"]
    SWAPS[swap](db, tmp_path)
    df, similar = _ask(a, "SELECT Name FROM Genre WHERE GenreId = 3", "genre three")
    assert len(df) == 1
    assert similar == []  # no Chroma read
    assert a.collection.documents == ["genre two"]  # no Chroma write
    assert _rows()[-1]["outcome"] == "answered"
    assert a.audit.identity.is_chinook is False


@pytest.mark.parametrize("mode", ["outcome_only", "keyed"])
def test_restoring_original_restores_chinook(tmp_path, monkeypatch, mode):
    if mode == "keyed":
        monkeypatch.setenv("ADQ_AUDIT_HMAC_KEY", secrets.token_hex(32))
    db = _startup_db(tmp_path)
    original = db.read_bytes()
    a = _assistant(db)
    _swap_new_inode(db, _modified_bytes(tmp_path))
    if mode == "keyed":
        _ask(a, "SELECT 1 AS x", "one")
        assert a.collection.documents == []
    else:
        with pytest.raises(AuditConfigError) as excinfo:
            _ask(a, "SELECT 1 AS x", "one")
        assert str(excinfo.value) == MISSING_KEY_MESSAGE
    _swap_new_inode(db, original, same_mtime=False)
    df, _ = _ask(a, "SELECT Name FROM Genre WHERE GenreId = 2", "genre two")
    assert len(df) == 1
    assert a.audit.identity.is_chinook is True
    assert a.collection.documents == ["genre two"]
    assert _rows()[-1]["outcome"] == "answered"


def test_identity_check_is_thread_safe(tmp_path, hash_calls):
    import threading

    db = _startup_db(tmp_path)
    cfg = load_audit_config(db)
    _modify_in_place(db)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(cfg.db_identity.check()))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [False] * 8
    assert len(hash_calls) == 2  # startup + one re-hash, not one per thread


def test_audit_config_for_other_db_is_rejected(tmp_db, tmp_path):
    other = tmp_path / "other.db"
    shutil.copyfile(REPO / "chinook.db", other)
    with pytest.raises(ValueError, match="different database"):
        _assistant(tmp_db, audit_config=load_audit_config(other))


class _SharedChroma:
    """Stands in for one on-disk ./chroma_db shared by several assistants."""

    def __init__(self):
        from conftest import FakeChromaClient

        self.clients = {}
        self._cls = FakeChromaClient

    def __call__(self, path=None, **kwargs):
        if path not in self.clients:
            self.clients[path] = self._cls(path=path)
        return self.clients[path]


def test_non_chinook_gets_no_similar_queries_from_existing_store(
    tmp_path, monkeypatch, key
):
    import enhanced_assistant

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        enhanced_assistant.chromadb, "PersistentClient", _SharedChroma()
    )
    seeded = _assistant(_startup_db(tmp_path))
    _ask(seeded, "SELECT Name FROM Genre WHERE GenreId = 2", "genre two")
    _, similar = _ask(seeded, "SELECT Name FROM Genre WHERE GenreId = 3", "three")
    assert similar  # the shared store has chinook history

    other = _assistant(_modified_copy(tmp_path, "other.db"))
    assert other.collection is seeded.collection
    _, similar = _ask(other, "SELECT Name FROM Genre WHERE GenreId = 4", "four")
    assert similar == []
    assert other.collection.documents == ["genre two", "three"]


def test_read_gate_follows_reclassification(tmp_path, key):
    db = _startup_db(tmp_path)
    a = _assistant(db)
    _ask(a, "SELECT Name FROM Genre WHERE GenreId = 2", "genre two")
    _, similar = _ask(a, "SELECT Name FROM Genre WHERE GenreId = 3", "three")
    assert similar
    _modify_in_place(db)
    _, similar = _ask(a, "SELECT Name FROM Genre WHERE GenreId = 4", "four")
    assert similar == []


def test_touch_without_change_rehashes_and_allows(tmp_path, hash_calls):
    db = _startup_db(tmp_path)
    a = _assistant(db)
    assert a.audit.config.mode == "outcome_only"
    assert len(hash_calls) == 1
    os.utime(db)  # new mtime, same contents
    df, _ = _ask(a, "SELECT Name FROM Genre WHERE GenreId = 2")
    assert len(hash_calls) == 2  # exactly one re-hash
    assert len(df) == 1
    assert a.audit.identity.is_chinook is True
    assert _rows()[-1]["outcome"] == "answered"
    _ask(a, "SELECT Name FROM Genre WHERE GenreId = 3")
    assert len(hash_calls) == 2


def test_api_refuses_swapped_db_like_startup(tmp_path, monkeypatch, caplog):
    db = _startup_db(tmp_path)
    a = _assistant(db)
    monkeypatch.setattr(mcp_server, "_assistant", a)
    with TestClient(mcp_server.app) as client:
        _swap_new_inode(db, _modified_bytes(tmp_path))
        with caplog.at_level("DEBUG"):
            body = _rpc(client).json()
    assert body["result"] is None
    assert body["error"]["message"] == MISSING_KEY_MESSAGE
    assert uuid.UUID(body["error"]["data"]["request_id"]).version == 4
    assert "error_class=AuditConfigError" in caplog.text
    assert a.client.completions.calls == []
    assert _rows()[-1]["error_class"] == "AuditConfigError"


def test_app_refuses_swapped_db_like_startup(app):
    app.run()
    assert not app.exception
    db = Path("chinook.db").resolve()
    _swap_new_inode(db, _modified_bytes(db.parent))
    app.text_input(key="query").input(SMITH_QUESTION).run()
    assert not app.exception
    assert [e.value for e in app.error] == [MISSING_KEY_MESSAGE]
    assert not app.success
