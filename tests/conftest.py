"""Shared test fixtures.

Every test runs offline: OPENAI_API_KEY is removed, Chroma telemetry is off,
outbound sockets raise, and enhanced_assistant's OpenAI client and Chroma
PersistentClient are replaced with in-memory fakes (no model call, no ONNX
embedding download, no ./chroma_db written).
"""

import os
import socket
import sys
from types import SimpleNamespace
from typing import ClassVar

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import enhanced_assistant

_LOCAL_HOSTS = (None, "localhost", "127.0.0.1", "::1")

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
def _offline(monkeypatch):
    """No key, no telemetry, no outbound sockets.

    A test that needs the network fails.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "False")

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

    def __init__(self):
        self.sql = "SELECT 1"
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        parsed = SimpleNamespace(sql_query=self.sql)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))]
        )


class FakeOpenAI:
    """Replaces openai.OpenAI inside enhanced_assistant. Makes no network call."""

    instances: ClassVar[list] = []

    def __init__(self, api_key=None, **kwargs):
        self.api_key = api_key
        self.completions = FakeCompletions()
        self.beta = SimpleNamespace(chat=SimpleNamespace(completions=self.completions))
        FakeOpenAI.instances.append(self)


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
    """OpenAI stays mocked in every test; Chroma is an in-memory fake."""
    FakeOpenAI.instances = []
    monkeypatch.setattr(enhanced_assistant, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(
        enhanced_assistant.chromadb, "PersistentClient", FakeChromaClient
    )
    yield
    FakeOpenAI.instances = []
