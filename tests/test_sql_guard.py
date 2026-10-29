"""Red-team tests for the read-only SQL guard (sql_guard.py).

Every case runs against a tmp_path copy of chinook.db. A case counts as
blocked only if it raised, the DB file's sha256 is unchanged, and no new file
appeared in the DB directory. Fake data uses only RFC 2606 domains and
555-01xx phone numbers.
"""

import hashlib
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import sql_guard  # noqa: E402
from sql_guard import (  # noqa: E402
    DEFAULT_ROW_CAP,
    DEFAULT_STEP_BUDGET,
    DEFAULT_TIMEOUT_S,
    GuardConfig,
    GuardError,
    QueryTimeoutError,
    StepBudgetExceededError,
    UnsafeSQLError,
    load_guard_config,
    open_guarded,
    run_readonly_query,
)

REPO_DB = Path(__file__).resolve().parent.parent / "chinook.db"
# timeout_s is set high so the step budget, not the backstop, decides every
# runaway test.
TEST_CONFIG = GuardConfig(step_budget=1_000_000, row_cap=100, timeout_s=60.0)

# (id, sql). {dir} is the tmp DB directory, {db} the tmp DB path.
RED_TEAM_CASES = [
    # writes
    ("insert", "INSERT INTO Genre (Name) VALUES ('qa@example.com')"),
    ("update", "UPDATE Customer SET Email = 'user@test.invalid' WHERE CustomerId = 1"),
    ("delete", "DELETE FROM InvoiceLine"),
    ("replace", "REPLACE INTO Genre (GenreId, Name) VALUES (1, 'Fake Name')"),
    (
        "upsert",
        "INSERT INTO Genre (GenreId, Name) VALUES (1, 'x') "
        "ON CONFLICT(GenreId) DO UPDATE SET Name = 'x'",
    ),
    (
        "update_returning",
        "UPDATE Customer SET Phone = '+1 555-0142' WHERE CustomerId = 1 "
        "RETURNING CustomerId",
    ),
    ("with_delete", "WITH x AS (SELECT 1) DELETE FROM Genre"),
    (
        "with_insert",
        "WITH x AS (SELECT 1) INSERT INTO Genre (Name) SELECT 'Fake Name' FROM x",
    ),
    (
        "with_update",
        "WITH x AS (SELECT 1) UPDATE Customer SET Email = 'qa@example.org'",
    ),
    ("with_comment_delete", "WITH x AS (SELECT 1) /* ; */ DELETE FROM Genre"),
    # DDL
    ("drop_table", "DROP TABLE Genre"),
    ("drop_index", "DROP INDEX IFK_TrackGenreId"),
    ("create_table", "CREATE TABLE t (x)"),
    ("create_temp_table", "CREATE TEMP TABLE t (x)"),
    ("create_view", "CREATE VIEW v AS SELECT 1"),
    (
        "create_trigger",
        "CREATE TRIGGER trg AFTER INSERT ON Genre BEGIN DELETE FROM Genre; END",
    ),
    ("alter_table", "ALTER TABLE Genre ADD COLUMN x TEXT"),
    # ATTACH / VACUUM
    ("attach_new_file", "ATTACH DATABASE '{dir}/attached_new.db' AS a"),
    ("attach_existing", "ATTACH DATABASE '{db}' AS a"),
    ("vacuum", "VACUUM"),
    ("vacuum_into", "VACUUM INTO '{dir}/copy.db'"),
    # PRAGMA
    ("pragma_journal_wal", "PRAGMA journal_mode = WAL"),
    ("pragma_writable_schema", "PRAGMA writable_schema = ON"),
    ("pragma_query_only_off", "PRAGMA query_only = OFF"),
    ("pragma_table_info", "PRAGMA table_info(Genre)"),
    # transactions / maintenance
    ("begin", "BEGIN"),
    ("savepoint", "SAVEPOINT s"),
    ("analyze", "ANALYZE"),
    ("reindex", "REINDEX"),
    # multi-statement
    ("multi_select_drop", "SELECT 1; DROP TABLE Genre"),
    ("multi_after_literal", "SELECT 'a;b'; DELETE FROM Genre"),
    ("multi_select_select", "SELECT 1; SELECT 2"),
    # comment tricks
    ("comment_prefix_delete", "/* SELECT */ DELETE FROM Genre"),
    ("line_comment_drop", "-- SELECT\nDROP TABLE Genre"),
    ("comment_hides_semicolon", "SELECT 1 /* ; */ ; DROP TABLE Genre"),
    ("line_comment_then_stmt", "SELECT 1 -- note\n; DELETE FROM Genre"),
    ("split_keyword", "SEL/**/ECT 1; DROP TABLE Genre"),
    ("lowercase_whitespace", "  \n\t delete from genre"),
    # functions
    ("load_extension", "SELECT load_extension('{dir}/x')"),
    # runaway queries (must abort on the VM step budget)
    (
        "recursive_no_limit",
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) "
        "SELECT count(*) FROM r",
    ),
    ("cross_join", "SELECT count(*) FROM Track a, Track b, Track c"),
]
STEP_BUDGET_CASES = {"recursive_no_limit", "cross_join"}

