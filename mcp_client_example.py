"""Example JSON-RPC client for the local /mcp endpoint.

Start the server first (uvicorn mcp_server:app --port 8000), then run:

    python mcp_client_example.py

Importing this module makes no network call; only main() does.
"""

import httpx

MCP_URL = "http://localhost:8000/mcp"


def build_request(natural_query: str, request_id: int = 1) -> dict:
    """JSON-RPC 2.0 body for a natural_language_query call."""
    return {
        "jsonrpc": "2.0",
        "method": "natural_language_query",
        "params": {"natural_query": natural_query},
        "id": request_id,
    }


def main(url: str = MCP_URL) -> None:
    response = httpx.post(url, json=build_request("Top 5 customers"))
    print(response.json())


if __name__ == "__main__":
    main()
