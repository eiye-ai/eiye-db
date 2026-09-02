"""PostgreSQL connector tests.

Live tests are gated on EIYE_TEST_PG_DSN, which CI supplies from a postgres:16
container. The DSN is an *admin* credential: each live test provisions a scratch
database, a SELECT-only login and a write-capable one, then points the connector
at the read-only login — which is how an operator is expected to deploy it, and
what the connector now refuses to run without.

The tests worth reading are the ones that take a layer away. This connector was
documented for a long time as the engine where the read-only transaction is the
whole boundary, and it is not: `test_live_read_only_transaction_permits_copy_to_program`
and `test_live_superuser_can_write_through_dblink` are the two measurements that
disproved it, and they are pinned here so the claim cannot drift back.
"""

import asyncio
import os
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from eiye_db.connectors.base import ConnectorError
from eiye_db.connectors.postgres import PostgresConnector, fk_rows_to_relationships, rows_to_tables


# --- pure, no server ---------------------------------------------------------


def test_rows_to_tables():
    columns = [
        ("users", "id", "integer"),
        ("users", "email", "text"),
        ("orders", "id", "integer"),
        ("orders", "user_id", "integer"),
    ]
    pks = {("users", "id"), ("orders", "id")}
    fk_cols = {("orders", "user_id")}
    tables = rows_to_tables(columns, pks, fk_cols)
    by_name = {t["name"]: t for t in tables}
    assert by_name["users"]["fields"] == [
        {"name": "id", "type": "integer", "is_primary_key": True, "is_foreign_key": False},
        {"name": "email", "type": "text", "is_primary_key": False, "is_foreign_key": False},
    ]
    assert by_name["orders"]["fields"][1] == {
        "name": "user_id",
        "type": "integer",
        "is_primary_key": False,
        "is_foreign_key": True,
    }


def test_fk_rows_to_relationships():
    rows = [("orders", "user_id", "users", "id")]
    assert fk_rows_to_relationships(rows) == [
        {"from_table": "orders", "from_column": "user_id", "to_table": "users", "to_column": "id"}
    ]


def test_missing_dsn():
    conn = PostgresConnector({})
    with pytest.raises(ConnectorError, match="dsn"):
        asyncio.run(conn.test_connection())


def test_missing_sql():
    conn = PostgresConnector({"dsn": "postgresql://localhost/x"})
    with pytest.raises(ConnectorError, match="sql"):
        asyncio.run(conn.query({}, limit=10))


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE customers",
        "UPDATE customers SET name = 'x'",
        "COPY customers TO PROGRAM 'cat'",
        "INSERT INTO customers VALUES (1)",
    ],
)
def test_non_select_is_refused_before_connecting(sql):
    """require_select runs ahead of _connect, so an unreachable host still gives
    the useful error. Postgres was the only SQL connector not calling it."""
    conn = PostgresConnector({"dsn": "postgresql://nobody@127.0.0.1:1/x"})
    with pytest.raises(ConnectorError, match="only SELECT"):
        asyncio.run(conn.query({"sql": sql}, limit=10))


# --- live ---------------------------------------------------------------------

# Read at import: conftest's autouse _clear_eiye_env deletes every EIYE_*
# variable before fixtures run, so reading this inside a fixture would always
# find it unset and silently skip every live test.
ADMIN_DSN = os.environ.get("EIYE_TEST_PG_DSN")

DB = "eiye_pg_test"
RO_USER, RO_PASSWORD = "eiye_pg_ro", "ro"
RW_USER, RW_PASSWORD = "eiye_pg_rw", "rw"

# Closes the wrapper's paren and appends a second statement. The payload is an
# INSERT rather than a DROP because DROP needs *ownership*, which GRANT ALL does
# not confer — a refused DROP would demonstrate authorization holding, not the
# protocol. The LIMIT $1 placeholder is left intact for the same reason: an
# attempt that removes it is refused for a missing bind, proving nothing.
BREAKOUT = (
    "SELECT 1) _a LIMIT $1; "
    "INSERT INTO customers VALUES (99, 'via-batch', 'batch@example.com'); "
    "SELECT * FROM (SELECT 1"
)
SMUGGLED = "SELECT count(*) FROM customers WHERE id = 99"


def _dsn_as(user: str, password: str, database: str = DB) -> str:
    parts = urlsplit(ADMIN_DSN)
    host = parts.hostname or "127.0.0.1"
    port = f":{parts.port}" if parts.port else ""
    return urlunsplit(("postgresql", f"{user}:{password}@{host}{port}", f"/{database}", "", ""))


