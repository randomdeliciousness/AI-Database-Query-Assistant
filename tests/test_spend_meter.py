"""Spend meter: budget, usage pricing, and one call per reservation.

Model calls are the conftest fakes. XAI_API_KEY is unset. The ledger is the
per-test temp file from ADQ_SPEND_LEDGER_PATH, except the spawn tests, which
use their own file so two processes share one ledger.
"""

import json
import multiprocessing
import os
import shutil
import sys
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import FakeCompletions, FakeOpenAI, FakeXaiClient
from streamlit.testing.v1 import AppTest

import streamlit_app
from config import (
    BUDGET_USD,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL_ID,
    MODEL_PRICES,
    ModelConfig,
    load_xai_api_key,
)
from enhanced_assistant import (
    SQL_SYSTEM_PROMPT,
    EnhancedQueryAssistant,
    SQLResponse,
    sql_user_prompt,
)
from spend_meter import SpendMeter, SpendRefused

REPO = Path(__file__).resolve().parent.parent
_MILLION = Decimal(1_000_000)


@pytest.fixture
def tmp_db(tmp_path):
    db = tmp_path / "chinook.db"
    shutil.copyfile(REPO / "chinook.db", db)
    return db


def _ledger() -> Path:
    return Path(os.environ["ADQ_SPEND_LEDGER_PATH"])


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _seed_settled(path: Path, usd: Decimal) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "timestamp": "2026-10-28T00:00:00+00:00",
        "kind": "settled",
        "reservation_id": "seed-reservation",
        "model": DEFAULT_MODEL_ID,
        "prompt_tokens": 0,
        "cached_prompt_tokens": 0,
        "completion_tokens": 0,
        "usd": format(usd, "f"),
    }
    path.write_text(json.dumps(row) + "\n", encoding="utf-8", newline="\n")


def _assistant(db, **kwargs):
    return EnhancedQueryAssistant(str(db), "test-not-a-key", **kwargs)


def _fallback(db):
    return EnhancedQueryAssistant(
        str(db),
        "test-not-a-key",
        model_config=ModelConfig("xai-openai-compat", DEFAULT_MODEL_ID),
    )


def _prompt_tokens(assistant, question: str) -> int:
    user = sql_user_prompt(assistant.schema, question)
    return SpendMeter.estimate_prompt_tokens(
        SQL_SYSTEM_PROMPT
    ) + SpendMeter.estimate_prompt_tokens(user)


def _prompt_cost(tokens: int) -> Decimal:
    price = MODEL_PRICES[DEFAULT_MODEL_ID]
    return Decimal(tokens) * price["prompt"] / _MILLION


def _completion_cost() -> Decimal:
    price = MODEL_PRICES[DEFAULT_MODEL_ID]
    return Decimal(DEFAULT_MAX_TOKENS) * price["completion"] / _MILLION


def _assert_xai_no_retry(client) -> None:
    options = dict(client.channel_options)
    assert options["grpc.enable_retries"] == 0
    assert json.loads(options["grpc.service_config"]) == {
        "methodConfig": [{"name": [{}]}]
    }
    assert "retryPolicy" not in options["grpc.service_config"]
    assert client.timeout == 60


def _tests_on_path() -> None:
    """Spawn re-imports spend_workers; the child inherits this sys.path."""
    tests_dir = str(Path(__file__).resolve().parent)
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)


def test_estimate_is_ceil_of_chars_over_two():
    assert SpendMeter.estimate_prompt_tokens("") == 0
    assert SpendMeter.estimate_prompt_tokens("ab") == 1
    assert SpendMeter.estimate_prompt_tokens("abc") == 2


def test_xai_parse_uses_schema_and_logs_proto_model(tmp_db, monkeypatch, caplog):
    monkeypatch.setattr(FakeXaiClient, "response_model", "reported-model-name")
    with caplog.at_level("INFO", logger="enhanced_assistant"):
        assistant = _assistant(tmp_db)
        assistant.client.scripted_sql = "SELECT 1 AS x"
        assistant.execute_query("one")
    assert "model provider=xai id=grok-4.20-0309-non-reasoning" in caplog.text
    assert "response model=reported-model-name" in caplog.text
    assert "test-not-a-key" not in caplog.text
    assert len(assistant.client.calls) == 1
    call = assistant.client.calls[0]
    assert call["shape"] is SQLResponse
    assert call["max_tokens"] == 1024
    assert call["model"] == DEFAULT_MODEL_ID
    assert "test-not-a-key" not in _ledger().read_text(encoding="utf-8")


