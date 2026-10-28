"""Streamlit wiring: secrets fallback and the row-cap truncation notice.

The app runs in-process with streamlit's AppTest (no server, no browser),
against a tmp_path copy of chinook.db, with the xAI client and Chroma mocked.
"""

import shutil
from pathlib import Path

import pandas as pd
import pytest
from conftest import FakeXaiClient
from streamlit.errors import StreamlitSecretNotFoundError
from streamlit.testing.v1 import AppTest

import streamlit_app
from config import DEFAULT_MODEL_ID

REPO = Path(__file__).resolve().parent.parent
GENRES = "SELECT GenreId, Name FROM Genre ORDER BY GenreId"


class _NoSecretsFile:
    def to_dict(self):
        raise StreamlitSecretNotFoundError("no secrets.toml")


def test_load_secrets_without_file_returns_empty(monkeypatch):
    monkeypatch.setattr(streamlit_app.st, "secrets", _NoSecretsFile())
    assert streamlit_app.load_secrets() == {}


def _notice_text(n):
    return (
        f"Results truncated at the row cap: showing the first {n} rows only. "
        "Totals and charts computed from these rows may be incomplete."
    )


def test_truncation_notice():
    df = pd.DataFrame({"x": [1, 2]})
    assert streamlit_app.truncation_notice(df) is None
    df.attrs.update(truncated=False, row_cap=2)
    assert streamlit_app.truncation_notice(df) is None
    df.attrs.update(truncated=True, row_cap=2)
    assert streamlit_app.truncation_notice(df) == _notice_text(2)


def test_truncation_notice_uses_row_cap_not_row_count():
    # N comes from the configured cap in attrs, never from len(df).
    df = pd.DataFrame({"x": [1, 2]})
    df.attrs.update(truncated=True, row_cap=7)
    assert streamlit_app.truncation_notice(df) == _notice_text(7)


@pytest.fixture
def app(tmp_path, monkeypatch):
    shutil.copyfile(REPO / "chinook.db", tmp_path / "chinook.db")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(FakeXaiClient, "default_sql", GENRES)
    return AppTest.from_file(str(REPO / "streamlit_app.py"), default_timeout=30)


def _ask(at, question="List genres"):
    at.run()
    assert not at.exception
    at.text_input[0].input(question).run()
    assert not at.exception
    return at


def test_app_shows_truncation_notice(app, monkeypatch):
    monkeypatch.setenv("ADQ_SQL_ROW_CAP", "5")
    app.secrets["xai"] = {"api_key": "test-not-a-key"}
    at = _ask(app)
    assert [w.value for w in at.warning] == [_notice_text(5)]
    assert len(at.dataframe[0].value) == 5


def test_app_no_notice_under_cap(app):
    app.secrets["xai"] = {"api_key": "test-not-a-key"}
    at = _ask(app)
    assert len(at.warning) == 0
    assert len(at.dataframe[0].value) == 25


def test_app_reads_model_and_limits_from_secrets(app):
    app.secrets["xai"] = {"api_key": "test-not-a-key"}
    app.secrets["model"] = {"model_id": DEFAULT_MODEL_ID}
    app.secrets["sql_guard"] = {"row_cap": 3}
    at = _ask(app)
    assert len(at.dataframe[0].value) == 3
    assert [w.value for w in at.warning] == [_notice_text(3)]
    fake = FakeXaiClient.instances[-1]
    assert fake.api_key == "test-not-a-key"
    call = fake.calls[-1]
    assert call["max_tokens"] == 1024
    assert call["model"] == DEFAULT_MODEL_ID


def test_app_missing_key_shows_message(app):
    # No XAI_API_KEY (conftest removes it) and no [xai] secrets.
    at = app
    at.run()
    assert not at.exception
    assert [e.value for e in at.error] == [
        "Set XAI_API_KEY or [xai] api_key in .streamlit/secrets.toml"
    ]
    assert len(at.text_input) == 0
    assert FakeXaiClient.instances == []


def _key_used(at):
    at.run()
    assert not at.exception
    assert len(at.error) == 0
    return FakeXaiClient.instances[-1].api_key


def test_app_key_from_env_only(app, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "test-not-a-key-env")
    assert _key_used(app) == "test-not-a-key-env"


def test_app_key_from_secrets_only(app):
    app.secrets["xai"] = {"api_key": "test-not-a-key-secrets"}
    assert _key_used(app) == "test-not-a-key-secrets"


def test_app_key_env_wins_over_secrets(app, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "test-not-a-key-env")
    app.secrets["xai"] = {"api_key": "test-not-a-key-secrets"}
    assert _key_used(app) == "test-not-a-key-env"


def test_sample_button_runs_the_sample(app):
    app.secrets["xai"] = {"api_key": "test-not-a-key"}
    at = app
    at.run()
    assert not at.exception
    assert len(at.dataframe) == 0
    sample = streamlit_app.EnhancedQueryAssistant.SAMPLE_QUERIES[2]
    at.button(key="sample_2").click().run()
    assert not at.exception
    assert at.text_input(key="query").value == sample
    assert len(at.dataframe) == 1
    prompt = FakeXaiClient.instances[-1].calls[-1]["messages"][-1]
    assert sample in prompt.content[0].text


def test_no_mcp_checkbox_or_overclaims(app):
    app.secrets["xai"] = {"api_key": "test-not-a-key"}
    at = app
    at.run()
    assert len(at.sidebar.checkbox) == 0
    # No key in this fixture, so the outcome-only audit caption follows.
    assert [c.value for c in at.caption] == [
        "Natural Language → SQL → Insights",
        "Audit log: outcomes only. "
        "Set ADQ_AUDIT_HMAC_KEY to also log hashed questions and masked SQL.",
    ]


def test_startup_error_shows_only_the_type(app, monkeypatch, capfd, caplog):
    # ADQ-9: a startup exception's message (paths, SQL, keys, cell values)
    # never reaches the UI or stderr; only its class name does.
    sentinel = "SENTINEL-sk-test-0000 /secret/path SELECT * FROM Customer"

    class BoomError(RuntimeError):
        pass

    def boom(*args, **kwargs):
        raise BoomError(sentinel)

    monkeypatch.setattr("enhanced_assistant.EnhancedQueryAssistant", boom)
    app.secrets["xai"] = {"api_key": "test-not-a-key"}
    at = app
    at.run()
    assert not at.exception
    assert [e.value for e in at.error] == ["Startup failed: BoomError"]
    assert len(at.text_input) == 0
    err = capfd.readouterr().err
    assert "Startup failed: BoomError" in err
    assert sentinel not in err
    assert "SENTINEL" not in err
    assert "Traceback" not in err
    assert "streamlit_app.py" not in err
    assert "Traceback" not in caplog.text
    assert sentinel not in caplog.text
