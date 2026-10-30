"""Schema retrieval, prompt blocks, and the dev-split tuner math.

The embedding model is the pinned ONNX file. Nothing here calls a model
API or the spend meter. A missing local model raises OfflineModelMissing.
"""

import hashlib
import json
import logging
import math
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from clinic_schema import DATA_CLASS, LABEL, quote_ident
from config import (
    load_blocked_questions_path,
    load_onnx_model_dir,
    load_retrieval_params_path,
    load_seed_pairs_path,
)
from sql_guard import open_readonly_plain

logger = logging.getLogger(__name__)

# Directory the image build leaves model.onnx in. Host runs leave this unset.
IMAGE_ONNX_DIR = "/opt/adq/models/all-MiniLM-L6-v2"

_DDL_QUERY = """
SELECT type, name, sql
FROM sqlite_master
WHERE sql IS NOT NULL
  AND type IN ('table', 'view')
  AND name NOT LIKE 'sqlite_%'
ORDER BY type, name
"""

_SCHEMA_OBJECTS = """
SELECT type, name
FROM sqlite_master
WHERE type IN ('table', 'view')
  AND name NOT LIKE 'sqlite_%'
ORDER BY name
"""

RETRIEVAL_OBJECTIVE = (
    "maximize mean dev coverage_at_k, then mean dev recall_at_k, "
    "then smaller k_tables, then stricter cutoff (None last)"
)

# None means no distance filter. 1.01 is on the grid and is not applied:
# a float is applied only when it is under 1.01.
CUTOFF_GRID = (None, 0.25, 0.35, 0.45, 0.55, 0.65, 0.75, 1.01)
TABLE_K_GRID = (1, 3, 5)
_PAIR_CANDIDATES = 10
_TABLE_CANDIDATES = 5


# Every file chromadb's ONNXMiniLM_L6_V2 checks before it would download.
ONNX_MODEL_FILES = (
    "config.json",
    "model.onnx",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.txt",
)
# Committed per-file sha256 pins. A placeholder or null entry fails closed.
MODEL_MANIFEST_PATH = (
    Path(__file__).resolve().parent / "model_pins" / "all-MiniLM-L6-v2.sha256.json"
)


class OfflineModelMissing(RuntimeError):
    """A pinned ONNX file is missing or its sha256 is wrong. Nothing is downloaded."""


def _load_manifest(manifest_path: Path) -> dict[str, tuple[str, int | None]]:
    """Return {name: (sha256, bytes)}. Placeholder or null sha256 fails closed.

    Each ``files`` entry is ``{"path", "sha256", "bytes"}``; ``bytes`` is
    checked whenever it is pinned. ``bootstrap_log`` holds the build log lines
    the pins were copied from and is not read here.
    """
    try:
        data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OfflineModelMissing(
            f"model manifest unreadable: {manifest_path}"
        ) from exc
    if data.get("status") != "pinned":
        raise OfflineModelMissing(f"model manifest is not pinned: {manifest_path}")
    files = data.get("files") or {}
    pins = {}
    for name in ONNX_MODEL_FILES:
        entry = files.get(name)
        if not isinstance(entry, dict):
            raise OfflineModelMissing(f"model manifest has no entry for {name}")
        sha = entry.get("sha256")
        if (
            not isinstance(sha, str)
            or len(sha) != 64
            or any(c not in "0123456789abcdefABCDEF" for c in sha)
        ):
            raise OfflineModelMissing(f"model manifest has no sha256 for {name}")
        size = entry.get("bytes")
        if size is not None and (
            isinstance(size, bool) or not isinstance(size, int) or size < 0
        ):
            raise OfflineModelMissing(f"model manifest has a bad byte count for {name}")
        pins[name] = (sha.lower(), size)
    return pins


def verify_model_dir(root: Path, manifest_path: Path | None = None) -> None:
    """Raise OfflineModelMissing unless every file exists and matches its pin."""
    pins = _load_manifest(manifest_path or MODEL_MANIFEST_PATH)
    for name in ONNX_MODEL_FILES:
        path = Path(root) / name
        if not path.is_file():
            raise OfflineModelMissing(f"{name} is not in {root}")
        expected_sha, expected_bytes = pins[name]
        if expected_bytes is not None and path.stat().st_size != expected_bytes:
            raise OfflineModelMissing(f"{name} byte count mismatch in {root}")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected_sha:
            raise OfflineModelMissing(f"{name} sha256 mismatch in {root}")