def test_fallback_logs_response_model(tmp_db, monkeypatch, caplog):
    monkeypatch.setattr(FakeCompletions, "response_model", "reported-model-name")
    with caplog.at_level("INFO", logger="enhanced_assistant"):
        assistant = _fallback(tmp_db)
    client = assistant.client
    assert isinstance(client, FakeOpenAI)
    assert client.base_url == "https://api.x.ai/v1"
    assert client.api_key == "test-not-a-key"
    assert client.max_retries == 0
    assert client.timeout == 60
    client.completions.sql = "SELECT 1 AS x"
    with caplog.at_level("INFO", logger="enhanced_assistant"):
        assistant.execute_query("one")
    assert len(client.completions.calls) == 1
    call = client.completions.calls[0]
    assert call["max_tokens"] == 1024
    assert call["response_format"] is SQLResponse
    assert "response model=reported-model-name" in caplog.text
    assert "test-not-a-key" not in caplog.text


def test_xai_key_env_beats_secrets(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "test-not-a-key-env")
    assert load_xai_api_key({"api_key": "test-not-a-key-secrets"}) == (
        "test-not-a-key-env"
    )


def test_xai_key_secrets_alone():
    assert load_xai_api_key({"api_key": "test-not-a-key-secrets"}) == (
        "test-not-a-key-secrets"
    )


def test_xai_key_missing_is_none():
    assert load_xai_api_key() is None
    assert load_xai_api_key({}) is None
    assert load_xai_api_key({"api_key": ""}) is None


def test_app_missing_key_builds_no_client(tmp_path, monkeypatch):
    shutil.copyfile(REPO / "chinook.db", tmp_path / "chinook.db")
    monkeypatch.chdir(tmp_path)
    app = AppTest.from_file(str(REPO / "streamlit_app.py"), default_timeout=30)
    app.run()
    assert not app.exception
    assert [item.value for item in app.error] == [streamlit_app.MISSING_KEY_MESSAGE]
    assert FakeXaiClient.instances == []
    assert FakeOpenAI.instances == []


def test_projected_equal_to_budget_is_allowed(tmp_db):
    assistant = _assistant(tmp_db)
    question = "one"
    worst = SpendMeter.worst_case_usd(
        DEFAULT_MODEL_ID, _prompt_tokens(assistant, question)
    )
    _seed_settled(_ledger(), BUDGET_USD - worst)
    assistant.client.scripted_sql = "SELECT 1 AS x"
    assistant.execute_query(question)
    assert len(assistant.client.calls) == 1


def test_projected_over_budget_is_refused(tmp_db):
    assistant = _assistant(tmp_db)
    question = "one"
    worst = SpendMeter.worst_case_usd(
        DEFAULT_MODEL_ID, _prompt_tokens(assistant, question)
    )
    _seed_settled(_ledger(), BUDGET_USD - worst + Decimal("0.0000001"))
    before = _read(_ledger())
    with pytest.raises(SpendRefused) as excinfo:
        assistant.execute_query(question)
    assert "test-not-a-key" not in str(excinfo.value)
    assert assistant.client.calls == []
    assert _read(_ledger()) == before


def test_prompt_fits_but_completion_crosses_budget(tmp_db):
    assistant = _assistant(tmp_db)
    question = "one"
    tokens = _prompt_tokens(assistant, question)
    prompt_cost = _prompt_cost(tokens)
    completion_cost = _completion_cost()
    seed = BUDGET_USD - prompt_cost - (completion_cost / 2)
    assert seed + prompt_cost <= BUDGET_USD
    assert seed + prompt_cost + completion_cost > BUDGET_USD
    _seed_settled(_ledger(), seed)
    before = _read(_ledger())
    with pytest.raises(SpendRefused):
        assistant.execute_query(question)
    assert assistant.client.calls == []
    assert _read(_ledger()) == before


def test_long_prompt_is_refused(tmp_db):
    assistant = _assistant(tmp_db)
    question = "a" * 400_000
    assert _prompt_tokens(assistant, question) >= 200_000
    with pytest.raises(SpendRefused) as excinfo:
        assistant.execute_query(question)
    assert "aaa" not in str(excinfo.value)
    assert assistant.client.calls == []
    assert _read(_ledger()) == []


def test_unknown_model_is_refused(monkeypatch, tmp_db):
    monkeypatch.setenv("ADQ_MODEL_ID", "not-a-priced-model")
    assistant = _assistant(tmp_db)
    assert assistant.model_config.model_id == "not-a-priced-model"
    before = _ledger().read_text(encoding="utf-8") if _ledger().exists() else ""
    with pytest.raises(SpendRefused):
        assistant.execute_query("one")
    assert assistant.client.calls == []
    after = _ledger().read_text(encoding="utf-8") if _ledger().exists() else ""
    assert after == before


