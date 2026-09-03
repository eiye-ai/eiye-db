"""SQLite metadata store: datasource registry and audit log.

Schema changes go through Alembic (`backend/alembic/`), not through editing a
model and hoping `create_all` notices. It will not: `create_all` adds missing
*tables* and never alters an existing one, so a column added to a model would
simply be absent on every database that already existed.

Both paths are kept, and `test_migrations.py` asserts they agree — a fresh
database is built by `create_all` and then stamped at head, while an existing
one is moved forward by `alembic upgrade head`. If those two ever diverge, the
drift test fails rather than a deployment discovering it.
"""

import logging
from contextlib import contextmanager
from datetime import datetime
from functools import cache
from pathlib import Path

from sqlalchemy import JSON, Boolean, DateTime, String, Text, UniqueConstraint, create_engine, inspect
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from eiye_db.config import settings

log = logging.getLogger(__name__)

#: Where the migration scripts live, resolved from this file so it does not
#: depend on the working directory a deployment happens to start in.
ALEMBIC_DIR = Path(__file__).resolve().parent.parent / "alembic"


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


def _alembic_config(url: str):
    """An Alembic config pointed at this deployment's own scripts and database.

    Built in code rather than read from alembic.ini so the runtime checks work
    from any working directory — a deployment started by systemd is not
    necessarily sitting in `backend/`.
    """
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def current_revision(engine) -> str | None:
    """The revision this database is stamped at, or None if it is unversioned."""
    from alembic.migration import MigrationContext

    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


@cache
def head_revision() -> str | None:
    """The newest revision on disk.

    Cached: it is a property of the code, not of any database, so it cannot
    change while the process runs. Reading it uncached cost the test suite about
    two seconds, because every `TestClient` boots the app and each boot reloaded
    the whole script directory.
    """
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(_alembic_config("sqlite://")).get_current_head()


def ensure_versioned(engine=None) -> str:
    """Reconcile the database with the migration history, and report what it found.

    Three states, and only one of them needs an operator:

    - **unversioned and empty of our tables** — nothing was there; `configure`
      has just built the current schema, so stamp it at head. A fresh install
      therefore ends up properly versioned with no extra step.
    - **unversioned but already carrying our tables** — a database created by
      an earlier version, before migrations existed. Also stamped at head: its
      schema matches, because that release's `create_all` built the same tables
      the initial revision does, which the drift test pins.
    - **stamped, but behind head** — a real pending upgrade. Warned about, with
      the command, and *not* applied: migrating someone's database as a side
      effect of starting a process is how two replicas booting at once corrupt
      a schema.

    Returns one of "stamped", "current", or "behind" so a caller (and a test)
    can tell which happened.
    """
    from alembic import command

    engine = engine or get_engine()
    head = head_revision()
    current = current_revision(engine)
    if current is None:
        command.stamp(_alembic_config(str(engine.url.render_as_string(hide_password=False))), "head")
        existing = [t for t in inspect(engine).get_table_names() if t in Base.metadata.tables]
        log.info(
            "stamped the metadata store at %s (%s)",
            head,
            "fresh database" if not existing else "existing schema, pre-migrations",
        )
        return "stamped"
    if current == head:
        return "current"
    log.warning(
        "the metadata store is at revision %s but this build expects %s. Schema changes are NOT "
        "applied at boot, deliberately — run `alembic upgrade head` from backend/ before relying "
        "on anything that needs the newer schema.",
        current,
        head,
    )
    return "behind"


def get_engine():
    if _engine is None:
        configure()
    return _engine


@contextmanager
def session():
    with Session(get_engine()) as s:
        yield s
