"""Core data models for eiye_db."""

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DataSourceType(StrEnum):
    """Types that have a working connector — nothing else.

    This enum is the register-time contract: `get_connector` must implement
    every member, so an unimplemented type is rejected by request validation
    (422) instead of registering successfully and failing later at test/query
    time. Add a member in the same change that adds its connector.
    """

    POSTGRESQL = "postgresql"
    # MySQL and MariaDB share this one member: MariaDB is a dialect, not a
    # second SKU, and the connector is tested against both.
    MYSQL = "mysql"
    SQLSERVER = "sqlserver"
    FILE_SYSTEM = "filesystem"
    REST_API = "rest_api"


class ConnectionStatus(StrEnum):
    DISCOVERED = "discovered"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"
    DISCONNECTED = "disconnected"


class DataSource(BaseModel):
    """A registered data source in the semantic surface."""

    id: str | None = Field(None, description="Unique identifier")
    name: str
    type: DataSourceType
    status: ConnectionStatus = ConnectionStatus.DISCOVERED
    config: dict[str, Any] = Field(default_factory=dict)
    pii_risk_level: Literal["low", "medium", "high", "unknown"] = "unknown"
    tags: list[str] = Field(default_factory=list)
    description: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    last_connected: datetime | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class SchemaField(BaseModel):
    """A field within a datasource schema."""

    name: str
    type: str
    pii_detected: bool = False
    pii_types: list[str] = Field(default_factory=list)
    sample_value: str | None = None
    description: str = ""
    is_primary_key: bool = False
    is_foreign_key: bool = False


class SchemaInfo(BaseModel):
    """Schema information discovered for a datasource."""

    datasource_id: str
    tables: list[dict[str, Any]] = Field(default_factory=list)
    fields: list[SchemaField] = Field(default_factory=list)
    relationships: list[dict[str, str]] = Field(default_factory=list)
    discovered_at: datetime = Field(default_factory=_utcnow)


class PIIResult(BaseModel):
    """PII detection result."""

    text: str
    entities: list[dict[str, Any]] = Field(default_factory=list)
    anonymized_text: str | None = None
    risk_score: float = 0.0
    detected_at: datetime = Field(default_factory=_utcnow)


class PolicyCreate(BaseModel):
    """An ABAC policy. `subjects` are API key ids matched exactly, case-
    sensitive ("primary", "mcp-stdio", or ["*"]); a typo silently never
    matches. `resource_id` is a datasource id or "*". A deny policy with
    conditions={"columns": [...]} (actions must be ["read"]) masks those
    columns from query results instead of blocking the source."""

    name: str
    description: str = ""
    effect: Literal["allow", "deny"]
    resource_id: str = "*"
    actions: list[Literal["read", "discover"]] = Field(default_factory=lambda: ["read"])
    subjects: list[str] = Field(default_factory=lambda: ["*"])
    conditions: dict[str, Any] = Field(default_factory=dict)


class AuditLog(BaseModel):
    """Audit trail entry."""

    id: str | None = None
    timestamp: datetime = Field(default_factory=_utcnow)
    action: str
    resource_type: str
    resource_id: str
    user_id: str | None = None
    api_key_id: str | None = None
    datasource_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    ip_address: str | None = None
    success: bool = True


class DataSourceCreate(BaseModel):
    """Request body for registering a datasource."""

    name: str
    type: DataSourceType
    config: dict[str, Any] = Field(default_factory=dict)
    description: str = ""
    tags: list[str] = Field(default_factory=list)


class DataSourceUpdate(BaseModel):
    """Request body for updating a datasource; None fields are left unchanged."""

    name: str | None = None
    config: dict[str, Any] | None = None
    description: str | None = None
    tags: list[str] | None = None


class RelationshipUpdate(BaseModel):
    """Human review of a candidate relationship (the 'human governs' step)."""

    status: Literal["approved", "rejected"]


class MetricCreate(BaseModel):
    """A governed query template. `params` maps name -> {"type": "string"|"number",
    "default": optional}; the template references them as {name} placeholders."""

    name: str
    description: str = ""
    datasource_id: str
    request_template: dict[str, Any]
    params: dict[str, Any] = Field(default_factory=dict)


class MetricQuery(BaseModel):
    """Execute an approved metric with concrete parameter values."""

    params: dict[str, Any] = Field(default_factory=dict)
    limit: int = Field(100, ge=1, le=1000)


class ResolveSide(BaseModel):
    """One side of an entity-resolution request: a governed query plus the
    column whose values name the entities."""

    datasource_id: str
    request: dict[str, Any]
    column: str


class ResolveRequest(BaseModel):
    """Match entity names across two governed query results."""

    left: ResolveSide
    right: ResolveSide
    limit: int = Field(100, ge=1, le=1000)


class SourceQueryRequest(BaseModel):
    """A source-scoped query. `request` is connector-specific:
    postgres: {"sql": "SELECT ..."} · filesystem: {"path": "rel/file.csv"} ·
    rest_api: {"path": "/endpoint", "params": {...}}."""

    datasource_id: str
    request: dict[str, Any]
    limit: int = Field(100, ge=1, le=1000)
    include_pii: bool = False


class SourceQueryResponse(BaseModel):
    """Result of a source-scoped query."""

    datasource_id: str
    rows: list[dict[str, Any]]
    row_count: int
    pii_filtered: bool
    pii_counts: dict[str, int] = Field(default_factory=dict)
    execution_time_ms: float = 0.0
    # Result-level lineage: where these rows came from and which governed
    # definition (if any) produced them — enough to trace a result back to its
    # source without consulting the audit log.
    lineage: dict[str, Any] = Field(default_factory=dict)


class AskRequest(BaseModel):
    """A natural-language question answered ONLY through approved metrics
    (deterministic matching; optional LLM assist drafts, never executes)."""

    question: str = Field(min_length=1, max_length=500)
    limit: int = Field(100, ge=1, le=1000)