@dataclass(frozen=True)
class RetrievalParams:
    k_tables: int
    k_pairs: int
    max_distance: float | None


START_PARAMS = RetrievalParams(k_tables=3, k_pairs=3, max_distance=None)


@dataclass(frozen=True)
class RetrievedTable:
    name: str
    text: str
    distance: float | None


@dataclass(frozen=True)
class RetrievedPair:
    question: str
    sql: str
    timestamp: str
    latency_ms: float | None
    question_id: str | None
    distance: float | None


@dataclass(frozen=True)
class RetryContext:
    failed_sql: str
    error_text: str


@dataclass(frozen=True)
class Trial:
    """One dev-split operating point. Scores are means over the dev rows."""

    k_tables: int
    max_distance: float | None
    coverage_at_k: float
    recall_at_k: float


def schema_ddl_text(db_path: str) -> str:
    """Canonical DDL text. A row update does not change it. ALTER TABLE does.

    Rows are sqlite_master entries with sql IS NOT NULL, type in table or
    view, name not like sqlite_%, ordered by type then name.
    """
    conn = open_readonly_plain(db_path)
    try:
        rows = conn.execute(_DDL_QUERY).fetchall()
    finally:
        conn.close()
    return "".join(f"{typ}\n{name}\n{sql}\n" for typ, name, sql in rows)


def schema_ddl_hash(db_path: str) -> str:
    """SHA-256 hex of the UTF-8 schema_ddl_text."""
    return hashlib.sha256(schema_ddl_text(db_path).encode("utf-8")).hexdigest()


def table_document(name: str, columns: list[tuple[str, str]]) -> str:
    """One table, in the same lines _get_db_schema uses."""
    return _schema_document("Table", name, columns)


def _schema_document(title: str, name: str, columns: Sequence[tuple[str, str]]) -> str:
    lines = [f"{title} {name}:"]
    lines.extend(f"  - {column} ({declared})" for column, declared in columns)
    return "\n".join(lines)


def iter_table_documents(db_path: str) -> list[tuple[str, str]]:
    """(name, document) for every table and view, ordered by name.

    A view document is titled ``View {name}:``.
    """
    conn = open_readonly_plain(db_path)
    try:
        objects = conn.execute(_SCHEMA_OBJECTS).fetchall()
        documents = []
        for kind, name in objects:
            info = conn.execute(f"PRAGMA table_info({quote_ident(name)})").fetchall()
            columns = [(row[1], row[2]) for row in info]
            title = "View" if kind == "view" else "Table"
            documents.append((name, _schema_document(title, name, columns)))
        return documents
    finally:
        conn.close()


