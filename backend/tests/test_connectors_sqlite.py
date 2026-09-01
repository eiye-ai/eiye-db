"""SQLite connector tests.

Nothing here is gated on an environment variable: the "server" is a file, so
every test creates a real database and exercises the real engine. The read-only
claims in particular are engine behaviour and are asserted against it — the
point of `mode=ro` is that SQLite refuses the write, not that the connector
declines to send it.
"""

import asyncio
import sqlite3

import pytest

from eiye_db.connectors import get_connector
from eiye_db.connectors.base import ConnectorError
from eiye_db.connectors.sqlite import SQLiteConnector
from eiye_db.models import DataSourceType


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "shop.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, joined);
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            user_id INTEGER REFERENCES users (id),
            total REAL
        );
        CREATE VIEW recent AS SELECT id, email FROM users;
        INSERT INTO users VALUES (1, 'a@example.com', '2026-01-01'), (2, 'b@example.com', '2026-02-01');
        INSERT INTO orders VALUES (10, 1, 9.99), (11, 2, 4.50);
        """
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def conn(db):
    return SQLiteConnector({"path": str(db)})


# --- config ------------------------------------------------------------------


def test_missing_path():
    with pytest.raises(ConnectorError, match="requires 'path'"):
        asyncio.run(SQLiteConnector({}).test_connection())


def test_relative_path_rejected():
    with pytest.raises(ConnectorError, match="must be absolute"):
        asyncio.run(SQLiteConnector({"path": "data/shop.db"}).test_connection())


def test_missing_file_is_not_created(tmp_path):
    ghost = tmp_path / "ghost.db"
    with pytest.raises(ConnectorError, match="connection failed"):
        asyncio.run(SQLiteConnector({"path": str(ghost)}).test_connection())
    # mode=ro must not create the file — a typo in the operator's path has to
    # surface as an error, not as a datasource that is silently empty forever.
    assert not ghost.exists()


def test_not_a_database_rejected(tmp_path):
    junk = tmp_path / "notes.db"
    junk.write_text("this is not a database")
    with pytest.raises(ConnectorError, match="connection test failed"):
        asyncio.run(SQLiteConnector({"path": str(junk)}).test_connection())


def test_query_string_characters_in_the_path_are_escaped(tmp_path):
    # The path becomes a file: URI, so a literal '?' in a filename must not be
    # readable as the start of the query string — that is where mode=ro lives.
    odd = tmp_path / "back?mode=rw.db"
    sqlite3.connect(odd).close()
    uri = SQLiteConnector({"path": str(odd)})._uri()
    assert uri.endswith("back%3Fmode%3Drw.db?mode=ro")
    asyncio.run(SQLiteConnector({"path": str(odd)}).test_connection())


def test_missing_sql(db):
    with pytest.raises(ConnectorError, match="requires 'sql'"):
        asyncio.run(SQLiteConnector({"path": str(db)}).query({}, limit=10))


def test_factory_needs_no_optional_driver(db):
    # sqlite3 is stdlib, so unlike mysql/sqlserver/s3 this type has no extra to
    # install and no require_driver entry to satisfy.
    assert isinstance(get_connector(DataSourceType.SQLITE, {"path": str(db)}), SQLiteConnector)


# --- discovery ----------------------------------------------------------------


def test_discover_schema(conn):
    tables = {t["name"]: t for t in asyncio.run(conn.discover_schema())}
    assert {"users", "orders", "recent"} <= set(tables)
    users = {f["name"]: f for f in tables["users"]["fields"]}
    assert users["id"]["is_primary_key"] is True
    assert users["email"]["is_primary_key"] is False
    assert users["email"]["type"] == "TEXT"
    # A column declared with no type is legal in SQLite; the catalogue says so
    # rather than showing an empty string.
    assert users["joined"]["type"] == "any"
    orders = {f["name"]: f for f in tables["orders"]["fields"]}
    assert orders["user_id"]["is_foreign_key"] is True
    assert orders["id"]["is_foreign_key"] is False


def test_discover_skips_internal_tables(tmp_path):
    path = tmp_path / "seq.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT)")
    raw.execute("INSERT INTO t DEFAULT VALUES")  # materializes sqlite_sequence
    raw.commit()
    raw.close()
    names = {t["name"] for t in asyncio.run(SQLiteConnector({"path": str(path)}).discover_schema())}
    assert names == {"t"}


def test_discover_relationships(conn):
    rels = asyncio.run(conn.discover_relationships())
    # `REFERENCES users (id)` names its target, so no resolution is needed.
    assert {"from_table": "orders", "from_column": "user_id", "to_table": "users", "to_column": "id"} in rels


def test_implicit_reference_resolves_to_the_parent_primary_key(tmp_path):
    path = tmp_path / "implicit.db"
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT);
        CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users);
        """
    )
    raw.commit()
    raw.close()
    rels = asyncio.run(SQLiteConnector({"path": str(path)}).discover_relationships())
    # pragma_foreign_key_list reports "to" as NULL for a column-less REFERENCES;
    # unresolved it would emit a relationship pointing at nothing.
    assert {"from_table": "orders", "from_column": "user_id", "to_table": "users", "to_column": "id"} in rels


