"""Append-only audit log of every query (ADQ-1b C6).

One JSON object per line in ADQ_AUDIT_PATH (default ./audit/adq_audit.jsonl;
audit/ is git- and docker-ignored). The file is opened O_APPEND and created
0600. There is no switch to turn auditing off.

Row schema (every mode):
    timestamp      UTC ISO-8601
    request_id     uuid4, one per query (also returned by the API)
    session_id     a client session UUID label, or null
    log_mode       "keyed_hash", "outcome_only" or "raw_text"
    outcome        "answered", "blocked", "truncated" or "error"
    block_reason   the guard's reason flag (e.g. "authorizer", "text_check",
                   "step_budget"); null unless outcome is "blocked"
    row_count      rows returned; null for blocked and error
    truncated      true when the result hit the row cap; null for blocked
                   and error
    steps          SQLite VM steps counted by the guard (in blocks of 1000)
    model_provider, model_id, response_model (the model the API reported)
    error_class    exception class name for blocked and error rows, else null
keyed_hash mode (a key is set) adds:
    error_code             the sqlite error code (int) when there is one
    question_hmac          HMAC-SHA256 keyed hash of the question
    executed_sql_hmac      HMAC-SHA256 keyed hash of the raw SQL
    error_hmac             HMAC-SHA256 keyed hash of the error message
                           (blocked and error rows; else null)
    executed_sql_redacted  the SQL with every string, number and blob
                           literal replaced by ?, comments dropped, and
                           quoted identifiers kept only if they name a table
                           or column in the DB schema
raw_text mode (opt-in, bundled chinook.db only) adds:
    question, executed_sql, error_message (plain text) and error_code, plus
    the keyed hashes and executed_sql_redacted when a key is also set.
outcome_only mode (no key, bundled chinook.db) adds no question or SQL
field at all.

Result rows and cell values are never logged. SQLite error message text
(which can quote SQL literals) is logged only in raw_text mode, which runs
only on chinook.db. keyed_hash mode logs the error class, sqlite code and
keyed hash; outcome_only mode logs the error class only. The key is never
logged; only its length is checked.
"""

import datetime
import hashlib
import hmac
import json
import logging
import os
import threading
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sql_guard import open_readonly_plain

logger = logging.getLogger(__name__)

# sha256 of the bundled Chinook sample DB (same value the CI job checks).
CHINOOK_SHA256 = "7651ba378ac2fcd0dfc3c66fb101f7a7eed3ba39a612ec642b96e20702061f15"
DEFAULT_AUDIT_PATH = "audit/adq_audit.jsonl"
MIN_KEY_BYTES = 32
KEYGEN_COMMAND = 'python -c "import secrets; print(secrets.token_hex(32))"'
MISSING_KEY_MESSAGE = (
    "Set ADQ_AUDIT_HMAC_KEY or [audit] hmac_key in .streamlit/secrets.toml\n"
    f"Generate one with: {KEYGEN_COMMAND}"
)
SHORT_KEY_MESSAGE = (
    f"The audit HMAC key must be at least {MIN_KEY_BYTES} bytes.\n"
    f"Generate one with: {KEYGEN_COMMAND}"
)
RAW_TEXT_REFUSED_MESSAGE = (
    "Raw-text audit mode works only with the bundled chinook.db; "
    "refusing to start. Unset ADQ_AUDIT_RAW_TEXT / [audit] raw_text."
)
OUTCOME_ONLY_WARNING = (
    "Audit: question/SQL logging is off until a key is set "
    "(ADQ_AUDIT_HMAC_KEY or [audit] hmac_key)."
)
OUTCOMES = ("answered", "blocked", "truncated", "error")

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"", "0", "false", "no", "off"}


class AuditConfigError(RuntimeError):
    """The audit configuration does not allow this process to start, or (if
    the DB file changed after startup) this connection to be made."""


def db_stat_key(path: str | os.PathLike) -> tuple[int, int, int]:
    """(st_size, st_mtime_ns, st_ino): cheap change detector for the DB file."""
    st = os.stat(path)
    return (st.st_size, st.st_mtime_ns, st.st_ino)


