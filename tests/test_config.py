"""Model/key config: env first, then a secrets mapping, then defaults."""

import pytest

from config import (
    DEFAULT_CORS_ORIGINS,
    DEFAULT_MODEL_ID,
    ModelConfig,
    load_cors_origins,
    load_model_config,
    load_secrets_file,
    load_xai_api_key,
    secrets_section,
)


def test_model_config_default():
    assert load_model_config() == ModelConfig("xai", DEFAULT_MODEL_ID)
    assert DEFAULT_MODEL_ID == "grok-4.20-0309-non-reasoning"


def test_model_config_from_secrets():
    cfg = load_model_config({"provider": "xai", "model_id": "test-model-secrets"})
    assert cfg == ModelConfig("xai", "test-model-secrets")


def test_model_config_env_beats_secrets(monkeypatch):
    monkeypatch.setenv("ADQ_MODEL_ID", "test-model-env")
    cfg = load_model_config({"model_id": "test-model-secrets"})
    assert cfg.model_id == "test-model-env"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(" XAI ", "xai"), (" XAI-OpenAI-Compat ", "xai-openai-compat")],
)
def test_provider_normalised(monkeypatch, raw, expected):
    monkeypatch.setenv("ADQ_MODEL_PROVIDER", raw)
    assert load_model_config().provider == expected


@pytest.mark.parametrize("provider", ["openai", "grok", "anthropic", "other"])
def test_unsupported_provider_rejected(monkeypatch, provider):
    monkeypatch.setenv("ADQ_MODEL_PROVIDER", provider)
    with pytest.raises(ValueError, match="provider not supported in this version"):
        load_model_config()


def test_blank_model_id_rejected():
    with pytest.raises(ValueError, match="model_id"):
        ModelConfig(model_id="  ")


def test_api_key_env_then_secrets(monkeypatch):
    assert load_xai_api_key() is None
    assert load_xai_api_key({"api_key": "test-not-a-key-1"}) == "test-not-a-key-1"
    monkeypatch.setenv("XAI_API_KEY", "test-not-a-key-2")
    assert load_xai_api_key({"api_key": "test-not-a-key-1"}) == "test-not-a-key-2"


class _MissingSecrets:
    """Behaves like st.secrets with no secrets.toml on disk."""

    def get(self, name, default=None):
        raise FileNotFoundError("No secrets found")


def test_secrets_section_missing_file_gives_empty():
    assert secrets_section(None, "model") == {}
    assert secrets_section(_MissingSecrets(), "model") == {}
    assert secrets_section({}, "model") == {}
    assert secrets_section({"model": "not-a-table"}, "model") == {}
    assert secrets_section({"model": {"model_id": "m"}}, "model") == {"model_id": "m"}


def test_missing_secrets_fall_back_to_env_and_defaults(monkeypatch):
    monkeypatch.setenv("ADQ_MODEL_ID", "test-model-env")
    cfg = load_model_config(secrets_section(_MissingSecrets(), "model"))
    assert cfg == ModelConfig("xai", "test-model-env")
    assert load_xai_api_key(secrets_section(_MissingSecrets(), "xai")) is None


def test_api_key_env_only(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "test-not-a-key-env")
    assert load_xai_api_key({}) == "test-not-a-key-env"
    assert load_xai_api_key(secrets_section(_MissingSecrets(), "xai")) == (
        "test-not-a-key-env"
    )


def test_api_key_secrets_only():
    assert load_xai_api_key({"api_key": "test-not-a-key-secrets"}) == (
        "test-not-a-key-secrets"
    )


def test_api_key_both_env_wins(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "test-not-a-key-env")
    assert load_xai_api_key({"api_key": "test-not-a-key-secrets"}) == (
        "test-not-a-key-env"
    )


def test_cors_default_is_localhost_only():
    origins = load_cors_origins()
    assert origins == list(DEFAULT_CORS_ORIGINS)
    assert all(
        o.startswith(("http://localhost:", "http://127.0.0.1:")) for o in origins
    )


def test_cors_from_secrets_list_or_string():
    assert load_cors_origins({"cors_origins": ["https://a.example.com"]}) == [
        "https://a.example.com"
    ]
    assert load_cors_origins(
        {"cors_origins": "https://a.example.com, https://b.example.org"}
    ) == ["https://a.example.com", "https://b.example.org"]


def test_cors_env_wins_over_secrets(monkeypatch):
    monkeypatch.setenv("ADQ_CORS_ORIGINS", "https://env.example.net")
    assert load_cors_origins({"cors_origins": ["https://a.example.com"]}) == [
        "https://env.example.net"
    ]


def test_cors_blank_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("ADQ_CORS_ORIGINS", " , ")
    assert load_cors_origins() == list(DEFAULT_CORS_ORIGINS)


def test_secrets_file_missing_and_present(tmp_path):
    assert load_secrets_file(tmp_path / "nope.toml") == {}
    f = tmp_path / "secrets.toml"
    f.write_text('[api]\ncors_origins = ["https://a.example.com"]\n')
    loaded = load_secrets_file(f)
    assert load_cors_origins(secrets_section(loaded, "api")) == [
        "https://a.example.com"
    ]