def test_success_ledger_is_reserved_then_settled(tmp_db):
    key = "test-not-a-key-spend-ledger"
    question = "question-token-not-in-ledger"
    assistant = EnhancedQueryAssistant(str(tmp_db), key)
    assistant.client.scripted_sql = "SELECT 1 AS x"
    assistant.execute_query(question)
    rows = _read(_ledger())
    assert [row["kind"] for row in rows] == ["reserved", "settled"]
    text = _ledger().read_text(encoding="utf-8")
    for row in rows:
        assert isinstance(row["usd"], str)
        assert f'"{row["usd"]}"' in text
    assert key not in text
    assert question not in text
    audit = Path(os.environ["ADQ_AUDIT_PATH"]).read_text(encoding="utf-8")
    assert key not in audit
    SpendMeter(_ledger()).settle(rows[0]["reservation_id"], assistant.client.usage)
    assert _read(_ledger()) == rows


def test_timeout_settles_at_reserved_worst_case(tmp_db):
    assistant = _assistant(tmp_db)
    assistant.client.parse_error = TimeoutError("timed out")
    with pytest.raises(TimeoutError):
        assistant.execute_query("one")
    assert len(assistant.client.calls) == 1
    reserved, settled = _read(_ledger())
    assert reserved["kind"] == "reserved"
    assert settled["kind"] == "settled"
    assert settled["usd"] == reserved["usd"]
    assert settled["prompt_tokens"] == reserved["prompt_tokens"]
    assert settled["cached_prompt_tokens"] == reserved["cached_prompt_tokens"]
    assert settled["completion_tokens"] == reserved["completion_tokens"]
    assert settled["usd"] == format(
        SpendMeter.worst_case_usd(DEFAULT_MODEL_ID, reserved["prompt_tokens"]),
        "f",
    )


def test_xai_failing_call_is_one_attempt(tmp_db):
    assistant = _assistant(tmp_db)
    assistant.client.parse_error = RuntimeError("fail")
    with pytest.raises(RuntimeError, match="fail"):
        assistant.execute_query("one")
    assert len(assistant.client.calls) == 1
    assert assistant.client.calls[0]["max_tokens"] == 1024
    _assert_xai_no_retry(assistant.client)


def test_fallback_failing_call_is_one_attempt(tmp_db):
    assistant = _fallback(tmp_db)
    client = assistant.client
    client.completions.error = RuntimeError("fail")
    with pytest.raises(RuntimeError, match="fail"):
        assistant.execute_query("one")
    assert len(client.completions.calls) == 1
    assert client.completions.calls[0]["max_tokens"] == 1024
    assert client.max_retries == 0
    assert client.timeout == 60
    assert client.base_url == "https://api.x.ai/v1"
    assert client.api_key == "test-not-a-key"


def test_cached_subset_ignores_ticks_and_reasoning(tmp_db):
    assistant = _assistant(tmp_db)
    assistant.client.usage = SimpleNamespace(
        prompt_tokens=1000,
        cached_prompt_text_tokens=200,
        completion_tokens=100,
        reasoning_tokens=50_000,
        cost_in_usd_ticks=10**18,
    )
    assistant.client.scripted_sql = "SELECT 1 AS x"
    assistant.execute_query("one")
    settled = _read(_ledger())[1]
    assert settled["usd"] == "0.00129"
    assert settled["prompt_tokens"] == 1000
    assert settled["cached_prompt_tokens"] == 200
    assert settled["completion_tokens"] == 100


def test_cached_above_prompt_settles_worst_case(tmp_db):
    assistant = _assistant(tmp_db)
    assistant.client.usage = SimpleNamespace(
        prompt_tokens=100,
        cached_prompt_text_tokens=500,
        completion_tokens=10,
    )
    assistant.client.scripted_sql = "SELECT 1 AS x"
    assistant.execute_query("one")
    reserved, settled = _read(_ledger())
    assert settled["usd"] == reserved["usd"]
    assert settled["prompt_tokens"] == reserved["prompt_tokens"]
    assert settled["prompt_tokens"] != 100
    assert settled["completion_tokens"] == reserved["completion_tokens"] == 1024


