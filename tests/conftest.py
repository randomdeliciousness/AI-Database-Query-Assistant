"""Shared test fixtures.

Every test runs offline: XAI_API_KEY and OPENAI_API_KEY are removed, Chroma
telemetry is off, outbound sockets raise, and enhanced_assistant's xAI client,
OpenAI client and Chroma PersistentClient are replaced with in-memory fakes
(no model call, no ONNX embedding download, no ./chroma_db written). The
spend ledger goes to a fresh temp file, same as the audit log.
"""

import os
import socket
import sys
from types import SimpleNamespace
from typing import ClassVar

import pytest

# Drop a developer ADQ_DB_PATH before test modules import mcp_server, which
# reads the database path once at import.
os.environ.pop("ADQ_DB_PATH", None)

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import enhanced_assistant

_LOCAL_HOSTS = (None, "localhost", "127.0.0.1", "::1")

# Config env vars cleared before each test so a developer's shell cannot
# change the defaults a test expects.
_ADQ_CONFIG_ENV = (
    "ADQ_MODEL_PROVIDER",
    "ADQ_MODEL_ID",
    "ADQ_SQL_STEP_BUDGET",
    "ADQ_SQL_ROW_CAP",
    "ADQ_SQL_TIMEOUT_S",
    "ADQ_CORS_ORIGINS",
    "ADQ_AUDIT_HMAC_KEY",
    "ADQ_AUDIT_RAW_TEXT",
    "ADQ_SPEND_LEDGER_PATH",
    "ADQ_DB_PATH",
)

_REDTEAM_KEY = pytest.StashKey[dict]()


def pytest_configure(config):
    config.stash[_REDTEAM_KEY] = {"layers": {}, "limits": None}


@pytest.fixture(scope="session")
def redteam_log(request):
    """Shared record of red-team outcomes, printed in the terminal summary."""
    return request.config.stash[_REDTEAM_KEY]


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    log = config.stash.get(_REDTEAM_KEY, None)
    if not log or not log["layers"]:
        return
    tr = terminalreporter
    tr.section("SQL guard red-team summary")
    full = log["layers"].get("full_guard", {})
    blocked = sum(1 for ok in full.values() if ok)
    tr.write_line(f"{blocked} of {len(full)} red-team cases blocked")
    auth = log["layers"].get("text_check_bypassed", {})
    if auth:
        a_blocked = sum(1 for ok in auth.values() if ok)
        tr.write_line(
            f"(with the text check bypassed: {a_blocked} of {len(auth)} red-team "
            "cases blocked by the connection/authorizer layers alone)"
        )
    if log["limits"]:
        tr.write_line(log["limits"])


class NetworkBlockedError(OSError):
    """Raised when a test tries to open an outbound connection."""


@pytest.fixture(autouse=True)
def _offline(monkeypatch, tmp_path_factory):
    """No key, no telemetry, no outbound sockets, no inherited ADQ_* config.

    A test that needs the network fails. The audit log and the spend ledger
    each go to a fresh per-test temp dir (outside tmp_path, so DB-directory
    snapshots are unaffected), never to the repo's audit/ or spend/.
    """
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    for name in _ADQ_CONFIG_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "False")
    audit_dir = tmp_path_factory.mktemp("audit")
    monkeypatch.setenv("ADQ_AUDIT_PATH", str(audit_dir / "adq_audit.jsonl"))
    spend_dir = tmp_path_factory.mktemp("spend")
    monkeypatch.setenv("ADQ_SPEND_LEDGER_PATH", str(spend_dir / "cost_ledger.jsonl"))

    def _deny_connect(self, address, *args, **kwargs):
        raise NetworkBlockedError(f"network access in tests is a failure: {address!r}")

    real_getaddrinfo = socket.getaddrinfo

    def _deny_getaddrinfo(host, *args, **kwargs):
        if host not in _LOCAL_HOSTS:
            raise NetworkBlockedError(f"network access in tests is a failure: {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", _deny_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _deny_connect)
    monkeypatch.setattr(socket, "getaddrinfo", _deny_getaddrinfo)