POSITIVE_CONTROLS = [
    ("simple_select", "SELECT Name FROM Genre ORDER BY GenreId LIMIT 5", 5),
    (
        "top_customers",
        "SELECT c.CustomerId, c.FirstName, c.LastName, SUM(i.Total) AS total "
        "FROM Customer c JOIN Invoice i ON i.CustomerId = c.CustomerId "
        "GROUP BY c.CustomerId ORDER BY total DESC LIMIT 5",
        5,
    ),
    (
        "genre_revenue",
        "SELECT g.Name, SUM(il.UnitPrice * il.Quantity) AS revenue "
        "FROM InvoiceLine il JOIN Track t ON t.TrackId = il.TrackId "
        "JOIN Genre g ON g.GenreId = t.GenreId GROUP BY g.Name "
        "ORDER BY revenue DESC",
        None,
    ),
    (
        "avg_invoice_by_country",
        "SELECT BillingCountry, AVG(Total) FROM Invoice GROUP BY BillingCountry",
        None,
    ),
    (
        "cte_select",
        "WITH t AS (SELECT GenreId, COUNT(*) AS n FROM Track GROUP BY GenreId) "
        "SELECT * FROM t ORDER BY n DESC LIMIT 3",
        3,
    ),
    (
        "recursive_with_limit",
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r LIMIT 10) "
        "SELECT n FROM r",
        10,
    ),
    ("literal_with_semicolon", "SELECT 'a;b -- c' AS s", 1),
    ("inner_comment", "SELECT /* note; */ 1 AS one -- trailing", 1),
    ("trailing_semicolon", "SELECT 1;", 1),
    ("lowercase", "select name from genre limit 2", 2),
    ("quoted_identifier", 'SELECT "Name" FROM "Genre" LIMIT 1', 1),
    ("fake_data_literals", "SELECT 'qa@example.com' AS email, '555-0142' AS phone", 1),
    ("schema_read", "SELECT name FROM sqlite_master WHERE type = 'table'", None),
]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot(db: Path):
    return _sha256(db), sorted(p.name for p in db.parent.iterdir())


@pytest.fixture
def tmp_db(tmp_path):
    d = tmp_path / "db"
    d.mkdir()
    db = d / "chinook.db"
    shutil.copyfile(REPO_DB, db)
    return db


@pytest.fixture(scope="session", autouse=True)
def _record_limits(redteam_log):
    redteam_log["limits"] = (
        f"limits: tests step_budget={TEST_CONFIG.step_budget} VM steps "
        f"row_cap={TEST_CONFIG.row_cap} backstop={TEST_CONFIG.timeout_s}s; "
        f"defaults step_budget={DEFAULT_STEP_BUDGET} row_cap={DEFAULT_ROW_CAP} "
        f"backstop={DEFAULT_TIMEOUT_S}s "
        "(env ADQ_SQL_STEP_BUDGET / ADQ_SQL_ROW_CAP / ADQ_SQL_TIMEOUT_S)"
    )