def question_sha256(text: str) -> str:
    """SHA-256 hex of the stripped question. This is what blocked.json stores."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def load_params(path: str | None = None) -> RetrievalParams:
    """Tuned params, or START_PARAMS when the file is not there yet."""
    chosen = load_retrieval_params_path() if path is None else path
    file = Path(chosen)
    if not file.is_file():
        return START_PARAMS
    data = json.loads(file.read_text(encoding="utf-8"))
    distance = data["max_distance"]
    if distance is not None:
        distance = float(distance)
    return RetrievalParams(
        k_tables=int(data["k_tables"]),
        k_pairs=int(data["k_pairs"]),
        max_distance=distance,
    )


def load_blocked(path: str | None = None) -> tuple[frozenset[str], frozenset[str]]:
    """(question ids, stripped-question sha256 hex strings). Missing file is empty."""
    chosen = load_blocked_questions_path() if path is None else path
    if not chosen:
        return frozenset(), frozenset()
    file = Path(chosen)
    if not file.is_file():
        return frozenset(), frozenset()
    data = json.loads(file.read_text(encoding="utf-8"))
    ids = data.get("ids") or []
    hashes = data.get("question_sha256") or []
    return frozenset(str(item) for item in ids), frozenset(str(item) for item in hashes)


def read_jsonl(path: str | Path) -> list[dict]:
    file = Path(path)
    if not file.is_file():
        return []
    rows = []
    for line in file.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


RETRY_FAILED_SQL_CHARS = 4_096
RETRY_ERROR_CHARS = 500

# Ticket budget of 475 tokens per pair (950 chars).
# (4,668 − 3,248) × 2 = 2,840 chars, minus 62 formatting, = 926 = 185 + 741.
# Three max-size pairs leave 6 tokens of headroom under 4,674.
PAIR_QUESTION_CHARS = 185
PAIR_SQL_CHARS = 741


class PairLimitExceeded(ValueError):
    """Stored user pair is over the question (185) or SQL (741) character limit."""

    def __init__(self, message: str, limit: int):
        super().__init__(message)
        self.limit = limit


def collapse_whitespace(text: str) -> str:
    """Whitespace collapse shared by pair formatting, save, and retrieval."""
    return " ".join(text.split())


def format_tables_block(table_texts: Sequence[str]) -> str:
    body = "\n".join(table_texts) if table_texts else "(none)"
    return f"Relevant tables:\n{body}"


def format_pairs_block(pairs: Sequence[tuple[str, str]]) -> str:
    header = (
        "Similar past questions and example SQL "
        "(examples only, do not execute them):"
    )
    if not pairs:
        return header + "\n(none)"
    lines = [header]
    for index, (question, sql) in enumerate(pairs, start=1):
        collapsed_q = collapse_whitespace(question)
        collapsed_sql = collapse_whitespace(sql)
        lines.append(f"{index}. Question: {collapsed_q}")
        lines.append(f"   SQL: {collapsed_sql}")
    return "\n".join(lines)


def format_retry_block(failed_sql: str, error_text: str) -> str:
    failed_sql = failed_sql[:RETRY_FAILED_SQL_CHARS]
    error_text = error_text[:RETRY_ERROR_CHARS]
    return "\n".join(
        (
            "The previous SQL failed at SQLite execution. It was not a guard block.",
            "Failed SQL:",
            failed_sql,
            "SQLite error:",
            error_text,
        )
    )


_RESPOND_LINE = "Respond with ONLY valid SQL. No explanations."


def retrieval_user_prompt(
    table_texts: Sequence[str],
    pairs: Sequence[tuple[str, str]],
    natural_query: str,
    retry: RetryContext | None = None,
) -> str:
    """Retrieval-mode user message. Pair SQL is text only."""
    chunks = [
        "Convert the following natural language query to SQL.",
        "Database Schema:",
        format_tables_block(table_texts),
        "",
        format_pairs_block(pairs),
        "",
        f"Natural language query: {natural_query}",
    ]
    if retry is not None:
        chunks.append("")
        chunks.append(format_retry_block(retry.failed_sql, retry.error_text))
    chunks.append("")
    chunks.append(_RESPOND_LINE)
    return "\n".join(chunks)


def full_schema_user_prompt(
    schema: str, natural_query: str, retry: RetryContext | None = None
) -> str:
    """Full-schema user message. The first try is exactly sql_user_prompt."""
    from enhanced_assistant import sql_user_prompt

    base = sql_user_prompt(schema, natural_query)
    if retry is None:
        return base
    if not base.endswith(_RESPOND_LINE):
        raise RuntimeError("sql_user_prompt shape changed")
    head = base[: -len(_RESPOND_LINE)].rstrip("\n")
    return (
        head
        + "\n\n"
        + format_retry_block(retry.failed_sql, retry.error_text)
        + "\n\n"
        + _RESPOND_LINE
    )


def sqlite_error_text(error: BaseException) -> str:
    """One line, at most 500 characters. The audit log does not store this."""
    return " ".join(str(error).split())[:RETRY_ERROR_CHARS]


def collection_names(ddl_hash: str) -> tuple[str, str, str]:
    """(tables, seeds, user pairs). The short key is the first 32 hex digits."""
    short = ddl_hash[:32]
    return (f"t{short}", f"s{short}", f"u{short}")


def database_id(db_path: str) -> str:
    """``chinook``, ``clinic``, or ``other``.

    Chinook is the bundled file hash. Clinic is the provenance identity
    C-DB writes (data_class, label, generator). Anything else is other:
    table documents only, no seed fill, no user writes.
    """
    from audit_log import CHINOOK_SHA256, file_sha256

    if file_sha256(db_path) == CHINOOK_SHA256:
        return "chinook"
    if _is_published_clinic(db_path):
        return "clinic"
    return "other"


def _database_kind(db_path: str, is_chinook: bool | None) -> str:
    """Same labels as database_id.

    When the caller already classified the file, that flag is used and the
    file is not hashed again. The audit tests count file_sha256 calls.
    """
    if is_chinook is None:
        return database_id(db_path)
    if is_chinook:
        return "chinook"
    if _is_published_clinic(db_path):
        return "clinic"
    return "other"


def _is_published_clinic(db_path: str) -> bool:
    try:
        conn = open_readonly_plain(db_path)
    except (OSError, sqlite3.DatabaseError):
        return False
    try:
        try:
            rows = conn.execute(
                "SELECT data_class, label, generator FROM provenance"
            ).fetchall()
        except sqlite3.DatabaseError:
            return False
    finally:
        conn.close()
    if len(rows) != 1:
        return False
    data_class, label, generator = rows[0]
    return (
        data_class == DATA_CLASS and label == LABEL and generator == "clinic_generator"
    )


def load_embedding_function(
    model_dir: str | None = None, manifest_path: Path | None = None
):
    """ONNX MiniLM pointed at a local directory. Never downloads.

    ``model_dir`` defaults to ADQ_ONNX_MODEL_DIR. The directory must already
    contain every file in ONNX_MODEL_FILES, each matching the committed
    manifest. The class's network fetch (_download) is replaced so nothing
    can fall through to the network.
    """
    resolved = model_dir if model_dir is not None else load_onnx_model_dir()
    if resolved is None or not str(resolved).strip():
        raise OfflineModelMissing("ADQ_ONNX_MODEL_DIR is unset")
    root = Path(resolved)
    verify_model_dir(root, manifest_path)

    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import (
        ONNXMiniLM_L6_V2,
    )

    def _refuse_download(*_args, **_kwargs):
        raise OfflineModelMissing(
            "offline embedding model: refusing to download weights"
        )

    # The class loads DOWNLOAD_PATH / EXTRACTED_FOLDER_NAME / model.onnx.
    # ADQ_ONNX_MODEL_DIR is the directory that already contains model.onnx.
    ONNXMiniLM_L6_V2.DOWNLOAD_PATH = root
    if hasattr(ONNXMiniLM_L6_V2, "EXTRACTED_FOLDER_NAME"):
        ONNXMiniLM_L6_V2.EXTRACTED_FOLDER_NAME = ""
    # Only the network fetch is replaced. _download_model_if_not_exists stays
    # real: chromadb calls it on every embed, and it finds the verified files.
    ONNXMiniLM_L6_V2._download = _refuse_download
    return ONNXMiniLM_L6_V2()


def recall_at_k(gold: Sequence[str], ranked: Sequence[str], k: int) -> float:
    needed = set(gold)
    if not needed:
        return 1.0
    return len(needed & set(ranked[:k])) / len(needed)


def coverage_at_k(gold: Sequence[str], ranked: Sequence[str], k: int) -> float:
    """1 when every gold table is inside the first k names, else 0."""
    needed = set(gold)
    if not needed:
        return 1.0
    return 1.0 if needed <= set(ranked[:k]) else 0.0


def score_rankings(rows: Sequence[Mapping], ks: Sequence[int] = (1, 3, 5)) -> dict:
    """Means of recall and coverage at each k. ``ranked`` is the uncutoff top 5."""
    count = len(rows)
    scored = {"n": count}
    for k in ks:
        if count == 0:
            scored[f"recall_at_{k}"] = 0.0
            scored[f"coverage_at_{k}"] = 0.0
            continue
        scored[f"recall_at_{k}"] = (
            sum(recall_at_k(row["gold"], row["ranked"], k) for row in rows) / count
        )
        scored[f"coverage_at_{k}"] = (
            sum(coverage_at_k(row["gold"], row["ranked"], k) for row in rows) / count
        )
    return scored


def limit_tables(
    hits: Sequence[tuple[str, float | None]],
    *,
    k_tables: int,
    max_distance: float | None,
) -> list[str]:
    """Nearest-first (name, distance) hits after cutoff, k, and the one-table floor.

    A float cutoff is applied only when it is under 1.01. Distance ties keep
    the incoming order. If the cutoff drops every hit, the nearest hit stays.
    """
    ordered = list(hits)
    if isinstance(max_distance, float) and max_distance < 1.01:
        filtered = [
            name
            for name, distance in ordered
            if distance is None or distance <= max_distance
        ]
    else:
        filtered = [name for name, _distance in ordered]
    kept = filtered[:k_tables]
    if not kept and ordered:
        kept = [ordered[0][0]]
    return kept


def select_params(trials: Sequence[Trial]) -> RetrievalParams:
    """Coverage, then recall, then smaller k, then the stricter cutoff.

    None ranks last among cutoffs. k_pairs stays 3.
    """
    if not trials:
        raise ValueError("select_params needs at least one trial")

    def sort_key(trial: Trial) -> tuple:
        if trial.max_distance is None:
            cutoff_key = (1, 0.0)
        else:
            cutoff_key = (0, float(trial.max_distance))
        return (
            -trial.coverage_at_k,
            -trial.recall_at_k,
            trial.k_tables,
            cutoff_key,
        )

    best = min(trials, key=sort_key)
    return RetrievalParams(
        k_tables=best.k_tables, k_pairs=3, max_distance=best.max_distance
    )


def assert_dev_split(path: Path) -> None:
    """Refuse a test-split path and any row whose split is not dev."""
    path = Path(path)
    name = path.name
    as_text = str(path).replace("\\", "/")
    if name.startswith("test") or as_text.startswith("test"):
        raise ValueError(f"refusing test split path {path}")
    if not path.is_file():
        raise FileNotFoundError(path)
    for row in read_jsonl(path):
        split = row.get("split")
        if split != "dev":
            raise ValueError(f"refusing split {split!r} in {path}")


def assert_ids_disjoint(paths: Sequence[Path]) -> None:
    """Raise when the same question id appears in more than one file."""
    seen: dict[str, Path] = {}
    for path in paths:
        file = Path(path)
        if not file.is_file():
            continue
        for row in read_jsonl(file):
            qid = str(row["id"])
            if qid in seen:
                raise ValueError(f"{qid} is in both {seen[qid]} and {file}")
            seen[qid] = file


def cosine_distance(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine distance, the space the table collection is created with."""
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for a, b in zip(left, right, strict=True):
        dot += a * b
        left_norm += a * a
        right_norm += b * b
    if left_norm == 0.0 or right_norm == 0.0:
        return 1.0
    return 1.0 - dot / math.sqrt(left_norm * right_norm)


