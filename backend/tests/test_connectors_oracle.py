"""Oracle connector tests.

Live tests are gated on EIYE_TEST_ORACLE_DSN, which CI supplies from an Oracle
Free container. The DSN is an *admin* credential: each live test provisions an
app schema and a SELECT-only login from it, then points the connector at the
read-only login — which is how an operator is expected to deploy it, and what
the connector refuses to run without.

The privilege tests are the ones worth reading. Oracle answers ORA-00942 whether
an object is missing or merely unreadable, so a reader and a writer cannot be
told apart by probing the way they can on MySQL. The connector reads privileges
instead, and three ways of holding a write privilege have to be covered or the
check has a silent hole: directly, through a role, and through PUBLIC. All three
were observed letting a "read-only" login write.
"""

import asyncio
import os

import pytest

from eiye_db.connectors.base import ConnectorError

oracledb = pytest.importorskip("oracledb")

from eiye_db.connectors.oracle import OracleConnector  # noqa: E402  (after importorskip)


# --- pure, no server ---------------------------------------------------------


def test_missing_dsn():
    with pytest.raises(ConnectorError, match="dsn"):
        asyncio.run(OracleConnector({}).test_connection())


def test_rejects_non_oracle_scheme():
    with pytest.raises(ConnectorError, match="oracle://"):
        asyncio.run(OracleConnector({"dsn": "postgresql://u:p@h:5432/db"}).test_connection())


def test_requires_a_service_in_the_dsn():
    with pytest.raises(ConnectorError, match="must name a service"):
        asyncio.run(OracleConnector({"dsn": "oracle://u:p@h:1521/"}).test_connection())


def test_missing_sql():
    with pytest.raises(ConnectorError, match="sql"):
        asyncio.run(OracleConnector({"dsn": "oracle://u:p@h:1521/FREE"}).query({}, limit=10))


def test_dsn_parsing():
    params = OracleConnector({"dsn": "oracle://u%40corp:p%3Aw@db.internal:1600/SALESPDB"})._params()
    assert params == {"user": "u@corp", "password": "p:w", "dsn": "db.internal:1600/SALESPDB"}


def test_dsn_defaults_port_1521():
    assert OracleConnector({"dsn": "oracle://u:p@h/FREE"})._params()["dsn"] == "h:1521/FREE"


def test_schema_is_upper_cased():
    """Oracle folds unquoted identifiers to upper case, so a lower-case config
    value would silently match nothing in ALL_TAB_COLUMNS."""
    assert OracleConnector({"dsn": "oracle://u:p@h/F", "schema": "appown"})._schema() == "APPOWN"
    assert OracleConnector({"dsn": "oracle://u:p@h/F"})._schema() is None


# --- live ---------------------------------------------------------------------

# Read at import: conftest's autouse _clear_eiye_env deletes every EIYE_*
# variable before fixtures run, so reading this inside a fixture would always
# find it unset and silently skip every live test.
ADMIN_DSN = os.environ.get("EIYE_TEST_ORACLE_DSN")

RO_USER, RO_PASSWORD = "eiye_ro", "ro"
APP_USER, APP_PASSWORD = "eiye_app", "app"
RW_USER, RW_PASSWORD = "eiye_rw", "rw"

# Leaves the connector's :eiye_limit placeholder intact on purpose. An attempt
# that comments it out is refused for a missing bind, which would pass this test
# while proving nothing about statement batching.
BREAKOUT = "SELECT 1 FROM dual) eiye_a; DROP TABLE eiye_app.customers"


def _admin_params():
    return OracleConnector({"dsn": ADMIN_DSN})._params()


def _exec(conn, *statements, ignore_errors=False):
    with conn.cursor() as cur:
        for s in statements:
            try:
                cur.execute(s)
            except oracledb.Error:
                if not ignore_errors:
                    raise
    conn.commit()


@pytest.fixture
def admin():
    if not ADMIN_DSN:
        pytest.skip("EIYE_TEST_ORACLE_DSN not set")
    conn = oracledb.connect(**_admin_params())
    conn.autocommit = True
    yield conn
    conn.close()


