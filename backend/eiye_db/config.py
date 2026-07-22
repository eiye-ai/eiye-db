"""Configuration management with environment variable support (EIYE_ prefix)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EIYE_", env_file=".env")

    app_name: str = "eiye_db"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    # Metadata store (registry + audit)
    database_url: str = "sqlite:///./eiye.db"

    # Governance: unset api_key = open dev mode; admin key may view raw PII
    api_key: str | None = None
    admin_api_key: str | None = None

    # ABAC posture: False (default) = access allowed unless a policy denies it,
    # so a fresh install works with zero policies. True = every non-admin access
    # needs an explicit allow policy (hardened deployments).
    abac_default_deny: bool = False

    # Browser access (CORS). Comma-separated origins; defaults cover the Vite dev server.
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # NL → governed query. Deterministic matching is always on; the LLM assist
    # (bootstraps metric selection + parameter drafting when determinism falls
    # short) is opt-in, and its output must still pass the catalog's typed
    # validation before anything executes. Requires the `nl` extra:
    #   pip install -e ".[nl]"   and an Anthropic API key.
    # DISCLOSURE: when enabled, question text and approved-metric metadata are
    # sent to the Anthropic API (questions are not pre-redacted).
    nl_llm_enabled: bool = False
    nl_llm_model: str = "claude-opus-4-8"
    anthropic_api_key: str | None = None  # falls back to the ANTHROPIC_API_KEY env var

    # Optional spaCy NER layer for name/location redaction (regex baseline always
    # runs). Off by default; when enabled the model must load or the first
    # redaction raises — never a silent fail-open. Requires the `ner` extra:
    #   pip install -e ".[ner]" && python -m spacy download en_core_web_sm
    pii_ner_enabled: bool = False
    pii_ner_model: str = "en_core_web_sm"
    pii_ner_max_chars: int = 100_000  # cap text scanned per string (NER cost is length-bound)


settings = Settings()