def params_payload(params: RetrievalParams) -> dict:
    return {
        "k_tables": params.k_tables,
        "k_pairs": params.k_pairs,
        "max_distance": params.max_distance,
        "cutoff": params.max_distance,
        "objective": RETRIEVAL_OBJECTIVE,
        "k_tables_grid": list(TABLE_K_GRID),
        "cutoff_grid": list(CUTOFF_GRID),
    }


def write_dev_report(
    path: str | Path,
    *,
    trials: Sequence[Trial],
    rankings: Sequence[Mapping],
    chosen: RetrievalParams,
    clinic_content_hash: str | None = None,
    image_digest: str | None = None,
    requirements_lock_sha256: str | None = None,
) -> dict:
    """Write the dev report. ``rankings`` rows have db, gold, ranked, operating.

    Recall and coverage at 1/3/5 use ``ranked`` (top 5, no cutoff).
    ``coverage_at_k`` uses ``operating`` (chosen k and cutoff, with the floor).
    ``grid`` has one entry for every trial, which the tuner fills with every
    k_tables in TABLE_K_GRID and every cutoff in CUTOFF_GRID.
    ``clinic_content_hash`` is the canonical clinic snapshot hash when the
    tuner has checked one. ``image_digest`` is ``ADQ_IMAGE_DIGEST``.
    ``requirements_lock_sha256`` is the sha256 hex of ``requirements.lock``.
    Hand-built trials omit any of these left as None.
    """
    report = _dev_report(
        trials,
        rankings,
        chosen,
        clinic_content_hash=clinic_content_hash,
        image_digest=image_digest,
        requirements_lock_sha256=requirements_lock_sha256,
    )
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    destination.write_text(payload, encoding="utf-8", newline="\n")
    return report


