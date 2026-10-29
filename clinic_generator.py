"""Seeded synthetic vet-clinic database generator.

This is the only module that opens a writer. Create never overwrites an
existing file. Resume appends one filler step from provenance.clock_end.
"""

import argparse
import datetime
import hashlib
import os
import random
import signal
import sqlite3
import sys
import time
from pathlib import Path

from clinic_clock import EPOCH, SimulatedClock
from clinic_schema import (
    APPOINTMENT_SECONDS,
    BREEDS,
    CHECKUP_APPOINTMENT_TYPE_ID,
    CITIES,
    DATA_CLASS,
    EXPECTED_USAGE,
    FILLER_APPOINTMENT_STATUS,
    FILLER_CLINICIAN_ROLE,
    FILLER_CLINICIANS,
    FILLER_INVOICE_AMOUNTS,
    FILLER_INVOICE_STATUS,
    FILLER_INVOICE_TOTAL,
    FILLER_OWNERS,
    FILLER_PATIENTS,
    FILLER_STEP_SECONDS,
    LABEL,
    LINE_DESCRIPTION,
    LOT_LIFE_DAYS,
    NOTICE,
    PET_NAMES,
    PLANTED_APPOINTMENT_TYPES,
    PLANTED_APPOINTMENTS,
    PLANTED_CLIENT_PRICES,
    PLANTED_CLINICIANS,
    PLANTED_DELIVERIES,
    PLANTED_ID_FLOOR,
    PLANTED_INVOICE_LINES,
    PLANTED_INVOICES,
    PLANTED_LOTS,
    PLANTED_OWNERS,
    PLANTED_PATIENTS,
    PLANTED_PRODUCTS,
    PLANTED_STOCK_DRAW,
    PLANTED_SUPPLIER_PRICES,
    PLANTED_SUPPLIERS,
    PLANTED_USAGE,
    PLANTED_VISIT_LOT,
    PLANTED_VISITS,
    SCHEMA_SQL,
    SPECIES,
    TABLE_COLUMNS,
    WEIGHT_GRAMS,
    quote_ident,
)
from clinic_snapshot import content_hash
from sql_guard import open_readonly_plain

# Private settings for this process. Never inserted, selected, logged,
# or copied into provenance.
_REORDER_POINTS = {1: 4, 9001: 2, 9002: 2, 9003: 6}

_CLOCK_END_SQL = "UPDATE provenance SET clock_end = ? WHERE provenance_id = 1"
_RESUME_OWNER_LIMIT = 99
_stop_requested = False


class RefuseOverwrite(FileExistsError):
    """The path exists and this process will not replace it."""


def _require_seed(seed: int) -> int:
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a non-negative int")
    return seed


def derive_rng(seed: int) -> random.Random:
    """Non-negative int. Reject bool. Used by fresh filler only."""
    return random.Random(_require_seed(seed))


