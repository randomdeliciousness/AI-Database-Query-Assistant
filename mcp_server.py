import logging
import os
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from enhanced_assistant import EnhancedQueryAssistant, is_truncated

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp-server")

app = FastAPI(
    title="AI Database Query Assistant MCP Server",
    description=(
        "MCP-compliant server for natural-language SQL queries. "
        "Proxy this endpoint through Runlayer for enterprise security, "
        "ABAC permissions, threat detection, audit logs, and observability."
    ),
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production
    allow_credentials=True,
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
    """JSON-RPC result body for one query. truncated is True at the row cap."""
    return {
        "results": df.to_dict(orient="records"),
        "row_count": len(df),
        "truncated": is_truncated(df),
        "similar_queries": similar,
        "sql_query": "Generated internally (visible in Runlayer audit logs)",
        "status": "success",
        "mcp_version": "2.0",
    }


@app.post("/mcp")
async def mcp_endpoint(request: MCPRequest):
    """MCP-compliant JSON-RPC endpoint (Runlayer proxy target)."""
    if request.method != "natural_language_query":
        raise HTTPException(400, "Only natural_language_query method supported")

    natural_query = request.params.get("natural_query")
    if not natural_query:
        raise HTTPException(400, "natural_query parameter required")

    try:
        assistant = get_assistant()
        df, similar = assistant.execute_query(natural_query)
        result = build_query_result(df, similar)

        logger.info(f"MCP query processed: {natural_query[:50]}...")
        return MCPResponse(result=result, id=request.id)

    except Exception as e:
        logger.error(f"MCP error: {e}")
        return MCPResponse(error={"code": -32000, "message": str(e)}, id=request.id)


@app.get("/health")
async def health():
    return {"status": "healthy", "mcp_ready": True, "runlayer_compatible": True}


if __name__ == "__main__":
    print("🚀 Starting MCP Server (Runlayer-ready)")
    print("→ Local: http://localhost:8000/mcp")
    print(
        "→ Runlayer: Add this URL to your Private Catalog → instant enterprise security"
    )
    uvicorn.run(app, host="0.0.0.0", port=8000)
