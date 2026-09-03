"""Alembic environment for eiye_db.

Two things this does differently from the generated template, both deliberate:

**The URL comes from the application's own settings, not from a value shipped
in alembic.ini.** An operator configures the database in exactly one place —
`EIYE_DATABASE_URL` — and a migration that could be pointed elsewhere by a
second, stale config file is a way to upgrade the wrong database. So
`alembic.ini` ships with `sqlalchemy.url` blank.

Resolution order, and each entry earns its place: `-x url=...` for a deliberate
one-off; then `sqlalchemy.url` *if something set it*, which is how
`db.ensure_versioned` targets the engine already open rather than re-reading
config; then the application settings. Omitting the second of those is a real
bug and was one — every programmatic migration silently ran against the default
database instead of the one it was handed.

**`render_as_batch` is on.** SQLite is the default metadata store and cannot
`ALTER` most things in place; batch mode rewrites the table instead. Without it
the first migration that alters a column would fail on the default deployment.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from eiye_db.config import settings
from eiye_db.db import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    explicit = context.get_x_argument(as_dictionary=True).get("url")
    if explicit:
        return explicit
    # Blank in the shipped alembic.ini, so this only wins when a caller set it.
    return config.get_main_option("sqlalchemy.url") or settings.database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _url()
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, poolclass=pool.NullPool, connect_args=connect_args)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