class DBIdentity:
    """Is the DB file the bundled chinook.db? Re-checked on every connection.

    Each check stats the file; only when size, mtime_ns or inode differ from
    the last check is the file re-hashed and re-classified. Thread-safe.
    """

    def __init__(self, path: Path, stat_key: tuple[int, int, int], is_chinook: bool):
        self.path = path
        self._stat_key = stat_key
        self._is_chinook = is_chinook
        self._lock = threading.Lock()

    @classmethod
    def measure(cls, path: str | os.PathLike) -> "DBIdentity":
        path = Path(path).resolve()
        # stat before hashing: a change during the hash shows up next check.
        key = db_stat_key(path)
        return cls(path, key, file_sha256(path) == CHINOOK_SHA256)

    @property
    def is_chinook(self) -> bool:
        return self._is_chinook

    def check(self) -> bool:
        """Re-stat (and if changed, re-hash); return whether it is chinook."""
        with self._lock:
            key = db_stat_key(self.path)
            if key != self._stat_key:
                self._is_chinook = file_sha256(self.path) == CHINOOK_SHA256
                self._stat_key = key
                logger.info("audit: DB file changed; is_chinook=%s", self._is_chinook)
            return self._is_chinook


def file_sha256(path: str | os.PathLike) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _parse_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise AuditConfigError(f"{name} must be true or false")


@dataclass(frozen=True)
class AuditConfig:
    path: Path
    mode: str
    db_is_chinook: bool
    key: bytes | None = field(default=None, repr=False, compare=False)
    # Startup identity of the DB file (path, stat key, classification).
    db_identity: "DBIdentity | None" = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self.mode not in ("keyed_hash", "outcome_only", "raw_text"):
            raise ValueError("unknown audit mode")


def load_audit_config(
    db_path: str | os.PathLike, secrets: Mapping | None = None
) -> AuditConfig:
    """Decide the audit mode at startup, failing closed.

    Key: env ADQ_AUDIT_HMAC_KEY, then secrets ([audit] hmac_key). Raw-text
    switch: env ADQ_AUDIT_RAW_TEXT, then secrets ([audit] raw_text). The DB
    counts as the bundled chinook.db only if the sha256 of the file that will
    be opened matches CHINOOK_SHA256 (the file name does not matter).
    """
    secrets = secrets or {}
    identity = DBIdentity.measure(db_path)
    db_is_chinook = identity.is_chinook

    raw_key = os.getenv("ADQ_AUDIT_HMAC_KEY") or secrets.get("hmac_key") or ""
    key = str(raw_key).encode("utf-8") if raw_key else None
    del raw_key
    if key is not None and len(key) < MIN_KEY_BYTES:
        raise AuditConfigError(SHORT_KEY_MESSAGE)

    raw_env = os.getenv("ADQ_AUDIT_RAW_TEXT")
    raw_text = _parse_bool(
        raw_env if raw_env not in (None, "") else secrets.get("raw_text", False),
        "ADQ_AUDIT_RAW_TEXT / [audit] raw_text",
    )

    if raw_text:
        if not db_is_chinook:
            raise AuditConfigError(RAW_TEXT_REFUSED_MESSAGE)
        mode = "raw_text"
    elif key is not None:
        mode = "keyed_hash"
    elif db_is_chinook:
        mode = "outcome_only"
    else:
        raise AuditConfigError(MISSING_KEY_MESSAGE)

    path = Path(os.getenv("ADQ_AUDIT_PATH") or DEFAULT_AUDIT_PATH)
    return AuditConfig(
        path=path,
        mode=mode,
        db_is_chinook=db_is_chinook,
        key=key,
        db_identity=identity,
    )


def parse_session_id(value: str | None) -> str | None:
    """A client session label is accepted only if it is a valid UUID."""
    if not value:
        return None
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, AttributeError, TypeError):
        return None


