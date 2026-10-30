"""F2 retrieval: prompts, one SQLite retry, offline embeddings, dev-split files.

Sockets stay blocked. No API key. The ONNX file is used only when it is
already on disk. A missing file raises OfflineModelMissing.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from conftest import FakeChromaClient

from clinic_generator import generate
from clinic_schema import SCHEMA_SQL
from config import BUDGET_USD, DEFAULT_MODEL_ID
from enhanced_assistant import (
    SQL_SYSTEM_PROMPT,
    EnhancedQueryAssistant,
    sql_user_prompt,
)
from retrieval import (
    CUTOFF_GRID,
    IMAGE_ONNX_DIR,
    MODEL_MANIFEST_PATH,
    ONNX_MODEL_FILES,
    TABLE_K_GRID,
    OfflineModelMissing,
    SchemaIndex,
    Trial,
    assert_dev_split,
    assert_ids_disjoint,
    collection_names,
    coverage_at_k,
    iter_table_documents,
    load_embedding_function,
    question_sha256,
    read_jsonl,
    recall_at_k,
    schema_ddl_hash,
    score_rankings,
    select_params,
    verify_model_dir,
    write_dev_report,
)
from spend_meter import SpendMeter, SpendRefused
from sql_guard import GuardError, check_sql, open_readonly_plain

REPO = Path(__file__).resolve().parent.parent
DEV_PATH = REPO / "eval" / "dev_questions.jsonl"
GOLD_PATH = REPO / "eval" / "gold_tables.jsonl"
SEED_PATH = REPO / "eval" / "seed_pairs.jsonl"
BLOCKED_PATH = REPO / "eval" / "blocked.json"
PARAMS_PATH = REPO / "eval" / "retrieval_params.json"
REPORT_PATH = REPO / "eval" / "retrieval_report_dev.json"
TEST_QUESTIONS_PATH = REPO / "eval" / "test_questions.jsonl"
TUNER_PATH = REPO / "eval" / "tune_retrieval.py"

_HAS_GIT = shutil.which("git") is not None and (REPO / ".git").exists()
_REQUIRE_GIT = os.environ.get("ADQ_REQUIRE_GIT") == "1"

# Same filter the chroma key uses, duplicated here so this test does not call
# schema_ddl_text. The DDL text itself comes from clinic_schema.SCHEMA_SQL.
_INDEPENDENT_DDL = """
SELECT type, name, sql
FROM sqlite_master
WHERE sql IS NOT NULL
  AND type IN ('table', 'view')
  AND name NOT LIKE 'sqlite_%'