def _dev_report(
    trials: Sequence[Trial],
    rankings: Sequence[Mapping],
    chosen: RetrievalParams,
    clinic_content_hash: str | None = None,
    image_digest: str | None = None,
    requirements_lock_sha256: str | None = None,
) -> dict:
    metrics = score_rankings(rankings)
    operating_rows = [_operating_view(row) for row in rankings]
    coverage = _mean_coverage(operating_rows)
    report = {
        "split": "dev",
        "n": metrics["n"],
        "k_tables": chosen.k_tables,
        "max_distance": chosen.max_distance,
        "cutoff": chosen.max_distance,
        "recall_at_1": metrics["recall_at_1"],
        "recall_at_3": metrics["recall_at_3"],
        "recall_at_5": metrics["recall_at_5"],
        "coverage_at_1": metrics["coverage_at_1"],
        "coverage_at_3": metrics["coverage_at_3"],
        "coverage_at_5": metrics["coverage_at_5"],
        "coverage_at_k": coverage,
        "by_db": {
            "chinook": _db_block(rankings, "chinook"),
            "clinic": _db_block(rankings, "clinic"),
        },
        "grid": [_trial_point(trial) for trial in trials],
    }
    if clinic_content_hash is not None:
        report["clinic_content_hash"] = clinic_content_hash
    if image_digest is not None:
        report["image_digest"] = image_digest
    if requirements_lock_sha256 is not None:
        report["requirements_lock_sha256"] = requirements_lock_sha256
    return report