def schema_identifiers(db_path: str | os.PathLike) -> frozenset[str]:
    """Casefolded table, view and column names of the DB (read-only)."""
    names: set[str] = set()
    conn = open_readonly_plain(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")
        tables = [row[0] for row in cur.fetchall()]
        for table in tables:
            names.add(table.casefold())
            quoted = '"' + table.replace('"', '""') + '"'
            cur.execute(f"PRAGMA table_info({quoted})")
            names.update(col[1].casefold() for col in cur.fetchall())
    finally:
        conn.close()
    return frozenset(names)


# --- SQL literal masker -----------------------------------------------------

_DIGITS = "0123456789"
_HEX = "0123456789abcdefABCDEF_"
_QUOTE_CLOSE = {'"': '"', "`": "`", "[": "]"}


def _skip_quoted(sql: str, i: int, close: str) -> tuple[int, bool]:
    """(index just past the quoted token starting at sql[i], terminated).

    Doubled close characters are escapes (except for [...]). An unterminated
    token runs to the end of the input.
    """
    n = len(sql)
    j = i + 1
    while j < n:
        if sql[j] == close:
            if close != "]" and j + 1 < n and sql[j + 1] == close:
                j += 2
                continue
            return j + 1, True
        j += 1
    return n, False


def _skip_number(sql: str, i: int) -> int:
    n = len(sql)
    j = i
    if sql[j] == "0" and j + 1 < n and sql[j + 1] in "xX":
        j += 2
        while j < n and sql[j] in _HEX:
            j += 1
        return j
    while j < n and (sql[j] in _DIGITS or sql[j] == "_"):
        j += 1
    if j < n and sql[j] == ".":
        j += 1
        while j < n and (sql[j] in _DIGITS or sql[j] == "_"):
            j += 1
    if j < n and sql[j] in "eE":
        k = j + 1
        if k < n and sql[k] in "+-":
            k += 1
        if k < n and sql[k] in _DIGITS:
            j = k
            while j < n and (sql[j] in _DIGITS or sql[j] == "_"):
                j += 1
    return j


def _is_ident_start(c: str) -> bool:
    return c.isalpha() or c == "_" or ord(c) > 127


def _is_ident_char(c: str) -> bool:
    return c.isalnum() or c in "_$" or ord(c) > 127


def mask_sql(sql: str, schema_names: Iterable[str] = ()) -> str:
    """Replace every literal in SQL with ?, by a hand-written lexer.

    Masked: '...' strings (with '' escapes), numbers (including 0x hex,
    decimals and exponents), X'..' blobs, and quoted tokens ("...", [...],
    `...`) that are not a table or column name in ``schema_names``.
    Comments (-- and /* */) are dropped, including any literal inside them.
    Unterminated strings or comments mask or drop the rest of the input.
    """
    names = {s.casefold() for s in schema_names}
    out: list[str] = []
    n = len(sql)
    i = 0
    while i < n:
        c = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if c == "-" and nxt == "-":
            j = sql.find("\n", i)
            i = n if j < 0 else j
            out.append(" ")
        elif c == "/" and nxt == "*":
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
        elif c == "'":
            i, _ = _skip_quoted(sql, i, "'")
            out.append("?")
        elif c in _QUOTE_CLOSE:
            j, terminated = _skip_quoted(sql, i, _QUOTE_CLOSE[c])
            token = sql[i:j]
            inner = token[1:-1]
            if c != "[":
                inner = inner.replace(c + c, c)
            out.append(token if terminated and inner.casefold() in names else "?")
            i = j
        elif c in _DIGITS or (c == "." and nxt != "" and nxt in _DIGITS):
            i = _skip_number(sql, i)
            out.append("?")
        elif _is_ident_start(c):
            j = i + 1
            while j < n and _is_ident_char(sql[j]):
                j += 1
            word = sql[i:j]
            if word in ("x", "X") and j < n and sql[j] == "'":
                i, _ = _skip_quoted(sql, j, "'")
                out.append("?")
            else:
                out.append(word)
                i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def sqlite_error_code(error: BaseException) -> int | None:
    """The sqlite error code of an error or the error it was raised from."""
    for e in (error, error.__cause__, error.__context__):
        code = getattr(e, "sqlite_errorcode", None)
        if isinstance(code, int):
            return code
    return None


def keyed_hash(key: bytes, text: str) -> str:
    """HMAC-SHA256 of text under the per-install key, hex."""
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()


# --- writer -----------------------------------------------------------------


class AuditLogger:
    """Builds and appends audit rows. One instance per assistant."""

    def __init__(self, config: AuditConfig, schema_names: Iterable[str] = ()):
        self.config = config
        self.schema_names = frozenset(s.casefold() for s in schema_names)
        self._lock = threading.Lock()
        self.identity = config.db_identity
        if config.mode == "outcome_only":
            logger.warning(OUTCOME_ONLY_WARNING)
        logger.info("audit log mode=%s path=%s", config.mode, config.path)

    def __repr__(self) -> str:
        return f"AuditLogger(mode={self.config.mode!r}, path={str(self.config.path)!r})"

    def build_row(
        self,
        *,
        request_id: str,
        session_id: str | None,
        outcome: str,
        question: str,
        sql: str | None,
        block_reason: str | None = None,
        row_count: int | None = None,
        truncated: bool | None = None,
        steps: int | None = None,
        model_provider: str | None = None,
        model_id: str | None = None,
        response_model: str | None = None,
        error: BaseException | None = None,
    ) -> dict[str, Any]:
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome {outcome!r}")
        if outcome in ("blocked", "error"):
            row_count = None
            truncated = None
        if outcome != "blocked":
            block_reason = None
        row: dict[str, Any] = {
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            "request_id": request_id,
            "session_id": parse_session_id(session_id),
            "log_mode": self.config.mode,
            "outcome": outcome,
            "block_reason": block_reason,
            "row_count": row_count,
            "truncated": truncated,
            "steps": steps,
            "model_provider": model_provider,
            "model_id": model_id,
            "response_model": response_model,
            "error_class": None if error is None else type(error).__name__,
        }
        if self.config.mode != "outcome_only":
            row["error_code"] = None if error is None else sqlite_error_code(error)
        key = self.config.key
        if self.config.mode == "raw_text":
            row["question"] = question
            row["executed_sql"] = sql
            row["error_message"] = None if error is None else str(error)
        if key is not None and self.config.mode in ("keyed_hash", "raw_text"):
            row["question_hmac"] = keyed_hash(key, question)
            row["executed_sql_hmac"] = None if sql is None else keyed_hash(key, sql)
            row["executed_sql_redacted"] = (
                None if sql is None else mask_sql(sql, self.schema_names)
            )
            row["error_hmac"] = None if error is None else keyed_hash(key, str(error))
        return row

    def db_is_chinook(self) -> bool:
        """Per-connection identity check (cheap stat; re-hash only on change)."""
        if self.identity is None:
            return self.config.db_is_chinook
        return self.identity.check()

    def require_db_allowed(self) -> bool:
        """Check the DB before a query. Returns whether it is chinook.

        Outcome-only and raw-text modes are allowed only on chinook.db, so a
        changed, non-chinook file is refused with the same AuditConfigError
        (and message) as a non-chinook DB at startup (fail closed).
        """
        is_chinook = self.db_is_chinook()
        if not is_chinook:
            if self.config.mode == "raw_text":
                raise AuditConfigError(RAW_TEXT_REFUSED_MESSAGE)
            if self.config.mode == "outcome_only":
                raise AuditConfigError(MISSING_KEY_MESSAGE)
        return is_chinook

    def record_refusal(self, error: BaseException, **fields) -> dict[str, Any]:
        """Error row for a query refused before anything ran: common fields
        and the error class only (no question, SQL or message, any mode)."""
        row = self.build_row(outcome="error", question="", sql=None, **fields)
        allowed = {
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
        }
        row = {k: v for k, v in row.items() if k in allowed}
        row["error_class"] = type(error).__name__
        self.write(row)
        return row

    def describe_error(self, error: BaseException) -> str:
        """Fixed-form text for a server log line, never the message text.

        Every mode: error class and the guard's reason flag. With a key (or
        in raw_text mode): also the sqlite code, and the keyed hash when a
        key is set.
        """
        parts = [f"error_class={type(error).__name__}"]
        reason = getattr(error, "reason", None)
        if isinstance(reason, str):
            parts.append(f"reason={reason}")
        if self.config.mode != "outcome_only":
            code = sqlite_error_code(error)
            if code is not None:
                parts.append(f"error_code={code}")
        if self.config.key is not None:
            parts.append(f"error_hmac={keyed_hash(self.config.key, str(error))}")
        return " ".join(parts)

    def write(self, row: dict[str, Any]) -> None:
        line = json.dumps(row, sort_keys=True) + "\n"
        path = self.config.path
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)

    def record(self, **fields) -> dict[str, Any]:
        row = self.build_row(**fields)
        self.write(row)
        return row
