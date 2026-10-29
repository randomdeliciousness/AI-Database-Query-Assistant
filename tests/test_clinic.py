"""Synthetic vet-clinic generator, snapshot hash, and read-only app path.

Generator output stays under tmp_path. No live model call and no Docker call.
"""

import datetime
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from test_audit_log import COMMON_KEYS, HASH_KEYS

from audit_log import AuditConfigError, load_audit_config, schema_identifiers
from clinic_clock import EPOCH, SimulatedClock
from clinic_generator import RefuseOverwrite, generate, price_on, resume_one_step
from clinic_schema import (
    CLINIC_SAMPLE_QUERIES,
    NOTICE,
    PLANTED_CASES,
    PLANTED_DELIVERIES,
)
from clinic_snapshot import content_hash, snapshot_document
from config import load_db_path
from enhanced_assistant import EnhancedQueryAssistant
from sql_guard import GuardError, open_readonly_plain, run_readonly_query
from streamlit_app import sample_queries_for

ROOT = Path(__file__).resolve().parent.parent
CLINIC_MODULES = (
    "clinic_schema.py",
    "clinic_clock.py",
    "clinic_snapshot.py",
    "clinic_generator.py",
)
SCHEMA_TABLES = (
    "appointment",
    "appointment_type",
    "client_price",
    "clinician",
    "delivery",
    "inventory_lot",
    "invoice",
    "invoice_line",
    "owner",
    "patient",
    "product",
    "provenance",
    "stock_use",
    "supplier",
    "supplier_price",
    "usage",
    "visit",
)
PROVENANCE_COLUMNS = (
    "provenance_id",
    "data_class",
    "label",
    "notice",
    "generator",
    "seed",
    "clock_start",
    "clock_end",
    "locale",
)
EXPIRY_SQL = (
    "SELECT COUNT(*) FROM stock_use u "
    "JOIN inventory_lot l ON l.lot_id = u.lot_id "
    "WHERE u.used_on >= l.expires_on"
)
T0 = "2024-02-07T14:00:00Z"
_SKIP_PARTS = {
    "tests",
    "__pycache__",
    "venv",
    ".venv",
    "site-packages",
    ".git",
}


@pytest.fixture
def key(monkeypatch):
    value = secrets.token_hex(32)
    monkeypatch.setenv("ADQ_AUDIT_HMAC_KEY", value)
    return value