@pytest.fixture
def ro_dsn(admin):
    """Provision an app schema plus a SELECT-only login; return its DSN."""
    _exec(
        admin,
        f"DROP USER {RO_USER} CASCADE",
        f"DROP USER {RW_USER} CASCADE",
        f"DROP USER {APP_USER} CASCADE",
        "DROP ROLE eiye_test_role",
        ignore_errors=True,
    )
    _exec(
        admin,
        f"CREATE USER {APP_USER} IDENTIFIED BY {APP_PASSWORD} QUOTA UNLIMITED ON users",
        f"GRANT CREATE SESSION, CREATE TABLE TO {APP_USER}",
        f"CREATE USER {RO_USER} IDENTIFIED BY {RO_PASSWORD}",
        f"GRANT CREATE SESSION TO {RO_USER}",
        f"CREATE USER {RW_USER} IDENTIFIED BY {RW_PASSWORD} QUOTA UNLIMITED ON users",
        f"GRANT CREATE SESSION, CREATE ANY TABLE, DROP ANY TABLE, INSERT ANY TABLE TO {RW_USER}",
    )
    host = _admin_params()["dsn"]
    app = oracledb.connect(user=APP_USER, password=APP_PASSWORD, dsn=host)
    app.autocommit = True
    _exec(
        app,
        "CREATE TABLE customers (id NUMBER PRIMARY KEY, name VARCHAR2(50), email VARCHAR2(80))",
        "CREATE TABLE orders (id NUMBER PRIMARY KEY, "
        "customer_id NUMBER REFERENCES customers(id), amount NUMBER)",
        "INSERT INTO customers VALUES (1, 'Alice', 'alice@example.com')",
        "INSERT INTO customers VALUES (2, 'Bob', 'bob@example.com')",
        "INSERT INTO orders VALUES (10, 1, 99.5)",
        f"GRANT SELECT ON customers TO {RO_USER}",
        f"GRANT SELECT ON orders TO {RO_USER}",
    )
    app.close()
    yield f"oracle://{RO_USER}:{RO_PASSWORD}@{host}"
    _exec(admin, f"DROP USER {RO_USER} CASCADE", f"DROP USER {RW_USER} CASCADE",
          f"DROP USER {APP_USER} CASCADE", "DROP ROLE eiye_test_role", ignore_errors=True)


@pytest.fixture
def connector(ro_dsn):
    return OracleConnector({"dsn": ro_dsn, "schema": APP_USER.upper()})


def test_live_connection_and_schema(connector):
    asyncio.run(connector.test_connection())
    tables = {t["name"]: t for t in asyncio.run(connector.discover_schema())}
    assert set(tables) == {f"{APP_USER.upper()}.CUSTOMERS", f"{APP_USER.upper()}.ORDERS"}
    customers = {f["name"]: f for f in tables[f"{APP_USER.upper()}.CUSTOMERS"]["fields"]}
    assert customers["ID"]["is_primary_key"] is True
    assert customers["NAME"]["type"] == "VARCHAR2"


def test_live_foreign_keys(connector):
    asyncio.run(connector.discover_schema())
    assert asyncio.run(connector.discover_relationships()) == [
        {
            "from_table": f"{APP_USER.upper()}.ORDERS",
            "from_column": "CUSTOMER_ID",
            "to_table": f"{APP_USER.upper()}.CUSTOMERS",
            "to_column": "ID",
        }
    ]


def test_live_query_and_limit(connector):
    rows = asyncio.run(connector.query({"sql": f"SELECT id, name FROM {APP_USER}.customers ORDER BY id"}, 10))
    assert rows == [{"ID": 1, "NAME": "Alice"}, {"ID": 2, "NAME": "Bob"}]
    assert len(asyncio.run(connector.query({"sql": f"SELECT id FROM {APP_USER}.customers"}, 1))) == 1


def test_live_order_by_survives_the_wrapper(connector):
    """T-SQL rejects ORDER BY inside a derived table, which is why the SQL Server
    connector cannot use this wrapper at all. Oracle permits it, so the wrapper
    is free here — pin that, because it is the reason the layer exists."""
    rows = asyncio.run(
        connector.query({"sql": f"SELECT name FROM {APP_USER}.customers ORDER BY name DESC"}, 10)
    )
    assert [r["NAME"] for r in rows] == ["Bob", "Alice"]


def test_live_writer_login_is_refused(ro_dsn):
    host = _admin_params()["dsn"]
    rw = OracleConnector({"dsn": f"oracle://{RW_USER}:{RW_PASSWORD}@{host}"})
    with pytest.raises(ConnectorError, match="can write"):
        asyncio.run(rw.test_connection())


def test_live_write_privilege_via_role_is_caught(admin, connector, ro_dsn):
    """USER_TAB_PRIVS shows only direct grants. A role carrying INSERT let a
    login that looked read-only actually write, so the check unions session
    roles — verified by observing the write succeed before the fix."""
    asyncio.run(connector.test_connection())  # clean to start
    _exec(admin, "CREATE ROLE eiye_test_role", f"GRANT INSERT ON {APP_USER}.customers TO eiye_test_role",
          f"GRANT eiye_test_role TO {RO_USER}")
    with pytest.raises(ConnectorError, match="can write"):
        asyncio.run(connector.test_connection())


