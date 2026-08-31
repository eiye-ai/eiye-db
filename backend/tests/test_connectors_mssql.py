"""SQL Server connector tests.

Live tests are gated on EIYE_TEST_MSSQL_DSN — an *admin* connection string, from
which each test provisions a schema and a db_datareader login and then points
the connector at the reader. CI supplies it from a service container.

Local note: real SQL Server images do not run under emulation on Apple Silicon
(`Invalid mapping of address ... in reserved address space`). Azure SQL Edge is
ARM-native and shares the engine, so it works for local development; CI runs the
real thing on amd64, which is the authoritative check.
"""

import asyncio
import os

import pytest

from eiye_db.connectors.base import ConnectorError
from eiye_db.connectors.sql import reject_multiple_statements

pymssql = pytest.importorskip("pymssql")

from eiye_db.connectors.mssql import SQLServerConnector  # noqa: E402  (after importorskip)


# --- pure, no server ---------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "SELECT 1;",
        "SELECT 1;   \n  ",
        "SELECT ';' AS semi",                  # separator inside a string literal
        "SELECT '''; DROP TABLE x' AS s",      # doubled-quote escape, then a fake separator
        'SELECT [a;b] FROM t',                 # bracketed identifier
        "SELECT 1 -- ; not a statement",
        "SELECT 1 /* ; nor this */",
        "SELECT 1 /* outer /* nested ; */ still comment */",
    ],
)
def test_single_statement_accepted(sql):
    reject_multiple_statements(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; DROP TABLE users",
        "SELECT 1 AS x) _q; DROP TABLE users; SELECT * FROM (SELECT 1 AS x",
        "SELECT 'safe'; DELETE FROM users",
        "SELECT 1 -- comment\n; DROP TABLE users",
    ],
)
def test_batch_rejected(sql):
    with pytest.raises(ConnectorError, match="single statement"):
        reject_multiple_statements(sql)


def test_missing_dsn():
    with pytest.raises(ConnectorError, match="dsn"):
        asyncio.run(SQLServerConnector({}).test_connection())


def test_rejects_wrong_scheme():
    with pytest.raises(ConnectorError, match="sqlserver://"):
        asyncio.run(SQLServerConnector({"dsn": "mysql://u:p@h:3306/db"}).test_connection())


def test_requires_a_database_in_the_dsn():
    with pytest.raises(ConnectorError, match="must name a database"):
        asyncio.run(SQLServerConnector({"dsn": "sqlserver://u:p@h:1433/"}).test_connection())


def test_missing_sql():
    with pytest.raises(ConnectorError, match="sql"):
        asyncio.run(SQLServerConnector({"dsn": "sqlserver://u:p@h:1433/db"}).query({}, limit=10))


def test_dsn_parsing():
    params = SQLServerConnector({"dsn": "mssql://u%40corp:p%3Aw@db.internal:1444/sales"})._params()
    assert params == {
        "server": "db.internal",
        "port": 1444,
        "user": "u@corp",
        "password": "p:w",
        "database": "sales",
    }


def test_dsn_defaults_port_1433():
    assert SQLServerConnector({"dsn": "sqlserver://u:p@h/db"})._params()["port"] == 1433


# --- live ---------------------------------------------------------------------

# Read at import: conftest's autouse _clear_eiye_env deletes EIYE_* before
# fixtures run, so reading this inside a fixture would always skip.
ADMIN_DSN = os.environ.get("EIYE_TEST_MSSQL_DSN")
RO_LOGIN, RO_PASSWORD = "eiye_ro", "eiyeRo!2026"
BREAKOUT = "SELECT 1 AS x) _q; DROP TABLE probe; SELECT * FROM (SELECT 1 AS x"

live = pytest.mark.skipif(not ADMIN_DSN, reason="EIYE_TEST_MSSQL_DSN not set")


class _Admin:
    """Connection plus its parsed params. pymssql's Connection is a C type, so
    the params cannot simply be attached to it the way pymysql allows."""

    def __init__(self, conn, params):
        self.conn = conn
        self.params = params

    def cursor(self):
        return self.conn.cursor()


@pytest.fixture
def admin():
    params = SQLServerConnector({"dsn": ADMIN_DSN})._params()
    conn = pymssql.connect(**params, autocommit=True)
    yield _Admin(conn, params)
    conn.close()


@pytest.fixture
def ro_dsn(admin):
    """Provision a schema plus a db_datareader login; return that login's DSN."""
    p = admin.params
    with admin.cursor() as cur:
        cur.execute("IF OBJECT_ID('orders') IS NOT NULL DROP TABLE orders")
        cur.execute("IF OBJECT_ID('users') IS NOT NULL DROP TABLE users")
        cur.execute("IF OBJECT_ID('probe') IS NOT NULL DROP TABLE probe")
        cur.execute("CREATE TABLE users (id INT PRIMARY KEY, email NVARCHAR(120))")
        cur.execute(
            "CREATE TABLE orders (id INT PRIMARY KEY, user_id INT "
            "CONSTRAINT fk_orders_user FOREIGN KEY REFERENCES users(id))"
        )
        # Standalone, so destruction tests are not masked by SQL Server
        # refusing to drop a table an FK still references.
        cur.execute("CREATE TABLE probe (i INT)")
        cur.execute("INSERT INTO users VALUES (1, 'a@example.com'), (2, 'b@example.com')")
        cur.execute("INSERT INTO orders VALUES (10, 1), (11, 2)")
        cur.execute("INSERT INTO probe VALUES (1), (2)")
        cur.execute(f"IF DATABASE_PRINCIPAL_ID('{RO_LOGIN}') IS NOT NULL DROP USER {RO_LOGIN}")
        cur.execute(f"IF SUSER_ID('{RO_LOGIN}') IS NOT NULL DROP LOGIN {RO_LOGIN}")
        cur.execute(f"CREATE LOGIN {RO_LOGIN} WITH PASSWORD = '{RO_PASSWORD}', CHECK_POLICY = OFF")
        cur.execute(f"CREATE USER {RO_LOGIN} FOR LOGIN {RO_LOGIN}")
        cur.execute(f"ALTER ROLE db_datareader ADD MEMBER {RO_LOGIN}")
    return f"sqlserver://{RO_LOGIN}:{RO_PASSWORD}@{p['server']}:{p['port']}/{p['database']}"