def _assert_blocked(tmp_db, sql_template, case_id, expect):
    sql = sql_template.format(dir=tmp_db.parent, db=tmp_db)
    before = _snapshot(tmp_db)
    with pytest.raises(expect) as excinfo:
        run_readonly_query(tmp_db, sql, TEST_CONFIG)
    assert _snapshot(tmp_db) == before, "DB hash changed or a new file appeared"
    if case_id in STEP_BUDGET_CASES:
        err = excinfo.value
        assert isinstance(err, StepBudgetExceededError)
        assert err.reason == "step_budget"
        assert err.budget == TEST_CONFIG.step_budget
        # Aborted on the first progress callback past the budget.
        assert (
            TEST_CONFIG.step_budget
            < err.steps
            <= TEST_CONFIG.step_budget + sql_guard._PROGRESS_STEP_N
        )
    return True


@pytest.mark.parametrize(
    "case_id,sql", RED_TEAM_CASES, ids=[c[0] for c in RED_TEAM_CASES]
)
def test_red_team_blocked(tmp_db, case_id, sql, redteam_log):
    layer = redteam_log["layers"].setdefault("full_guard", {})
    layer[case_id] = False
    layer[case_id] = _assert_blocked(tmp_db, sql, case_id, GuardError)


@pytest.mark.parametrize(
    "case_id,sql", RED_TEAM_CASES, ids=[c[0] for c in RED_TEAM_CASES]
)
def test_red_team_blocked_without_text_check(
    tmp_db, case_id, sql, redteam_log, monkeypatch
):
    """The authorizer is the real gate: bypass the text check, still blocked."""
    monkeypatch.setattr(sql_guard, "check_sql", lambda s: s)
    layer = redteam_log["layers"].setdefault("text_check_bypassed", {})
    layer[case_id] = False
    # Without the text check some inputs fail as plain sqlite3 syntax errors,
    # which still means nothing ran.
    layer[case_id] = _assert_blocked(tmp_db, sql, case_id, (GuardError, sqlite3.Error))


def test_sqlite3_refuses_multiple_statements_per_execute(tmp_db):
    """sqlite3's execute() refuses more than one statement on its own."""
    before = _snapshot(tmp_db)
    conn = open_guarded(tmp_db)
    try:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1; DROP TABLE Genre")
    finally:
        conn.close()
    assert _snapshot(tmp_db) == before


@pytest.mark.parametrize(
    "case_id,sql,expected_rows",
    POSITIVE_CONTROLS,
    ids=[c[0] for c in POSITIVE_CONTROLS],
)
def test_positive_controls_allowed(tmp_db, case_id, sql, expected_rows):
    before = _snapshot(tmp_db)
    columns, rows, truncated = run_readonly_query(tmp_db, sql, TEST_CONFIG)
    assert columns
    assert not truncated
    if expected_rows is not None:
        assert len(rows) == expected_rows
    else:
        assert rows
    assert _snapshot(tmp_db) == before


def test_row_cap_truncates(tmp_db):
    columns, rows, truncated = run_readonly_query(
        tmp_db, "SELECT * FROM Track", TEST_CONFIG
    )
    assert len(rows) == TEST_CONFIG.row_cap
    assert truncated is True


def test_streaming_recursive_without_limit_is_capped(tmp_db):
    """A row-streaming infinite CTE ends at the row cap, not the step budget.

    Reported separately from the red-team count: it is truncated, not blocked.
    """
    sql = (
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) "
        "SELECT n FROM r"
    )
    _, rows, truncated = run_readonly_query(tmp_db, sql, TEST_CONFIG)
    assert truncated is True
    assert len(rows) == TEST_CONFIG.row_cap