def test_composite_primary_key_target_is_dropped_not_guessed(tmp_path):
    path = tmp_path / "composite.db"
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE parts (maker TEXT, sku TEXT, PRIMARY KEY (maker, sku));
        CREATE TABLE uses (id INTEGER PRIMARY KEY, part TEXT REFERENCES parts);
        """
    )
    raw.commit()
    raw.close()
    rels = asyncio.run(SQLiteConnector({"path": str(path)}).discover_relationships())
    assert [r for r in rels if r["to_table"] == "parts"] == []


# --- query ---------------------------------------------------------------------


def test_query_roundtrip(conn):
    assert asyncio.run(conn.query({"sql": "SELECT 1 AS one"}, limit=10)) == [{"one": 1}]


def test_query_limit_is_applied(conn):
    rows = asyncio.run(conn.query({"sql": "SELECT id FROM users"}, limit=1))
    assert rows == [{"id": 1}]


def test_query_order_by_survives_the_wrapper(conn):
    rows = asyncio.run(conn.query({"sql": "SELECT id FROM users ORDER BY id DESC"}, limit=10))
    assert [r["id"] for r in rows] == [2, 1]


def test_query_trailing_semicolon_accepted(conn):
    assert asyncio.run(conn.query({"sql": "SELECT 1 AS one;"}, limit=10)) == [{"one": 1}]


def test_blob_reported_by_size(tmp_path):
    path = tmp_path / "blobs.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE f (id INTEGER PRIMARY KEY, body BLOB)")
    raw.execute("INSERT INTO f VALUES (1, ?)", (b"\xff\xfe\x00binary",))
    raw.commit()
    raw.close()
    rows = asyncio.run(SQLiteConnector({"path": str(path)}).query({"sql": "SELECT body FROM f"}, limit=10))
    # bytes do not survive JSON — invalid UTF-8 would fail serialization on the
    # way out, well past the point where the cause is visible.
    assert rows == [{"body": "<9 bytes>"}]


# --- read-only -----------------------------------------------------------------


def row_count(db, table):
    raw = sqlite3.connect(db)
    try:
        return raw.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        raw.close()


def table_exists(db, name):
    raw = sqlite3.connect(db)
    try:
        return raw.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = ?", (name,)).fetchone()[0] == 1
    finally:
        raw.close()


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO users VALUES (99, 'x@example.com', '2026-03-01')",
        "UPDATE users SET email = 'x@example.com'",
        "DELETE FROM users",
        "DROP TABLE users",
        "CREATE TABLE t_new (i INT)",
        "ALTER TABLE users ADD COLUMN x INT",
    ],
)
def test_writes_rejected(db, conn, sql):
    with pytest.raises(ConnectorError):
        asyncio.run(conn.query({"sql": sql}, limit=10))
    assert table_exists(db, "users")
    assert row_count(db, "users") == 2
    assert not table_exists(db, "t_new")


def test_write_is_refused_by_the_engine_not_only_the_select_check(db):
    # require_select is a usability guard; the boundary is mode=ro. Bypass the
    # guard by going straight at the connection and confirm SQLite still says no.
    raw = SQLiteConnector({"path": str(db)})._connect()
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            raw.execute("DELETE FROM users")
    finally:
        raw.close()
    assert row_count(db, "users") == 2


def test_attached_database_is_read_only_too(db, tmp_path):
    # mode=ro binds the main database only. PRAGMA query_only is what stops a
    # write to something ATTACHed afterwards.
    side = tmp_path / "side.db"
    raw = sqlite3.connect(side)
    raw.execute("CREATE TABLE scratch (i INT)")
    raw.commit()
    raw.close()

    ro = SQLiteConnector({"path": str(db)})._connect()
    try:
        ro.execute("ATTACH DATABASE ? AS side", (str(side),))
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("INSERT INTO side.scratch VALUES (1)")
    finally:
        ro.close()
    assert row_count(side, "scratch") == 0


def test_multi_statement_breakout_rejected(db, conn):
    # Closes the LIMIT wrapper's paren and appends a statement — the input that
    # dropped a table under aiomysql. Python's sqlite3 executes one statement
    # per call, so the server never sees the second.
    breakout = "SELECT 1) _x LIMIT 1; DROP TABLE users; SELECT * FROM (SELECT 1"
    with pytest.raises(ConnectorError):
        asyncio.run(conn.query({"sql": breakout}, limit=10))
    assert table_exists(db, "users")
    assert row_count(db, "users") == 2


@pytest.mark.parametrize("sql", ["DROP TABLE users", "PRAGMA writable_schema = ON", "ATTACH DATABASE 'x' AS y"])
def test_non_select_rejected_before_it_reaches_the_engine(conn, sql):
    with pytest.raises(ConnectorError, match="only SELECT"):
        asyncio.run(conn.query({"sql": sql}, limit=10))