def _operating_view(row: Mapping) -> Mapping:
    operating = row.get("operating")
    if operating is None:
        operating = list(row["ranked"])
    return {"gold": row["gold"], "ranked": list(operating)}


def _mean_coverage(rows: Sequence[Mapping]) -> float:
    if not rows:
        return 0.0
    total = 0.0
    for row in rows:
        ranked = list(row["ranked"])
        total += coverage_at_k(row["gold"], ranked, len(ranked) or 1)
    return total / len(rows)


def _db_block(rankings: Sequence[Mapping], db_name: str) -> dict:
    rows = [row for row in rankings if row.get("db") == db_name]
    metrics = score_rankings(rows)
    block = {
        "n": metrics["n"],
        "recall_at_1": metrics["recall_at_1"],
        "recall_at_3": metrics["recall_at_3"],
        "recall_at_5": metrics["recall_at_5"],
        "coverage_at_1": metrics["coverage_at_1"],
        "coverage_at_3": metrics["coverage_at_3"],
        "coverage_at_5": metrics["coverage_at_5"],
        "coverage_at_k": _mean_coverage([_operating_view(row) for row in rows]),
    }
    return block


def _trial_point(trial: Trial) -> dict:
    return {
        "k_tables": trial.k_tables,
        "max_distance": trial.max_distance,
        "cutoff": trial.max_distance,
        "coverage_at_k": trial.coverage_at_k,
        "recall_at_k": trial.recall_at_k,
    }


def _blocked_hit(
    document: str,
    metadata: Mapping | None,
    chroma_id: str,
    blocked_ids: frozenset[str],
    blocked_hashes: frozenset[str],
) -> bool:
    meta = metadata or {}
    question_id = meta.get("question_id")
    if isinstance(question_id, str) and question_id in blocked_ids:
        return True
    if chroma_id in blocked_ids:
        return True
    for prefix in ("seed:", "user:"):
        if chroma_id.startswith(prefix) and chroma_id[len(prefix) :] in blocked_ids:
            return True
    return question_sha256(document or "") in blocked_hashes


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


