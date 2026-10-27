import logging
import os
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import load_cors_origins, load_secrets_file, secrets_section
from enhanced_assistant import EnhancedQueryAssistant, is_truncated

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp-server")

app = FastAPI(
    title="AI Database Query Assistant JSON-RPC API",
    description=(
        "FastAPI JSON-RPC endpoint (`/mcp`) for natural-language SQL queries. "
        "Not a full MCP implementation."
    ),
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    # Env ADQ_CORS_ORIGINS, then [api] cors_origins in .streamlit/secrets.toml,
    # then localhost only. No cookies or auth headers are accepted cross-origin.
    allow_origins=load_cors_origins(secrets_section(load_secrets_file(), "api")),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_assistant: EnhancedQueryAssistant | None = None


def get_assistant() -> EnhancedQueryAssistant:
    global _assistant
    if _assistant is None:
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise ValueError("OPENAI_API_KEY required")
        _assistant = EnhancedQueryAssistant("chinook.db", key)
    return _assistant


class MCPRequest(BaseModel):
    jsonrpc: str = "2.0"
    method: str
    params: dict[str, Any]
    id: Any = None


class MCPResponse(BaseModel):
    jsonrpc: str = "2.0"
    result: Any = None
    error: dict[str, Any] = None
    id: Any = None


def build_query_result(df, similar: list[dict[str, Any]]) -> dict[str, Any]:
    """JSON-RPC result body for one query.

    truncated is True at the row cap; sql_query is the SQL that was executed.
    """
    return {
        "results": df.to_dict(orient="records"),
        "row_count": len(df),
        "truncated": is_truncated(df),
        "similar_queries": similar,
        "sql_query": df.attrs.get("sql_query"),
        "status": "success",
    }


@app.post("/mcp")
async def mcp_endpoint(request: MCPRequest):
    """JSON-RPC endpoint; not a full MCP implementation."""
    if request.method != "natural_language_query":
        raise HTTPException(400, "Only natural_language_query method supported")

    natural_query = request.params.get("natural_query")
    if not natural_query:
        raise HTTPException(400, "natural_query parameter required")

    try:
        assistant = get_assistant()
        # execute_query is synchronous (it runs its own asyncio.run), so it runs
        # in a worker thread, not inside this event loop.
        df, similar = await run_in_threadpool(assistant.execute_query, natural_query)
        result = build_query_result(df, similar)

        logger.info(f"MCP query processed: {natural_query[:50]}...")
        return MCPResponse(result=result, id=request.id)

    except Exception as e:
        logger.error(f"MCP error: {e}")
        return MCPResponse(error={"code": -32000, "message": str(e)}, id=request.id)


@app.get("/health")
async def health():
    return {"status": "healthy"}


if __name__ == "__main__":
    print("🚀 Starting JSON-RPC API")
    print("→ Local: http://localhost:8000/mcp")
    uvicorn.run(app, host="0.0.0.0", port=8000)
