import datetime
import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

import chromadb
import pandas as pd
from chromadb.config import Settings
from openai import OpenAI
from pydantic import BaseModel
from xai_sdk import Client as XaiClient
from xai_sdk.chat import system as xai_system
from xai_sdk.chat import user as xai_user

from audit_log import AuditConfig, AuditLogger, load_audit_config, schema_identifiers
from config import (
    CALL_PROMPT_TOKEN_CAP,
    DEFAULT_MAX_TOKENS,
    REQUEST_TIMEOUT_S,
    ModelConfig,
    load_chroma_path,
    load_ledger_path,
    load_model_config,
    load_schema_mode,
)
from retrieval import (
    PairLimitExceeded,
    RetryContext,
    SchemaIndex,
    full_schema_user_prompt,
    iter_table_documents,
    load_embedding_function,
    retrieval_user_prompt,
    sqlite_error_text,
)
from spend_meter import SpendMeter, SpendRefused
from sql_guard import (
    GuardConfig,
    GuardError,
    check_sql,
    load_guard_config,
    run_readonly_query,
)

logger = logging.getLogger(__name__)

# Replaces the SDK default (retries on, maxAttempts 5, 1620s timeout).
_NO_RETRY_SERVICE_CONFIG = json.dumps({"methodConfig": [{"name": [{}]}]})

SQL_SYSTEM_PROMPT = "You are a SQL expert. Convert natural language to precise SQL."


class SQLResponse(BaseModel):
    sql_query: str