ORDER BY type, name
"""


def _chinook(tmp_path: Path) -> Path:
    db = tmp_path / "chinook.db"
    shutil.copyfile(REPO / "chinook.db", db)
    return db


def _assistant(db: Path) -> EnhancedQueryAssistant:
    return EnhancedQueryAssistant(str(db), "test-not-a-key")


def _user_text(call: dict) -> str:
    return call["messages"][-1].content[0].text


def _audit_rows() -> list[dict]:
    path = Path(os.environ["ADQ_AUDIT_PATH"])
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _ledger_rows() -> list[dict]:
    path = Path(os.environ["ADQ_SPEND_LEDGER_PATH"])
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _store_text(client) -> str:
    chunks = []
    for collection in client.collections.values():
        chunks.append(json.dumps(collection.documents))
        chunks.append(json.dumps(collection.metadatas))
    return "\n".join(chunks)


def _prompt_tokens(assistant, question: str) -> int:
    user = assistant.render_prompt(question)
    return SpendMeter.estimate_prompt_tokens(
        SQL_SYSTEM_PROMPT
    ) + SpendMeter.estimate_prompt_tokens(user)


def _independent_ddl_hash(db_path: Path) -> str:
    """SHA-256 of the clinic DDL file after executescript(SCHEMA_SQL).

    Serialization is local to this test. It does not call schema_ddl_text.
    """
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(_INDEPENDENT_DDL).fetchall()
    finally:
        conn.close()
    text = "".join(f"{typ}\n{name}\n{sql}\n" for typ, name, sql in rows)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _grid_points() -> set[tuple]:
    return {(k_tables, cutoff) for k_tables in TABLE_K_GRID for cutoff in CUTOFF_GRID}


def _assert_no_pass_fail_keys(value) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            folded = str(key).casefold()
            assert "pass" not in folded
            assert "fail" not in folded
            _assert_no_pass_fail_keys(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_pass_fail_keys(item)


def _master_names(db_path: Path) -> set[str]:
    conn = open_readonly_plain(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
    finally:
        conn.close()
    return {row[0] for row in rows}


def test_ddl_hash_changes_when_schema_changes(tmp_path):
    db = tmp_path / "schema.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE Genre (GenreId INTEGER, Name TEXT)")
    conn.execute("INSERT INTO Genre VALUES (1, 'a')")
    conn.commit()
    conn.close()
    first = schema_ddl_hash(str(db))
    conn = sqlite3.connect(db)
    conn.execute("UPDATE Genre SET Name = 'b' WHERE GenreId = 1")
    conn.commit()
    conn.close()
    assert schema_ddl_hash(str(db)) == first
    conn = sqlite3.connect(db)
    conn.execute("CREATE INDEX ix_genre_name ON Genre (Name)")
    conn.commit()
    conn.close()
    assert schema_ddl_hash(str(db)) == first
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE Genre ADD COLUMN extra_marker TEXT")
    conn.commit()
    conn.close()
    assert schema_ddl_hash(str(db)) != first
    conn = sqlite3.connect(db)
    conn.execute("CREATE VIEW GenreView AS SELECT GenreId FROM Genre")
    conn.commit()
    conn.close()
    documents = dict(iter_table_documents(str(db)))
    assert documents["GenreView"].startswith("View GenreView:")
    assert "extra_marker" in documents["Genre"]


def test_index_rebuilds_when_ddl_hash_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("ADQ_AUDIT_HMAC_KEY", "ab" * 32)
    db = _chinook(tmp_path)
    assistant = _assistant(db)
    assistant.client.scripted_sql = "SELECT 1 AS x"
    question = "list genre names once"
    assistant.execute_query(question)
    old_name = assistant.collection.name
    old_docs = list(assistant.collection.documents)
    assert old_docs == [question]
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE Genre ADD COLUMN extra_marker TEXT")
    conn.commit()
    conn.close()
    _frame, similar = assistant.execute_query("list genre names again")
    assert assistant.collection.name != old_name
    new_hash = schema_ddl_hash(str(db))
    assert assistant.collection.name == collection_names(new_hash)[2]
    assert assistant.chroma_client.collections[old_name].documents == old_docs
    assert question not in assistant.collection.documents
    assert all(item["natural_query"] != question for item in similar)
    blob = "\n".join(assistant.index.tables.documents)
    assert "extra_marker" in blob


def test_full_hash_is_in_collection_metadata(tmp_path):
    db = _chinook(tmp_path)
    assistant = _assistant(db)
    digest = schema_ddl_hash(str(db))
    assert len(digest) == 64
    names = collection_names(digest)
    assert assistant.collection.name == names[2]
    for collection in (
        assistant.index.tables,
        assistant.index.seeds,
        assistant.index.users,
    ):
        assert collection.metadata["ddl_hash"] == digest
    assert assistant.index.ddl_hash == digest


def test_clinic_ddl_hash_matches_cdb_schema(tmp_path):
    """Chroma's clinic key is the DDL in clinic_schema.SCHEMA_SQL.

    The schema-only database is that script. The populated file is created
    the same way as the C-DB tests: clinic_generator.generate(path, 11).
    """
    schema_only = tmp_path / "schema-only.db"
    conn = sqlite3.connect(schema_only)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()
    independent = _independent_ddl_hash(schema_only)
    assert schema_ddl_hash(str(schema_only)) == independent
    clinic = tmp_path / "clinic.db"
    generate(clinic, 11)
    assert schema_ddl_hash(str(clinic)) == independent
    client = FakeChromaClient()
    index = SchemaIndex(str(clinic), client, None)
    index.sync()
    names = collection_names(independent)
    assert index.tables.name == names[0]
    assert index.seeds.name == names[1]
    assert index.users.name == names[2]
    assert index.ddl_hash == independent
    for collection in (index.tables, index.seeds, index.users):
        assert collection.metadata["ddl_hash"] == independent


def test_retrieved_tables_and_pairs_are_in_the_prompt(tmp_path):
    assistant = _assistant(_chinook(tmp_path))
    marker = "PLANTED_TABLE_MARKER"
    pair_sql = "SELECT 99 AS planted_pair"
    assistant.index.tables.add(
        ids=["table:Planted"],
        documents=[f"Table Planted:\n  - marker (TEXT)\n{marker}"],
        metadatas=[{"name": "Planted"}],
    )
    assistant.collection.add(
        ids=["user:planted"],
        documents=["How many planted rows are there?"],
        metadatas=[
            {
                "sql_query": pair_sql,
                "timestamp": "seed",
                "question_id": "plant-1",
            }
        ],
    )
    assistant.client.scripted_sql = "SELECT 1 AS x"
    frame, _similar = assistant.execute_query("show the planted table")
    text = _user_text(assistant.client.calls[-1])
    assert marker in text
    assert pair_sql in text
    assert frame.iloc[0, 0] == 1
    assert assistant.collection.metadatas[-1]["sql_query"] == "SELECT 1 AS x"
    assert pair_sql != assistant.collection.metadatas[-1]["sql_query"]


def test_full_schema_mode_prompt_matches_sql_user_prompt(monkeypatch, tmp_path):
    monkeypatch.setenv("ADQ_SCHEMA_MODE", "full")
    assistant = _assistant(_chinook(tmp_path))
    pair_sql = "SELECT 99 AS planted_pair"
    assistant.collection.add(
        ids=["user:planted"],
        documents=["Planted pair question"],
        metadatas=[{"sql_query": pair_sql, "timestamp": "seed"}],
    )
    question = "List three genres"
    prompt = assistant.render_prompt(question)
    assert prompt == sql_user_prompt(assistant.schema, question)
    assert "Relevant tables:" not in prompt
    assert pair_sql not in prompt
    assistant.client.scripted_sql = "SELECT 1 AS x"
    assistant.execute_query(question)
    assert _user_text(assistant.client.calls[0]) == prompt
    assert "SQLite error:" not in prompt


def test_reservation_counts_retrieved_text(tmp_path):
    assistant = _assistant(_chinook(tmp_path))
    marker = "PLANTED_TABLE_MARKER"
    assistant.index.tables.add(
        ids=["table:Planted"],
        documents=[marker],
        metadatas=[{"name": "Planted"}],
    )
    question = "show planted rows"
    prompt = assistant.render_prompt(question)
    expected = _prompt_tokens(assistant, question)
    assistant.client.scripted_sql = "SELECT 1 AS x"
    assistant.execute_query(question)
    reserved = _ledger_rows()[0]
    assert reserved["kind"] == "reserved"
    assert reserved["prompt_tokens"] == expected
    assert marker in prompt
    assert marker in _user_text(assistant.client.calls[0])


def test_retrieved_text_can_trip_the_200k_cap(tmp_path):
    assistant = _assistant(_chinook(tmp_path))
    assistant.index.tables.add(
        ids=["table:Huge"],
        documents=["H" * 400_001],
        metadatas=[{"name": "Huge"}],
    )
    with pytest.raises(SpendRefused):
        assistant.execute_query("tiny question")
    assert assistant.client.calls == []
    assert _ledger_rows() == []


def test_retrieved_example_sql_is_not_executed(tmp_path):
    db = _chinook(tmp_path)
    assistant = _assistant(db)
    pair_sql = "DROP TABLE Genre"
    assistant.collection.add(
        ids=["user:drop"],
        documents=["Remove every genre row"],
        metadatas=[{"sql_query": pair_sql, "timestamp": "seed"}],
    )
    assistant.client.scripted_sql = "SELECT 1 AS x"
    frame, _similar = assistant.execute_query("count genres safely")
    assert frame.iloc[0, 0] == 1
    assert pair_sql in _user_text(assistant.client.calls[0])
    assert assistant.collection.metadatas[-1]["sql_query"] == "SELECT 1 AS x"
    conn = sqlite3.connect(db)
    try:
        count = conn.execute("SELECT COUNT(*) FROM Genre").fetchone()[0]
    finally:
        conn.close()
    assert count > 0


def test_seed_sql_passes_check_sql():
    rows = read_jsonl(SEED_PATH)
    assert len([row for row in rows if row["db"] == "chinook"]) >= 8
    assert len([row for row in rows if row["db"] == "clinic"]) >= 8
    for row in rows:
        checked = check_sql(row["sql"])
        assert checked.split()[0].upper() in ("SELECT", "WITH")


def test_sqlite_error_is_retried_once(tmp_path):
    assistant = _assistant(_chinook(tmp_path))
    assistant.client.scripted_sql = "SELECT missing_alpha FROM Genre"
    with pytest.raises(sqlite3.OperationalError):
        assistant.execute_query("list genres")
    assert len(assistant.client.calls) == 2
    rows = _audit_rows()
    assert len(rows) == 1
    assert rows[0]["outcome"] == "error"


def test_guard_block_is_not_retried(tmp_path):
    assistant = _assistant(_chinook(tmp_path))
    assistant.client.scripted_sql = "DROP TABLE Genre"
    with pytest.raises(GuardError):
        assistant.execute_query("remove genres")
    assert len(assistant.client.calls) == 1
    assert "SQLite error:" not in _user_text(assistant.client.calls[0])
    rows = _audit_rows()
    assert len(rows) == 1
    assert rows[0]["outcome"] == "blocked"


def test_retry_guard_block_is_final(tmp_path):
    assistant = _assistant(_chinook(tmp_path))
    assistant.client.scripted_sqls = [
        "SELECT missing_alpha FROM Genre",
        "DROP TABLE Genre",
    ]
    with pytest.raises(GuardError):
        assistant.execute_query("list genres")
    assert len(assistant.client.calls) == 2
    rows = _audit_rows()
    assert len(rows) == 1
    assert rows[0]["outcome"] == "blocked"


@pytest.mark.parametrize("mode", ["retrieval", "full"])
def test_retry_error_is_only_in_the_second_prompt(monkeypatch, tmp_path, mode):
    monkeypatch.setenv("ADQ_SCHEMA_MODE", mode)
    assistant = _assistant(_chinook(tmp_path))
    assistant.client.scripted_sql = "SELECT missing_alpha FROM Genre"
    with pytest.raises(sqlite3.OperationalError):
        assistant.execute_query("list genres")
    first = _user_text(assistant.client.calls[0])
    second = _user_text(assistant.client.calls[1])
    assert "missing_alpha" not in first
    assert "SQLite error:" not in first
    assert "SELECT missing_alpha FROM Genre" in second
    assert "missing_alpha" in second
    assert "no such column: missing_alpha" in second


@pytest.mark.parametrize("keyed", [False, True])
def test_retry_error_not_in_audit_or_logs(tmp_path, monkeypatch, caplog, keyed):
    if keyed:
        monkeypatch.setenv("ADQ_AUDIT_HMAC_KEY", "cd" * 32)
    assistant = _assistant(_chinook(tmp_path))
    assistant.client.scripted_sqls = [
        "SELECT 1 FROM Genre WHERE Name = 'zzAlphaCol' 'zzAlphaCol'",
        "SELECT 1 FROM Genre WHERE Name = 'zzBetaCol' 'zzBetaCol'",
    ]
    with caplog.at_level("DEBUG"), pytest.raises(sqlite3.OperationalError):
        assistant.execute_query("count the genre rows")
    assert len(assistant.client.calls) == 2
    audit = Path(os.environ["ADQ_AUDIT_PATH"]).read_text(encoding="utf-8")
    stored = _store_text(assistant.chroma_client)
    for token in ("zzAlphaCol", "zzBetaCol"):
        assert token not in audit
        assert token not in caplog.text
        assert token not in stored
    assert "zzAlphaCol" in _user_text(assistant.client.calls[1])


def test_retry_first_error_not_in_raw_text_row(tmp_path, monkeypatch):
    monkeypatch.setenv("ADQ_AUDIT_RAW_TEXT", "1")
    assistant = _assistant(_chinook(tmp_path))
    assistant.client.scripted_sqls = [
        "SELECT 1 FROM Genre WHERE Name = 'zzAlphaCol' 'zzAlphaCol'",
        "SELECT 1 FROM Genre WHERE Name = 'zzBetaCol' 'zzBetaCol'",
    ]
    with pytest.raises(sqlite3.OperationalError) as excinfo:
        assistant.execute_query("count the genre rows")
    rows = _audit_rows()
    assert len(rows) == 1
    assert rows[0]["error_message"] == str(excinfo.value)
    assert "zzBetaCol" in rows[0]["error_message"]
    assert "zzAlphaCol" not in json.dumps(rows[0])


def test_retry_is_its_own_reservation(tmp_path):
    assistant = _assistant(_chinook(tmp_path))
    assistant.client.usage = None
    assistant.client.scripted_sql = "SELECT missing_alpha FROM Genre"
    with pytest.raises(sqlite3.OperationalError):
        assistant.execute_query("list genres")
    rows = _ledger_rows()
    assert [row["kind"] for row in rows] == [
        "reserved",
        "settled",
        "reserved",
        "settled",
    ]
    assert rows[0]["prompt_tokens"] != rows[2]["prompt_tokens"]
    assert rows[1]["prompt_tokens"] == rows[0]["prompt_tokens"]
    assert rows[3]["prompt_tokens"] == rows[2]["prompt_tokens"]


def test_retry_reserve_refusal_makes_no_second_call(tmp_path):
    assistant = _assistant(_chinook(tmp_path))
    assistant.client.usage = None
    assistant.client.scripted_sql = "SELECT missing_alpha FROM Genre"
    question = "list genres"
    tokens = _prompt_tokens(assistant, question)
    worst = SpendMeter.worst_case_usd(DEFAULT_MODEL_ID, tokens)
    path = Path(os.environ["ADQ_SPEND_LEDGER_PATH"])
    path.parent.mkdir(parents=True, exist_ok=True)
    seed = {
        "timestamp": "2026-10-28T00:00:00+00:00",
        "kind": "settled",
        "reservation_id": "seed-reservation",
        "model": DEFAULT_MODEL_ID,
        "prompt_tokens": 0,
        "cached_prompt_tokens": 0,
        "completion_tokens": 0,
        "usd": format(BUDGET_USD - worst, "f"),
    }
    path.write_text(json.dumps(seed) + "\n", encoding="utf-8", newline="\n")
    with pytest.raises(SpendRefused):
        assistant.execute_query(question)
    assert len(assistant.client.calls) == 1
    assert [row["kind"] for row in _ledger_rows()] == [
        "settled",
        "reserved",
        "settled",
    ]


def test_api_error_is_not_retried(tmp_path):
    assistant = _assistant(_chinook(tmp_path))
    assistant.client.parse_error = RuntimeError("parse failed")
    with pytest.raises(RuntimeError, match="parse failed"):
        assistant.execute_query("one")
    assert len(assistant.client.calls) == 1


def test_clinic_run_writes_no_raw_question_text(tmp_path, monkeypatch):
    monkeypatch.setenv("ADQ_AUDIT_HMAC_KEY", "ef" * 32)
    clinic = tmp_path / "clinic.db"
    generate(clinic, 11)
    question = "Count clinicians without showing a name"
    assistant = _assistant(clinic)
    assistant.client.scripted_sql = "SELECT COUNT(*) AS n FROM clinician"
    assistant.execute_query(question)
    assert question not in _store_text(assistant.chroma_client)
    assert assistant.collection.documents == []


def test_eval_question_is_not_retrievable(tmp_path):
    dev = next(row for row in read_jsonl(DEV_PATH) if row["id"] == "chinook-dev-01")
    assistant = _assistant(_chinook(tmp_path))
    assert (
        assistant.index.add_user_pair(
            "totally different wording",
            "SELECT 1 AS x",
            {},
            allow_raw_write=True,
            question_id=dev["id"],
        )
        is False
    )
    assert (
        assistant.index.add_user_pair(
            dev["question"],
            "SELECT 1 AS x",
            {},
            allow_raw_write=True,
            question_id=None,
        )
        is False
    )
    assert dev["question"] not in assistant.collection.documents
    assistant.collection.add(
        ids=["user:sneak"],
        documents=[dev["question"]],
        metadatas=[
            {
                "sql_query": "SELECT 1 AS x",
                "timestamp": "t",
                "question_id": dev["id"],
            }
        ],
    )
    _tables, pairs = assistant.index.retrieve(dev["question"], include_user=True)
    assert all(pair.question != dev["question"] for pair in pairs)
    assert all(pair.question_id != dev["id"] for pair in pairs)


def test_question_id_sets_must_be_disjoint(tmp_path):
    dev = tmp_path / "dev.jsonl"
    seed = tmp_path / "seed.jsonl"
    test = tmp_path / "test.jsonl"
    dev.write_text(
        '{"id":"same","db":"chinook","split":"dev","question":"a"}\n',
        encoding="utf-8",
    )
    seed.write_text(
        '{"id":"same","db":"chinook","question":"b","sql":"SELECT 1"}\n',
        encoding="utf-8",
    )
    test.write_text(
        '{"id":"other","db":"chinook","split":"test","question":"c"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        assert_ids_disjoint([dev, seed, test])
    seed.write_text(
        '{"id":"seed-only","db":"chinook","question":"b","sql":"SELECT 1"}\n',
        encoding="utf-8",
    )
    test.write_text(
        '{"id":"test-only","db":"chinook","split":"test","question":"c"}\n',
        encoding="utf-8",
    )
    assert_ids_disjoint([dev, seed, test])


def test_committed_dev_seed_ids_do_not_overlap():
    paths = [DEV_PATH, SEED_PATH]
    if TEST_QUESTIONS_PATH.is_file():
        paths.append(TEST_QUESTIONS_PATH)
    assert_ids_disjoint(paths)


def test_dev_ids_match_gold_and_blocked():
    dev = read_jsonl(DEV_PATH)
    gold = read_jsonl(GOLD_PATH)
    seeds = read_jsonl(SEED_PATH)
    blocked = json.loads(BLOCKED_PATH.read_text(encoding="utf-8"))
    assert {row["id"] for row in dev} == {row["id"] for row in gold}
    assert len(dev) == 40
    assert sum(row["db"] == "chinook" for row in dev) == 20
    assert sum(row["db"] == "clinic" for row in dev) == 20
    assert all(row["split"] == "dev" for row in dev)
    assert all(row["split"] == "dev" for row in gold)
    blocked_ids = set(blocked["ids"])
    blocked_hashes = set(blocked["question_sha256"])
    for row in dev:
        assert row["id"] in blocked_ids
        assert question_sha256(row["question"]) in blocked_hashes
    seed_ids = {row["id"] for row in seeds}
    assert seed_ids.isdisjoint(blocked_ids)
    assert seed_ids.isdisjoint({row["id"] for row in dev})
    assert seed_ids.isdisjoint({row["id"] for row in gold})
    for row in seeds:
        assert question_sha256(row["question"]) not in blocked_hashes


def test_gold_tables_exist_in_schema(tmp_path):
    clinic = tmp_path / "clinic.db"
    generate(clinic, 11)
    names = {
        "chinook": _master_names(REPO / "chinook.db"),
        "clinic": _master_names(clinic),
    }
    for row in read_jsonl(GOLD_PATH):
        assert row["tables"]
        for table in row["tables"]:
            assert table in names[row["db"]]


def test_scorer_recall_and_coverage():
    gold = ["A", "B"]
    ranked = ["A", "C", "B", "D", "E"]
    assert recall_at_k(gold, ranked, 1) == 0.5
    assert recall_at_k(gold, ranked, 3) == 1.0
    assert recall_at_k(gold, ranked, 5) == 1.0
    assert coverage_at_k(gold, ranked, 1) == 0.0
    assert coverage_at_k(gold, ranked, 3) == 1.0
    assert coverage_at_k(gold, ranked, 5) == 1.0
    rows = [
        {"gold": gold, "ranked": ranked},
        {"gold": ["A"], "ranked": ["Z", "A", "B", "C", "D"]},
    ]
    scored = score_rankings(rows)
    assert scored["n"] == 2
    assert scored["recall_at_1"] == 0.25
    assert scored["recall_at_3"] == 1.0
    assert scored["recall_at_5"] == 1.0
    assert scored["coverage_at_1"] == 0.0
    assert scored["coverage_at_3"] == 1.0
    assert scored["coverage_at_5"] == 1.0


def test_tuner_selects_by_coverage_then_recall_then_k_then_cutoff():
    higher_coverage = [
        Trial(1, 0.25, 0.1, 1.0),
        Trial(5, 0.75, 0.9, 0.0),
    ]
    chosen = select_params(higher_coverage)
    assert (chosen.k_tables, chosen.max_distance, chosen.k_pairs) == (5, 0.75, 3)
    higher_recall = [
        Trial(5, 0.75, 0.5, 0.9),
        Trial(1, 0.25, 0.5, 0.2),
    ]
    chosen = select_params(higher_recall)
    assert (chosen.k_tables, chosen.max_distance) == (5, 0.75)
    smaller_k = [
        Trial(5, 0.25, 0.4, 0.4),
        Trial(1, 0.75, 0.4, 0.4),
        Trial(3, None, 0.4, 0.4),
    ]
    assert select_params(smaller_k).k_tables == 1
    stricter = [
        Trial(3, None, 0.4, 0.4),
        Trial(3, 1.01, 0.4, 0.4),
        Trial(3, 0.65, 0.4, 0.4),
        Trial(3, 0.25, 0.4, 0.4),
    ]
    assert select_params(stricter).max_distance == 0.25
    none_last = [Trial(3, None, 0.4, 0.4), Trial(3, 1.01, 0.4, 0.4)]
    assert select_params(none_last).max_distance == 1.01


def test_report_writer_lists_every_grid_point(tmp_path):
    trials = [
        Trial(k_tables, cutoff, 0.5, 0.25)
        for k_tables in TABLE_K_GRID
        for cutoff in CUTOFF_GRID
    ]
    assert len(trials) == len(TABLE_K_GRID) * len(CUTOFF_GRID)
    chosen = select_params(trials)
    rankings = [
        {
            "db": "chinook",
            "gold": ["Genre"],
            "ranked": ["Genre", "Track"],
            "operating": ["Genre"],
        },
        {
            "db": "clinic",
            "gold": ["patient", "owner"],
            "ranked": ["patient"],
            "operating": ["patient"],
        },
    ]
    path = tmp_path / "retrieval_report_dev.json"
    report = write_dev_report(path, trials=trials, rankings=rankings, chosen=chosen)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded == report
    assert report["split"] == "dev"
    assert report["n"] == 2
    assert report["k_tables"] == 1
    assert report["max_distance"] == 0.25
    assert report["cutoff"] == report["max_distance"]
    assert report["k_tables"] == chosen.k_tables
    assert report["max_distance"] == chosen.max_distance
    for key in (
        "recall_at_1",
        "recall_at_3",
        "recall_at_5",
        "coverage_at_1",
        "coverage_at_3",
        "coverage_at_5",
        "coverage_at_k",
    ):
        assert key in report
    assert report["coverage_at_k"] == 0.5
    assert set(report["by_db"]) == {"chinook", "clinic"}
    points = {(point["k_tables"], point["cutoff"]) for point in report["grid"]}
    assert points == _grid_points()
    for point in report["grid"]:
        assert "coverage_at_k" in point
        assert "recall_at_k" in point
        assert "max_distance" in point
    _assert_no_pass_fail_keys(report)


def test_committed_params_are_on_the_grid():
    if not PARAMS_PATH.is_file():
        pytest.skip("pending in-image tune run")
    data = json.loads(PARAMS_PATH.read_text(encoding="utf-8"))
    assert data["k_tables"] in TABLE_K_GRID
    assert data["k_pairs"] == 3
    assert data["max_distance"] in CUTOFF_GRID
    assert "k_tables_grid" in data
    assert "cutoff_grid" in data


def test_dev_report_schema():
    if not REPORT_PATH.is_file():
        pytest.skip("pending in-image tune run")
    data = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    dev_count = len(read_jsonl(DEV_PATH))
    assert data["split"] == "dev"
    assert data["n"] == dev_count
    for key in (
        "k_tables",
        "max_distance",
        "cutoff",
        "recall_at_1",
        "recall_at_3",
        "recall_at_5",
        "coverage_at_1",
        "coverage_at_3",
        "coverage_at_5",
        "coverage_at_k",
        "by_db",
        "grid",
    ):
        assert key in data
    assert set(data["by_db"]) >= {"chinook", "clinic"}
    assert len(data["grid"]) == len(TABLE_K_GRID) * len(CUTOFF_GRID)
    points = {(point["k_tables"], point["max_distance"]) for point in data["grid"]}
    assert points == _grid_points()
    for point in data["grid"]:
        assert "coverage_at_k" in point
        assert "recall_at_k" in point
    _assert_no_pass_fail_keys(data)


def test_tune_refuses_test_split(tmp_path):
    test_path = tmp_path / "test_questions.jsonl"
    test_path.write_text(
        '{"id":"t","split":"dev","db":"chinook","question":"q"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        assert_dev_split(test_path)
    with pytest.raises(ValueError):
        assert_dev_split(Path("test_questions.jsonl"))
    bad = tmp_path / "dev_questions.jsonl"
    bad.write_text(
        '{"id":"t","split":"test","db":"chinook","question":"q"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        assert_dev_split(bad)
    ok = tmp_path / "ok_dev.jsonl"
    ok.write_text(
        '{"id":"t","split":"dev","db":"chinook","question":"q"}\n',
        encoding="utf-8",
    )
    assert_dev_split(ok)


def test_tuner_does_not_import_spend_meter_or_model_clients():
    text = TUNER_PATH.read_text(encoding="utf-8")
    for banned in ("spend_meter", "openai", "xai_sdk", "enhanced_assistant"):
        assert banned not in text


def test_chroma_path_comes_from_env(monkeypatch, tmp_path):
    path = tmp_path / "chroma-from-env"
    monkeypatch.setenv("ADQ_CHROMA_PATH", str(path))
    assistant = _assistant(_chinook(tmp_path))
    assert assistant.chroma_client.path == str(path)
    assert assistant.chroma_client.settings.anonymized_telemetry is False


def _fake_model_dir(tmp_path):
    root = tmp_path / "model"
    root.mkdir()
    pins = {}
    for name in ONNX_MODEL_FILES:
        data = f"fake {name}".encode()
        (root / name).write_bytes(data)
        pins[name] = {
            "path": f"onnx/{name}",
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
        }
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, pins)
    return root, manifest


def _write_manifest(manifest, pins):
    manifest.write_text(
        json.dumps({"status": "pinned", "files": pins, "bootstrap_log": []}),
        encoding="utf-8",
    )


def _pins(manifest):
    return json.loads(manifest.read_text(encoding="utf-8"))["files"]


def test_model_files_list_matches_chromadb():
    import inspect

    from chromadb.utils.embedding_functions import onnx_mini_lm_l6_v2

    source = inspect.getsource(onnx_mini_lm_l6_v2)
    for name in ONNX_MODEL_FILES:
        assert f'"{name}"' in source, name


def test_model_dir_present_and_matching(tmp_path):
    root, manifest = _fake_model_dir(tmp_path)
    verify_model_dir(root, manifest)


def test_model_dir_missing_file_raises(tmp_path):
    root, manifest = _fake_model_dir(tmp_path)
    (root / "tokenizer.json").unlink()
    with pytest.raises(OfflineModelMissing):
        verify_model_dir(root, manifest)
    with pytest.raises(OfflineModelMissing):
        load_embedding_function(str(root), manifest)


def test_model_dir_wrong_hash_raises(tmp_path):
    root, manifest = _fake_model_dir(tmp_path)
    (root / "model.onnx").write_bytes(b"tampered")
    with pytest.raises(OfflineModelMissing):
        verify_model_dir(root, manifest)
    with pytest.raises(OfflineModelMissing):
        load_embedding_function(str(root), manifest)


def test_model_dir_wrong_byte_count_raises(tmp_path):
    root, manifest = _fake_model_dir(tmp_path)
    pins = _pins(manifest)
    pins["vocab.txt"]["bytes"] += 1
    _write_manifest(manifest, pins)
    with pytest.raises(OfflineModelMissing):
        verify_model_dir(root, manifest)


def test_model_dir_unpinned_byte_count_checks_sha_only(tmp_path):
    root, manifest = _fake_model_dir(tmp_path)
    pins = _pins(manifest)
    for entry in pins.values():
        entry["bytes"] = None
    _write_manifest(manifest, pins)
    verify_model_dir(root, manifest)


def test_manifest_null_sha_fails_closed(tmp_path):
    root, manifest = _fake_model_dir(tmp_path)
    pins = _pins(manifest)
    pins["model.onnx"]["sha256"] = None
    _write_manifest(manifest, pins)
    with pytest.raises(OfflineModelMissing):
        verify_model_dir(root, manifest)


def test_committed_manifest_shape():
    data = json.loads(MODEL_MANIFEST_PATH.read_text(encoding="utf-8"))
    assert set(data["files"]) == set(ONNX_MODEL_FILES)
    for name, entry in data["files"].items():
        assert set(entry) == {"path", "sha256", "bytes"}, name
        assert entry["path"] == f"onnx/{name}"
    assert isinstance(data["bootstrap_log"], list)
    if data["status"] != "pinned":
        assert data["bootstrap_log"] == []
        assert all(e["sha256"] is None for e in data["files"].values())
        assert all(e["bytes"] is None for e in data["files"].values())


def test_placeholder_manifest_fails_closed(tmp_path):
    root, _ = _fake_model_dir(tmp_path)
    placeholder = json.loads(MODEL_MANIFEST_PATH.read_text(encoding="utf-8"))
    placeholder["status"] = "placeholder"
    placeholder["bootstrap_log"] = []
    for entry in placeholder["files"].values():
        entry["sha256"] = None
        entry["bytes"] = None
    manifest = tmp_path / "placeholder_manifest.json"
    manifest.write_text(json.dumps(placeholder), encoding="utf-8")
    with pytest.raises(OfflineModelMissing):
        verify_model_dir(root, manifest)


def _isolate_onnx_class(monkeypatch):
    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import (
        ONNXMiniLM_L6_V2,
    )

    for attr in ("DOWNLOAD_PATH", "EXTRACTED_FOLDER_NAME", "_download"):
        monkeypatch.setattr(ONNXMiniLM_L6_V2, attr, getattr(ONNXMiniLM_L6_V2, attr))
    return ONNXMiniLM_L6_V2


def test_embed_with_verified_files_never_downloads(tmp_path, monkeypatch):
    import numpy as np

    cls = _isolate_onnx_class(monkeypatch)
    root, manifest = _fake_model_dir(tmp_path)
    monkeypatch.setattr(
        cls,
        "_forward",
        lambda self, docs, batch_size=32: np.ones((len(docs), 384), np.float32),
    )
    embedding = load_embedding_function(str(root), manifest)
    calls = []
    refuse = cls._download

    def counting(*args, **kwargs):
        calls.append(args)
        return refuse(*args, **kwargs)

    monkeypatch.setattr(cls, "_download", counting)
    vectors = embedding(["offline probe"])
    assert len(list(vectors[0])) == 384
    assert calls == []


def test_embed_with_missing_file_raises_without_fetch(tmp_path, monkeypatch):
    _isolate_onnx_class(monkeypatch)
    root, manifest = _fake_model_dir(tmp_path)
    (root / "vocab.txt").unlink()
    with pytest.raises(OfflineModelMissing):
        load_embedding_function(str(root), manifest)

    root2 = tmp_path / "second"
    root2.mkdir()
    root2, manifest2 = _fake_model_dir(root2)
    embedding = load_embedding_function(str(root2), manifest2)
    (root2 / "model.onnx").unlink()
    with pytest.raises(OfflineModelMissing, match="refusing to download"):
        embedding(["offline probe"])


def test_embedding_unset_dir_raises(monkeypatch):
    monkeypatch.delenv("ADQ_ONNX_MODEL_DIR", raising=False)
    with pytest.raises(OfflineModelMissing):
        load_embedding_function(None)


@pytest.mark.skipif(
    not (Path(IMAGE_ONNX_DIR) / "model.onnx").is_file(),
    reason="in-image only: needs the baked ONNX model (run with --network none)",
)
def test_in_image_real_embed_offline(monkeypatch):
    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import (
        ONNXMiniLM_L6_V2,
    )

    calls = []
    monkeypatch.setattr(
        ONNXMiniLM_L6_V2, "_download", lambda *a, **k: calls.append(a), raising=False
    )
    embedding = load_embedding_function(IMAGE_ONNX_DIR)
    vectors = embedding(["offline probe"])
    assert len(list(vectors[0])) == 384
    assert not calls


@pytest.mark.repo_files
@pytest.mark.skipif(
    not _HAS_GIT and not _REQUIRE_GIT,
    reason="needs a .git checkout (absent in the Docker test image)",
)
def test_onnx_assets_are_not_tracked():
    listed = subprocess.check_output(["git", "ls-files", "-z"], cwd=REPO)
    paths = listed.decode("utf-8", "replace").split("\0")
    for path in paths:
        if not path:
            continue
        name = path.replace("\\", "/")
        assert not name.endswith(".onnx"), name
        assert not name.endswith("onnx.tar.gz"), name
        assert name != "onnx.tar.gz"
        assert not name.startswith("models/"), name