class _Sql:
    """Run statements against one DSN, synchronously.

    Every call opens and closes its own connection. Holding one open across
    tests is not an option: `asyncio.run` closes the loop it created, and an
    asyncpg connection bound to a closed loop fails on next use.
    """

    def __init__(self, dsn: str):
        self.dsn = dsn

    def exec(self, *statements: str, ignore_errors: bool = False) -> None:
        asyncio.run(self._exec(*statements, ignore_errors=ignore_errors))

    def fetchval(self, sql: str):
        return asyncio.run(self._fetchval(sql))

    async def _exec(self, *statements: str, ignore_errors: bool) -> None:
        conn = await asyncpg.connect(self.dsn)
        try:
            for s in statements:
                try:
                    await conn.execute(s)
                except asyncpg.PostgresError:
                    if not ignore_errors:
                        raise
        finally:
            await conn.close()

    async def _fetchval(self, sql: str):
        conn = await asyncpg.connect(self.dsn)
        try:
            return await conn.fetchval(sql)
        finally:
            await conn.close()


@pytest.fixture
def pg() -> _Sql:
    """Rebuild the scratch database, both logins and the fixture data.

    Rebuilt per test rather than shared: several tests grant a privilege in
    order to watch the check catch it, and a leaked grant would make the next
    test pass for the wrong reason.
    """
    if not ADMIN_DSN:
        pytest.skip("EIYE_TEST_PG_DSN not set")
    parts = urlsplit(ADMIN_DSN)
    server = _Sql(ADMIN_DSN)
    server.exec(
        f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)",
        f"DROP ROLE IF EXISTS {RO_USER}",
        f"DROP ROLE IF EXISTS {RW_USER}",
        "DROP ROLE IF EXISTS eiye_pg_writer_role",
    )
    server.exec(
        f"CREATE DATABASE {DB}",
        f"CREATE ROLE {RO_USER} LOGIN PASSWORD '{RO_PASSWORD}'",
        f"CREATE ROLE {RW_USER} LOGIN PASSWORD '{RW_PASSWORD}'",
    )
    db = _Sql(_dsn_as(parts.username, parts.password))
    db.exec(
        "CREATE TABLE customers (id int PRIMARY KEY, name text, email text)",
        "CREATE TABLE orders (id int PRIMARY KEY, customer_id int REFERENCES customers(id), amount numeric)",
        "INSERT INTO customers VALUES (1,'Alice','alice@example.com'), (2,'Bob','bob@example.com')",
        "INSERT INTO orders VALUES (10, 1, 99.5)",
        f"GRANT USAGE ON SCHEMA public TO {RO_USER}, {RW_USER}",
        f"GRANT SELECT ON customers, orders TO {RO_USER}",
        f"GRANT ALL ON customers, orders TO {RW_USER}",
        f"GRANT CREATE ON SCHEMA public TO {RW_USER}",
    )
    return db


@pytest.fixture
def connector(pg):
    return PostgresConnector({"dsn": _dsn_as(RO_USER, RO_PASSWORD)})


def _bounded(sql: str) -> str:
    """The connector's own wrapper, so a test that bypasses the connector still
    attacks the shape the connector actually sends."""
    return f"SELECT * FROM ({sql.rstrip().rstrip(';')}) _eiye_q LIMIT $1"


# --- live: it works ----------------------------------------------------------


def test_live_connection_and_schema(connector):
    asyncio.run(connector.test_connection())
    tables = {t["name"]: t for t in asyncio.run(connector.discover_schema())}
    assert set(tables) == {"customers", "orders"}
    customers = {f["name"]: f for f in tables["customers"]["fields"]}
    assert customers["id"]["is_primary_key"] is True
    assert customers["name"]["type"] == "text"
    assert {f["name"]: f["is_foreign_key"] for f in tables["orders"]["fields"]}["customer_id"] is True


def test_live_foreign_keys(connector):
    asyncio.run(connector.discover_schema())
    assert asyncio.run(connector.discover_relationships()) == [
        {"from_table": "orders", "from_column": "customer_id", "to_table": "customers", "to_column": "id"}
    ]


def test_live_query_and_limit(connector):
    rows = asyncio.run(connector.query({"sql": "SELECT id, name FROM customers ORDER BY id"}, 10))
    assert rows == [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]
    assert len(asyncio.run(connector.query({"sql": "SELECT id FROM customers"}, 1))) == 1


def test_live_order_by_survives_the_wrapper(connector):
    """T-SQL rejects ORDER BY inside a derived table, which is why the SQL Server
    connector cannot use this wrapper at all. Postgres permits it, so the
    wrapper is free here — pin that, because it is the reason the layer exists."""
    rows = asyncio.run(connector.query({"sql": "SELECT name FROM customers ORDER BY name DESC"}, 10))
    assert [r["name"] for r in rows] == ["Bob", "Alice"]


# --- live: the login is the boundary ------------------------------------------


