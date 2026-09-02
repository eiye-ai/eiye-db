"""Configuration management with environment variable support (EIYE_ prefix)."""

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class NamedKey(BaseModel):
    """One entry in the EIYE_API_KEYS map: a credential and the authority it carries.

    `sha256` is the hex digest of the key itself, so a leaked config file leaks
    no working credential. A plain digest rather than a password KDF is adequate
    *only because* these secrets are machine-generated at full entropy
    (`scripts/mint_key.py` emits 256 bits). Never hash a human-chosen passphrase
    into this field: at SHA-256 speed it is guessable, and the map would then be
    a liability rather than a protection.

    Unknown fields are refused. A map is edited by hand, and `"admin": true`
    instead of `"is_admin": true` would otherwise be accepted in silence as a
    non-admin key -- the reading of it nobody intended.
    """

    model_config = ConfigDict(extra="forbid")

    sha256: str
    is_admin: bool = False
    expires_at: datetime | None = None

    @field_validator("sha256")
    @classmethod
    def _is_hex_digest(cls, value: str) -> str:
        digest = value.strip().lower()
        if len(digest) != 64 or digest.strip("0123456789abcdef"):
            raise ValueError("sha256 must be a 64-character hex digest (see scripts/mint_key.py)")
        return digest

    @field_validator("expires_at")
    @classmethod
    def _assume_utc(cls, value: datetime | None) -> datetime | None:
        # "2027-01-01" parses to a naive datetime, and comparing naive to aware
        # raises TypeError -- which would surface as a 500 on every request
        # rather than as a bad setting. Read a bare timestamp as UTC.
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EIYE_", env_file=".env")

    app_name: str = "eiye_db"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    # Metadata store (registry + audit)
    database_url: str = "sqlite:///./eiye.db"

    # Governance: both keys unset = open dev mode; admin key may view raw PII.
    # Setting exactly one is refused at boot (see main.lifespan) — a half-secured
    # service is worse than an obviously open one.
    api_key: str | None = None
    admin_api_key: str | None = None

    # Named keys, as JSON: EIYE_API_KEYS='{"support": {"sha256": "<hex>",
    # "is_admin": false, "expires_at": "2027-01-01"}}'. Each entry is its own
    # ABAC subject and audit principal, which is the entire point: EIYE_API_KEY
    # resolves every HTTP caller to key_id="primary", so policy `subjects`
    # matching is a no-op over REST and one agent cannot be told from another.
    # Coexists with the two single-key settings above (which keep working and
    # keep their reserved ids); mint entries with scripts/mint_key.py. Held in
    # the environment, so adding or revoking a key takes a restart.
    api_keys: dict[str, NamedKey] = {}

    # The principal the stdio MCP server runs as: its ABAC subject and audit
    # identity. Set EIYE_KEY_ID per agent in the MCP client's launch env to give
    # each one a distinct, policy-targetable identity. NOT a credential — any
    # local process can claim any id; stdio already trusts whoever spawned it.
    key_id: str = "mcp-stdio"

    # Path to a signed licence file. Unset = the BSL Additional Use Grant (free
    # tier: 5 datasources, 1,000 queries/month). If set but unusable, boot fails
    # loudly rather than silently falling back to free-tier limits.
    license_file: str | None = None

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