def resume_seed_int(seed: int, clock_end: str) -> int:
    """int.from_bytes(sha256(f"{seed}|{clock_end}").digest()[:8], "big").

    A new stream for rows appended at this clock_end. Independent of how
    many draws generate() already consumed. CPython's str seeding of
    Random is not used; this integer is.
    """
    _require_seed(seed)
    digest = hashlib.sha256(f"{seed}|{clock_end}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def build_faker(seed: int):
    """Faker('en_US') then seed_instance(seed) on that instance only.

    Do not call Faker.seed. Import faker inside this function.
    No extras. Calls are first_name() and last_name() only.
    """
    _require_seed(seed)
    from faker import Faker

    faker = Faker("en_US")
    faker.seed_instance(seed)
    return faker


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()
    if mode is None or str(mode[0]).lower() != "wal":
        raise RuntimeError("journal_mode is not wal")
    conn.execute("PRAGMA synchronous = FULL")


def open_writer(db_path: str | os.PathLike) -> sqlite3.Connection:
    """Create a new file and open it as a WAL writer.

    os.open(path, O_CREAT | O_EXCL | O_RDWR, 0o600), close the fd,
    then sqlite3.connect(path). An existing path raises FileExistsError
    before SQLite opens it.
    """
    path = os.fspath(db_path)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.isolation_level = None
    try:
        _apply_pragmas(conn)
    except Exception:
        conn.close()
        raise
    return conn


def _schema_matches(conn: sqlite3.Connection) -> bool:
    rows = conn.execute("""
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        """).fetchall()
    names = {row[0] for row in rows}
    if names != set(TABLE_COLUMNS):
        return False
    for table, columns in TABLE_COLUMNS.items():
        info = conn.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall()
        got = tuple(row[1] for row in info)
        if got != columns:
            return False
    return True


def _validated_clock_end(db_path: str | os.PathLike, seed: int) -> str:
    """Read-only checks. Missing path raises FileNotFoundError."""
    path = os.fspath(db_path)
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    if not os.path.isfile(path):
        raise RefuseOverwrite(path)
    try:
        conn = open_readonly_plain(path)
    except FileNotFoundError:
        raise
    except sqlite3.DatabaseError as exc:
        raise RefuseOverwrite(path) from exc
    try:
        if not _schema_matches(conn):
            raise RefuseOverwrite(path)
        try:
            rows = conn.execute(
                "SELECT provenance_id, data_class, label, generator, seed, "
                "clock_end FROM provenance"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise RefuseOverwrite(path) from exc
        if len(rows) != 1:
            raise RefuseOverwrite(path)
        provenance_id, data_class, label, generator, row_seed, clock_end = rows[0]
        if (
            provenance_id != 1
            or data_class != DATA_CLASS
            or label != LABEL
            or generator != "clinic_generator"
            or int(row_seed) != seed
            or not isinstance(clock_end, str)
        ):
            raise RefuseOverwrite(path)
        SimulatedClock.parse_iso(clock_end)
        return clock_end
    finally:
        conn.close()


def open_resume(db_path: str | os.PathLike, seed: int) -> sqlite3.Connection:
    """Existing clinic file only. Validate through open_readonly_plain.

    On failure raise RefuseOverwrite without a writer and without
    changing the file. Then sqlite3.connect(path) and the writer PRAGMAs.
    journal_mode must already return wal.
    """
    _require_seed(seed)
    path = os.fspath(db_path)
    _validated_clock_end(path, seed)
    conn = sqlite3.connect(path)
    conn.isolation_level = None
    try:
        current = conn.execute("PRAGMA journal_mode").fetchone()
        if current is None or str(current[0]).lower() != "wal":
            raise RefuseOverwrite(path)
        _apply_pragmas(conn)
    except Exception:
        conn.close()
        raise
    return conn


def price_on(conn, supplier_id: int, product_id: int, day: str) -> int:
    """The supplier price in effect on day. Raises if no row."""
    row = conn.execute(
        """
        SELECT unit_cents
        FROM supplier_price
        WHERE supplier_id = ? AND product_id = ? AND effective_on <= ?
        ORDER BY effective_on DESC
        LIMIT 1
        """,
        (supplier_id, product_id, day),
    ).fetchone()
    if row is None:
        raise RuntimeError(
            f"no supplier price for supplier {supplier_id} product {product_id}"
        )
    return int(row[0])


def usage_lines(conn, appointment_type_id: int) -> tuple[tuple[int, int], ...]:
    """Product and quantity rows for one appointment type, in usage_id order.

    Raises if the query returns no row. The returned pairs are the only
    source of stock_use.product_id and stock_use.qty.
    """
    rows = conn.execute(
        """
        SELECT product_id, qty
        FROM usage
        WHERE appointment_type_id = ?
        ORDER BY usage_id
        """,
        (appointment_type_id,),
    ).fetchall()
    if not rows:
        raise RuntimeError(f"no usage rows for appointment type {appointment_type_id}")
    return tuple((int(product_id), int(qty)) for product_id, qty in rows)


def _insert_rows(conn: sqlite3.Connection, table: str, rows: tuple) -> None:
    if not rows:
        return
    marks = ", ".join("?" for _ in range(len(rows[0])))
    sql = f"INSERT INTO {quote_ident(table)} VALUES ({marks})"
    conn.executemany(sql, rows)


def _draw_planted_stock(conn: sqlite3.Connection) -> None:
    for type_id, expected in EXPECTED_USAGE.items():
        if usage_lines(conn, type_id) != expected:
            raise RuntimeError(f"usage read-back mismatch for type {type_id}")
    for stock_use_id, visit_id in PLANTED_STOCK_DRAW:
        visit = conn.execute(
            "SELECT appointment_type_id, visited_at FROM visit WHERE visit_id = ?",
            (visit_id,),
        ).fetchone()
        if visit is None:
            raise RuntimeError(f"missing planted visit {visit_id}")
        type_id = int(visit[0])
        pairs = usage_lines(conn, type_id)
        if pairs != EXPECTED_USAGE[type_id]:
            raise RuntimeError(f"usage read-back mismatch for type {type_id}")
        if len(pairs) != 1:
            raise RuntimeError(f"planted visit {visit_id} usage is not one row")
        product_id, qty = pairs[0]
        lot_id = PLANTED_VISIT_LOT[visit_id]
        lot = conn.execute(
            "SELECT product_id, expires_on FROM inventory_lot WHERE lot_id = ?",
            (lot_id,),
        ).fetchone()
        if lot is None:
            raise RuntimeError(f"missing planted lot {lot_id}")
        lot_product = int(lot[0])
        expires_on = lot[1]
        used_on = visit[1][:10]
        if not expires_on > used_on:
            raise RuntimeError(f"lot {lot_id} is not eligible on {used_on}")
        if product_id != lot_product:
            raise RuntimeError(
                f"lot {lot_id} product differs from usage for visit {visit_id}"
            )
        conn.execute(
            "INSERT INTO stock_use VALUES (?, ?, ?, ?, ?, ?)",
            (stock_use_id, visit_id, lot_id, product_id, qty, used_on),
        )


def insert_planted(conn: sqlite3.Connection) -> None:
    """Constants only. No RNG, no Faker, no clock.advance.

    Inserts appointment types, usage rows, and supplier lead times.
    Reads stock product and quantity back through usage_lines and
    requires that read-back to equal EXPECTED_USAGE for types 1, 9001,
    and 9002. Draws one stock_use per returned pair.
    """
    _insert_rows(conn, "clinician", PLANTED_CLINICIANS)
    _insert_rows(conn, "owner", PLANTED_OWNERS)
    _insert_rows(conn, "patient", PLANTED_PATIENTS)
    _insert_rows(conn, "appointment_type", PLANTED_APPOINTMENT_TYPES)
    _insert_rows(conn, "product", PLANTED_PRODUCTS)
    _insert_rows(conn, "usage", PLANTED_USAGE)
    _insert_rows(conn, "supplier", PLANTED_SUPPLIERS)
    _insert_rows(conn, "supplier_price", PLANTED_SUPPLIER_PRICES)
    _insert_rows(conn, "client_price", PLANTED_CLIENT_PRICES)
    _insert_rows(conn, "appointment", PLANTED_APPOINTMENTS)
    _insert_rows(conn, "visit", PLANTED_VISITS)
    _insert_rows(conn, "invoice", PLANTED_INVOICES)
    _insert_rows(conn, "invoice_line", PLANTED_INVOICE_LINES)
    for delivery in PLANTED_DELIVERIES:
        got = price_on(conn, delivery[1], delivery[2], delivery[4])
        if got != delivery[6]:
            raise RuntimeError(f"delivery {delivery[0]} price mismatch")
    for delivery, lot in zip(PLANTED_DELIVERIES, PLANTED_LOTS, strict=True):
        if delivery[3] != lot[0] or delivery[6] != lot[7]:
            raise RuntimeError("delivery and lot constants differ")
    _insert_rows(conn, "inventory_lot", PLANTED_LOTS)
    _insert_rows(conn, "delivery", PLANTED_DELIVERIES)
    _draw_planted_stock(conn)


def _species_breed(patient_id: int) -> tuple[str, str]:
    index = (patient_id - 1) % len(SPECIES)
    return SPECIES[index], BREEDS[index]


def _date_of_birth(rng: random.Random) -> str:
    offset = 400 + rng.randrange(1, 3000)
    born = EPOCH.date() - datetime.timedelta(days=offset)
    return born.isoformat()


def _iso_plus(text: str, seconds: int) -> str:
    start = SimulatedClock.parse_iso(text)
    moved = start + datetime.timedelta(seconds=seconds)
    return moved.strftime("%Y-%m-%dT%H:%M:%SZ")


def _date_plus(day: str, days: int) -> str:
    parsed = datetime.date.fromisoformat(day)
    return (parsed + datetime.timedelta(days=days)).isoformat()


def _require_checkup_usage(conn: sqlite3.Connection) -> tuple[int, int]:
    pairs = usage_lines(conn, CHECKUP_APPOINTMENT_TYPE_ID)
    if pairs != ((1, 1),):
        raise RuntimeError("checkup usage is not product 1 quantity 1")
    return pairs[0]


def _insert_filler_patient_rows(
    conn: sqlite3.Connection,
    rng: random.Random,
    *,
    patient_id: int,
    owner_id: int,
    clinician_id: int,
    appointment_id: int,
    visit_id: int,
    invoice_id: int,
    line_ids: tuple[int, int],
    delivery_id: int,
    lot_id: int,
    stock_use_id: int,
    starts_iso: str,
) -> None:
    product_id, qty = _require_checkup_usage(conn)
    species, breed = _species_breed(patient_id)
    dob = _date_of_birth(rng)
    weight = rng.randint(*WEIGHT_GRAMS[species])
    day = starts_iso[:10]
    conn.execute(
        "INSERT INTO patient VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            patient_id,
            owner_id,
            PET_NAMES[(patient_id - 1) % len(PET_NAMES)],
            species,
            breed,
            "F" if patient_id % 2 else "M",
            dob,
            f"SYN-CHIP-{patient_id}",
            "alive",
            None,
        ),
    )
    conn.execute(
        "INSERT INTO appointment VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            appointment_id,
            patient_id,
            clinician_id,
            CHECKUP_APPOINTMENT_TYPE_ID,
            starts_iso,
            _iso_plus(starts_iso, APPOINTMENT_SECONDS),
            FILLER_APPOINTMENT_STATUS,
        ),
    )
    conn.execute(
        "INSERT INTO visit VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            visit_id,
            patient_id,
            clinician_id,
            appointment_id,
            CHECKUP_APPOINTMENT_TYPE_ID,
            starts_iso,
            weight,
        ),
    )
    conn.execute(
        "INSERT INTO invoice VALUES (?, ?, ?, ?, ?, ?)",
        (
            invoice_id,
            owner_id,
            visit_id,
            day,
            FILLER_INVOICE_STATUS,
            FILLER_INVOICE_TOTAL,
        ),
    )
    for line_id, amount in zip(line_ids, FILLER_INVOICE_AMOUNTS, strict=True):
        conn.execute(
            "INSERT INTO invoice_line VALUES (?, ?, ?, ?, ?)",
            (line_id, invoice_id, None, LINE_DESCRIPTION, amount),
        )
    unit = price_on(conn, 1, product_id, day)
    conn.execute(
        "INSERT INTO inventory_lot VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            lot_id,
            product_id,
            1,
            day,
            _date_plus(day, LOT_LIFE_DAYS),
            qty,
            0,
            unit,
        ),
    )
    conn.execute(
        "INSERT INTO delivery VALUES (?, ?, ?, ?, ?, ?, ?)",
        (delivery_id, 1, product_id, lot_id, day, qty, unit),
    )
    conn.execute(
        "INSERT INTO stock_use VALUES (?, ?, ?, ?, ?, ?)",
        (stock_use_id, visit_id, lot_id, product_id, qty, day),
    )