def test_live_write_privilege_via_public_is_caught(admin, connector):
    """A PUBLIC grant is equally invisible to the direct-grant view, and equally
    effective. Stock Oracle grants writes to PUBLIC on its *own* objects, which
    is why the query is scoped to non-Oracle-maintained schemas rather than
    refusing every PUBLIC write grant."""
    asyncio.run(connector.test_connection())
    _exec(admin, f"GRANT INSERT ON {APP_USER}.customers TO PUBLIC")
    try:
        with pytest.raises(ConnectorError, match="can write"):
            asyncio.run(connector.test_connection())
    finally:
        _exec(admin, f"REVOKE INSERT ON {APP_USER}.customers FROM PUBLIC", ignore_errors=True)


def test_live_statement_batch_cannot_escape_the_wrapper(connector, admin):
    with pytest.raises(ConnectorError):
        asyncio.run(connector.query({"sql": BREAKOUT}, 10))
    with admin.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM all_tables WHERE owner = :o AND table_name = 'CUSTOMERS'",
            o=APP_USER.upper(),
        )
        assert cur.fetchone()[0] == 1, "the batch dropped the table"


def test_live_server_itself_refuses_the_batch(ro_dsn):
    """Test the mechanism, not just the outcome. The test above passes if *any*
    layer holds; this one removes every connector guard and shows the server
    rejecting the batch on its own (ORA-03405), which is the claim the module
    docstring makes."""
    host = _admin_params()["dsn"]
    conn = oracledb.connect(user=RW_USER, password=RW_PASSWORD, dsn=host)
    raw = (
        f"SELECT * FROM (SELECT 1 FROM dual) eiye_a; DROP TABLE {APP_USER}.customers) "
        "eiye_q FETCH FIRST :eiye_limit ROWS ONLY"
    )
    try:
        with conn.cursor() as cur:
            with pytest.raises(oracledb.Error) as exc:
                cur.execute(raw, eiye_limit=5)
        assert exc.value.args[0].code == 3405
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("label", "inner"),
    [
        ("insert", "INSERT INTO {app}.customers VALUES (9, 'x', 'y')"),
        ("update", "UPDATE {app}.customers SET name = 'x'"),
        ("delete", "DELETE FROM {app}.customers"),
        ("merge", "MERGE INTO {app}.customers d USING dual s ON (d.id = 1) "
                  "WHEN MATCHED THEN UPDATE SET d.name = 'x'"),
        ("drop", "DROP TABLE {app}.customers"),
    ],
)
def test_live_wrapper_makes_writes_unexpressible(ro_dsn, label, inner):
    """This is why the connector carries no SET TRANSACTION READ ONLY: the
    wrapper already makes every write a *syntax* error, so the read-only
    transaction would block nothing new. Run as a login holding every write
    privilege, so a refusal cannot come from authorization instead.

    If a future Oracle release starts accepting DML inside a derived table, this
    fails and the read-only transaction has to come back."""
    host = _admin_params()["dsn"]
    conn = oracledb.connect(user=RW_USER, password=RW_PASSWORD, dsn=host)
    sql = f"SELECT * FROM ({inner.format(app=APP_USER)}) eiye_q FETCH FIRST :eiye_limit ROWS ONLY"
    try:
        with conn.cursor() as cur:
            with pytest.raises(oracledb.Error) as exc:
                cur.execute(sql, eiye_limit=5)
        # ORA-00903 invalid table name / ORA-00907 missing right parenthesis:
        # the parser rejects it before privileges are ever consulted.
        assert exc.value.args[0].code in (903, 907)
    finally:
        conn.close()


def test_live_query_works_immediately_after_ddl(admin, connector):
    """The cost a read-only transaction would add, pinned. Under one, a table
    whose definition changed moments earlier fails with ORA-01466 for a few
    seconds. Without one it reads fine, which is the behaviour operators expect
    and the reason the layer was dropped."""
    _exec(admin, f"CREATE TABLE {APP_USER}.freshly_made (id NUMBER)", ignore_errors=True)
    _exec(admin, f"INSERT INTO {APP_USER}.freshly_made VALUES (1)",
          f"GRANT SELECT ON {APP_USER}.freshly_made TO {RO_USER}")
    rows = asyncio.run(connector.query({"sql": f"SELECT id FROM {APP_USER}.freshly_made"}, 10))
    assert rows == [{"ID": 1}]


def test_live_non_select_is_refused(connector):
    for sql in (f"DROP TABLE {APP_USER}.customers", f"UPDATE {APP_USER}.customers SET name = 'x'"):
        with pytest.raises(ConnectorError, match="only SELECT"):
            asyncio.run(connector.query({"sql": sql}, 10))
