"""Canonical content snapshot of a clinic database.

Opens only through sql_guard.open_readonly_plain. Does not checkpoint and
does not set journal_mode.
"""

import hashlib
import json
import os

from clinic_schema import quote_ident
from sql_guard import open_readonly_plain


def _cell(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("bool cell")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return ["hex", value.hex()]
    if isinstance(value, float):
        raise TypeError("float cell")
    raise TypeError(f"unsupported cell type {type(value).__name__}")


def snapshot_document(db_path: str | os.PathLike) -> list:
    """Canonical snapshot. Opens read-only and closes before returning.

    [
      ["schema", [[type, name, sql_or_null], ...]],
      ["tables", [[name, pk_columns, column_names, rows], ...]],
    ]
    Schema rows: sqlite_master type in ('table','index','view','trigger'),
    name not starting with 'sqlite_', sorted by (type, name).
    Tables: type 'table', sorted by name.
    pk_columns: PRAGMA table_info columns with pk > 0, ordered by pk ordinal.
    If there is no declared pk, pk_columns is ["rowid"] and rows are ordered
    by rowid, but rowid is not a hashed cell unless it is a declared column.
    column_names: PRAGMA table_info cid order.
    rows: cells in that column order, rows ordered by pk_columns.
    Cells: None, int, or str. bytes become ["hex", lowercase_hex].
    A float raises TypeError.
    """
    conn = open_readonly_plain(db_path)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT type, name, sql
            FROM sqlite_master
            WHERE type IN ('table', 'index', 'view', 'trigger')
              AND name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """)
        schema_rows = [[typ, name, sql] for typ, name, sql in cur.fetchall()]
        cur.execute("""
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """)
        table_names = [row[0] for row in cur.fetchall()]
        tables = []
        for name in table_names:
            quoted = quote_ident(name)
            cur.execute(f"PRAGMA table_info({quoted})")
            info = cur.fetchall()
            column_names = [row[1] for row in info]
            pk_pairs = sorted((int(row[5]), row[1]) for row in info if int(row[5]) > 0)
            if pk_pairs:
                pk_columns = [col for _, col in pk_pairs]
                order_sql = ", ".join(quote_ident(col) for col in pk_columns)
            else:
                pk_columns = ["rowid"]
                order_sql = "rowid"
            selected = ", ".join(quote_ident(col) for col in column_names)
            cur.execute(f"SELECT {selected} FROM {quoted} ORDER BY {order_sql}")
            rows = [[_cell(value) for value in record] for record in cur.fetchall()]
            tables.append([name, pk_columns, column_names, rows])
        return [["schema", schema_rows], ["tables", tables]]
    finally:
        conn.close()


def content_hash(db_path: str | os.PathLike) -> str:
    """SHA-256 hex of the canonical snapshot document.

    json.dumps(snapshot_document(db_path), ensure_ascii=False,
    separators=(",", ":"), sort_keys=True).encode("utf-8").

    The top-level value is a list, so the schema section stays first.
    """
    payload = json.dumps(
        snapshot_document(db_path),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