def insert_filler_population(conn, rng, faker, clock) -> None:
    """The 36 fresh filler patients. One transaction, committed by generate.

    Each visit is type checkup. Stock comes from usage_lines. Rolls back
    if that read-back is not ((1, 1),).
    """
    _require_checkup_usage(conn)
    for clinician_id in range(1, FILLER_CLINICIANS + 1):
        name = f"{faker.first_name()} {faker.last_name()}"
        conn.execute(
            "INSERT INTO clinician VALUES (?, ?, ?)",
            (clinician_id, name, FILLER_CLINICIAN_ROLE),
        )
    for owner_id in range(1, FILLER_OWNERS + 1):
        name = f"{faker.first_name()} {faker.last_name()}"
        conn.execute(
            "INSERT INTO owner VALUES (?, ?, ?, ?, ?)",
            (
                owner_id,
                name,
                f"owner{owner_id}@example.com",
                f"+1-555-01{owner_id:02d}",
                CITIES[(owner_id - 1) % len(CITIES)],
            ),
        )
    for number in range(1, FILLER_PATIENTS + 1):
        starts = clock.iso()
        _insert_filler_patient_rows(
            conn,
            rng,
            patient_id=number,
            owner_id=((number - 1) % FILLER_OWNERS) + 1,
            clinician_id=((number - 1) % FILLER_CLINICIANS) + 1,
            appointment_id=number,
            visit_id=number,
            invoice_id=number,
            line_ids=((number - 1) * 2 + 1, (number - 1) * 2 + 2),
            delivery_id=number,
            lot_id=number,
            stock_use_id=number,
            starts_iso=starts,
        )
        clock.advance(FILLER_STEP_SECONDS)