class SchemaIndex:
    """Three collections keyed by the schema DDL hash. Old hashes stay on disk."""

    def __init__(
        self,
        db_path: str,
        client,
        embedding_function,
        params: RetrievalParams | None = None,
        seed_path: str | None = None,
        blocked_path: str | None = None,
    ):
        self.db_path = db_path
        self.client = client
        self.embedding_function = embedding_function
        self.params = params if params is not None else load_params()
        self.seed_path = load_seed_pairs_path() if seed_path is None else seed_path
        self.blocked_path = (
            load_blocked_questions_path() if blocked_path is None else blocked_path
        )
        self.ddl_hash = ""
        self.db_kind = "other"
        self.tables = None
        self.seeds = None
        self.users = None
        self.blocked_ids: frozenset[str] = frozenset()
        self.blocked_hashes: frozenset[str] = frozenset()

    def sync(self, *, is_chinook: bool | None = None) -> None:
        """Open t/s/u{hash[:32]} and store the full hash in collection metadata.

        An empty table collection is filled from the live schema. An empty
        seed collection is filled for chinook and clinic only. User pairs
        are not copied onto a new hash, and other hashes are not deleted.
        """
        self.ddl_hash = schema_ddl_hash(self.db_path)
        self.db_kind = _database_kind(self.db_path, is_chinook)
        self.blocked_ids, self.blocked_hashes = load_blocked(self.blocked_path)
        self.params = load_params()
        names = collection_names(self.ddl_hash)
        metadata = {"ddl_hash": self.ddl_hash, "hnsw:space": "cosine"}
        self.tables = self._open(names[0], metadata)
        self.seeds = self._open(names[1], metadata)
        self.users = self._open(names[2], metadata)
        if _collection_is_empty(self.tables):
            self._fill_tables(self.tables)
        if self.db_kind in ("chinook", "clinic") and _collection_is_empty(self.seeds):
            self._fill_seeds(self.seeds, self.db_kind)

    def _open(self, name: str, metadata: dict):
        try:
            return self.client.get_collection(
                name, embedding_function=self.embedding_function
            )
        except Exception:
            return self.client.create_collection(
                name,
                embedding_function=self.embedding_function,
                metadata=metadata,
            )

    def _fill_tables(self, collection) -> None:
        rows = iter_table_documents(self.db_path)
        if not rows:
            return
        collection.add(
            ids=[f"table:{name}" for name, _doc in rows],
            documents=[doc for _name, doc in rows],
            metadatas=[
                {"name": name, "ddl_hash": self.ddl_hash} for name, _doc in rows
            ],
        )

    def _fill_seeds(self, collection, kind: str) -> None:
        if not self.seed_path:
            return
        ids = []
        documents = []
        metadatas = []
        for row in read_jsonl(self.seed_path):
            if row.get("db") != kind:
                continue
            qid = str(row["id"])
            if qid in self.blocked_ids:
                continue
            ids.append(f"seed:{qid}")
            documents.append(row["question"])
            metadatas.append(
                {
                    "sql_query": row["sql"],
                    "timestamp": "seed",
                    "question_id": qid,
                    "ddl_hash": self.ddl_hash,
                }
            )
        if not ids:
            return
        collection.add(ids=ids, documents=documents, metadatas=metadatas)

    def retrieve(
        self, query: str, *, include_user: bool
    ) -> tuple[list[RetrievedTable], list[RetrievedPair]]:
        """Five table hits, then the cutoff, k_tables, and the one-table floor.

        Seed pairs are queried for chinook and clinic. User pairs are queried
        only when ``include_user`` is true. Blocked ids and question hashes
        are dropped.
        """
        if self.tables is None or self.seeds is None or self.users is None:
            self.sync()
        table_hits = _query_hits(self.tables, query, _TABLE_CANDIDATES)
        limited = limit_tables(
            [(hit["name"], hit["distance"]) for hit in table_hits],
            k_tables=self.params.k_tables,
            max_distance=self.params.max_distance,
        )
        by_name = {hit["name"]: hit for hit in table_hits}
        tables = [
            RetrievedTable(
                name=name,
                text=by_name[name]["text"],
                distance=by_name[name]["distance"],
            )
            for name in limited
            if name in by_name
        ]
        pair_hits: list[RetrievedPair] = []
        if self.db_kind in ("chinook", "clinic"):
            pair_hits.extend(self._pair_hits(self.seeds, query))
        if include_user:
            pair_hits.extend(self._pair_hits(self.users, query))
        pair_hits.sort(
            key=lambda hit: hit.distance if hit.distance is not None else 0.0
        )
        return tables, pair_hits[: self.params.k_pairs]

    def _pair_hits(self, collection, query: str) -> list[RetrievedPair]:
        found = []
        for hit in _query_hits(collection, query, _PAIR_CANDIDATES):
            if _blocked_hit(
                hit["text"],
                hit["metadata"],
                hit["id"],
                self.blocked_ids,
                self.blocked_hashes,
            ):
                continue
            meta = hit["metadata"] or {}
            sql = meta.get("sql_query")
            if not isinstance(sql, str):
                continue
            collapsed_question = collapse_whitespace(hit["text"])
            collapsed_sql = collapse_whitespace(sql)
            if len(collapsed_question) > PAIR_QUESTION_CHARS:
                logger.warning(
                    "question exceeds the %s character limit",
                    PAIR_QUESTION_CHARS,
                )
                continue
            if len(collapsed_sql) > PAIR_SQL_CHARS:
                logger.warning(
                    "SQL exceeds the %s character limit",
                    PAIR_SQL_CHARS,
                )
                continue
            question_id = meta.get("question_id")
            found.append(
                RetrievedPair(
                    question=hit["text"],
                    sql=sql,
                    timestamp=str(meta.get("timestamp") or ""),
                    latency_ms=_as_float(meta.get("latency_ms")),
                    question_id=question_id if isinstance(question_id, str) else None,
                    distance=hit["distance"],
                )
            )
        return found

    def add_user_pair(
        self,
        question: str,
        sql: str,
        metadata: Mapping,
        *,
        allow_raw_write: bool,
        question_id: str | None = None,
    ) -> bool:
        """Store one chinook user pair. Returns false without calling add otherwise."""
        if self.users is None:
            self.sync()
        if not allow_raw_write:
            return False
        if question_id and question_id in self.blocked_ids:
            return False
        if question_sha256(question) in self.blocked_hashes:
            return False
        collapsed_question = collapse_whitespace(question)
        collapsed_sql = collapse_whitespace(sql)
        if len(collapsed_question) > PAIR_QUESTION_CHARS:
            raise PairLimitExceeded(
                "Your answer is fine. It wasn't saved as an example because "
                f"the question is longer than {PAIR_QUESTION_CHARS} characters.",
                PAIR_QUESTION_CHARS,
            )
        if len(collapsed_sql) > PAIR_SQL_CHARS:
            raise PairLimitExceeded(
                "Your answer is fine. It wasn't saved as an example because "
                "the generated SQL is longer than "
                f"{PAIR_SQL_CHARS} characters.",
                PAIR_SQL_CHARS,
            )
        stored = {
            key: value
            for key, value in metadata.items()
            if value is not None and key != "question_id"
        }
        stored["sql_query"] = sql
        stored.setdefault("timestamp", _now_stamp())
        if question_id:
            stored["question_id"] = question_id
        stored["ddl_hash"] = self.ddl_hash
        self.users.add(
            ids=[f"user:{uuid.uuid4()}"],
            documents=[question],
            metadatas=[stored],
        )
        return True


