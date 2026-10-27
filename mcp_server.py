import logging
import os
import uuid
from contextlib import asynccontextmanager
from functools import partial
from typing import Any

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from audit_log import AuditConfig, load_audit_config, parse_session_id
from config import load_cors_origins, load_secrets_file, secrets_section
from enhanced_assistant import EnhancedQueryAssistant, is_truncated

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp-server")

DB_PATH = "chinook.db"
_audit_config: AuditConfig | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Check the audit configuration before serving; refuse to start if the
    key is missing for a non-chinook DB, too short, or raw-text mode is on
    for a non-chinook DB (audit_log.AuditConfigError)."""
    global _audit_config
    _audit_config = load_audit_config(
        DB_PATH, secrets_section(load_secrets_file(), "audit")
    )
    yield


app = FastAPI(
    lifespan=lifespan,
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
        _assistant = EnhancedQueryAssistant(DB_PATH, key, audit_config=_audit_config)
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


def build_query_result(
    df, similar: list[dict[str, Any]], request_id: str | None = None
) -> dict[str, Any]:
    """JSON-RPC result body for one query.

    truncated is True at the row cap; sql_query is the SQL that was executed;
    request_id matches the query's audit row.
    """
    return {
        "request_id": request_id or df.attrs.get("request_id"),
        "results": df.to_dict(orient="records"),
        "row_count": len(df),
        "truncated": is_truncated(df),
        "similar_queries": similar,
        "sql_query": df.attrs.get("sql_query"),
        "status": "success",
    }


@app.post("/mcp")
async def mcp_endpoint(
    request: MCPRequest, x_session_id: str | None = Header(default=None)
):
    """JSON-RPC endpoint; not a full MCP implementation.

    Each query gets a uuid4 request_id, returned in the result (or in
    error.data). An X-Session-ID header is logged as a label only if it is a
    valid UUID; otherwise it is ignored.
    """
    if request.method != "natural_language_query":
        raise HTTPException(400, "Only natural_language_query method supported")

    natural_query = request.params.get("natural_query")
    if not natural_query:
        raise HTTPException(400, "natural_query parameter required")

    request_id = str(uuid.uuid4())
    session_id = parse_session_id(x_session_id)
    try:
        assistant = get_assistant()
        # execute_query is synchronous (it runs its own asyncio.run), so it runs
        # in a worker thread, not inside this event loop.
        df, similar = await run_in_threadpool(
            partial(
                assistant.execute_query,
                natural_query,
                session_id=session_id,
                request_id=request_id,
            )
        )
        result = build_query_result(df, similar, request_id)

        # The question text is not logged here; the audit row has its keyed hash.
        logger.info("query processed request_id=%s", request_id)
        return MCPResponse(result=result, id=request.id)

    except Exception as e:
        # Never the message text: it can quote the caller's SQL literals.
        if _assistant is not None:
            detail = _assistant.audit.describe_error(e)
        else:
            detail = f"error_class={type(e).__name__}"
        logger.error("query failed request_id=%s %s", request_id, detail)
        return MCPResponse(
            error={
                "code": -32000,
                "message": str(e),
                "data": {"request_id": request_id},
            },
            id=request.id,
        )


@app.get("/health")
async def health():
    return {"status": "healthy"}


if __name__ == "__main__":
    print("🚀 Starting JSON-RPC API")
    print("→ Local: http://localhost:8000/mcp")
    uvicorn.run(app, host="0.0.0.0", port=8000)
