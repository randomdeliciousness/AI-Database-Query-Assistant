"""Tune provenance: clinic hash, clinic path, image digest, lock hash.

No network, no API key, and no ONNX model. eval has no package tests yet,
so the tune module is loaded from its file path.
"""

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

from clinic_generator import generate
from clinic_snapshot import content_hash
from retrieval import Trial, select_params, write_dev_report

REPO = Path(__file__).resolve().parent.parent
_TUNE_FILE = REPO / "eval" / "tune_retrieval.py"


def _load_tune():
    spec = importlib.util.spec_from_file_location("tune_retrieval", _TUNE_FILE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {_TUNE_FILE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["tune_retrieval"] = module
    spec.loader.exec_module(module)
    return module


TUNE = _load_tune()


def _forbid_embed(*_args, **_kwargs):
    raise AssertionError("embedding started")


def test_seed_11_content_hash_matches_tune_constant(tmp_path):
    path = tmp_path / "clinic.db"
    generate(path, 11)
    assert (
        content_hash(path) == TUNE.EXPECTED_CLINIC_CONTENT_HASH
    ), "generator output changed"


def test_missing_clinic_db_exits_without_repo_root_fallback(
    tmp_path, monkeypatch, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copyfile(REPO / "chinook.db", repo / "chinook.db")
    decoy = repo / "clinic.db"
    decoy.write_bytes(b"repo-root clinic.db must not be read")
    missing = tmp_path / "absent" / "clinic.db"
    monkeypatch.setattr(TUNE, "CLINIC_DB_PATH", missing)

    with pytest.raises(SystemExit) as caught:
        TUNE._schema_documents(repo)
    assert isinstance(caught.value.code, int) and caught.value.code != 0
    assert "/data/clinic.db" in capsys.readouterr().err
    assert decoy.read_bytes() == b"repo-root clinic.db must not be read"

    with pytest.raises(SystemExit) as caught_override:
        TUNE._frozen_clinic(missing)
    assert isinstance(caught_override.value.code, int)
    assert caught_override.value.code != 0
    assert "/data/clinic.db" in capsys.readouterr().err


def test_missing_image_digest_exits_before_embedding(monkeypatch, capsys):
    monkeypatch.setattr(TUNE, "load_embedding_function", _forbid_embed)
    monkeypatch.delenv("ADQ_IMAGE_DIGEST", raising=False)
    with pytest.raises(SystemExit) as caught:
        TUNE.main()
    assert isinstance(caught.value.code, int) and caught.value.code != 0
    assert "ADQ_IMAGE_DIGEST" in capsys.readouterr().err

    monkeypatch.setenv("ADQ_IMAGE_DIGEST", "   ")
    with pytest.raises(SystemExit) as caught_empty:
        TUNE.main()
    assert isinstance(caught_empty.value.code, int) and caught_empty.value.code != 0
    assert "ADQ_IMAGE_DIGEST" in capsys.readouterr().err


def test_report_records_digest_and_lock_hash(tmp_path, capsys):
    trials = [Trial(3, 0.45, 0.5, 0.25)]
    chosen = select_params(trials)
    rankings = [
        {
            "db": "chinook",
            "gold": ["Genre"],
            "ranked": ["Genre"],
            "operating": ["Genre"],
        }
    ]
    image_digest = "sha256:provenance-test"
    lock_hash = "ab" * 32
    clinic_hash = "c" * 64
    path = tmp_path / "retrieval_report_dev.json"
    report = write_dev_report(
        path,
        trials=trials,
        rankings=rankings,
        chosen=chosen,
        clinic_content_hash=clinic_hash,
        image_digest=image_digest,
        requirements_lock_sha256=lock_hash,
    )
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert report["clinic_content_hash"] == clinic_hash
    assert report["image_digest"] == image_digest
    assert report["requirements_lock_sha256"] == lock_hash
    assert loaded["image_digest"] == image_digest
    assert loaded["requirements_lock_sha256"] == lock_hash
    bare = write_dev_report(
        tmp_path / "bare.json",
        trials=trials,
        rankings=rankings,
        chosen=chosen,
    )
    assert "image_digest" not in bare
    assert "requirements_lock_sha256" not in bare
    assert "clinic_content_hash" not in bare

    payload = b"pkg==1 --hash=sha256:abc\n"
    lock = tmp_path / "requirements.lock"
    lock.write_bytes(payload)
    assert TUNE.requirements_lock_sha256(lock) == (hashlib.sha256(payload).hexdigest())
    missing_lock = tmp_path / "no-lock" / "requirements.lock"
    with pytest.raises(SystemExit) as caught:
        TUNE.requirements_lock_sha256(missing_lock)
    assert isinstance(caught.value.code, int) and caught.value.code != 0
    assert "requirements.lock" in capsys.readouterr().err
