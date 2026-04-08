from openai import OpenAI
import sqlite3
import pandas as pd
import chromadb
import datetime
import time
from typing import Dict, List, Tuple, Any
from pydantic import BaseModel

class SQLResponse(BaseModel):
    sql_query: str

class DataAccessLayer:
    """Abstract base for swapping direct DB vs MCP-routed access."""
    async def execute(self, natural_query: str) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
        raise NotImplementedError

class DirectDBLayer(DataAccessLayer):
    def __init__(self, assistant):
        self.assistant = assistant

    async def execute(self, natural_query: str) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
        return self.assistant._execute_direct(natural_query)

class EnhancedQueryAssistant:
    SAMPLE_QUERIES = [
        "Show me the top 5 customers by total purchase amount",
        "Which genres generated the most revenue?",
        "List the most popular artists by number of tracks sold",
        "Calculate the average invoice amount by country",
        "Show sales trends by month for the year 2009",
        "Find employees who exceeded the average sales amount",
        "What's the distribution of track lengths by genre?",
        "Which customers bought classical music in 2009?",
        "Rank artists by average track price",
        "Show the most common media types by sales volume"
    ]

    def __init__(self, db_path: str, openai_api_key: str, use_mcp: bool = False):
        self.db_path = db_path
        self.client = OpenAI(api_key=openai_api_key)
        self.chroma_client = chromadb.PersistentClient(path="./chroma_db")
        
        try:
            self.collection = self.chroma_client.get_collection(name="query_history")
        except Exception:  # fixed: no bare except
            self.collection = self.chroma_client.create_collection(name="query_history")
        
        self.schema = self._get_db_schema()
        self.data_layer: DataAccessLayer = DirectDBLayer(self) if not use_mcp else None

    def _execute_direct(self, natural_query: str) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
        start = time.time()
        similar_queries = self._find_similar_queries(natural_query)
        sql_query = self._generate_sql(natural_query)
        
        conn = sqlite3.connect(self.db_path)
        result_df = pd.read_sql_query(sql_query, conn)
        conn.close()
        
        latency_ms = round((time.time() - start) * 1000, 2)
        metadata = {
            "row_count": len(result_df),
            "execution_time": datetime.datetime.now().isoformat(),
            "latency_ms": latency_ms
        }
        self._store_query(natural_query, sql_query, metadata)
        return result_df, similar_queries

    def _get_db_schema(self) -> str:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        schema_info = []
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = cursor.fetchall()
        for table in tables:
            table_name = table[0]
            cursor.execute(f"PRAGMA table_info({table_name});")
            columns = cursor.fetchall()
            column_info = [f"{col[1]} ({col[2]})" for col in columns]
            schema_info.append(f"Table {table_name}:")
            schema_info.extend([f"  - {col}" for col in column_info])
        conn.close()
        return "\n".join(schema_info)

    def _store_query(self, natural_query: str, sql_query: str, metadata: Dict[str, Any]):
        timestamp = datetime.datetime.now().isoformat()
        self.collection.add(
            documents=[natural_query],
            metadatas=[{"sql_query": sql_query, "timestamp": timestamp, **metadata}],
            ids=[f"query_{timestamp}"]
        )

    def _find_similar_queries(self, query: str, n_results: int = 3) -> List[Dict[str, Any]]:
        results = self.collection.query(query_texts=[query], n_results=n_results)
        if not results["documents"][0]:
            return []
        return [
            {
                "natural_query": doc,
                "sql_query": meta["sql_query"],
                "timestamp": meta["timestamp"],
                "latency_ms": meta.get("latency_ms")
            }
            for doc, meta in zip(results["documents"][0], results["metadatas"][0])
        ]

    def _generate_sql(self, natural_query: str) -> str:
        """2026 upgrade: gpt-4o + structured output."""
        prompt = f"""Convert the following natural language query to SQL.
Database Schema:
{self.schema}

Natural language query: {natural_query}

Respond with ONLY valid SQL. No explanations."""

        response = self.client.beta.chat.completions.parse(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are a SQL expert. Convert natural language to precise SQL."},
                {"role": "user", "content": prompt}
            ],
            response_format=SQLResponse
        )
        return response.choices[0].message.parsed.sql_query.strip()

    def execute_query(self, natural_query: str) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
        if self.data_layer is not None:
            import asyncio
            return asyncio.run(self.data_layer.execute(natural_query))
        return self._execute_direct(natural_query)