def _next_id(conn: sqlite3.Connection, table: str, column: str) -> int:
    quoted_table = quote_ident(table)
    quoted_column = quote_ident(column)
    row = conn.execute(
        f"SELECT MAX({quoted_column}) FROM {quoted_table} "
        f"WHERE {quoted_column} < ?",
        (PLANTED_ID_FLOOR,),
    ).fetchone()
    if row is None or row[0] is None:
        return 1
    return int(row[0]) + 1


def _ensure_clinician(conn: sqlite3.Connection, clinician_id: int, faker) -> None:
    found = conn.execute(
        "SELECT 1 FROM clinician WHERE clinician_id = ?",
        (clinician_id,),
    ).fetchone()
    if found is not None:
        return
    name = f"{faker.first_name()} {faker.last_name()}"
    conn.execute(
        "INSERT INTO clinician VALUES (?, ?, ?)",
        (clinician_id, name, FILLER_CLINICIAN_ROLE),
    )


def insert_filler_step(conn, seed: int, clock: SimulatedClock) -> None:
    """One resume step. Faker and Random seeded with resume_seed_int.

    The seed is (seed, clock.iso() at entry). Stock comes from usage_lines
    for type checkup. Then clock.advance(86400).
    """
    owner_id = _next_id(conn, "owner", "owner_id")
    patient_id = _next_id(conn, "patient", "patient_id")
    appointment_id = _next_id(conn, "appointment", "appointment_id")
    visit_id = _next_id(conn, "visit", "visit_id")
    invoice_id = _next_id(conn, "invoice", "invoice_id")
    line_a = _next_id(conn, "invoice_line", "line_id")
    line_b = line_a + 1
    delivery_id = _next_id(conn, "delivery", "delivery_id")
    lot_id = _next_id(conn, "inventory_lot", "lot_id")
    stock_use_id = _next_id(conn, "stock_use", "stock_use_id")
    ids = (
        owner_id,
        patient_id,
        appointment_id,
        visit_id,
        invoice_id,
        line_a,
        line_b,
        delivery_id,
        lot_id,
        stock_use_id,
    )
    if owner_id >= _RESUME_OWNER_LIMIT or any(item >= PLANTED_ID_FLOOR for item in ids):
        raise RefuseOverwrite("next filler id is reserved")
    _require_checkup_usage(conn)
    step_seed = resume_seed_int(seed, clock.iso())
    rng = random.Random(step_seed)
    faker = build_faker(step_seed)
    conn.execute(
        "INSERT INTO owner VALUES (?, ?, ?, ?, ?)",
        (
            owner_id,
            f"{faker.first_name()} {faker.last_name()}",
            f"owner{owner_id}@example.com",
            f"+1-555-01{owner_id:02d}",
            CITIES[(owner_id - 1) % len(CITIES)],
        ),
    )
    clinician_id = ((patient_id - 1) % FILLER_CLINICIANS) + 1
    _ensure_clinician(conn, clinician_id, faker)
    starts = clock.iso()
    _insert_filler_patient_rows(
        conn,
        rng,
        patient_id=patient_id,
        owner_id=owner_id,
        clinician_id=clinician_id,
        appointment_id=appointment_id,
        visit_id=visit_id,
        invoice_id=invoice_id,
        line_ids=(line_a, line_b),
        delivery_id=delivery_id,
        lot_id=lot_id,
        stock_use_id=stock_use_id,
        starts_iso=starts,
    )
    clock.advance(FILLER_STEP_SECONDS)


