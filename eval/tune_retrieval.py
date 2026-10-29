"""Tune k_tables and the distance cutoff on the dev split only.

Run inside the image, with no network and no API key:

    python eval/tune_retrieval.py

This module does not import the spend meter or a model client. A missing
ONNX file raises OfflineModelMissing. It does not invent vectors.

Clinic table documents come only from ``/data/clinic.db``
(``CLINIC_DB_PATH``). There is no fallback to a repo-root ``clinic.db``.
Before any embedding, the tune requires ``ADQ_IMAGE_DIGEST``, a readable
``requirements.lock``, and a clinic content hash equal to
``EXPECTED_CLINIC_CONTENT_HASH`` (seed 11). A missing file or digest
prints to stderr and exits nonzero. A hash mismatch prints both hashes
and exits nonzero. The dev report records the clinic hash, the image
digest, and the lock hash.
"""

import hashlib
import json
import os
import sys
from pathlib import Path

from clinic_snapshot import content_hash
from retrieval import (
    CUTOFF_GRID,
    TABLE_K_GRID,
    Trial,
    assert_dev_split,
    cosine_distance,
    coverage_at_k,
    iter_table_documents,
    limit_tables,
    load_embedding_function,
    params_payload,
    read_jsonl,
    recall_at_k,
    select_params,
    write_dev_report,
)

# Seed-11 clinic content hash. A fresh generate(path, 11) must match it.
EXPECTED_CLINIC_CONTENT_HASH = (
    "b845161b9507434424c13a50f97f839d74a032acee89242a51ff2b25795e020c"
)
# In-image clinic file. main() uses this path. Tests may pass an override.
CLINIC_DB_PATH = Path("/data/clinic.db")


def _embed(embedding, texts: list[str]) -> list[list[float]]:
    vectors = embedding(texts)
    return [[float(value) for value in vector] for vector in vectors]


def _rank(question_vector: list[float], tables: list[tuple[str, list[float]]]):
    """Nearest five (name, cosine distance) hits. The tuner asks for five."""
    scored = [
        (name, cosine_distance(question_vector, vector)) for name, vector in tables
    ]
    scored.sort(key=lambda item: item[1])
    return scored[:5]


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _exit_if_hash_mismatch(computed: str, expected: str) -> None:
    if computed == expected:
        return
    print(f"computed {computed}", flush=True)
    print(f"expected {expected}", flush=True)
    raise SystemExit(1)


def _require_image_digest() -> str:
    """Return ADQ_IMAGE_DIGEST. Missing or empty exits nonzero. No default."""
    raw = os.environ.get("ADQ_IMAGE_DIGEST")
    if raw is None or raw.strip() == "":
        sys.stderr.write(
            "ADQ_IMAGE_DIGEST is unset or empty. "
            "Pass the image digest at docker run. No default.\n"
        )
        raise SystemExit(1)
    return raw.strip()


def _lock_path(lock_path: Path | None) -> Path:
    if lock_path is not None:
        return lock_path
    return Path(__file__).resolve().parent.parent / "requirements.lock"


def requirements_lock_sha256(lock_path: Path | None = None) -> str:
    """SHA-256 hex of requirements.lock bytes. A missing file exits nonzero."""
    path = _lock_path(lock_path)
    if not path.is_file():
        sys.stderr.write(f"requirements.lock is missing: {path}\n")
        raise SystemExit(1)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _clinic_file(clinic_path: Path | None) -> Path:
    if clinic_path is not None:
        return clinic_path
    return CLINIC_DB_PATH


def _frozen_clinic(clinic_path: Path | None = None) -> tuple[Path, str]:
    """Hash the clinic snapshot and refuse a seed-11 mismatch.

    ``clinic_path`` overrides ``CLINIC_DB_PATH`` for tests. main() passes
    none, so the file read is ``/data/clinic.db`` only.
    """
    path = _clinic_file(clinic_path)
    if not path.is_file():
        sys.stderr.write(
            f"clinic snapshot not found: {path}. "
            "The tune reads only /data/clinic.db. "
            "No fallback to a repo-root clinic.db.\n"
        )
        raise SystemExit(1)
    computed = content_hash(path)
    _exit_if_hash_mismatch(computed, EXPECTED_CLINIC_CONTENT_HASH)
    return path, computed


