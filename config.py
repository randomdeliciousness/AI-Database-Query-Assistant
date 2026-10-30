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
    ADQ_CHROMA_PATH      Chroma PersistentClient path (default ./chroma_db)
    ADQ_ONNX_MODEL_DIR   extracted ONNX directory (unset outside the image)
    ADQ_SCHEMA_MODE      retrieval (default) or full
    ADQ_SEED_PAIRS       seed JSONL path; empty string seeds nothing
    ADQ_RETRIEVAL_PARAMS tuned params JSON (default eval/retrieval_params.json)
    ADQ_BLOCKED_QUESTIONS  blocked ids and question hashes
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
from pathlib import Path
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
# Per-call prompt cap. Exactly this estimate is allowed. reserve() still
# enforces MAX_PROMPT_TOKENS.
CALL_PROMPT_TOKEN_CAP = 4_674
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


def _default_repo_file(*parts: str) -> str:
    """Default path next to this file. An env override is returned unchanged."""
    return str(Path(__file__).resolve().parent.joinpath(*parts))


def load_chroma_path() -> str:
    """Env ADQ_CHROMA_PATH, then ./chroma_db. No secrets section."""
    return os.getenv("ADQ_CHROMA_PATH") or "./chroma_db"


def load_onnx_model_dir() -> str | None:
    """Env ADQ_ONNX_MODEL_DIR. Unset outside the image. No secrets section."""
    if "ADQ_ONNX_MODEL_DIR" not in os.environ:
        return None
    value = os.environ["ADQ_ONNX_MODEL_DIR"].strip()
    return value or None


def load_schema_mode() -> str:
    """Env ADQ_SCHEMA_MODE, then ``retrieval``. Empty falls through.

    The only accepted values are ``retrieval`` and ``full``.
    """
    raw = os.getenv("ADQ_SCHEMA_MODE", "")
    mode = raw.strip().lower() or "retrieval"
    if mode not in ("retrieval", "full"):
        raise ValueError(f"ADQ_SCHEMA_MODE must be 'retrieval' or 'full', got {mode!r}")
    return mode


def load_seed_pairs_path() -> str:
    """Env ADQ_SEED_PAIRS, then eval/seed_pairs.jsonl.

    A set but empty value seeds nothing and does not fall through.
    """
    if "ADQ_SEED_PAIRS" in os.environ:
        return os.environ["ADQ_SEED_PAIRS"]
    return _default_repo_file("eval", "seed_pairs.jsonl")


def load_retrieval_params_path() -> str:
    """Env ADQ_RETRIEVAL_PARAMS, then eval/retrieval_params.json."""
    if "ADQ_RETRIEVAL_PARAMS" in os.environ:
        return os.environ["ADQ_RETRIEVAL_PARAMS"]
    return _default_repo_file("eval", "retrieval_params.json")


def load_blocked_questions_path() -> str:
    """Env ADQ_BLOCKED_QUESTIONS, then eval/blocked.json.

    A set but empty value blocks nothing and does not fall through.
    """
    if "ADQ_BLOCKED_QUESTIONS" in os.environ:
        return os.environ["ADQ_BLOCKED_QUESTIONS"]
    return _default_repo_file("eval", "blocked.json")