def test_live_superuser_login_is_refused(pg):
    """The admin DSN is what an operator reaches for first, and it is exactly
    the credential that can escape every other layer — see the dblink and
    pg_read_file tests below."""
    with pytest.raises(ConnectorError, match="superuser"):
        asyncio.run(PostgresConnector({"dsn": ADMIN_DSN}).test_connection())


def test_live_writer_login_is_refused(pg):
    with pytest.raises(ConnectorError, match="can write"):
        asyncio.run(PostgresConnector({"dsn": _dsn_as(RW_USER, RW_PASSWORD)}).test_connection())


@pytest.mark.parametrize(
    ("label", "grant"),
    [
        ("direct", f"GRANT INSERT ON customers TO {RO_USER}"),
        ("public", "GRANT INSERT ON customers TO PUBLIC"),
        ("predefined role", f"GRANT pg_write_all_data TO {RO_USER}"),
        ("auto-updatable view", f"GRANT INSERT ON customers_v TO {RO_USER}"),
    ],
)
def test_live_write_privilege_is_caught_however_it_is_held(pg, connector, label, grant):
    """Oracle needed a three-way union here because USER_TAB_PRIVS shows only
    direct grants. Postgres needs one query: has_table_privilege reports the
    *effective* privilege, so a grant made directly, to PUBLIC, through a
    predefined role, or on a view over the table is caught the same way. Each
    arm was observed letting the login actually write before the check existed."""
    asyncio.run(connector.test_connection())  # clean to start
    pg.exec("CREATE VIEW customers_v AS SELECT * FROM customers", ignore_errors=True)
    pg.exec(f"GRANT SELECT ON customers_v TO {RO_USER}", grant)
    with pytest.raises(ConnectorError, match="can write"):
        asyncio.run(connector.test_connection())


def test_live_write_privilege_via_role_is_caught(pg, connector):
    """Granted through a role rather than to the login, which is the arm that a
    direct-grant view misses entirely."""
    asyncio.run(connector.test_connection())
    pg.exec(
        "CREATE ROLE eiye_pg_writer_role",
        "GRANT INSERT ON customers TO eiye_pg_writer_role",
        f"GRANT eiye_pg_writer_role TO {RO_USER}",
    )
    with pytest.raises(ConnectorError, match="can write"):
        asyncio.run(connector.test_connection())


def test_live_check_runs_on_every_connect_not_just_registration(pg, connector):
    """A privilege granted after the datasource was registered has to be caught
    too, so the check cannot move to registration time."""
    asyncio.run(connector.query({"sql": "SELECT 1 AS one"}, 10))
    pg.exec(f"GRANT UPDATE ON customers TO {RO_USER}")
    with pytest.raises(ConnectorError, match="can write"):
        asyncio.run(connector.query({"sql": "SELECT 1 AS one"}, 10))


# --- live: what each remaining layer is worth ---------------------------------


@pytest.mark.parametrize(
    ("label", "sql"),
    [
        ("insert", "INSERT INTO customers VALUES (3,'c','c@example.com')"),
        ("update", "UPDATE customers SET name = 'x'"),
        ("delete", "DELETE FROM customers"),
        ("create", "CREATE TABLE made_in_txn (id int)"),
        ("drop", "DROP TABLE customers"),
        ("truncate", "TRUNCATE customers"),
        ("grant", "GRANT SELECT ON customers TO PUBLIC"),
        ("nextval", "SELECT nextval('customers_seq')"),
        ("select into", "SELECT * INTO customers_copy FROM customers"),
    ],
)
def test_live_read_only_transaction_refuses(pg, label, sql):
    """Postgres's read-only transaction is genuinely stronger than MySQL's,
    which does not cover DDL. Run as the login holding every privilege on these
    objects, so a refusal cannot come from authorization instead."""
    pg.exec("CREATE SEQUENCE customers_seq", f"GRANT ALL ON customers_seq TO {RW_USER}")

    async def run():
        conn = await asyncpg.connect(_dsn_as(RW_USER, RW_PASSWORD))
        try:
            async with conn.transaction(readonly=True):
                await conn.execute(sql)
        finally:
            await conn.close()

    with pytest.raises(asyncpg.PostgresError) as exc:
        asyncio.run(run())
    assert "read-only transaction" in str(exc.value)


def test_live_read_only_transaction_permits_copy_to_program(pg):
    """The measurement that demoted this layer from "the boundary" to "one of
    five". COPY ... TO PROGRAM runs a shell command on the database server and a
    read-only transaction does not stop it — only the login check and the
    wrapper do. If a future Postgres starts refusing this, the module docstring
    overstates the risk and should be revisited."""

    async def run():
        conn = await asyncpg.connect(ADMIN_DSN)  # superuser: the privilege exists
        try:
            async with conn.transaction(readonly=True):
                await conn.execute("COPY (SELECT 1) TO PROGRAM 'cat > /dev/null'")
        finally:
            await conn.close()

    asyncio.run(run())  # no exception: the transaction permitted it