def _run(args):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONUNBUFFERED"] = "1"
    for name in ("XAI_API_KEY", "OPENAI_API_KEY", "ADQ_AUDIT_HMAC_KEY"):
        env.pop(name, None)
    return subprocess.run(
        [sys.executable, "-m", "clinic_generator", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _popen(args):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONUNBUFFERED"] = "1"
    for name in ("XAI_API_KEY", "OPENAI_API_KEY", "ADQ_AUDIT_HMAC_KEY"):
        env.pop(name, None)
    return subprocess.Popen(
        [sys.executable, "-m", "clinic_generator", *args],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _stop(proc):
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=5)


def _rows(db, sql):
    _columns, rows, truncated = run_readonly_query(db, sql)
    assert truncated is False
    return list(rows)


def _count(db, table):
    return _rows(db, f"SELECT COUNT(*) FROM {table}")[0][0]


def _assert_cases(db):
    for case in PLANTED_CASES:
        assert list(_rows(db, case.sql)) == list(case.expected_rows)


def _plus_day(stamp):
    parsed = SimulatedClock.parse_iso(stamp)
    return (parsed + datetime.timedelta(seconds=86400)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _high_id_rows(db):
    document = snapshot_document(db)
    kept = {}
    for name, pk_columns, column_names, rows in document[1][1]:
        if name == "provenance":
            continue
        index = column_names.index(pk_columns[0])
        kept[name] = [
            row for row in rows if isinstance(row[index], int) and row[index] >= 9000
        ]
    return kept


def _production_sources():
    for path in ROOT.rglob("*.py"):
        if any(part in _SKIP_PARTS for part in path.parts):
            continue
        yield path


def _module_text(name):
    return (ROOT / name).read_text(encoding="utf-8")


def test_same_seed_same_content_hash(tmp_path):
    paths = []
    hashes = []
    for name in ("a", "b", "c"):
        path = tmp_path / name / "clinic.db"
        path.parent.mkdir()
        paths.append(path)
        hashes.append(generate(path, 11))
    assert hashes[0] == hashes[1] == hashes[2]
    for path, digest in zip(paths, hashes, strict=True):
        assert content_hash(path) == digest
    for path in paths[:2]:
        assert _rows(path, "SELECT clock_end FROM provenance") == [(T0,)]


def test_different_seed_different_content_hash(tmp_path):
    first = tmp_path / "a.db"
    second = tmp_path / "b.db"
    hash_11 = generate(first, 11)
    hash_29 = generate(second, 29)
    assert hash_11 != hash_29

    def owner_name(path, owner_id):
        return _rows(path, f"SELECT full_name FROM owner WHERE owner_id = {owner_id}")[
            0
        ][0]

    assert owner_name(first, 1) != owner_name(second, 1)
    assert owner_name(first, 9001) == "Synthetic Owner 9001"
    assert owner_name(second, 9001) == "Synthetic Owner 9001"
    assert _rows(first, "SELECT seed FROM provenance") == [(11,)]
    assert _rows(second, "SELECT seed FROM provenance") == [(29,)]


def test_content_hash_matches_across_subprocess(tmp_path):
    inside = tmp_path / "inside.db"
    outside = tmp_path / "outside.db"
    digest = generate(inside, 11)
    proc = _run(["--seed", "11", "--db", str(outside)])
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"content_hash={digest}"
    assert content_hash(outside) == digest


def test_content_hash_schema_section_precedes_tables(tmp_path):
    path = tmp_path / "clinic.db"
    generate(path, 11)
    document = snapshot_document(path)
    assert document[0][0] == "schema"
    assert document[1][0] == "tables"
    schema_rows = document[0][1]
    table_names = [name for typ, name, _sql in schema_rows if typ == "table"]
    assert table_names == list(SCHEMA_TABLES)
    for _typ, name, sql in schema_rows:
        assert not name.startswith("sqlite_")
        if sql:
            assert "REAL" not in sql


def test_content_hash_sorts_each_table_by_primary_key(tmp_path):
    path = tmp_path / "sort.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO t VALUES (2, 'b')")
    conn.execute("INSERT INTO t VALUES (1, 'a')")
    conn.commit()
    conn.close()
    document = snapshot_document(path)
    tables = {name: rows for name, _pk, _cols, rows in document[1][1]}
    assert tables["t"] == [[1, "a"], [2, "b"]]
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE f (id INTEGER PRIMARY KEY, n REAL)")
    conn.execute("INSERT INTO f VALUES (1, 1.5)")
    conn.commit()
    conn.close()
    with pytest.raises(TypeError):
        snapshot_document(path)


def test_generator_refuses_to_overwrite(tmp_path):
    path = tmp_path / "clinic.db"
    digest = generate(path, 11)
    with pytest.raises(FileExistsError):
        generate(path, 11)
    assert content_hash(path) == digest
    with pytest.raises(RefuseOverwrite):
        resume_one_step(path, 29)
    assert content_hash(path) == digest

    folder = tmp_path / "foreign"
    folder.mkdir()
    foreign = folder / "chinook.db"
    shutil.copyfile(ROOT / "chinook.db", foreign)
    before = foreign.read_bytes()
    names = {item.name for item in folder.iterdir()}
    with pytest.raises(RefuseOverwrite):
        resume_one_step(foreign, 11)
    assert foreign.read_bytes() == before
    assert {item.name for item in folder.iterdir()} == names


def test_second_process_does_not_become_a_writer(tmp_path):
    solo = tmp_path / "solo.db"
    solo_hash = generate(solo, 11)
    solo_size = solo.stat().st_size
    path = tmp_path / "race.db"
    procs = [
        _popen(["--seed", "11", "--db", str(path)]),
        _popen(["--seed", "11", "--db", str(path)]),
    ]
    try:
        codes = []
        for proc in procs:
            proc.communicate(timeout=60)
            codes.append(proc.returncode)
    finally:
        for proc in procs:
            _stop(proc)
    assert sorted(codes) == [0, 2]
    assert content_hash(path) == solo_hash
    assert path.stat().st_size >= solo_size


def test_journal_mode_is_wal(tmp_path):
    path = tmp_path / "clinic.db"
    generate(path, 11)
    conn = open_readonly_plain(path)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert str(mode).lower() == "wal"


def test_guard_blocks_writes_while_generator_runs(tmp_path):
    path = tmp_path / "hold.db"
    stop = tmp_path / "stop"
    proc = _popen(["--seed", "11", "--db", str(path), "--stop-file", str(stop)])
    ready = Path(f"{os.fspath(path)}.ready")
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if ready.exists() and proc.poll() is None:
                break
            time.sleep(0.05)
        else:
            _stop(proc)
            raise AssertionError("generator did not reach the stop-file hold")

        def reason(sql):
            with pytest.raises(GuardError) as exc:
                run_readonly_query(path, sql)
            return exc.value.reason

        assert reason("INSERT INTO provenance VALUES (2, 'x')") == "text_check"
        assert (
            reason("UPDATE patient SET patient_name = 'x' WHERE patient_id = 1")
            == "text_check"
        )
        assert reason("DELETE FROM provenance") == "text_check"
        with_insert = (
            "WITH x AS (SELECT 1) INSERT INTO owner "
            "(owner_id, full_name, email, phone, city) VALUES "
            "(50, 'A', 'owner50@example.com', '+1-555-0150', 'North Synthetic')"
        )
        assert reason(with_insert) in {"authorizer", "read_only"}
        assert _count(path, "provenance") == 1
        held = content_hash(path)
        assert content_hash(path) == held
        stop.write_text("stop\n", encoding="utf-8", newline="\n")
        _out, err = proc.communicate(timeout=30)
        assert proc.returncode == 0, err
    finally:
        _stop(proc)
    solo = tmp_path / "solo.db"
    assert content_hash(path) == generate(solo, 11)


def test_provenance_exists_and_is_labeled_synthetic(tmp_path):
    path = tmp_path / "clinic.db"
    generate(path, 11)
    rows = _rows(
        path,
        "SELECT data_class, label, notice, generator, locale FROM provenance",
    )
    assert rows == [("synthetic", "synthetic", NOTICE, "clinic_generator", "en_US")]
    assert _count(path, "provenance") == 1


def test_only_generator_writes_provenance():
    inserts = []
    updates = []
    connects = []
    for path in _production_sources():
        text = path.read_text(encoding="utf-8")
        if "INSERT INTO provenance" in text:
            inserts.append(path.name)
        if "UPDATE provenance" in text:
            updates.append(path.name)
        if "sqlite3.connect(" in text:
            connects.append(path.name)
        if path.name != "clinic_generator.py":
            assert "import clinic_generator" not in text
            assert "from clinic_generator" not in text
    assert inserts == ["clinic_generator.py"]
    assert updates == ["clinic_generator.py"]
    assert sorted(connects) == ["clinic_generator.py", "sql_guard.py"]
    source = _module_text("clinic_generator.py")
    assert "DELETE FROM" not in source
    assert "DROP " not in source
    assert "O_TRUNC" not in source


def test_emails_and_phones_stay_in_synthetic_ranges(tmp_path):
    path = tmp_path / "clinic.db"
    generate(path, 11)
    emails = [row[0] for row in _rows(path, "SELECT email FROM owner")]
    phones = [row[0] for row in _rows(path, "SELECT phone FROM owner")]
    assert emails
    assert all(re.fullmatch(r"owner[0-9]+@example\.com", item) for item in emails)
    assert all(re.fullmatch(r"\+1-555-01[0-9]{2}", item) for item in phones)
    assert _rows(path, "SELECT owner_id FROM owner WHERE phone = '+1-555-0199'") == [
        (9001,)
    ]
    species = {row[0] for row in _rows(path, "SELECT DISTINCT species FROM patient")}
    assert species == {"dog", "cat", "rabbit"}
    assert sorted(
        row[0] for row in _rows(path, "SELECT supplier_name FROM supplier")
    ) == ["Synthetic Supplier 1", "Synthetic Supplier 9001"]
    assert sorted(
        row[0] for row in _rows(path, "SELECT product_name FROM product")
    ) == ["SYN-OVERORDER", "SYN-PRICE-GAP", "SYN-SUPPLY", "SYN-VACCINE"]


def test_clock_ignores_wall_time(tmp_path):
    clock = SimulatedClock()
    with pytest.raises(ValueError):
        clock.advance(-1)
    assert clock.iso() == "2024-01-02T14:00:00Z"
    assert SimulatedClock.parse_iso(clock.iso()) == EPOCH
    with pytest.raises(ValueError):
        SimulatedClock.parse_iso("2024-01-02T14:00:00")
    joined = "\n".join(_module_text(name) for name in CLINIC_MODULES)
    for banned in ("datetime.now", "time.time(", "uuid4"):
        assert banned not in joined
    assert joined.count("time.sleep") == 2
    assert "time.sleep(0.05)" in joined
    assert "time.sleep(0.5)" in joined
    first = tmp_path / "a.db"
    second = tmp_path / "b.db"
    assert generate(first, 11) == generate(second, 11)


def _assert_events(path):
    visits = _rows(path, "SELECT visit_id, appointment_type_id FROM visit")
    assert visits
    for visit_id, type_id in visits:
        stock = _rows(
            path,
            "SELECT product_id, qty FROM stock_use "
            f"WHERE visit_id = {int(visit_id)} ORDER BY stock_use_id",
        )
        usage = _rows(
            path,
            "SELECT product_id, qty FROM usage "
            f"WHERE appointment_type_id = {int(type_id)} ORDER BY usage_id",
        )
        assert stock == usage
        assert stock
    missing = _rows(
        path,
        "SELECT COUNT(*) FROM visit v WHERE NOT EXISTS "
        "(SELECT 1 FROM stock_use u WHERE u.visit_id = v.visit_id)",
    )
    assert missing == [(0,)]
    deliveries = _rows(
        path,
        "SELECT d.supplier_id, d.product_id, d.delivered_on, d.unit_cents, "
        "l.unit_cents FROM delivery d "
        "JOIN inventory_lot l ON l.lot_id = d.lot_id",
    )
    conn = open_readonly_plain(path)
    try:
        for supplier_id, product_id, day, unit, lot_unit in deliveries:
            assert unit == lot_unit
            assert unit == price_on(conn, supplier_id, product_id, day)
    finally:
        conn.close()
    assert _rows(path, EXPIRY_SQL) == [(0,)]
    assert _rows(
        path,
        "SELECT qty_received, qty_on_hand, expires_on FROM inventory_lot "
        "WHERE lot_id = 9001",
    ) == [(2, 1, "2024-01-07")]
    assert _rows(path, "SELECT COUNT(*) FROM stock_use WHERE visit_id = 9001") == [(1,)]


def test_generator_events_stock_delivery_and_expiry(tmp_path):
    first = tmp_path / "a.db"
    second = tmp_path / "b.db"
    generate(first, 11)
    generate(second, 29)
    _assert_events(first)
    _assert_events(second)
    assert _rows(
        first,
        "SELECT qty_received, qty_on_hand FROM inventory_lot WHERE lot_id = 9001",
    ) == _rows(
        second,
        "SELECT qty_received, qty_on_hand FROM inventory_lot WHERE lot_id = 9001",
    )


def test_supplier_lead_time_and_min_order_qty_are_seeded_only(tmp_path):
    source = _module_text("clinic_generator.py")
    assert "lead_time_days" not in source
    assert "min_order_qty" not in source
    for seed, name in ((11, "a.db"), (29, "b.db")):
        path = tmp_path / name
        generate(path, seed)
        assert _rows(
            path,
            "SELECT supplier_id, lead_time_days, min_order_qty FROM supplier "
            "ORDER BY supplier_id",
        ) == [(1, 2, 1), (9001, 5, 10)]
        planted = _rows(
            path,
            "SELECT delivery_id, supplier_id, product_id, lot_id, delivered_on, "
            "qty, unit_cents FROM delivery WHERE delivery_id >= 9000 "
            "ORDER BY delivery_id",
        )
        assert planted == list(PLANTED_DELIVERIES)
        short = _rows(
            path,
            "SELECT delivery_id, qty, delivered_on FROM delivery "
            "WHERE delivery_id IN (9001, 9003, 9004) ORDER BY delivery_id",
        )
        assert short == [
            (9001, 2, "2024-01-02"),
            (9003, 1, "2024-03-01"),
            (9004, 1, "2024-07-15"),
        ]
        assert all(qty < 10 for _delivery_id, qty, _day in short)
        filler_days = [
            row[0]
            for row in _rows(
                path,
                "SELECT delivered_on, qty FROM delivery "
                "WHERE delivery_id < 9000 ORDER BY delivery_id",
            )
        ]
        filler_qty = _rows(
            path,
            "SELECT qty FROM delivery WHERE delivery_id < 9000 ORDER BY delivery_id",
        )
        visit_days = [
            row[0]
            for row in _rows(
                path,
                "SELECT substr(visited_at, 1, 10) FROM visit "
                "WHERE visit_id < 9000 ORDER BY visit_id",
            )
        ]
        assert filler_qty == [(1,)] * len(visit_days)
        assert filler_days == visit_days


def _assert_usage_keeps_cases(path):
    assert _rows(
        path,
        "SELECT usage_id, appointment_type_id, product_id, qty FROM usage "
        "ORDER BY usage_id",
    ) == [(1, 1, 1, 1), (9001, 9001, 9001, 1), (9002, 9002, 9003, 1)]
    assert _rows(
        path,
        "SELECT visit_id, appointment_type_id FROM visit "
        "WHERE visit_id >= 9000 ORDER BY visit_id",
    ) == [(9001, 1), (9002, 9001), (9003, 9001), (9004, 9002)]
    assert _rows(
        path,
        "SELECT COUNT(*) FROM visit WHERE visit_id < 9000 "
        "AND appointment_type_id != 1",
    ) == [(0,)]
    visits = _rows(path, "SELECT visit_id, appointment_type_id FROM visit")
    for visit_id, type_id in visits:
        stock = _rows(
            path,
            "SELECT product_id, qty FROM stock_use "
            f"WHERE visit_id = {int(visit_id)} ORDER BY stock_use_id",
        )
        usage = _rows(
            path,
            "SELECT product_id, qty FROM usage "
            f"WHERE appointment_type_id = {int(type_id)} ORDER BY usage_id",
        )
        assert stock == usage
    assert _rows(path, "SELECT COUNT(*) FROM usage WHERE product_id = 9002") == [(0,)]
    assert _rows(path, "SELECT COUNT(*) FROM stock_use WHERE product_id = 9002") == [
        (0,)
    ]
    appointments = _rows(
        path,
        "SELECT a.appointment_id, a.appointment_type_id, v.visit_id "
        "FROM appointment a LEFT JOIN visit v "
        "ON v.appointment_id = a.appointment_id "
        "WHERE a.appointment_id BETWEEN 9001 AND 9004 "
        "ORDER BY a.appointment_id",
    )
    assert appointments == [
        (9001, 1, None),
        (9002, 1, None),
        (9003, 1, None),
        (9004, 1, None),
    ]
    filler_products = _rows(
        path,
        "SELECT DISTINCT product_id FROM stock_use "
        "WHERE stock_use_id < 9000 ORDER BY product_id",
    )
    assert filler_products == [(1,)]
    _assert_cases(path)


def test_stock_drawn_from_usage_keeps_the_nine_cases(tmp_path):
    for seed, name in ((11, "a.db"), (29, "b.db")):
        path = tmp_path / name
        generate(path, seed)
        _assert_usage_keeps_cases(path)
    resumed = tmp_path / "resumed.db"
    generate(resumed, 11)
    type_count = _count(resumed, "appointment_type")
    usage_count = _count(resumed, "usage")
    resume_one_step(resumed, 11)
    assert _count(resumed, "appointment_type") == type_count
    assert _count(resumed, "usage") == usage_count
    _assert_usage_keeps_cases(resumed)


def test_reorder_points_are_hidden_from_readers(tmp_path, key):
    import clinic_generator

    assert clinic_generator._REORDER_POINTS == {1: 4, 9001: 2, 9002: 2, 9003: 6}
    folded = _module_text("clinic_generator.py").casefold()
    assert folded.count("reorder") == 1
    for name in (
        "clinic_schema.py",
        "README.md",
        "streamlit_app.py",
        "mcp_server.py",
        "enhanced_assistant.py",
    ):
        assert "reorder" not in _module_text(name).casefold()
    for question in CLINIC_SAMPLE_QUERIES:
        assert "reorder" not in question.casefold()
    for case in PLANTED_CASES:
        assert "reorder" not in case.sql.casefold()
    path = tmp_path / "clinic.db"
    generate(path, 11)
    assert all("reorder" not in name for name in schema_identifiers(path))
    conn = open_readonly_plain(path)
    try:
        views = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'view'"
        ).fetchone()
        sql_rows = conn.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    assert views[0] == 0
    for (sql,) in sql_rows:
        assert "reorder" not in sql.casefold()
    document = snapshot_document(path)
    provenance = None
    for name, _pk, columns, rows in document[1][1]:
        if name == "provenance":
            provenance = columns
        for row in rows:
            for cell in row:
                if isinstance(cell, str):
                    assert "reorder" not in cell.casefold()
    assert tuple(provenance) == PROVENANCE_COLUMNS
    assistant = EnhancedQueryAssistant(str(path), "test-not-a-key")
    assert "reorder" not in assistant._get_db_schema().casefold()
    assert key not in repr(assistant.audit.config)


def test_resume_continues_from_provenance_clock_end(tmp_path):
    path = tmp_path / "clinic.db"
    other = tmp_path / "other.db"
    copy = tmp_path / "copy.db"
    h0 = generate(path, 11)
    assert _rows(path, "SELECT clock_end FROM provenance") == [(T0,)]
    shutil.copyfile(path, copy)
    before_rows = _high_id_rows(path)
    before = {
        "patient": _count(path, "patient"),
        "appointment": _count(path, "appointment"),
        "delivery": _count(path, "delivery"),
        "stock_use": _count(path, "stock_use"),
        "appointment_type": _count(path, "appointment_type"),
        "usage": _count(path, "usage"),
        "supplier": _count(path, "supplier"),
    }
    clock_start = _rows(path, "SELECT clock_start FROM provenance")[0][0]
    h1 = resume_one_step(path, 11)
    assert h1 != h0
    assert _rows(path, "SELECT seed, clock_start, clock_end FROM provenance") == [
        (11, clock_start, _plus_day(T0))
    ]
    assert _count(path, "patient") == before["patient"] + 1
    assert _count(path, "appointment") == before["appointment"] + 1
    assert _count(path, "delivery") == before["delivery"] + 1
    assert _count(path, "stock_use") == before["stock_use"] + 1
    assert _count(path, "appointment_type") == before["appointment_type"]
    assert _count(path, "usage") == before["usage"]
    assert _count(path, "supplier") == before["supplier"]
    assert _rows(
        path,
        "SELECT supplier_id, lead_time_days, min_order_qty FROM supplier "
        "ORDER BY supplier_id",
    ) == [(1, 2, 1), (9001, 5, 10)]
    assert _rows(
        path,
        f"SELECT appointment_type_id FROM appointment WHERE starts_at = '{T0}'",
    ) == [(1,)]
    visit = _rows(
        path,
        "SELECT visit_id, appointment_type_id FROM visit " f"WHERE visited_at = '{T0}'",
    )
    assert visit == [(visit[0][0], 1)]
    assert _rows(
        path,
        "SELECT product_id, qty FROM stock_use "
        f"WHERE visit_id = {int(visit[0][0])} ORDER BY stock_use_id",
    ) == [(1, 1)]
    assert _high_id_rows(path) == before_rows
    assert generate(other, 11) == h0
    assert content_hash(other) == h0
    assert resume_one_step(copy, 11) == h1
    _assert_cases(path)
    missing = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        resume_one_step(missing, 11)
    assert not missing.exists()


def test_crash_resume_adds_seeded_clinician_only_on_planted_only_file(tmp_path):
    import clinic_generator

    planted = tmp_path / "planted.db"
    clock = SimulatedClock()
    conn = clinic_generator.open_writer(planted)
    try:
        conn.executescript(clinic_generator.SCHEMA_SQL)
        conn.execute("BEGIN IMMEDIATE")
        try:
            clinic_generator.insert_planted(conn)
            clinic_generator._insert_provenance(conn, 11, clock)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    assert clock.now == EPOCH
    assert _rows(planted, "SELECT clock_end FROM provenance") == [(clock.iso(),)]
    copies = [tmp_path / "a.db", tmp_path / "b.db"]
    for path in copies:
        shutil.copyfile(planted, path)
    clinician_9001 = (
        "SELECT clinician_id, full_name, role FROM clinician "
        "WHERE clinician_id = 9001"
    )
    low_clinicians = (
        "SELECT clinician_id FROM clinician WHERE clinician_id < 9000 "
        "ORDER BY clinician_id"
    )
    before_9001 = _rows(planted, clinician_9001)
    assert len(before_9001) == 1
    assert _rows(planted, low_clinicians) == []
    digests = [resume_one_step(path, 11) for path in copies]
    assert digests[0] == digests[1]
    assert content_hash(copies[0]) == content_hash(copies[1]) == digests[0]
    assert len(PLANTED_CASES) == 9
    added_ids = []
    for path in copies:
        added = _rows(path, low_clinicians)
        assert len(added) == 1
        assert added[0][0] < 9000
        added_ids.append(added[0][0])
        assert _rows(path, clinician_9001) == before_9001
        filler_visits = _rows(
            path,
            "SELECT clinician_id FROM visit WHERE visit_id < 9000 ORDER BY visit_id",
        )
        assert filler_visits == [(added[0][0],)]
        assert _rows(
            path,
            "SELECT COUNT(*) FROM visit WHERE visit_id < 9000 AND clinician_id = 9001",
        ) == [(0,)]
        _assert_cases(path)
    assert added_ids[0] == added_ids[1]
    fresh = tmp_path / "fresh.db"
    generate(fresh, 11)
    clinician_count = _count(fresh, "clinician")
    resume_one_step(fresh, 11)
    assert _count(fresh, "clinician") == clinician_count


def test_follow_appends_without_overwriting(tmp_path):
    solo = tmp_path / "solo.db"
    h0 = generate(solo, 11)
    fresh_count = _count(solo, "patient")
    path = tmp_path / "follow.db"
    proc = _popen(["--follow", "--seed", "11", "--db", str(path)])
    line = {}

    def _read():
        line["value"] = proc.stdout.readline()

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    try:
        reader.join(30)
        if reader.is_alive():
            raise AssertionError("follow did not print the fresh content hash")
        assert line["value"].strip() == f"content_hash={h0}"
        deadline = time.time() + 30
        count = fresh_count
        while time.time() < deadline:
            try:
                count = _count(path, "patient")
            except (OSError, sqlite3.Error, GuardError):
                count = fresh_count
            if count > fresh_count:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("follow did not append a patient")
        case = next(item for item in PLANTED_CASES if item.id == "vaccine_not_billed")
        assert list(_rows(path, case.sql)) == list(case.expected_rows)
        proc.terminate()
        try:
            _out, err = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            _stop(proc)
            err = ""
            if os.name != "nt":
                raise
        if os.name != "nt":
            assert proc.returncode == 0, err
        assert _rows(path, "SELECT seed FROM provenance") == [(11,)]
        _assert_cases(path)
    finally:
        _stop(proc)


def test_clinic_refuses_to_start_without_audit_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ADQ_AUDIT_HMAC_KEY", raising=False)
    path = tmp_path / "clinic.db"
    generate(path, 11)
    with pytest.raises(AuditConfigError) as exc:
        load_audit_config(path)
    assert "ADQ_AUDIT_HMAC_KEY" in str(exc.value)


def test_clinic_refuses_raw_text_audit(tmp_path, monkeypatch):
    path = tmp_path / "clinic.db"
    generate(path, 11)
    monkeypatch.setenv("ADQ_AUDIT_HMAC_KEY", secrets.token_hex(32))
    monkeypatch.setenv("ADQ_AUDIT_RAW_TEXT", "1")
    with pytest.raises(AuditConfigError) as exc:
        load_audit_config(path)
    assert "chinook" in str(exc.value).casefold()


def test_clinic_audit_keyed_hash_logs_no_cell_values(tmp_path, key, caplog, capsys):
    path = tmp_path / "clinic.db"
    generate(path, 11)
    question = "Show the label and the owner contact"
    sql = "SELECT notice, full_name FROM provenance, owner WHERE owner_id = 9001"
    with caplog.at_level("DEBUG"):
        assistant = EnhancedQueryAssistant(str(path), "test-not-a-key")
        assistant.client.scripted_sql = sql
        frame, _similar = assistant.execute_query(question)
    captured = capsys.readouterr()
    assert len(frame) == 1
    text = Path(os.environ["ADQ_AUDIT_PATH"]).read_text(encoding="utf-8")
    row = json.loads(text.splitlines()[0])
    assert set(row) == COMMON_KEYS | HASH_KEYS
    assert row["log_mode"] == "keyed_hash"
    assert row["row_count"] == 1
    for absent in ("question", "executed_sql", "error_message"):
        assert absent not in row
    for secret in (
        NOTICE,
        "Synthetic Owner 9001",
        "owner9001@example.com",
        key,
        question,
    ):
        assert secret not in text
    for blob in (text, caplog.text, captured.out, captured.err):
        assert key not in blob
    assert key not in repr(assistant.audit)
    assert key not in repr(assistant.audit.config)


def test_app_opens_clinic_readonly(tmp_path, key):
    path = tmp_path / "clinic.db"
    generate(path, 11)
    assistant = EnhancedQueryAssistant(str(path), "test-not-a-key")
    for label in (
        "Table provenance",
        "Table patient",
        "Table supplier",
        "Table inventory_lot",
        "Table appointment_type",
        "Table usage",
    ):
        assert label in assistant.schema
    assistant.client.scripted_sql = "SELECT data_class FROM provenance"
    frame, _similar = assistant.execute_query("data class")
    assert frame.iloc[0, 0] == "synthetic"
    assistant.client.scripted_sql = "DELETE FROM provenance"
    with pytest.raises(GuardError) as exc:
        assistant.execute_query("remove provenance")
    assert exc.value.reason == "text_check"
    assert _count(path, "provenance") == 1
    assert assistant.audit.db_is_chinook() is False
    assert assistant.collection.documents == []
    assert sample_queries_for(False) == CLINIC_SAMPLE_QUERIES
    assert key not in repr(assistant.audit.config)


def test_load_db_path_env_over_secrets_over_default(monkeypatch):
    monkeypatch.delenv("ADQ_DB_PATH", raising=False)
    assert load_db_path(None) == "chinook.db"
    assert load_db_path({}) == "chinook.db"
    assert load_db_path({"path": ""}) == "chinook.db"
    assert load_db_path({"path": "   "}) == "chinook.db"
    assert load_db_path({"path": "from-secrets.db"}) == "from-secrets.db"
    monkeypatch.setenv("ADQ_DB_PATH", "from-env.db")
    assert load_db_path({"path": "from-secrets.db"}) == "from-env.db"
    monkeypatch.setenv("ADQ_DB_PATH", "")
    assert load_db_path({"path": "from-secrets.db"}) == "from-secrets.db"
    monkeypatch.setenv("ADQ_DB_PATH", "   ")
    assert load_db_path({"path": "from-secrets.db"}) == "from-secrets.db"


def test_readme_planted_cases_match_generated_rows(tmp_path):
    readme = _module_text("README.md")
    assert len(PLANTED_CASES) == 9
    for case in PLANTED_CASES:
        assert case.sql in readme
    paths = []
    for seed, name in ((11, "a.db"), (29, "b.db")):
        path = tmp_path / name
        generate(path, seed)
        paths.append(path)
    resumed = tmp_path / "resumed.db"
    generate(resumed, 11)
    resume_one_step(resumed, 11)
    paths.append(resumed)
    for path in paths:
        names = {
            row[0]
            for row in _rows(path, "SELECT full_name FROM owner WHERE owner_id < 9000")
        }
        pets = {
            row[0]
            for row in _rows(
                path, "SELECT patient_name FROM patient WHERE patient_id < 9000"
            )
        }
        for case in PLANTED_CASES:
            assert list(_rows(path, case.sql)) == list(case.expected_rows)
            for row in case.expected_rows:
                for cell in row:
                    assert cell not in names
                    assert cell not in pets


def test_clinic_samples_are_not_the_chinook_list():
    assert len(CLINIC_SAMPLE_QUERIES) == 3
    assert CLINIC_SAMPLE_QUERIES == (
        "Which vaccines were given at a visit and not billed "
        "on that visit's invoice?",
        "Which products have a supplier price that went up "
        "while the client price stayed the same?",
        "Which products were delivered in more than four times "
        "the quantity that was used?",
    )
    assert set(CLINIC_SAMPLE_QUERIES).isdisjoint(EnhancedQueryAssistant.SAMPLE_QUERIES)
    assert len(sample_queries_for(True)) == 10
    assert sample_queries_for(False) == CLINIC_SAMPLE_QUERIES


def test_gitignore_ignores_generated_clinic_db():
    lines = _module_text(".gitignore").splitlines()
    assert "clinic.db" in lines
    assert "*.db-wal" in lines
    assert "*.db-shm" in lines
    assert "*.db" not in lines


@pytest.mark.repo_files
def test_dockerfile_ships_generator_entrypoint_without_changing_app_cmd():
    text = _module_text("Dockerfile")
    for name in CLINIC_MODULES:
        assert name in text
    assert "-m clinic_generator" in text
    assert "docker compose up" in text
    assert (
        'CMD ["sh", "-c", "streamlit run streamlit_app.py & '
        'uvicorn mcp_server:app --host 0.0.0.0 --port 8000"]'
    ) in text
    for line in text.splitlines():
        assert not line.strip().upper().startswith("ENTRYPOINT")
    assert "ADQ_AUDIT_HMAC_KEY" not in text
    assert "ADQ_DB_PATH" not in text


@pytest.mark.repo_files
def test_compose_and_ci_stay_local():
    text = _module_text("compose.yaml")
    assert text.count("image: adq:ci") == 2
    assert "target: runtime" in text
    generator = text.split("app:", 1)[0]
    assert "network_mode: none" in generator
    assert "127.0.0.1:8501:8501" in text
    assert "clinic-data:/data" in text
    for banned in ("docker.io", "ghcr.io", ".:", "8000:8000", "secrets:"):
        assert banned not in text
    assert re.search(r"(?m)^\s+- [A-Za-z]:", text) is None
    stripped = text.replace("${ADQ_AUDIT_HMAC_KEY}", "")
    assert stripped.count("ADQ_AUDIT_HMAC_KEY") == 1
    workflow = _module_text(".github/workflows/ci.yml")
    for snippet in (
        "network_mode: none",
        "ports: !override []",
        "--no-build",
        "vaccine_not_billed",
        "secrets.token_hex",
        "docker compose down --volumes",
    ):
        assert snippet in workflow
    assert "docker push" not in workflow
    assert "docker compose push" not in workflow
    assert "${{ secrets." not in workflow
    assert "${{secrets." not in workflow
