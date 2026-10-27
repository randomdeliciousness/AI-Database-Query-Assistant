"""Model and key configuration (ADQ-1b C3).

Lookup order for every value: environment variable, then the matching
secrets mapping (for example a section of st.secrets), then the default.
This module never imports streamlit, so the API server and the tests work
without a secrets file.

Env:
    ADQ_MODEL_PROVIDER  model provider (only "openai" in this version)
    ADQ_MODEL_ID        model ID passed to the API (default "gpt-4o")
    OPENAI_API_KEY      OpenAI key
Secrets (st.secrets layout):
    [model]   provider, model_id
    [openai]  api_key
    [sql_guard] step_budget, row_cap, timeout_s (read by sql_guard)
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

DEFAULT_PROVIDER = "openai"
DEFAULT_MODEL_ID = "gpt-4o"
SUPPORTED_PROVIDERS = ("openai",)


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


def load_openai_api_key(secrets: Mapping | None = None) -> str | None:
    """Env OPENAI_API_KEY, then secrets (e.g. st.secrets["openai"]["api_key"]).

    Returns None if neither is set. The key is never logged.
    """
    secrets = secrets or {}
    return os.getenv("OPENAI_API_KEY") or secrets.get("api_key") or None
