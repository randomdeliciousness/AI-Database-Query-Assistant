import datetime
import logging
import time
from typing import Any

import chromadb
import pandas as pd
from openai import OpenAI
from pydantic import BaseModel

from config import ModelConfig, load_model_config
from sql_guard import (
    GuardConfig,
    check_sql,
    load_guard_config,
    open_readonly_plain,
    run_readonly_query,
)

logger = logging.getLogger(__name__)


class SQLResponse(BaseModel):
    sql_query: str


class DataAccessLayer:
    """Abstract data-access layer (only direct DB access exists)."""

    async def execute(
        self, natural_query: str
    ) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
        raise NotImplementedError


class DirectDBLayer(DataAccessLayer):
    def __init__(self, assistant):
        self.assistant = assistant

    async def execute(
        self, natural_query: str
    ) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
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
        "Show the most common media types by sales volume",
    ]

    def __init__(
        self,
        db_path: str,
        openai_api_key: str,
        guard_config: GuardConfig | None = None,
        model_config: ModelConfig | None = None,
    ):
        self.db_path = db_path
        self.guard_config = guard_config or load_guard_config()
        self.model_config = model_config or load_model_config()
        logger.info(
            "model provider=%s id=%s",
            self.model_config.provider,
            self.model_config.model_id,
        )
        self.client = OpenAI(api_key=openai_api_key)
        self.chroma_client = chromadb.PersistentClient(path="./chroma_db")

        try:
            self.collection = self.chroma_client.get_collection(name="query_history")
        except Exception:  # fixed: no bare except
            self.collection = self.chroma_client.create_collection(name="query_history")

        self.schema = self._get_db_schema()
        self.data_layer: DataAccessLayer = DirectDBLayer(self)

    def _execute_direct(
        self, natural_query: str
    ) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
        start = time.time()
        similar_queries = self._find_similar_queries(natural_query)
        # check_sql is idempotent; it returns the exact statement that runs.
        sql_query = check_sql(self._generate_sql(natural_query))

        columns, rows, truncated = run_readonly_query(
            self.db_path, sql_query, self.guard_config
        )
        result_df = pd.DataFrame.from_records(rows, columns=columns)
        result_df.attrs["truncated"] = truncated
        result_df.attrs["row_cap"] = self.guard_config.row_cap
        result_df.attrs["sql_query"] = sql_query

        latency_ms = round((time.time() - start) * 1000, 2)
        metadata = {
            "row_count": len(result_df),
            "execution_time": datetime.datetime.now().isoformat(),
            "latency_ms": latency_ms,
            "truncated": truncated,
            "model_provider": self.model_config.provider,
            "model_id": self.model_config.model_id,
        }
        self._store_query(natural_query, sql_query, metadata)
        return result_df, similar_queries

    def _get_db_schema(self) -> str:
        conn = open_readonly_plain(self.db_path)
        cursor = conn.cursor()
        schema_info = []
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = cursor.fetchall()
        for table in tables:
            table_name = table[0]
            quoted = '"' + table_name.replace('"', '""') + '"'
            cursor.execute(f"PRAGMA table_info({quoted});")
            columns = cursor.fetchall()
            column_info = [f"{col[1]} ({col[2]})" for col in columns]
            schema_info.append(f"Table {table_name}:")
            schema_info.extend([f"  - {col}" for col in column_info])
        conn.close()
        return "\n".join(schema_info)

    def _store_query(
        self, natural_query: str, sql_query: str, metadata: dict[str, Any]
    ):
        timestamp = datetime.datetime.now().isoformat()
        self.collection.add(
            documents=[natural_query],
            metadatas=[{"sql_query": sql_query, "timestamp": timestamp, **metadata}],
            ids=[f"query_{timestamp}"],
        )

    def _find_similar_queries(
        self, query: str, n_results: int = 3
    ) -> list[dict[str, Any]]:
        results = self.collection.query(query_texts=[query], n_results=n_results)
        if not results["documents"][0]:
            return []
        return [
            {
                "natural_query": doc,
                "sql_query": meta["sql_query"],
                "timestamp": meta["timestamp"],
                "latency_ms": meta.get("latency_ms"),
            }
            for doc, meta in zip(
                results["documents"][0], results["metadatas"][0], strict=False
            )
        ]

    def _generate_sql(self, natural_query: str) -> str:
        """Generate SQL with the configured model using structured output."""
        prompt = f"""Convert the following natural language query to SQL.
Database Schema:
{self.schema}

Natural language query: {natural_query}

Respond with ONLY valid SQL. No explanations."""

        response = self.client.beta.chat.completions.parse(
            model=self.model_config.model_id,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a SQL expert. "
                        "Convert natural language to precise SQL."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            response_format=SQLResponse,
        )
        # Q12: keep the alias as the default, but log the model the API ran.
        logger.info("response model=%s", getattr(response, "model", None))
        return response.choices[0].message.parsed.sql_query.strip()

    def execute_query(
        self, natural_query: str
    ) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
        """Run a natural-language query.

        Returns (results, similar_queries). results.attrs["truncated"] is True
        when the result hit the row cap (results.attrs["row_cap"]) and later
        rows were dropped; see is_truncated(). results.attrs["sql_query"] is
        the SQL that was executed.
        """
        if self.data_layer is not None:
            import asyncio

            return asyncio.run(self.data_layer.execute(natural_query))
        return self._execute_direct(natural_query)


def is_truncated(df: pd.DataFrame) -> bool:
    """True if this result was cut at the guard's row cap."""
    return bool(df.attrs.get("truncated", False))