def _insert_provenance(
    conn: sqlite3.Connection, seed: int, clock: SimulatedClock
) -> None:
    stamp = clock.iso()
    conn.execute(
        "INSERT INTO provenance VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            1,
            DATA_CLASS,
            LABEL,
            NOTICE,
            "clinic_generator",
            seed,
            stamp,
            stamp,
            "en_US",
        ),
    )


def _hold_for_stop_file(conn: sqlite3.Connection, db_path, stop_file) -> None:
    ready = Path(f"{os.fspath(db_path)}.ready")
    ready.write_text("ready\n", encoding="utf-8", newline="\n")
    stop = Path(os.fspath(stop_file))
    while not stop.exists():
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("COMMIT")
        time.sleep(0.05)


def generate(
    db_path: str | os.PathLike,
    seed: int,
    *,
    stop_file: str | os.PathLike | None = None,
) -> str:
    """Create one full snapshot and return content_hash(db_path)."""
    _require_seed(seed)
    rng = derive_rng(seed)
    faker = build_faker(seed)
    clock = SimulatedClock()
    try:
        conn = open_writer(db_path)
    except FileExistsError as exc:
        raise RefuseOverwrite(os.fspath(db_path)) from exc
    try:
        conn.executescript(SCHEMA_SQL)
        conn.execute("BEGIN IMMEDIATE")
        try:
            insert_planted(conn)
            _insert_provenance(conn, seed, clock)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        if stop_file is not None:
            _hold_for_stop_file(conn, db_path, stop_file)
        conn.execute("BEGIN IMMEDIATE")
        try:
            insert_filler_population(conn, rng, faker, clock)
            conn.execute(_CLOCK_END_SQL, (clock.iso(),))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return content_hash(db_path)


