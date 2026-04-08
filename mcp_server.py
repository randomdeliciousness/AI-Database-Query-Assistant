import asyncio
import os
from typing import Dict, Any
import pandas as pd

from mcp.server.fastmcp import FastMCP
from enhanced_assistant import EnhancedQueryAssistant

'''
Runlayer (or any MCP control plane) can proxy/route this 
server with zero code changes — just give Runlayer the endpoint URL. 
It automatically adds permissions, threat detection, observability, PII masking, audit logs, etc.
'''

# Initialize MCP server (Runlayer will proxy this endpoint)
mcp = FastMCP(
    "AI-Database-Query-Assistant",
    json_response=True,
    description="Natural language to SQL query assistant with vector history and visualizations. Secured via Runlayer MCP control plane."
)

# Lazy-load assistant (shares DB and ChromaDB across tool calls)
_assistant: EnhancedQueryAssistant | None = None

def _get_assistant() -> EnhancedQueryAssistant:
    global _assistant
    if _assistant is None:
        openai_key = os.getenv("OPENAI_API_KEY")
        if not openai_key:
            raise ValueError("OPENAI_API_KEY environment variable required")
        _assistant = EnhancedQueryAssistant("chinook.db", openai_key)
    return _assistant

@mcp.tool()
async def natural_language_query(natural_query: str) -> Dict[str, Any]:
    """Execute a natural language query against the music store database.
    
    Returns structured results + similar historical queries.
    Perfect for agentic workflows routed through Runlayer MCP control plane.
    """
    assistant = _get_assistant()
    
    df: pd.DataFrame
    similar_queries: list[dict]
    df, similar_queries = assistant.execute_query(natural_query)
    
    return {
        "results": df.to_dict(orient="records"),
        "row_count": len(df),
        "similar_queries": similar_queries,
        "sql_query": "Generated internally (visible in Runlayer audit logs)",
        "status": "success"
    }

if __name__ == "__main__":
    # Run locally for testing (stdio or HTTP)
    # For production/Runlayer: use transport="streamable-http" and expose port
    print("🚀 Starting AI Database Query MCP Server...")
    print("→ Connect via Runlayer or any MCP client (Claude Desktop, Cursor, etc.)")
    print("→ Example endpoint (when using streamable-http): http://localhost:8000/mcp")
    mcp.run(transport="streamable-http")  # Change to "stdio" for direct Claude/Cursor integration
