"""SQLite metadata store: datasource registry and audit log."""

from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, String, Text, UniqueConstraint, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from eiye_db.config import settings


class Base(DeclarativeBase):
    pass


class DataSourceRow(Base):
    __tablename__ = "datasources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    type: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    pii_risk_level: Mapped[str] = mapped_column(String(10), default="unknown")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
    last_connected: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    schema_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class RelationshipRow(Base):
    """A semantic link between two columns (same or different datasources).

    Trust model: source="structural" rows (real DB foreign keys) are created
    approved; heuristic/proposed rows start as candidates and only a human
    approval makes them authoritative to agents.
    """

    __tablename__ = "relationships"
    # Directed uniqueness at the DB level; undirected dedup is enforced in
    # semantic.upsert (a DB constraint can't express symmetric uniqueness).
    __table_args__ = (
        UniqueConstraint(
            "from_datasource_id", "from_table", "from_column", "to_datasource_id", "to_table", "to_column"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    from_datasource_id: Mapped[str] = mapped_column(String(36))
    from_table: Mapped[str] = mapped_column(String(255))
    from_column: Mapped[str] = mapped_column(String(255))
    to_datasource_id: Mapped[str] = mapped_column(String(36))
    to_table: Mapped[str] = mapped_column(String(255))
    to_column: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(30))  # foreign_key | candidate_join
    source: Mapped[str] = mapped_column(String(20))  # structural | heuristic | proposed
    status: Mapped[str] = mapped_column(String(20))  # approved | candidate | rejected
    confidence: Mapped[float] = mapped_column(default=1.0)
    rationale: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class MetricRow(Base):
    """A named, governed query template (the metric catalog).

    Trust model mirrors relationships: human-authored metrics (source="human",
    created with the admin key) are approved on creation; agent-proposed ones
    (source="proposed") stay candidates until a human approves. Only approved
    metrics can be executed.
    """

    __tablename__ = "metrics"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    datasource_id: Mapped[str] = mapped_column(String(36))
    request_template: Mapped[dict] = mapped_column(JSON)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(20))  # human | proposed
    status: Mapped[str] = mapped_column(String(20))  # approved | candidate | rejected
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class PolicyRow(Base):
    """An ABAC policy: who (subjects) may do what (actions) to which
    datasource (resource_id, or '*'), with optional column masking."""

    __tablename__ = "policies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    effect: Mapped[str] = mapped_column(String(10))  # allow | deny
    resource_type: Mapped[str] = mapped_column(String(50), default="datasource")
    resource_id: Mapped[str] = mapped_column(String(255))  # datasource id | "*"
    actions: Mapped[list] = mapped_column(JSON)  # subset of {"read", "discover"}
    subjects: Mapped[list] = mapped_column(JSON)  # key ids | ["*"]
    conditions: Mapped[dict] = mapped_column(JSON, default=dict)  # {"columns": [...]} on deny
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class AuditRow(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime)
    action: Mapped[str] = mapped_column(String(50))
    resource_type: Mapped[str] = mapped_column(String(50))
    resource_id: Mapped[str] = mapped_column(String(255))
    api_key_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    datasource_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    success: Mapped[bool] = mapped_column(Boolean, default=True)


_engine = None


def configure(url: str | None = None):
    """Create (or replace) the engine and ensure tables exist."""
    global _engine
    if url is None:
        url = settings.database_url
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    _engine = create_engine(url, connect_args=connect_args)
    Base.metadata.create_all(_engine)
    return _engine


def get_engine():
    if _engine is None:
        configure()
    return _engine


@contextmanager
def session():
    with Session(get_engine()) as s:
        yield s
