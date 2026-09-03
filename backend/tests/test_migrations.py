"""Migration tests.

The repo keeps two ways of getting a schema: `create_all` builds a fresh one,
and `alembic upgrade head` moves an existing one forward. Keeping both is a
deliberate trade — running the whole migration chain for each of several hundred
tests would be slow, and `create_all` cannot alter an existing table, so neither
alone is enough.

The trade is only safe while the two agree, so `test_migrations_match_the_models`
is the load-bearing test in this file. Everything else checks the runtime
reconciliation around it.
"""

import logging

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from eiye_db import db


def _url(tmp_path, name="m.db") -> str:
    return f"sqlite:///{tmp_path}/{name}"


def _upgrade(url: str) -> None:
    command.upgrade(db._alembic_config(url), "head")


# --- the load-bearing one -----------------------------------------------------


def test_migrations_match_the_models(tmp_path):
    """Run the chain, then ask Alembic what it would autogenerate next. Nothing.

    This is what permits `create_all` to stay in `configure()`. If a model gains
    a column and no migration is written, this fails here rather than on someone
    else's database — where `create_all` would silently not add it, because it
    only ever creates missing tables.

    The diff is asserted empty rather than merely inspected: `compare_metadata`
    reports added and removed tables, columns, nullability and (with
    `compare_type`) type changes.
    """
    url = _url(tmp_path)
    _upgrade(url)
    engine = create_engine(url)
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "target_metadata": db.Base.metadata}
        )
        diff = compare_metadata(context, db.Base.metadata)
    engine.dispose()
    assert diff == [], (
        "the migration chain and the models have diverged. Generate the missing revision with:\n"
        "  cd backend && alembic revision --autogenerate -m '<what changed>'"
    )


def test_the_chain_builds_every_table_the_app_uses(tmp_path):
    url = _url(tmp_path)
    _upgrade(url)
    engine = create_engine(url)
    tables = set(inspect(engine).get_table_names())
    engine.dispose()
    assert set(db.Base.metadata.tables) <= tables
    assert "alembic_version" in tables


def test_downgrade_returns_to_an_empty_schema(tmp_path):
    """A revision that cannot be undone is a one-way door. Checked on the
    initial revision so the habit is established while it is still trivial."""
    url = _url(tmp_path)
    _upgrade(url)
    command.downgrade(db._alembic_config(url), "base")
    engine = create_engine(url)
    remaining = set(inspect(engine).get_table_names()) & set(db.Base.metadata.tables)
    engine.dispose()
    assert remaining == set()


# --- runtime reconciliation ----------------------------------------------------


def test_a_fresh_database_is_stamped_at_head(tmp_path):
    """A fresh install must end up versioned without the operator doing
    anything, or the first real migration would have no baseline to run from."""
    engine = db.configure(_url(tmp_path))
    assert db.current_revision(engine) is None
    assert db.ensure_versioned(engine) == "stamped"
    assert db.current_revision(engine) == db.head_revision()


def test_a_pre_migrations_database_is_stamped_rather_than_rebuilt(tmp_path):
    """The upgrade path for every database that already exists. Its schema was
    built by an earlier release's `create_all`, which the drift test says
    matches the initial revision, so stamping is correct and re-running the
    chain over live tables would not be."""
    url = _url(tmp_path)
    engine = create_engine(url)
    db.Base.metadata.create_all(engine)  # what an older release left behind
    with engine.connect() as connection:
        connection.execute(
            text("INSERT INTO datasources (id, name, type, status, config, pii_risk_level, tags, "
                 "description, created_at, updated_at, meta) VALUES ('d1', 'n', 'sqlite', "
                 "'discovered', '{}', 'unknown', '[]', '', '2026-01-01', '2026-01-01', '{}')")
        )
        connection.commit()

    assert db.ensure_versioned(engine) == "stamped"
    assert db.current_revision(engine) == db.head_revision()
    with engine.connect() as connection:
        survived = connection.execute(text("SELECT count(*) FROM datasources")).scalar()
    engine.dispose()
    assert survived == 1, "stamping destroyed existing rows"


def test_an_up_to_date_database_reports_current(tmp_path):
    engine = db.configure(_url(tmp_path))
    db.ensure_versioned(engine)
    assert db.ensure_versioned(engine) == "current"


def test_a_behind_database_warns_and_is_not_upgraded(tmp_path, caplog):
    """The behaviour that matters most here is the one that does *nothing*.
    Migrating a database as a side effect of starting a process is how two
    replicas booting together corrupt a schema, so boot warns and stops."""
    url = _url(tmp_path)
    engine = db.configure(url)
    with engine.connect() as connection:
        connection.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num varchar(32) NOT NULL)"))
        connection.execute(text("DELETE FROM alembic_version"))
        connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('0000deadbeef')"))
        connection.commit()

    with caplog.at_level(logging.WARNING, logger="eiye_db.db"):
        assert db.ensure_versioned(engine) == "behind"
    assert "alembic upgrade head" in caplog.text

    with engine.connect() as connection:
        unchanged = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    engine.dispose()
    assert unchanged == "0000deadbeef", "boot upgraded the database on its own"


def test_the_script_directory_resolves_without_a_working_directory(monkeypatch, tmp_path):
    """`head_revision` has to work from wherever the process was started — a
    deployment launched by systemd is not sitting in `backend/`."""
    monkeypatch.chdir(tmp_path)
    assert db.head_revision() is not None
    assert db.ALEMBIC_DIR.is_dir()
