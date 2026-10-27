"""Repo hygiene and README claims, checked on every CI run.

- The README's "N of N red-team cases" number equals the real case count,
  and every case is blocked.
- .gitignore and .dockerignore both keep secrets and local state out.
- .streamlit/secrets.example.toml parses, holds only empty values, and is
  not ignored, while .streamlit/secrets.toml is.
"""

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
from test_sql_guard import RED_TEAM_CASES, _assert_blocked

from config import (
    DEFAULT_CORS_ORIGINS,
    ModelConfig,
    load_cors_origins,
    load_model_config,
    load_openai_api_key,
    secrets_section,
)
from sql_guard import GuardConfig, GuardError, load_guard_config

REPO = Path(__file__).resolve().parent.parent
EXPECTED_RED_TEAM_CASES = 41
README_CLAIM = re.compile(
    r"\((\d+) of (\d+) red-team cases in `tests/test_sql_guard\.py` are blocked; "
    r"CI enforces this on every commit\)"
)
MUST_IGNORE = (".env", ".streamlit/secrets.toml", "audit/", "chroma_db/")
EXAMPLE = REPO / ".streamlit" / "secrets.example.toml"


def test_every_red_team_case_blocked_and_count_is_41(tmp_path):
    ids = [case_id for case_id, _ in RED_TEAM_CASES]
    assert len(ids) == len(set(ids)), "duplicate red-team case ids"
    assert len(RED_TEAM_CASES) == EXPECTED_RED_TEAM_CASES
    blocked = 0
    for case_id, sql in RED_TEAM_CASES:
        case_dir = tmp_path / case_id
        case_dir.mkdir()
        db = case_dir / "chinook.db"
        shutil.copyfile(REPO / "chinook.db", db)
        blocked += _assert_blocked(db, sql, case_id, GuardError)
    assert blocked == EXPECTED_RED_TEAM_CASES


def test_readme_red_team_number_matches_case_count():
    claims = README_CLAIM.findall((REPO / "README.md").read_text(encoding="utf-8"))
    assert len(claims) == 1, "README must state the red-team result exactly once"
    n_blocked, n_total = (int(x) for x in claims[0])
    assert n_blocked == n_total == len(RED_TEAM_CASES)


def _patterns(name):
    lines = (REPO / name).read_text(encoding="utf-8").splitlines()
    return {ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")}


@pytest.mark.parametrize("ignore_file", [".gitignore", ".dockerignore"])
@pytest.mark.parametrize("entry", MUST_IGNORE)
def test_ignore_files_list_secrets_and_state(ignore_file, entry):
    assert entry in _patterns(ignore_file)


def test_dockerignore_does_not_reinclude_secrets():
    assert not any(p.startswith("!") for p in _patterns(".dockerignore"))


def _git_check_ignore(path):
    if shutil.which("git") is None or not (REPO / ".git").exists():
        pytest.skip("needs a git checkout")
    return subprocess.run(
        ["git", "check-ignore", "-q", path], cwd=REPO, check=False
    ).returncode


def test_git_ignores_secrets_but_not_the_example():
    assert _git_check_ignore(".streamlit/secrets.example.toml") == 1
    for path in (".streamlit/secrets.toml", ".env", "audit/x.jsonl", "chroma_db/x"):
        assert _git_check_ignore(path) == 0, path


def _leaves(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from _leaves(v)
    else:
        yield value


def test_secrets_example_parses_with_empty_values_only():
    raw = EXAMPLE.read_text(encoding="utf-8")
    data = tomllib.loads(raw)
    assert set(data) == {"openai", "model", "sql_guard", "api", "audit"}
    assert data["openai"] == {"api_key": ""}
    assert data["audit"] == {"hmac_key": ""}
    assert all(v in ("", []) for v in _leaves(data))
    assert "sk-" not in raw
    assert "secrets.token_hex(32)" in raw


def test_secrets_example_values_fall_back_to_defaults():
    data = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert load_model_config(secrets_section(data, "model")) == ModelConfig()
    assert load_guard_config(secrets_section(data, "sql_guard")) == GuardConfig()
    assert load_cors_origins(secrets_section(data, "api")) == list(DEFAULT_CORS_ORIGINS)
    assert load_openai_api_key(secrets_section(data, "openai")) is None