def test_exact_cap_is_not_truncated(tmp_db):
    cfg = GuardConfig(
        step_budget=TEST_CONFIG.step_budget, row_cap=25, timeout_s=TEST_CONFIG.timeout_s
    )
    _, rows, truncated = run_readonly_query(
        tmp_db, "SELECT GenreId FROM Genre LIMIT 25", cfg
    )
    assert len(rows) == 25
    assert truncated is False


def test_config_env_overrides_and_defaults(monkeypatch):
    monkeypatch.delenv("ADQ_SQL_TIMEOUT_S", raising=False)
    monkeypatch.delenv("ADQ_SQL_STEP_BUDGET", raising=False)
    monkeypatch.delenv("ADQ_SQL_ROW_CAP", raising=False)
    assert load_guard_config() == GuardConfig(
        DEFAULT_STEP_BUDGET, DEFAULT_ROW_CAP, DEFAULT_TIMEOUT_S
    )
    secrets = {"step_budget": 2000, "row_cap": 50, "timeout_s": 2}
    assert load_guard_config(secrets) == GuardConfig(2000, 50, 2.0)
    monkeypatch.setenv("ADQ_SQL_STEP_BUDGET", "1500")
    monkeypatch.setenv("ADQ_SQL_ROW_CAP", "7")
    monkeypatch.setenv("ADQ_SQL_TIMEOUT_S", "1.5")
    assert load_guard_config(secrets) == GuardConfig(1500, 7, 1.5)
    monkeypatch.setenv("ADQ_SQL_ROW_CAP", "0")
    with pytest.raises(ValueError):
        load_guard_config()


def test_step_budget_is_deterministic(tmp_db):
    """Same query, same budget: the abort lands on the same step count."""
    cfg = GuardConfig(step_budget=20_000, row_cap=100, timeout_s=60.0)
    sql = "SELECT count(*) FROM Track a, Track b"
    counts = []
    for _ in range(3):
        with pytest.raises(StepBudgetExceededError) as excinfo:
            run_readonly_query(tmp_db, sql, cfg)
        counts.append(excinfo.value.steps)
    assert counts == [counts[0]] * 3
    assert counts[0] == cfg.step_budget + sql_guard._PROGRESS_STEP_N


def test_query_under_budget_runs(tmp_db):
    cfg = GuardConfig(step_budget=100_000, row_cap=100, timeout_s=60.0)
    _, rows, truncated = run_readonly_query(
        tmp_db, "SELECT Name FROM Genre LIMIT 5", cfg
    )
    assert len(rows) == 5 and truncated is False


def test_wall_clock_backstop(tmp_db):
    """With an effectively unlimited step budget, the 5 s-style backstop fires."""
    cfg = GuardConfig(step_budget=10**15, row_cap=100, timeout_s=0.05)
    before = _snapshot(tmp_db)
    with pytest.raises(QueryTimeoutError) as excinfo:
        run_readonly_query(
            tmp_db, "SELECT count(*) FROM Track a, Track b, Track c", cfg
        )
    assert excinfo.value.reason == "timeout"
    assert _snapshot(tmp_db) == before