def test_live_wrapper_makes_copy_unexpressible(pg):
    """...and this is what actually stops it. Run as a superuser with the
    connector's guards removed, so the refusal is the parser and not a
    privilege."""

    async def run():
        conn = await asyncpg.connect(ADMIN_DSN)
        try:
            async with conn.transaction(readonly=True):
                await conn.fetch(_bounded("COPY (SELECT 1) TO PROGRAM 'cat > /dev/null'"), 5)
        finally:
            await conn.close()

    with pytest.raises(asyncpg.PostgresSyntaxError):
        asyncio.run(run())


def test_live_data_modifying_cte_is_refused(connector):
    """A writing CTE is the one write that is syntactically a SELECT, so
    require_select passes it through. The wrapper rejects it: Postgres requires
    a data-modifying WITH to be at the top level."""
    with pytest.raises(ConnectorError):
        asyncio.run(
            connector.query(
                {"sql": "WITH w AS (INSERT INTO customers VALUES (7,'g','g@x') RETURNING id) SELECT * FROM w"},
                10,
            )
        )


def test_live_statement_batch_cannot_escape_the_wrapper(pg, connector):
    # Matching the message matters: it shows the batch was refused at parse
    # time, rather than executing and then being stopped by the read-only
    # login, which would leave the wrapper untested.
    with pytest.raises(ConnectorError, match="multiple commands"):
        asyncio.run(connector.query({"sql": BREAKOUT}, 10))
    assert pg.fetchval(SMUGGLED) == 0, "the batch smuggled a write through"


def test_live_fetch_is_what_refuses_the_batch(pg):
    """Test the mechanism, not just the outcome. The test above passes if any
    layer holds; this one shows which. `fetch` always prepares, and the extended
    query protocol cannot carry two statements — while `execute` with no
    arguments takes the simple protocol and runs both halves, dropping the
    table. Run as the login allowed to drop it, so the difference is the
    protocol and not a privilege.

    This is the test that stops the query path being 'simplified' to `execute`.
    """

    async def via(method: str, sql: str, *args):
        conn = await asyncpg.connect(_dsn_as(RW_USER, RW_PASSWORD))
        try:
            await getattr(conn, method)(sql, *args)
        finally:
            await conn.close()

    # As the connector sends it.
    with pytest.raises(asyncpg.PostgresSyntaxError, match="cannot insert multiple commands"):
        asyncio.run(via("fetch", _bounded(BREAKOUT), 10))
    assert pg.fetchval(SMUGGLED) == 0

    # The bind parameter is not what does it: fetch refuses the batch without one.
    with pytest.raises(asyncpg.PostgresSyntaxError, match="cannot insert multiple commands"):
        asyncio.run(via("fetch", _bounded(BREAKOUT).replace(" LIMIT $1", "")))
    assert pg.fetchval(SMUGGLED) == 0

    # execute() with no arguments does: simple protocol, every statement runs.
    asyncio.run(via("execute", _bounded(BREAKOUT).replace("$1", "10")))
    assert pg.fetchval(SMUGGLED) == 1, (
        "the simple protocol no longer executes batches; the docstring overstates the risk"
    )


def test_live_superuser_can_write_through_dblink(pg):
    """Why a superuser DSN is refused outright rather than trusted to the
    transaction. dblink opens a *second* session, and the read-only transaction
    is scoped to the first, so this INSERT commits — through the connector's own
    wrapper, as a plain SELECT that require_select accepts. The connector never
    reaches this because _assert_read_only rejects the login first, which is the
    point."""
    pg.exec("CREATE EXTENSION IF NOT EXISTS dblink")
    parts = urlsplit(ADMIN_DSN)
    # The server dials itself, so it needs the port it is listening on, not the
    # one the client reached it through — those differ whenever the server runs
    # in a container with a remapped port, which is the normal local setup.
    server_port = pg.fetchval("SELECT current_setting('port')")
    target = (
        f"dbname={DB} user={parts.username} password={parts.password} "
        f"host=127.0.0.1 port={server_port}"
    )

    async def run():
        conn = await asyncpg.connect(_dsn_as(parts.username, parts.password))
        try:
            async with conn.transaction(readonly=True):
                await conn.fetch(
                    _bounded(
                        f"SELECT dblink_exec('{target}', "
                        "'INSERT INTO customers VALUES (77,''via-dblink'',''d@x'')')"
                    ),
                    5,
                )
        finally:
            await conn.close()

    asyncio.run(run())
    assert pg.fetchval("SELECT count(*) FROM customers WHERE id = 77") == 1

    # And the connector refuses that credential before any of it can happen.
    with pytest.raises(ConnectorError, match="superuser"):
        asyncio.run(PostgresConnector({"dsn": _dsn_as(parts.username, parts.password)}).test_connection())
