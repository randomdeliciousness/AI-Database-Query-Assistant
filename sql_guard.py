"""Read-only SQL guard for model-generated queries.

Layers, in order of trust:
1. The sqlite3 authorizer allowlist is the real gate. It allows only
   SQLITE_SELECT, SQLITE_READ, SQLITE_FUNCTION and SQLITE_RECURSIVE.
   Every other action is denied, including PRAGMA, ATTACH, DDL, writes and
   transactions.
2. The connection is opened with the URI mode=ro and PRAGMA query_only = ON
   (set before the authorizer is installed).
3. A small text check (one statement, starting with SELECT or WITH) is only
   a backup.
A progress handler counts SQLite VM steps and aborts once the count passes a
step budget; that is the primary, deterministic limit. A wall-clock time limit
is kept only as a backstop. Results are capped at a row limit with a truncated
flag. Errors are classified from flags set inside the authorizer and progress
callbacks (and sqlite error codes), never from sqlite's message text.
"""

import os
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

DEFAULT_STEP_BUDGET = 50_000_000
DEFAULT_ROW_CAP = 10_000
# Backstop only; the step budget is the primary limit.
DEFAULT_TIMEOUT_S = 5.0
# SQLite calls the progress handler every N VM steps; the guard adds N to its
# step count on each call, so the count is exact to within N steps.
_PROGRESS_STEP_N = 1_000

_SQLITE_RECURSIVE = getattr(sqlite3, "SQLITE_RECURSIVE", 33)
_ALLOWED_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        _SQLITE_RECURSIVE,
    }
)
_DENIED_FUNCTIONS = frozenset({"load_extension"})
_SQLITE_READONLY = getattr(sqlite3, "SQLITE_READONLY", 8)


class GuardError(Exception):
    """Base class for queries the guard refused or stopped.

    ``reason`` names the layer that stopped it: "text_check", "authorizer",
    "read_only", "sqlite3_api", "step_budget" or "timeout".
    """

    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.reason = reason


class UnsafeSQLError(GuardError):
    """The query is not a single read-only SELECT/WITH statement."""


class StepBudgetExceededError(GuardError):
    """The query ran past the VM step budget and was aborted."""

    def __init__(self, steps: int, budget: int):
        super().__init__(
            f"query exceeded the step budget ({steps} > {budget} VM steps)",
            reason="step_budget",
        )
        self.steps = steps
        self.budget = budget


class QueryTimeoutError(GuardError):
    """Backstop: the query ran past the wall-clock limit and was aborted."""

    def __init__(self, timeout_s: float):
        super().__init__(
            f"query exceeded the {timeout_s}s wall-clock backstop", reason="timeout"
        )
        self.timeout_s = timeout_s


@dataclass(frozen=True)
class GuardConfig:
    step_budget: int = DEFAULT_STEP_BUDGET
    row_cap: int = DEFAULT_ROW_CAP
    timeout_s: float = DEFAULT_TIMEOUT_S


def load_guard_config(secrets: Mapping | None = None) -> GuardConfig:
    """Read limits from env, then an optional secrets mapping, then defaults.

    Env: ADQ_SQL_STEP_BUDGET, ADQ_SQL_ROW_CAP, ADQ_SQL_TIMEOUT_S.
    Secrets: a mapping with keys step_budget / row_cap / timeout_s
    (e.g. st.secrets["sql_guard"]).
    """
    secrets = secrets or {}
    step_budget = int(
        os.getenv("ADQ_SQL_STEP_BUDGET")
        or secrets.get("step_budget")
        or DEFAULT_STEP_BUDGET
    )
    row_cap = int(
        os.getenv("ADQ_SQL_ROW_CAP") or secrets.get("row_cap") or DEFAULT_ROW_CAP
    )
    timeout_s = float(
        os.getenv("ADQ_SQL_TIMEOUT_S") or secrets.get("timeout_s") or DEFAULT_TIMEOUT_S
    )
    if step_budget <= 0 or row_cap <= 0 or timeout_s <= 0:
        raise ValueError("step_budget, row_cap and timeout_s must be positive")
    return GuardConfig(step_budget=step_budget, row_cap=row_cap, timeout_s=timeout_s)


def _strip_literals_and_comments(sql: str) -> str:
    """Replace string/identifier literals with a placeholder and drop comments."""
    out = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c in ("'", '"', "`") or c == "[":
            close = "]" if c == "[" else c
            j = i + 1
            while j < n:
                if sql[j] == close:
                    if close != "]" and j + 1 < n and sql[j + 1] == close:
                        j += 2  # doubled quote escape
                        continue
                    break
                j += 1
            if j >= n:
                raise UnsafeSQLError("unterminated quoted literal", "text_check")
            out.append(" _ ")
            i = j + 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j + 1
            out.append(" ")
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            if j == -1:
                raise UnsafeSQLError("unterminated block comment", "text_check")
            i = j + 2
            out.append(" ")
        else:
            out.append(c)
            i += 1
    return "".join(out)