def resume_one_step(db_path: str | os.PathLike, seed: int) -> str:
    """Append one filler step from provenance.clock_end and return the new hash.

    Refuses a missing file, a foreign file, and a seed mismatch.
    """
    _require_seed(seed)
    observed = _validated_clock_end(db_path, seed)
    conn = open_resume(db_path, seed)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT seed, clock_end FROM provenance WHERE provenance_id = 1"
            ).fetchone()
            if row is None or int(row[0]) != seed or row[1] != observed:
                raise RefuseOverwrite(os.fspath(db_path))
            clock = SimulatedClock(start=SimulatedClock.parse_iso(observed))
            insert_filler_step(conn, seed, clock)
            conn.execute(_CLOCK_END_SQL, (clock.iso(),))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return content_hash(db_path)


def _on_stop(_signum, _frame) -> None:
    global _stop_requested
    _stop_requested = True


def _checkpoint(db_path: str | os.PathLike) -> None:
    conn = sqlite3.connect(os.fspath(db_path))
    try:
        conn.isolation_level = None
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def _cmd_follow(db_path: str | os.PathLike, seed: int) -> int:
    global _stop_requested
    _stop_requested = False
    path = os.fspath(db_path)
    if not os.path.exists(path):
        digest = generate(path, seed)
        sys.stdout.write(f"content_hash={digest}\n")
        sys.stdout.flush()
    else:
        conn = open_resume(path, seed)
        conn.close()
    signal.signal(signal.SIGTERM, _on_stop)
    signal.signal(signal.SIGINT, _on_stop)
    while not _stop_requested:
        resume_one_step(path, seed)
        if _stop_requested:
            break
        time.sleep(0.5)
    _checkpoint(path)
    return 0


def main(argv: list[str] | None = None) -> int:
    """--db and --seed required.

    Optional: --stop-file (one-shot generate only), --resume (one step),
    or --follow (create or continue, then loop).
    Combining --follow with --stop-file or --resume returns 1 and writes nothing.
    stdout of generate and of --resume is one line: content_hash=<hex>.
    stdout of --follow is that one line for the H0 file, then silence.
    RefuseOverwrite and FileExistsError return 2. FileNotFoundError and any
    other failure return 1. Row contents and the audit key are not printed.
    """
    parser = argparse.ArgumentParser(prog="clinic_generator")
    parser.add_argument("--db", required=True)
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--stop-file")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--follow", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 1
    if args.follow and (args.stop_file is not None or args.resume):
        return 1
    if args.seed < 0:
        return 1
    try:
        if args.follow:
            return _cmd_follow(args.db, args.seed)
        if args.resume:
            digest = resume_one_step(args.db, args.seed)
        else:
            digest = generate(args.db, args.seed, stop_file=args.stop_file)
    except (RefuseOverwrite, FileExistsError):
        return 2
    except FileNotFoundError:
        return 1
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        return 1
    sys.stdout.write(f"content_hash={digest}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
