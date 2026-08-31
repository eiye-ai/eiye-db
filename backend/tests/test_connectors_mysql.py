"""MySQL / MariaDB connector tests.

Live tests are gated on EIYE_TEST_MYSQL_DSN / EIYE_TEST_MARIADB_DSN, which CI
supplies from service containers. Both servers run the same suite on purpose:
"MariaDB is a dialect, not a second SKU" is only an honest claim if something
exercises it.

The DSNs are *admin* credentials. Each live test provisions a schema and a
SELECT-only login from them, then points the connector at the read-only login —
which is how an operator is expected to deploy it, and what the connector
refuses to run without.
"""

import asyncio
import os

import pytest

from eiye_db.connectors.base import ConnectorError
from eiye_db.connectors.mysql import MySQLConnector
from eiye_db.connectors.sql import require_select

pymysql = pytest.importorskip("pymysql")


# --- pure, no server ---------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select 1",
        "  \n SELECT 1",
        "(SELECT 1)",
        "WITH t AS (SELECT 1) SELECT * FROM t",
        "-- a comment\nSELECT 1",
        "/* block */ SELECT 1",
        "# hash comment\nSELECT 1",
    ],
)
def test_require_select_accepts_reads(sql):
    require_select(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE users",
        "INSERT INTO users VALUES (1)",
        "UPDATE users SET id = 1",
        "DELETE FROM users",
        "TRUNCATE TABLE users",
        "GRANT SELECT ON x.* TO 'y'@'%'",
        "CREATE TABLE t (i INT)",
    ],
)
def test_require_select_rejects_writes(sql):
    with pytest.raises(ConnectorError, match="only SELECT"):
        require_select(sql)


def test_require_select_does_not_skip_executable_comments():
    # MySQL *runs* the contents of /*! ... */, so it is not a comment and must
    # not be treated as leading noise to look past.
    with pytest.raises(ConnectorError, match="only SELECT"):
        require_select("/*!80000 DROP TABLE users */")


def test_missing_dsn():
    with pytest.raises(ConnectorError, match="dsn"):
        asyncio.run(MySQLConnector({}).test_connection())


def test_rejects_non_mysql_scheme():
    with pytest.raises(ConnectorError, match="mysql://"):
        asyncio.run(MySQLConnector({"dsn": "postgresql://u:p@h:5432/db"}).test_connection())


def test_requires_a_database_in_the_dsn():
    with pytest.raises(ConnectorError, match="must name a database"):
        asyncio.run(MySQLConnector({"dsn": "mysql://u:p@h:3306/"}).test_connection())


def test_missing_sql():
    with pytest.raises(ConnectorError, match="sql"):
        asyncio.run(MySQLConnector({"dsn": "mysql://u:p@h:3306/db"}).query({}, limit=10))


def test_dsn_parsing():
    params = MySQLConnector({"dsn": "mariadb://u%40corp:p%3Aw@db.internal:3307/sales"})._params()
    assert params == {
        "host": "db.internal",
        "port": 3307,
        "user": "u@corp",       # percent-decoded, so an @ in the username survives
        "password": "p:w",
        "database": "sales",
    }


def test_dsn_defaults_port_3306():
    assert MySQLConnector({"dsn": "mysql://u:p@h/db"})._params()["port"] == 3306


# --- live ---------------------------------------------------------------------

RO_USER, RO_PASSWORD = "eiye_ro", "ro"
BREAKOUT = "SELECT 1) _x LIMIT 1; DROP TABLE users; SELECT * FROM (SELECT 1"

# Read at import, like test_connectors_pg.py: conftest's autouse _clear_eiye_env
# deletes every EIYE_* variable before fixtures run, so reading these inside a
# fixture would always find them unset and silently skip every live test.
LIVE_DSNS = {
    "mysql": os.environ.get("EIYE_TEST_MYSQL_DSN"),
    "mariadb": os.environ.get("EIYE_TEST_MARIADB_DSN"),
}


@pytest.fixture(params=sorted(LIVE_DSNS))
def admin_dsn(request):
    dsn = LIVE_DSNS[request.param]
    if not dsn:
        pytest.skip(f"EIYE_TEST_{request.param.upper()}_DSN not set")
    return dsn


@pytest.fixture
def admin(admin_dsn):
    params = MySQLConnector({"dsn": admin_dsn})._params()
    conn = pymysql.connect(**params, client_flag=0)
    # Namespaced so it cannot shadow an attribute pymysql maintains itself.
    conn.eiye_params = params
    yield conn
    conn.close()