def check_sql(sql: str) -> str:
    """Backup text check. Returns the SQL with trailing semicolons removed."""
    if not isinstance(sql, str) or not sql.strip():
        raise UnsafeSQLError("empty query", "text_check")
    if "\x00" in sql:
        raise UnsafeSQLError("NUL byte in query", "text_check")
    sql = sql.strip()
    while sql.endswith(";"):
        sql = sql[:-1].rstrip()
    bare = _strip_literals_and_comments(sql)
    if ";" in bare:
        raise UnsafeSQLError("more than one statement", "text_check")
    words = bare.split()
    first = words[0].upper() if words else ""
    if first not in ("SELECT", "WITH"):
        raise UnsafeSQLError(
            f"only SELECT/WITH queries are allowed, got {first!r}", "text_check"
        )
    return sql


def _is_allowed(action, arg2) -> bool:
    if action not in _ALLOWED_ACTIONS:
        return False
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in _DENIED_FUNCTIONS:
        return False
    return True


def _make_authorizer(state: dict | None = None):
    """Allowlist authorizer. Records each denial in ``state["denied"]``."""

    def _authorizer(action, arg1, arg2, db_name, trigger):
        if _is_allowed(action, arg2):
            return sqlite3.SQLITE_OK
        if state is not None:
            state["denied"].append(action)
        return sqlite3.SQLITE_DENY

    return _authorizer


def _ro_uri(db_path: str | os.PathLike) -> str:
    path = Path(db_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(str(path))
    return f"file:{quote(str(path))}?mode=ro"


def open_readonly_plain(db_path: str | os.PathLike) -> sqlite3.Connection:
    """mode=ro plus query_only, no authorizer. For internal schema reads only.

    Double-quoted string literals are turned off (DQS_DML and DQS_DDL), so
    "Smith" is always an identifier and an unknown one is an error instead
    of silently matching as the string 'Smith'.
    """
    conn = sqlite3.connect(_ro_uri(db_path), uri=True)
    conn.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DML, False)
    conn.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DDL, False)
    conn.execute("PRAGMA query_only = ON")
    return conn


def open_guarded(
    db_path: str | os.PathLike, state: dict | None = None
) -> sqlite3.Connection:
    """mode=ro, then PRAGMA query_only = ON, then the authorizer allowlist."""
    conn = open_readonly_plain(db_path)
    conn.set_authorizer(_make_authorizer(state))
    return conn


def run_readonly_query(
    db_path: str | os.PathLike, sql: str, config: GuardConfig | None = None
) -> tuple[list[str], list[tuple], bool]:
    """Run one read-only query. Returns (columns, rows, truncated)."""
    config = config or load_guard_config()
    sql = check_sql(sql)
    state = {"denied": [], "abort": None, "steps": 0}
    conn = open_guarded(db_path, state)
    deadline = time.monotonic() + config.timeout_s

    def _progress() -> int:
        state["steps"] += _PROGRESS_STEP_N
        if state["steps"] > config.step_budget:
            state["abort"] = "step_budget"
            return 1
        if time.monotonic() > deadline:
            state["abort"] = "timeout"
            return 1
        return 0

    conn.set_progress_handler(_progress, _PROGRESS_STEP_N)
    try:
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchmany(config.row_cap + 1)
        columns = [d[0] for d in (cur.description or [])]
    except sqlite3.ProgrammingError as e:
        # Raised by the sqlite3 module before anything runs, e.g. more than
        # one statement per execute().
        raise UnsafeSQLError(f"rejected by sqlite3: {e}", "sqlite3_api") from e
    except sqlite3.DatabaseError as e:
        if state["abort"] == "step_budget":
            raise StepBudgetExceededError(state["steps"], config.step_budget) from e
        if state["abort"] == "timeout":
            raise QueryTimeoutError(config.timeout_s) from e
        if state["denied"]:
            raise UnsafeSQLError(f"rejected by authorizer: {e}", "authorizer") from e
        if (getattr(e, "sqlite_errorcode", 0) or 0) & 0xFF == _SQLITE_READONLY:
            raise UnsafeSQLError(
                f"rejected by read-only connection: {e}", "read_only"
            ) from e
        raise
    finally:
        conn.close()
    truncated = len(rows) > config.row_cap
    return columns, rows[: config.row_cap], truncated