def _now_stamp() -> str:
    import datetime

    return datetime.datetime.now().isoformat()


def _collection_is_empty(collection) -> bool:
    found = collection.get()
    ids = found.get("ids") or []
    return len(ids) == 0


def _query_hits(collection, text: str, n_results: int) -> list[dict]:
    if n_results <= 0:
        return []
    result = collection.query(
        query_texts=[text],
        n_results=n_results,
        include=["documents", "metadatas", "distances"],
    )
    docs = (result.get("documents") or [[]])[0] or []
    metas = (result.get("metadatas") or [[]])[0] or []
    ids = (result.get("ids") or [[]])[0] or []
    distances = (result.get("distances") or [[]])[0] or []
    hits = []
    for index, document in enumerate(docs):
        if not isinstance(document, str):
            continue
        meta = metas[index] if index < len(metas) and metas[index] else {}
        chroma_id = ids[index] if index < len(ids) and ids[index] else ""
        if index < len(distances):
            distance = _as_float(distances[index])
        else:
            distance = 0.0
        name = ""
        if isinstance(meta, Mapping) and isinstance(meta.get("name"), str):
            name = meta["name"]
        if not name and chroma_id.startswith("table:"):
            name = chroma_id[len("table:") :]
        hits.append(
            {
                "id": chroma_id,
                "text": document,
                "metadata": meta if isinstance(meta, Mapping) else {},
                "distance": distance,
                "name": name or document,
            }
        )
    return hits
