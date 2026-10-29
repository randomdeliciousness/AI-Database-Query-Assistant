"""Model, key, and spend configuration (ADQ-10).

Lookup order for every value: environment variable, then the matching
secrets mapping (for example a section of st.secrets), then the default.
This module never imports streamlit, so the API server and the tests work
without a secrets file.

Env:
    ADQ_MODEL_PROVIDER   model provider ("xai" or "xai-openai-compat")
    ADQ_MODEL_ID         model id passed to the API
    ADQ_SPEND_LEDGER_PATH  JSONL spend ledger (default spend/cost_ledger.jsonl)
    ADQ_DB_PATH          database file the app opens (default chinook.db)
    XAI_API_KEY          xAI key
Secrets (st.secrets layout):
    [model]   provider, model_id
    [xai]     api_key
    [database] path (file the app opens; empty uses chinook.db)
    [sql_guard] step_budget, row_cap, timeout_s (read by sql_guard)
    [api]     cors_origins (list of origins for the JSON-RPC API)
Env ADQ_CORS_ORIGINS: comma-separated origins for the JSON-RPC API.
"""

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

DEFAULT_PROVIDER = "xai"
DEFAULT_MODEL_ID = "grok-4.20-0309-non-reasoning"
SUPPORTED_PROVIDERS = ("xai", "xai-openai-compat")
DEFAULT_SECRETS_PATH = ".streamlit/secrets.toml"
# Browser origins allowed to call the JSON-RPC API: localhost only by default
# (the API on :8000 and the Streamlit app on :8501).
DEFAULT_CORS_ORIGINS = (
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://localhost:8501",
    "http://127.0.0.1:8501",
)

# Dated xAI list prices. The meter, not ModelConfig, refuses an id with no row.
PRICES_AS_OF = "2026-10-28"
PRICE_SOURCE = "https://docs.x.ai/docs/models"
MODEL_PRICES: dict[str, dict[str, Decimal]] = {
    "grok-4.20-0309-non-reasoning": {
        "prompt": Decimal("1.25"),
        "cached_prompt": Decimal("0.20"),
        "completion": Decimal("2.50"),
    }
}
BUDGET_USD = Decimal("9")
DEFAULT_MAX_TOKENS = 1024
REQUEST_TIMEOUT_S = 60
MAX_PROMPT_TOKENS = 200_000
CHARS_PER_TOKEN = 2
DEFAULT_LEDGER_PATH = "spend/cost_ledger.jsonl"
DEFAULT_DB_PATH = "chinook.db"


@dataclass(frozen=True)
class ModelConfig:
    provider: str = DEFAULT_PROVIDER
    model_id: str = DEFAULT_MODEL_ID

    def __post_init__(self):
        if self.provider not in SUPPORTED_PROVIDERS:
            raise ValueError(
                f"model provider {self.provider!r}: "
                "provider not supported in this version"
            )
        if not self.model_id or not self.model_id.strip():
            raise ValueError("model_id must be a non-empty string")


def secrets_section(secrets: Mapping | None, name: str) -> Mapping:
    """Return secrets[name] as a mapping, or {} if it is missing.

    A missing secrets file must not crash: any lookup error (including
    streamlit's StreamlitSecretNotFoundError, a FileNotFoundError) gives {}.
    """
    if secrets is None:
        return {}
    try:
        section: Any = secrets.get(name)
    except (FileNotFoundError, KeyError, AttributeError):
        return {}
    return section if isinstance(section, Mapping) else {}


def load_model_config(secrets: Mapping | None = None) -> ModelConfig:
    """Env ADQ_MODEL_PROVIDER / ADQ_MODEL_ID, then secrets, then defaults.

    ``secrets`` is the model section (e.g. st.secrets["model"]), with keys
    provider and model_id.
    """
    secrets = secrets or {}
    provider = (
        os.getenv("ADQ_MODEL_PROVIDER") or secrets.get("provider") or DEFAULT_PROVIDER
    )
    model_id = os.getenv("ADQ_MODEL_ID") or secrets.get("model_id") or DEFAULT_MODEL_ID
    return ModelConfig(provider=str(provider).strip().lower(), model_id=str(model_id))


def load_xai_api_key(secrets: Mapping | None = None) -> str | None:
    """Env XAI_API_KEY, then secrets (e.g. st.secrets["xai"]["api_key"]).

    Returns None if neither is set. The key is never logged.
    """
    secrets = secrets or {}
    return os.getenv("XAI_API_KEY") or secrets.get("api_key") or None


def load_ledger_path() -> str:
    """Env ADQ_SPEND_LEDGER_PATH, then spend/cost_ledger.jsonl."""
    return os.getenv("ADQ_SPEND_LEDGER_PATH") or DEFAULT_LEDGER_PATH


def load_db_path(secrets: Mapping | None = None) -> str:
    """Env ADQ_DB_PATH, then secrets['path'], then chinook.db.

    Does not open the file. A blank string at either layer falls through.
    """
    secrets = secrets or {}
    env = os.getenv("ADQ_DB_PATH")
    if env is not None and str(env).strip():
        return str(env)
    secret = secrets.get("path")
    if secret is not None and str(secret).strip():
        return str(secret)
    return DEFAULT_DB_PATH


def load_secrets_file(path: str | os.PathLike = DEFAULT_SECRETS_PATH) -> dict:
    """Parse a secrets.toml for processes without st.secrets (the API server).

    A missing file gives {}. The values are never logged.
    """
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


def load_cors_origins(secrets: Mapping | None = None) -> list[str]:
    """Env ADQ_CORS_ORIGINS (comma-separated), then secrets, then localhost.

    ``secrets`` is the api section (e.g. secrets["api"]) with key
    cors_origins, a list of origins or a comma-separated string.
    """
    secrets = secrets or {}
    raw = os.getenv("ADQ_CORS_ORIGINS") or secrets.get("cors_origins")
    if not raw:
        return list(DEFAULT_CORS_ORIGINS)
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    origins = [str(o).strip() for o in items if str(o).strip()]
    return origins or list(DEFAULT_CORS_ORIGINS)