def _schema_documents(
    repo: Path,
    clinic_path: Path | None = None,
) -> tuple[dict[str, list[tuple[str, str]]], str]:
    clinic, digest = _frozen_clinic(clinic_path)
    chinook = repo / "chinook.db"
    if not chinook.is_file():
        raise FileNotFoundError(chinook)
    return {
        "chinook": iter_table_documents(str(chinook)),
        "clinic": iter_table_documents(str(clinic)),
    }, digest


def main() -> None:
    """Embed the dev split, score every grid point, and write both JSON files."""
    image_digest = _require_image_digest()
    lock_hash = requirements_lock_sha256()
    root = Path(__file__).resolve().parent
    repo = root.parent
    questions_path = root / "dev_questions.jsonl"
    gold_path = root / "gold_tables.jsonl"
    assert_dev_split(questions_path)
    assert_dev_split(gold_path)
    questions = read_jsonl(questions_path)
    gold = {str(row["id"]): row for row in read_jsonl(gold_path)}
    if {str(row["id"]) for row in questions} != set(gold):
        raise ValueError("dev question ids and gold ids differ")
    documents, clinic_hash = _schema_documents(repo)
    embedding = load_embedding_function()
    table_vectors: dict[str, list[tuple[str, list[float]]]] = {}
    for db_name, docs in documents.items():
        names = [name for name, _document in docs]
        vectors = _embed(embedding, [document for _name, document in docs])
        table_vectors[db_name] = list(zip(names, vectors, strict=True))
    question_vectors = _embed(embedding, [str(row["question"]) for row in questions])
    ranked_rows = []
    for row, vector in zip(questions, question_vectors, strict=True):
        db_name = str(row["db"])
        if db_name not in table_vectors:
            raise ValueError(f"no schema for db {db_name!r}")
        ranked_rows.append(
            {
                "db": db_name,
                "gold": list(gold[str(row["id"])]["tables"]),
                "hits": _rank(vector, table_vectors[db_name]),
            }
        )
    trials: list[Trial] = []
    for k_tables in TABLE_K_GRID:
        for cutoff in CUTOFF_GRID:
            coverages = []
            recalls = []
            for row in ranked_rows:
                kept = limit_tables(row["hits"], k_tables=k_tables, max_distance=cutoff)
                coverages.append(coverage_at_k(row["gold"], kept, k_tables))
                recalls.append(recall_at_k(row["gold"], kept, k_tables))
            trials.append(
                Trial(
                    k_tables=k_tables,
                    max_distance=cutoff,
                    coverage_at_k=_mean(coverages),
                    recall_at_k=_mean(recalls),
                )
            )
    chosen = select_params(trials)
    rankings = []
    for row in ranked_rows:
        operating = limit_tables(
            row["hits"],
            k_tables=chosen.k_tables,
            max_distance=chosen.max_distance,
        )
        rankings.append(
            {
                "db": row["db"],
                "gold": row["gold"],
                "ranked": [name for name, _distance in row["hits"]],
                "operating": operating,
            }
        )
    params_path = root / "retrieval_params.json"
    payload = json.dumps(params_payload(chosen), indent=2, sort_keys=True) + "\n"
    params_path.write_text(payload, encoding="utf-8", newline="\n")
    write_dev_report(
        root / "retrieval_report_dev.json",
        trials=trials,
        rankings=rankings,
        chosen=chosen,
        clinic_content_hash=clinic_hash,
        image_digest=image_digest,
        requirements_lock_sha256=lock_hash,
    )


if __name__ == "__main__":
    main()