@pytest.fixture
def ro_dsn(admin):
    """Provision a schema plus a SELECT-only login; return that login's DSN."""
    with admin.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS orders")
        cur.execute("DROP TABLE IF EXISTS users")
        cur.execute("CREATE TABLE users (id INT PRIMARY KEY, email VARCHAR(120))")
        cur.execute(
            "CREATE TABLE orders (id INT PRIMARY KEY, user_id INT, "
            "CONSTRAINT fk_orders_user FOREIGN KEY (user_id) REFERENCES users (id))"
        )
        cur.execute("INSERT INTO users VALUES (1, 'a@example.com'), (2, 'b@example.com')")
        cur.execute("INSERT INTO orders VALUES (10, 1), (11, 2)")
        cur.execute(f"DROP USER IF EXISTS '{RO_USER}'@'%'")
        cur.execute(f"CREATE USER '{RO_USER}'@'%' IDENTIFIED BY '{RO_PASSWORD}'")
        cur.execute(f"GRANT SELECT ON `{admin.eiye_params['database']}`.* TO '{RO_USER}'@'%'")
        cur.execute("FLUSH PRIVILEGES")
    admin.commit()
    p = admin.eiye_params
    return f"mysql://{RO_USER}:{RO_PASSWORD}@{p['host']}:{p['port']}/{p['database']}"


def table_exists(admin, name):
    with admin.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() "
                    "AND TABLE_NAME = %s", (name,))
        return cur.fetchone()[0] == 1


def row_count(admin, name):
    admin.commit()  # see writes committed by other connections
    with admin.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM `{name}`")
        return cur.fetchone()[0]


def test_live_refuses_a_write_capable_login(admin_dsn):
    # The admin DSN can write, so the connector must refuse it outright rather
    # than trust the read-only transaction to contain it.
    with pytest.raises(ConnectorError, match="can write"):
        asyncio.run(MySQLConnector({"dsn": admin_dsn}).test_connection())


def test_live_connection_does_not_negotiate_multi_statements(ro_dsn):
    # Pins the mechanism, not just the outcome. The behavioural breakout test
    # below passes if *any* layer holds, so it would stay green if someone
    # swapped the driver back to aiomysql and left only the grant standing.
    # This asserts the specific property that makes the wrapper a boundary.
    from pymysql.constants import CLIENT

    conn = MySQLConnector({"dsn": ro_dsn})._connect()
    try:
        assert not conn.client_flag & CLIENT.MULTI_STATEMENTS, (
            "the connector negotiated CLIENT_MULTI_STATEMENTS; a caller can now close "
            "the LIMIT wrapper's paren and append its own statement"
        )
    finally:
        conn.close()


def test_live_roundtrip(ro_dsn):
    conn = MySQLConnector({"dsn": ro_dsn})
    asyncio.run(conn.test_connection())
    assert asyncio.run(conn.query({"sql": "SELECT 1 AS one"}, limit=10)) == [{"one": 1}]


def test_live_discover_schema(ro_dsn):
    tables = {t["name"]: t for t in asyncio.run(MySQLConnector({"dsn": ro_dsn}).discover_schema())}
    assert {"users", "orders"} <= set(tables)
    users = {f["name"]: f for f in tables["users"]["fields"]}
    assert users["id"]["is_primary_key"] is True
    assert users["email"]["is_primary_key"] is False
    orders = {f["name"]: f for f in tables["orders"]["fields"]}
    assert orders["user_id"]["is_foreign_key"] is True
    assert orders["id"]["is_foreign_key"] is False


def test_live_discover_relationships(ro_dsn):
    rels = asyncio.run(MySQLConnector({"dsn": ro_dsn}).discover_relationships())
    assert {"from_table": "orders", "from_column": "user_id", "to_table": "users", "to_column": "id"} in rels


def test_live_limit_is_applied(ro_dsn):
    rows = asyncio.run(MySQLConnector({"dsn": ro_dsn}).query({"sql": "SELECT id FROM users"}, limit=1))
    assert len(rows) == 1


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO users VALUES (99, 'x@example.com')",
        "UPDATE users SET email = 'x@example.com'",
        "DELETE FROM users",
    ],
)
def test_live_dml_rejected(admin, ro_dsn, sql):
    with pytest.raises(ConnectorError):
        asyncio.run(MySQLConnector({"dsn": ro_dsn}).query({"sql": sql}, limit=10))
    assert row_count(admin, "users") == 2


@pytest.mark.parametrize("sql", ["DROP TABLE users", "TRUNCATE TABLE users", "CREATE TABLE t_new (i INT)"])
def test_live_ddl_rejected(admin, ro_dsn, sql):
    # A MySQL read-only transaction does NOT cover DDL — CREATE/DROP/TRUNCATE
    # all execute inside one, unlike Postgres. What stops them is the SELECT
    # check, the wrapper, and the read-only login.
    with pytest.raises(ConnectorError):
        asyncio.run(MySQLConnector({"dsn": ro_dsn}).query({"sql": sql}, limit=10))
    assert table_exists(admin, "users")
    assert row_count(admin, "users") == 2
    assert not table_exists(admin, "t_new")


def test_live_multi_statement_breakout_rejected(admin, ro_dsn):
    # The regression that matters: this input closes the LIMIT wrapper's paren
    # and appends its own statement. Under aiomysql it dropped the table on
    # both servers, because aiomysql forces CLIENT_MULTI_STATEMENTS on. PyMySQL
    # leaves it off, so the server sees one malformed statement.
    with pytest.raises(ConnectorError):
        asyncio.run(MySQLConnector({"dsn": ro_dsn}).query({"sql": BREAKOUT}, limit=10))
    assert table_exists(admin, "users")
    assert row_count(admin, "users") == 2