def test_sampling_usage_exposes_priced_fields():
    """Installed SamplingUsage names the fields the meter reads.

    The pyi lists them on __slots__. The upb runtime leaves __slots__ empty
    and publishes the names on the descriptor. Either source counts.
    """
    from xai_sdk.proto import usage_pb2

    cls = usage_pb2.SamplingUsage
    names = set(cls.__slots__)
    names.update(field.name for field in cls.DESCRIPTOR.fields)
    assert {
        "prompt_tokens",
        "cached_prompt_text_tokens",
        "completion_tokens",
    } <= names


def test_fallback_cached_details_match_xai_price(tmp_db):
    assistant = _fallback(tmp_db)
    assistant.client.completions.usage = SimpleNamespace(
        prompt_tokens=1000,
        completion_tokens=100,
        prompt_tokens_details=SimpleNamespace(cached_tokens=200),
    )
    assistant.client.completions.sql = "SELECT 1 AS x"
    assistant.execute_query("one")
    assert _read(_ledger())[1]["usd"] == "0.00129"


def test_fallback_missing_details_counts_cached_as_zero(tmp_db):
    assistant = _fallback(tmp_db)
    assistant.client.completions.usage = SimpleNamespace(
        prompt_tokens=1000,
        completion_tokens=100,
    )
    assistant.client.completions.sql = "SELECT 1 AS x"
    assistant.execute_query("one")
    settled = _read(_ledger())[1]
    assert settled["usd"] == "0.0015"
    assert settled["cached_prompt_tokens"] == 0


def test_fallback_cached_above_prompt_settles_worst_case(tmp_db):
    assistant = _fallback(tmp_db)
    assistant.client.completions.usage = SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=10,
        prompt_tokens_details=SimpleNamespace(cached_tokens=500),
    )
    assistant.client.completions.sql = "SELECT 1 AS x"
    assistant.execute_query("one")
    reserved, settled = _read(_ledger())
    assert settled["usd"] == reserved["usd"]
    assert settled["prompt_tokens"] == reserved["prompt_tokens"]
    assert settled["completion_tokens"] == 1024


def test_gitignore_ignores_spend_dir():
    text = (REPO / ".gitignore").read_text(encoding="utf-8")
    assert "spend/" in text.splitlines()


def test_two_writers_one_reserve_lands_on_budget(tmp_path):
    _tests_on_path()
    from spend_workers import try_reserve

    ledger = tmp_path / "cost_ledger.jsonl"
    worst = SpendMeter.worst_case_usd(DEFAULT_MODEL_ID, 0)
    _seed_settled(ledger, BUDGET_USD - worst)
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    queue = ctx.Queue()
    procs = [
        ctx.Process(
            target=try_reserve,
            args=(str(ledger), DEFAULT_MODEL_ID, 0, barrier, queue),
        )
        for _ in range(2)
    ]
    for proc in procs:
        proc.start()
    try:
        for proc in procs:
            proc.join(60)
        assert all(not proc.is_alive() for proc in procs)
        results = [queue.get(timeout=10) for _ in range(2)]
    finally:
        for proc in procs:
            if proc.is_alive():
                proc.kill()
                proc.join(10)
    kinds = [item[0] for item in results]
    assert "error" not in kinds, results
    assert sorted(kinds) == ["ok", "refused"]
    reserved = [row for row in _read(ledger) if row["kind"] == "reserved"]
    assert len(reserved) == 1


def test_killed_child_keeps_open_reservation(tmp_path):
    _tests_on_path()
    from spend_workers import reserve_then_wait

    ledger = tmp_path / "cost_ledger.jsonl"
    worst = SpendMeter.worst_case_usd(DEFAULT_MODEL_ID, 0)
    _seed_settled(ledger, BUDGET_USD - worst)
    ready = tmp_path / "ready.txt"
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(
        target=reserve_then_wait,
        args=(str(ledger), DEFAULT_MODEL_ID, 0, str(ready)),
    )
    proc.start()
    try:
        deadline = time.time() + 30
        while time.time() < deadline and not ready.exists():
            if not proc.is_alive():
                break
            time.sleep(0.05)
        assert ready.is_file()
        reservation_id = ready.read_text(encoding="utf-8").strip()
        proc.kill()
        proc.join(30)
    finally:
        if proc.is_alive():
            proc.kill()
            proc.join(10)
    rows = _read(ledger)
    settled_ids = {row["reservation_id"] for row in rows if row["kind"] == "settled"}
    assert reservation_id not in settled_ids
    assert any(row["kind"] == "settled" for row in rows)
    with pytest.raises(SpendRefused):
        SpendMeter(ledger).reserve(DEFAULT_MODEL_ID, 0)
