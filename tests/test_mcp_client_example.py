"""mcp_client_example.py must not open a connection when imported."""

import importlib
import socket
import sys

import httpx


def test_import_makes_no_network_call(monkeypatch):
    calls = []

    def _deny(*args, **kwargs):
        calls.append(args)
        raise AssertionError("network call on import")

    monkeypatch.setattr(socket.socket, "connect", _deny)
    monkeypatch.setattr(socket, "create_connection", _deny)
    monkeypatch.setattr(httpx, "post", _deny)
    monkeypatch.delitem(sys.modules, "mcp_client_example", raising=False)

    module = importlib.import_module("mcp_client_example")

    assert calls == []
    assert callable(module.main)


def test_main_posts_the_example_request(monkeypatch, capsys):
    sent = {}

    class _Response:
        def json(self):
            return {"jsonrpc": "2.0", "result": "ok", "id": 1}

    def _fake_post(url, json):
        sent.update(url=url, json=json)
        return _Response()

    import mcp_client_example

    monkeypatch.setattr(mcp_client_example.httpx, "post", _fake_post)
    mcp_client_example.main()

    assert sent["url"] == "http://localhost:8000/mcp"
    assert sent["json"]["method"] == "natural_language_query"
    assert sent["json"]["params"] == {"natural_query": "Top 5 customers"}
    assert "ok" in capsys.readouterr().out