def sql_user_prompt(schema: str, natural_query: str) -> str:
    """The user message sent to the model. Counted once by the spend meter."""
    return f"""Convert the following natural language query to SQL.
Database Schema:
{schema}

Natural language query: {natural_query}

Respond with ONLY valid SQL. No explanations."""


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
        api_key: str,
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
        self.client = self._build_client(api_key)
        self.schema_mode = load_schema_mode()
        self._question_id: str | None = None
        self.chroma_client = chromadb.PersistentClient(
            path=load_chroma_path(),
            settings=Settings(anonymized_telemetry=False),
        )
        # The test fake has no ONNX file. Production loads the pinned model.
        embedding = None
        if not hasattr(self.chroma_client, "is_fake"):
            embedding = load_embedding_function()
        self.index = SchemaIndex(db_path, self.chroma_client, embedding)
        self._sync_index()
        self.schema = self._get_db_schema()
        self.data_layer: DataAccessLayer = DirectDBLayer(self)

    def _build_client(self, api_key: str):
        """One client for the configured provider. The key is not logged."""
        provider = self.model_config.provider
        if provider == "xai":
            return XaiClient(
                api_key=api_key,
                timeout=REQUEST_TIMEOUT_S,
                channel_options=[
                    ("grpc.enable_retries", 0),
                    ("grpc.service_config", _NO_RETRY_SERVICE_CONFIG),
                ],
            )
        if provider == "xai-openai-compat":
            return OpenAI(
                api_key=api_key,
                base_url="https://api.x.ai/v1",
                max_retries=0,
                timeout=REQUEST_TIMEOUT_S,
            )
        raise ValueError(
            f"model provider {provider!r}: provider not supported in this version"
        )

    def _execute_direct(
        self,
        natural_query: str,
        *,
        session_id: str | None = None,
        request_id: str | None = None,
        question_id: str | None = None,
    ) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
        start = time.time()
        request_id = request_id or str(uuid.uuid4())
        self._question_id = question_id
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
        # no longer chinook.db. User-pair Chroma history is read and written
        # only while this check says the file is chinook.
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
            self._sync_index()
            similar_queries = self._find_similar_queries(natural_query)
            retry = None
            for attempt in (1, 2):
                try:
                    raw_sql, audit["response_model"] = self._generate(
                        natural_query, retry=retry
                    )
                    audit["sql"] = raw_sql
                    # check_sql is idempotent; it returns the statement that runs.
                    sql_query = check_sql(raw_sql)
                    audit["sql"] = sql_query
                    columns, rows, truncated = run_readonly_query(
                        self.db_path, sql_query, self.guard_config, stats=stats
                    )
                    break
                except GuardError:
                    # A guard block is never fed back as a retry.
                    raise
                except sqlite3.Error as error:
                    if attempt == 2:
                        raise
                    # The failed attempt is not logged and not stored.
                    retry = RetryContext(sql_query, sqlite_error_text(error))
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
        # Plain question/SQL history only while the per-query check says chinook.
        # An over-limit pair is not saved. The result is already computed, so
        # the limit message is a notice on the frame rather than a failure.
        if is_chinook:
            try:
                self._store_query(natural_query, sql_query, metadata)
            except PairLimitExceeded as exc:
                notice = str(exc)
                result_df.attrs["notice"] = notice
                logger.warning("%s", notice)
        return result_df, similar_queries

    def _sync_index(self) -> None:
        """Open the collections for the current DDL hash.

        ``collection`` stays the user-pair collection. The chinook flag is
        the audit identity check, so this does not hash the file again.
        """
        self.index.sync(is_chinook=self.audit.db_is_chinook())
        self.collection = self.index.users

    def _get_db_schema(self) -> str:
        """Table and view documents ordered by name, joined with newlines."""
        documents = iter_table_documents(self.db_path)
        return "\n".join(document for _name, document in documents)

    def _store_query(
        self, natural_query: str, sql_query: str, metadata: dict[str, Any]
    ):
        # Called only after a successful chinook run. The failed attempt is
        # never stored. question_id is the optional label from execute_query.
        self.index.add_user_pair(
            natural_query,
            sql_query,
            metadata,
            allow_raw_write=True,
            question_id=self._question_id,
        )

    def _find_similar_queries(
        self, query: str, n_results: int = 3
    ) -> list[dict[str, Any]]:
        # n_results stays for callers. The operating k_pairs is the tuned one.
        del n_results
        include_user = bool(self.audit.db_is_chinook())
        _tables, pairs = self.index.retrieve(query, include_user=include_user)
        return [
            {
                "natural_query": pair.question,
                "sql_query": pair.sql,
                "timestamp": pair.timestamp,
                "latency_ms": pair.latency_ms,
            }
            for pair in pairs
        ]

    def render_prompt(
        self, natural_query: str, *, retry: RetryContext | None = None
    ) -> str:
        """User message for this schema mode. Pair SQL is text only."""
        if self.schema_mode == "full":
            return full_schema_user_prompt(self.schema, natural_query, retry)
        include_user = bool(self.audit.db_is_chinook())
        tables, pairs = self.index.retrieve(natural_query, include_user=include_user)
        return retrieval_user_prompt(
            [table.text for table in tables],
            [(pair.question, pair.sql) for pair in pairs],
            natural_query,
            retry,
        )

    def _generate_sql(self, natural_query: str) -> str:
        """Generate SQL with the configured model using structured output."""
        return self._generate(natural_query)[0]

    def _generate(
        self, natural_query: str, *, retry: RetryContext | None = None
    ) -> tuple[str, str | None]:
        """(SQL, model name the API reported) for one attempt.

        Each attempt builds its own meter. A prompt over the per-call cap
        raises SpendRefused before reserve, so nothing is reserved and
        ``_complete`` does not run. The reservation is settled in
        ``finally`` before this returns, so a later SQL-guard error cannot
        leave it open. SpendRefused from reserve is not settled.
        """
        system_text = SQL_SYSTEM_PROMPT
        prompt = self.render_prompt(natural_query, retry=retry)
        meter = SpendMeter(load_ledger_path())
        prompt_tokens = meter.estimate_prompt_tokens(
            system_text
        ) + meter.estimate_prompt_tokens(prompt)
        if prompt_tokens > CALL_PROMPT_TOKEN_CAP:
            raise SpendRefused(
                f"estimated prompt tokens {prompt_tokens} exceed the "
                f"{CALL_PROMPT_TOKEN_CAP} token cap"
            )
        reservation_id = meter.reserve(self.model_config.model_id, prompt_tokens)
        usage = None
        try:
            sql, response_model, usage = self._complete(system_text, prompt)
            return sql, response_model
        except Exception:
            usage = None
            raise
        finally:
            meter.settle(reservation_id, usage)

    def _complete(self, system_text: str, prompt: str):
        """One completion. Returns (sql, reported model, usage or None)."""
        model_id = self.model_config.model_id
        if self.model_config.provider == "xai":
            chat = self.client.chat.create(
                model=model_id,
                max_tokens=DEFAULT_MAX_TOKENS,
                messages=[xai_system(system_text), xai_user(prompt)],
            )
            response, parsed = chat.parse(SQLResponse)
            # Response does not proxy .model; the API name is proto.model.
            response_model = response.proto.model
            logger.info("response model=%s", response_model)
            usage = getattr(response, "usage", None)
            return parsed.sql_query.strip(), response_model, usage
        response = self.client.beta.chat.completions.parse(
            model=model_id,
            messages=[
                {"role": "system", "content": system_text},
                {"role": "user", "content": prompt},
            ],
            response_format=SQLResponse,
            max_tokens=DEFAULT_MAX_TOKENS,
        )
        response_model = getattr(response, "model", None)
        logger.info("response model=%s", response_model)
        sql = response.choices[0].message.parsed.sql_query.strip()
        return sql, response_model, getattr(response, "usage", None)

    def execute_query(
        self,
        natural_query: str,
        *,
        session_id: str | None = None,
        request_id: str | None = None,
        question_id: str | None = None,
    ) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
        """Run a natural-language query.

        Returns (results, similar_queries). results.attrs["truncated"] is True
        when the result hit the row cap (results.attrs["row_cap"]) and later
        rows were dropped; see is_truncated(). results.attrs["sql_query"] is
        the SQL that was executed and results.attrs["request_id"] the audit
        row's request_id. results.attrs["notice"] is set when a chinook
        question or its executed SQL is over the stored-pair character
        limit: the result is still returned and nothing is saved. Every call
        writes exactly one audit row, also when the query is blocked or
        fails. A SQLite execution error is retried once; a guard block is not.
        """
        labels = {
            "session_id": session_id,
            "request_id": request_id,
            "question_id": question_id,
        }
        if self.data_layer is not None:
            import asyncio

            return asyncio.run(self.data_layer.execute(natural_query, **labels))
        return self._execute_direct(natural_query, **labels)


def is_truncated(df: pd.DataFrame) -> bool:
    """True if this result was cut at the guard's row cap."""
    return bool(df.attrs.get("truncated", False))
