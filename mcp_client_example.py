import httpx
response = httpx.post("http://localhost:8000/mcp", json={
    "jsonrpc": "2.0",
    "method": "natural_language_query",
    "params": {"natural_query": "Top 5 customers"},
    "id": 1
})
print(response.json())
