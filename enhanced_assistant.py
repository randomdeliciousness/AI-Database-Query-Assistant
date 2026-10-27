import datetime
import logging
import time
import uuid
from pathlib import Path
from typing import Any

import chromadb
import pandas as pd
from openai import OpenAI
from pydantic import BaseModel

from audit_log import AuditConfig, AuditLogger, load_audit_config, schema_identifiers
from config import ModelConfig, load_model_config
from sql_guard import (
    GuardConfig,
    GuardError,
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
        self, natural_query: str, **audit_labels
    ) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
        raise NotImplementedError


class DirectDBLayer(DataAccessLayer):
    def __init__(self, assistant):
        self.assistant = assistant

    async def execute(
        self, natural_query: str, **audit_labels
    ) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
        return self.assistant._execute_direct(natural_query, **audit_labels)


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
        audit_config: AuditConfig | None = None,
    ):
        self.db_path = db_path
        self.guard_config = guard_config or load_guard_config()
        self.model_config = model_config or load_model_config()
        # Fails closed (AuditConfigError) before any client is built; see
        # audit_log.load_audit_config. There is no way to turn auditing off.
        audit_config = audit_config or load_audit_config(db_path)
        identity = audit_config.db_identity
        if identity is not None and identity.path != Path(db_path).resolve():
            raise ValueError("audit_config was loaded for a different database")
        self.audit = AuditLogger(audit_config, schema_identifiers(db_path))
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
        self,
        natural_query: str,
        *,
        session_id: str | None = None,
        request_id: str | None = None,
    ) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
        start = time.time()
        request_id = request_id or str(uuid.uuid4())
        audit = {
            "request_id": request_id,
            "session_id": session_id,
            "question": natural_query,
            "sql": None,
            "steps": 0,
            "model_provider": self.model_config.provider,
            "model_id": self.model_config.model_id,
            "response_model": None,
        }
        stats: dict[str, int] = {}
        # Per-connection identity check before anything runs (no model call,
        # no SQL): fails closed in outcome-only / raw-text mode if the file is
        # no longer chinook.db. Chroma history is read and written only for
        # chinook.db.
        try:
            is_chinook = self.audit.require_db_allowed()
        except Exception as e:
            self.audit.record_refusal(
                e,
                request_id=request_id,
                session_id=session_id,
                model_provider=self.model_config.provider,
                model_id=self.model_config.model_id,
            )
            raise
        try:
            similar_queries = (
                self._find_similar_queries(natural_query) if is_chinook else []
            )
            raw_sql, audit["response_model"] = self._generate(natural_query)
            audit["sql"] = raw_sql
            # check_sql is idempotent; it returns the exact statement that runs.
            sql_query = check_sql(raw_sql)
            audit["sql"] = sql_query
            columns, rows, truncated = run_readonly_query(
                self.db_path, sql_query, self.guard_config, stats=stats
            )
        except GuardError as e:
            audit["steps"] = getattr(e, "steps", None) or stats.get("steps", 0)
            self.audit.record(
                outcome="blocked", block_reason=e.reason, error=e, **audit
            )
            raise
        except Exception as e:
            audit["steps"] = stats.get("steps", 0)
            self.audit.record(outcome="error", error=e, **audit)
            raise
        audit["steps"] = stats.get("steps", 0)
        # Only counts and flags are audited, never result rows or cell values.
        self.audit.record(
            outcome="truncated" if truncated else "answered",
            row_count=len(rows),
            truncated=truncated,
            **audit,
        )

        result_df = pd.DataFrame.from_records(rows, columns=columns)
        result_df.attrs["truncated"] = truncated
        result_df.attrs["row_cap"] = self.guard_config.row_cap
        result_df.attrs["sql_query"] = sql_query
        result_df.attrs["request_id"] = request_id

        latency_ms = round((time.time() - start) * 1000, 2)
        metadata = {
            "row_count": len(result_df),
            "execution_time": datetime.datetime.now().isoformat(),
            "latency_ms": latency_ms,
            "truncated": truncated,
            "model_provider": self.model_config.provider,
            "model_id": self.model_config.model_id,
        }
        # Chroma keeps plain question/SQL history, so only for the bundled
        # sample DB (identified by sha256 at startup).
        if is_chinook:
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
        return self._generate(natural_query)[0]

    def _generate(self, natural_query: str) -> tuple[str, str | None]:
        """(SQL, model name the API reported) for one question."""
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
        response_model = getattr(response, "model", None)
        logger.info("response model=%s", response_model)
        return response.choices[0].message.parsed.sql_query.strip(), response_model

    def execute_query(
        self,
        natural_query: str,
        *,
        session_id: str | None = None,
        request_id: str | None = None,
    ) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
        """Run a natural-language query.

        Returns (results, similar_queries). results.attrs["truncated"] is True
        when the result hit the row cap (results.attrs["row_cap"]) and later
        rows were dropped; see is_truncated(). results.attrs["sql_query"] is
        the SQL that was executed and results.attrs["request_id"] the audit
        row's request_id. Every call writes exactly one audit row, also when
        the query is blocked or fails.
        """
        labels = {"session_id": session_id, "request_id": request_id}
        if self.data_layer is not None:
            import asyncio

            return asyncio.run(self.data_layer.execute(natural_query, **labels))
        return self._execute_direct(natural_query, **labels)


def is_truncated(df: pd.DataFrame) -> bool:
    """True if this result was cut at the guard's row cap."""
    return bool(df.attrs.get("truncated", False))
