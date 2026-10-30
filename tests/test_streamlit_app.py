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
from retrieval import PairLimitExceeded, collapse_whitespace

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


_QUESTION_NOTICE = (
    "Your answer is fine. It wasn't saved as an example because "
    "the question is longer than 185 characters."
)
_SQL_NOTICE = (
    "Your answer is fine. It wasn't saved as an example because "
    "the generated SQL is longer than 741 characters."
)


def _over_limit_sql() -> str:
    """One Genre row. Length is the IN list, so collapsing spaces keeps it over 741."""
    return "SELECT Name FROM Genre WHERE GenreId IN (" + ",".join(["1"] * 400) + ")"


def _spy_on_add_user_pair(index):
    """Count add_user_pair calls and keep a raised PairLimitExceeded."""
    bound = index.add_user_pair

    def spy(*args, **kwargs):
        spy.calls += 1
        try:
            return bound(*args, **kwargs)
        except PairLimitExceeded as exc:
            spy.exc = exc
            raise

    spy.calls = 0
    spy.exc = None
    index.add_user_pair = spy
    return spy


def _prepare_over_limit(app):
    """Secrets, first run, one under-limit seed, and a save-path spy.

    Does not submit a question. The caller sets any over-limit SQL, asserts
    its collapsed length, then submits.
    """
    app.secrets["xai"] = {"api_key": "test-not-a-key"}
    app.run()
    assert not app.exception
    fake = FakeXaiClient.instances[-1]
    assert fake.scripted_sql == GENRES
    assistant = app.session_state["assistant"]
    collection = assistant.collection
    collection.add(
        documents=["how many genres are listed"],
        metadatas=[{"sql_query": "SELECT COUNT(*) AS n FROM Genre"}],
        ids=["user:seed"],
    )
    before = len(collection.documents)
    assert before >= 1
    spy = _spy_on_add_user_pair(assistant.index)
    return app, spy, collection, before


def _assert_over_limit_widgets(at, notice, number, banned, rows):
    assert not at.exception
    assert len(at.error) == 0
    assert len(at.success) == 1
    assert len(at.dataframe) == 1
    assert len(at.warning) == 1
    warning = at.warning[0].value
    assert warning == notice
    assert str(number) in warning
    for text in banned:
        assert text not in warning
    assert len(at.dataframe[0].value) == rows


def test_app_over_limit_notice_renders_without_storing(app):
    at, spy, collection, before = _prepare_over_limit(app)
    at.text_input[0].input("q" * 186).run()
    assert spy.calls == 1
    assert isinstance(spy.exc, PairLimitExceeded)
    assert spy.exc.limit == 185
    assert len(collection.documents) == before
    assert collection.documents == ["how many genres are listed"]
    _assert_over_limit_widgets(at, _QUESTION_NOTICE, 185, ["741"], 25)


def test_app_over_limit_sql_notice_renders_without_storing(app):
    at, spy, collection, before = _prepare_over_limit(app)
    question = "how many genres"
    assert len(collapse_whitespace(question)) <= 185
    sql = _over_limit_sql()
    assert len(collapse_whitespace(sql)) >= 742
    FakeXaiClient.instances[-1].scripted_sql = sql
    at.text_input[0].input(question).run()
    assert spy.calls == 1
    assert isinstance(spy.exc, PairLimitExceeded)
    assert spy.exc.limit == 741
    assert len(collection.documents) == before
    assert collection.documents == ["how many genres are listed"]
    _assert_over_limit_widgets(at, _SQL_NOTICE, 741, ["185"], 1)


def test_app_over_limit_both_names_the_question(app):
    at, spy, collection, before = _prepare_over_limit(app)
    sql = _over_limit_sql()
    assert len(collapse_whitespace(sql)) >= 742
    FakeXaiClient.instances[-1].scripted_sql = sql
    at.text_input[0].input("q" * 186).run()
    assert spy.calls == 1
    assert isinstance(spy.exc, PairLimitExceeded)
    assert spy.exc.limit == 185
    assert len(collection.documents) == before
    assert collection.documents == ["how many genres are listed"]
    _assert_over_limit_widgets(at, _QUESTION_NOTICE, 185, ["741", "generated SQL"], 1)