class FakeCompletions:
    """Stands in for client.beta.chat.completions; returns a scripted SQL string."""

    default_sql: ClassVar[str] = "SELECT 1"
    # Model name the fake API reports back; None echoes the requested model.
    response_model: ClassVar[str | None] = None

    def __init__(self):
        self.sql = self.default_sql
        self.calls = []
        self.usage = None
        self.error = None

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        parsed = SimpleNamespace(sql_query=self.sql)
        reported = type(self).response_model
        if reported is None:
            reported = kwargs.get("model")
        return SimpleNamespace(
            model=reported,
            choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))],
            usage=self.usage,
        )


class FakeOpenAI:
    """Replaces openai.OpenAI inside enhanced_assistant. Makes no network call."""

    instances: ClassVar[list] = []

    def __init__(
        self, api_key=None, base_url=None, max_retries=None, timeout=None, **kwargs
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.max_retries = max_retries
        self.timeout = timeout
        self.extra = kwargs
        self.completions = FakeCompletions()
        self.beta = SimpleNamespace(chat=SimpleNamespace(completions=self.completions))
        FakeOpenAI.instances.append(self)


class FakeXaiClient:
    """Replaces xai_sdk.Client. create returns self; parse returns a pair."""

    instances: ClassVar[list] = []
    default_sql: ClassVar[str] = "SELECT 1"
    # Name exposed as response.proto.model. None echoes the requested model.
    response_model: ClassVar[str | None] = None

    def __init__(self, api_key=None, timeout=None, channel_options=None, **kwargs):
        self.api_key = api_key
        self.timeout = timeout
        self.channel_options = channel_options
        self.extra = kwargs
        self.chat = self
        self.calls = []
        self.scripted_sql = type(self).default_sql
        self.usage = SimpleNamespace(
            prompt_tokens=12,
            cached_prompt_text_tokens=0,
            completion_tokens=3,
        )
        self.parse_error = None
        self._created: dict = {}
        FakeXaiClient.instances.append(self)

    def create(self, **kwargs):
        self._created = kwargs
        return self

    def parse(self, shape):
        created = self._created
        model = created.get("model")
        self.calls.append(
            {
                "model": model,
                "max_tokens": created.get("max_tokens"),
                "messages": created.get("messages"),
                "shape": shape,
            }
        )
        if self.parse_error is not None:
            raise self.parse_error
        reported = type(self).response_model
        if reported is None:
            reported = model
        parsed = SimpleNamespace(sql_query=self.scripted_sql)
        response = SimpleNamespace(
            proto=SimpleNamespace(model=reported),
            usage=self.usage,
        )
        return response, parsed


class FakeCollection:
    """In-memory stand-in for a Chroma collection (no embedding model)."""

    def __init__(self, name):
        self.name = name
        self.documents = []
        self.metadatas = []
        self.ids = []

    def add(self, documents, metadatas, ids):
        self.documents.extend(documents)
        self.metadatas.extend(metadatas)
        self.ids.extend(ids)

    def query(self, query_texts, n_results=3):
        docs = self.documents[-n_results:][::-1]
        metas = self.metadatas[-n_results:][::-1]
        return {"documents": [docs], "metadatas": [metas]}


class FakeChromaClient:
    """Replaces chromadb.PersistentClient; never touches disk."""

    def __init__(self, path=None, **kwargs):
        self.path = path
        self.collections = {}

    def get_collection(self, name):
        if name not in self.collections:
            raise ValueError(f"Collection {name} does not exist")
        return self.collections[name]

    def create_collection(self, name):
        self.collections[name] = FakeCollection(name)
        return self.collections[name]


@pytest.fixture(autouse=True)
def _fake_model_and_store(monkeypatch):
    """xAI and OpenAI stay mocked in every test; Chroma is an in-memory fake."""
    FakeOpenAI.instances = []
    FakeXaiClient.instances = []
    FakeXaiClient.response_model = None
    FakeXaiClient.default_sql = "SELECT 1"
    FakeCompletions.response_model = None
    FakeCompletions.default_sql = "SELECT 1"
    monkeypatch.setattr(enhanced_assistant, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(enhanced_assistant, "XaiClient", FakeXaiClient)
    monkeypatch.setattr(
        enhanced_assistant.chromadb, "PersistentClient", FakeChromaClient
    )
    yield
    FakeOpenAI.instances = []
    FakeXaiClient.instances = []