@pytest.mark.parametrize(
    "case_id,sql,reason",
    [
        ("drop_table", "DROP TABLE Genre", "authorizer"),
        ("attach", "ATTACH DATABASE ':memory:' AS x", "authorizer"),
        ("pragma", "PRAGMA query_only = OFF", "authorizer"),
        ("multi_statement", "SELECT 1; DROP TABLE Genre", "sqlite3_api"),
    ],
)
def test_classification_uses_callback_flags(tmp_db, monkeypatch, case_id, sql, reason):
    """Bypass the text check and garble sqlite's wording: classification holds."""
    monkeypatch.setattr(sql_guard, "check_sql", lambda s: s)
    real_connect = sqlite3.connect

    class _Garbled(sqlite3.Connection):
        def cursor(self, *a, **k):
            cur = super().cursor(*a, **k)
            real_execute = cur.execute

            class _Cur:
                description = property(lambda _s: cur.description)

                def execute(self, q):
                    try:
                        return real_execute(q)
                    except sqlite3.ProgrammingError:
                        raise
                    except sqlite3.DatabaseError as e:
                        raise type(e)("reworded by a future sqlite") from e

                fetchmany = cur.fetchmany

            return _Cur()

    monkeypatch.setattr(
        sql_guard.sqlite3,
        "connect",
        lambda *a, **k: real_connect(*a, factory=_Garbled, **k),
    )
    with pytest.raises(UnsafeSQLError) as excinfo:
        run_readonly_query(tmp_db, sql, TEST_CONFIG)
    assert excinfo.value.reason == reason


def test_schema_quotes_table_names(tmp_path):
    from enhanced_assistant import EnhancedQueryAssistant

    db = tmp_path / "odd.db"
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE "we""ird name" (id INTEGER, email TEXT)')
    conn.commit()
    conn.close()
    before = _sha256(db)
    schema = EnhancedQueryAssistant._get_db_schema(SimpleNamespace(db_path=str(db)))
    assert 'Table we"ird name:' in schema
    assert "email (TEXT)" in schema
    assert _sha256(db) == before


def _fake_assistant(db, sql):
    from audit_log import AuditLogger, load_audit_config
    from config import ModelConfig

    stored = []
    return SimpleNamespace(
        db_path=str(db),
        guard_config=TEST_CONFIG,
        model_config=ModelConfig(),
        audit=AuditLogger(load_audit_config(db)),
        _sync_index=lambda: None,
        _find_similar_queries=lambda q: [],
        _generate=lambda q, retry=None: (sql, None),
        _store_query=lambda q, s, m: stored.append(m),
        stored=stored,
    )


def test_execute_direct_uses_guard(tmp_db):
    from enhanced_assistant import EnhancedQueryAssistant

    before = _snapshot(tmp_db)
    fake = _fake_assistant(tmp_db, "DROP TABLE Genre")
    with pytest.raises(UnsafeSQLError):
        EnhancedQueryAssistant._execute_direct(fake, "please drop genre")
    assert _snapshot(tmp_db) == before
    assert fake.stored == []

    fake = _fake_assistant(tmp_db, "SELECT Name FROM Genre LIMIT 3")
    df, similar = EnhancedQueryAssistant._execute_direct(fake, "three genres")
    assert list(df.columns) == ["Name"]
    assert len(df) == 3
    assert df.attrs["truncated"] is False
    assert fake.stored[0]["row_count"] == 3
    assert fake.stored[0]["truncated"] is False


def test_double_quoted_strings_are_identifiers(tmp_db):
    # DQS off: "Smith" is a column name, and Customer has no such column, so
    # the query errors instead of matching LastName against the string Smith.
    with pytest.raises(sqlite3.OperationalError, match="no such column"):
        run_readonly_query(
            tmp_db, 'SELECT * FROM Customer WHERE LastName = "Smith"', TEST_CONFIG
        )
    cols, rows, _ = run_readonly_query(
        tmp_db, "SELECT LastName FROM Customer WHERE LastName = 'Smith'", TEST_CONFIG
    )
    assert rows == [("Smith",)]
    # Double-quoted identifiers still work.
    _, rows, _ = run_readonly_query(
        tmp_db, 'SELECT "LastName" FROM "Customer" LIMIT 1', TEST_CONFIG
    )
    assert len(rows) == 1


def test_dqs_disabled_on_connection(tmp_db):
    conn = sql_guard.open_guarded(tmp_db)
    try:
        assert conn.getconfig(sqlite3.SQLITE_DBCONFIG_DQS_DML) is False
        assert conn.getconfig(sqlite3.SQLITE_DBCONFIG_DQS_DDL) is False
    finally:
        conn.close()