def probe_state(admin):
    with admin.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM sys.tables WHERE name = 'probe'")
        if not cur.fetchone()[0]:
            return "TABLE DROPPED"
        cur.execute("SELECT COUNT(*) FROM probe")
        return f"table ok, {cur.fetchone()[0]} row(s)"


@live
def test_live_refuses_a_write_capable_login(admin, ro_dsn):
    with pytest.raises(ConnectorError, match="can write"):
        asyncio.run(SQLServerConnector({"dsn": ADMIN_DSN}).test_connection())


@live
def test_live_roundtrip(ro_dsn):
    conn = SQLServerConnector({"dsn": ro_dsn})
    asyncio.run(conn.test_connection())
    assert asyncio.run(conn.query({"sql": "SELECT 1 AS one"}, limit=10)) == [{"one": 1}]


@live
def test_live_discover_schema(ro_dsn):
    tables = {t["name"]: t for t in asyncio.run(SQLServerConnector({"dsn": ro_dsn}).discover_schema())}
    assert {"users", "orders"} <= set(tables)
    users = {f["name"]: f for f in tables["users"]["fields"]}
    assert users["id"]["is_primary_key"] is True
    assert users["email"]["is_primary_key"] is False
    orders = {f["name"]: f for f in tables["orders"]["fields"]}
    assert orders["user_id"]["is_foreign_key"] is True


@live
def test_live_discover_relationships(ro_dsn):
    rels = asyncio.run(SQLServerConnector({"dsn": ro_dsn}).discover_relationships())
    assert {"from_table": "orders", "from_column": "user_id", "to_table": "users", "to_column": "id"} in rels


@live
def test_live_limit_is_applied(ro_dsn):
    rows = asyncio.run(SQLServerConnector({"dsn": ro_dsn}).query({"sql": "SELECT id FROM users"}, limit=1))
    assert len(rows) == 1


@live
def test_live_order_by_still_works(ro_dsn):
    # The reason this connector uses SET ROWCOUNT instead of the derived-table
    # wrapper the other SQL connectors use: T-SQL rejects ORDER BY inside a
    # derived table (error 1033), so wrapping would break ordinary queries.
    rows = asyncio.run(
        SQLServerConnector({"dsn": ro_dsn}).query({"sql": "SELECT id FROM users ORDER BY id DESC"}, limit=1)
    )
    assert rows == [{"id": 2}]


@live
@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO probe VALUES (99)",
        "UPDATE probe SET i = 99",
        "DELETE FROM probe",
        "DROP TABLE probe",
        "TRUNCATE TABLE probe",
        "CREATE TABLE t_new (i INT)",
    ],
)
def test_live_writes_rejected(admin, ro_dsn, sql):
    with pytest.raises(ConnectorError):
        asyncio.run(SQLServerConnector({"dsn": ro_dsn}).query({"sql": sql}, limit=10))
    assert probe_state(admin) == "table ok, 2 row(s)"


@live
def test_live_batch_breakout_rejected(admin, ro_dsn):
    # TDS transmits multi-statement batches and no driver flag disables that,
    # so unlike MySQL this is caught by the text check — with the read-only
    # login as the layer underneath if the check is ever fooled.
    with pytest.raises(ConnectorError, match="single statement"):
        asyncio.run(SQLServerConnector({"dsn": ro_dsn}).query({"sql": BREAKOUT}, limit=10))
    assert probe_state(admin) == "table ok, 2 row(s)"


@live
def test_live_batch_reaching_the_driver_is_still_caught(admin, ro_dsn):
    # Pins the nextset() guard specifically. Calls _query_sync directly to
    # bypass require_select/reject_multiple_statements, simulating a text check
    # that was fooled: the batch must not be reported as a successful query.
    conn = SQLServerConnector({"dsn": ro_dsn})

    # Second statement succeeds -> caught by nextset() returning a further set.
    with pytest.raises(ConnectorError, match="single statement"):
        conn._query_sync("SELECT 1 AS one; SELECT 2 AS two", 10)

    # Second statement is refused -> caught as the 3701 nextset() raises.
    with pytest.raises(ConnectorError):
        conn._query_sync("SELECT 1 AS one; DROP TABLE probe", 10)
    assert probe_state(admin) == "table ok, 2 row(s)"


@live
def test_live_read_only_login_is_the_real_boundary(admin, ro_dsn):
    # Bypass the connector's text checks entirely and send the batch straight
    # down the driver, proving the claim the module docstring makes: with a
    # reader login, permissions alone stop it.
    params = SQLServerConnector({"dsn": ro_dsn})._params()
    conn = pymssql.connect(**params, autocommit=True)
    try:
        with conn.cursor() as cur:
            # execute() does NOT raise: it returns the first result set and the
            # refused DROP only surfaces as 3701 when the batch is drained.
            # That asymmetry is why _query_sync calls nextset().
            cur.execute("SELECT 1; DROP TABLE probe")
            assert cur.fetchall() == [(1,)]
            with pytest.raises(pymssql.Error):
                cur.nextset()
    finally:
        conn.close()
    assert probe_state(admin) == "table ok, 2 row(s)"
