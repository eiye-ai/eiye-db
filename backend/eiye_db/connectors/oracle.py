"""Oracle connector. Thin-mode python-oracledb, so no Instant Client is needed.

Read-only enforcement is layered, because no single Oracle mechanism is
sufficient. Every claim below was measured against Oracle Free 23.26 (26ai),
not assumed:

1. **A login that cannot write**, checked on every connect
   (`_assert_read_only`). This is the layer that actually holds.
2. **The protocol refuses multiple statements.** `SELECT 1 FROM dual; DROP
   TABLE t` is rejected with ORA-03405 ("no additional text should follow"),
   including when the batch is appended after the wrapper's closing paren. A
   lone trailing semicolon is still accepted, so ordinary SQL keeps working.
   Oracle behaves like Postgres here, not like SQL Server, whose TDS batches
   are the reason that connector's guarantee is weaker.
3. **The derived-table wrapper**, which makes both DDL *and* DML unexpressible:
   inside `SELECT * FROM ( ... ) eiye_q`, `DROP`, `INSERT`, `UPDATE` and
   `DELETE` are all ORA-00903 and `MERGE` is ORA-00907 — measured against a
   login holding every write privilege. Unlike T-SQL, Oracle permits `ORDER BY`
   inside a derived table, so the wrapper does not break ordinary ordered
   queries the way it would on SQL Server.

**There is deliberately no `SET TRANSACTION READ ONLY` here**, unlike the
Postgres and MySQL connectors. Two measurements decided that. It buys nothing:
the statement it blocks is DML, and layer 3 already makes DML a syntax error
inside the wrapper, which no batch can escape past layer 2. And it costs real
availability: an Oracle read-only transaction takes a transaction-level
consistent snapshot, so querying a table whose definition changed moments
earlier fails with ORA-01466 ("table definition has changed") — verified, and
verified to disappear both a few seconds later and immediately without the
read-only transaction. Trading working reads for a layer that blocks nothing
new is the wrong way round. Note also, for anyone tempted to add it back for
defence in depth: like MySQL and unlike Postgres, it would not cover DDL anyway
— a DROP issued inside one destroyed the table.

The MySQL trick of aiming a write at a table that does not exist does **not**
work here: Oracle answers ORA-00942 whether the object is missing or merely
unreadable, so a reader and a writer are indistinguishable. Hence privilege
introspection instead, which is also what the SQL Server connector does.
"""

from typing import Any
from urllib.parse import unquote, urlsplit

import oracledb

from eiye_db.connectors.base import Connector, ConnectorError
from eiye_db.connectors.sql import require_select, rows_to_tables

# Mirrors service.QUERY_TIMEOUT_SECONDS.
_QUERY_TIMEOUT_SECONDS = 30
_CONNECT_TIMEOUT_SECONDS = 10

# System privileges that cannot modify data or schema. An allowlist, not a
# denylist, because it fails closed: a SELECT-only login holds exactly one of
# these (CREATE SESSION), a writer held eight, and SYSTEM holds 289 — so
# enumerating the safe ones is both short and complete, while enumerating the
# dangerous ones never is. ALTER SESSION is included because it is session-local
# (NLS settings, current schema) and extremely common on real accounts;
# excluding it would reject legitimate read-only logins.
_READ_SAFE_SYSTEM_PRIVS = frozenset(
    {
        "CREATE SESSION",
        "ALTER SESSION",
        "SELECT ANY TABLE",
        "READ ANY TABLE",
        "SELECT ANY DICTIONARY",
        "SELECT ANY SEQUENCE",
    }
)

# Object privileges that are reads. INHERIT PRIVILEGES is here because Oracle
# grants it by default on every user's own schema; it governs definer's-rights
# execution, not data modification.
_READ_SAFE_OBJECT_PRIVS = frozenset({"SELECT", "READ", "INHERIT PRIVILEGES"})

_SESSION_PRIVS_SQL = "SELECT privilege FROM session_privs"

# Effective object privileges, which is not the same as granted ones. Three
# sources have to be unioned or the check has a silent hole, all verified:
#   - USER: direct grants.
#   - session roles: USER_TAB_PRIVS misses these entirely. A role carrying
#     INSERT let a "read-only" login write, while the direct-grant view still
#     showed only SELECT.
#   - PUBLIC: a PUBLIC grant of INSERT also worked and was equally invisible.
# The join to ALL_USERS excludes Oracle-maintained schemas, because stock Oracle
# grants DELETE/INSERT/UPDATE to PUBLIC on its own internal objects. Without
# that filter every real database fails the check; with it, the scope is exactly
# "can this login write to your data".
_OBJECT_PRIVS_SQL = """
SELECT DISTINCT p.privilege
FROM all_tab_privs p
JOIN all_users u ON u.username = p.table_schema
WHERE u.oracle_maintained = 'N'
  AND (p.grantee = USER OR p.grantee = 'PUBLIC'
       OR p.grantee IN (SELECT role FROM session_roles))
"""

# Recycle-bin objects (BIN$...) are dropped tables awaiting purge. They are
# accessible and would otherwise appear as real tables in the schema.
_NOT_RECYCLE_BIN = "t.table_name NOT LIKE 'BIN$%'"

_SCHEMA_SQL = f"""
SELECT t.owner, t.table_name, t.column_name, t.data_type
FROM all_tab_columns t
JOIN all_users u ON u.username = t.owner
WHERE u.oracle_maintained = 'N' AND {_NOT_RECYCLE_BIN}
  AND (:schema IS NULL OR t.owner = :schema)
ORDER BY t.owner, t.table_name, t.column_id
"""

_PK_SQL = """
SELECT c.owner, c.table_name, cc.column_name
FROM all_constraints c
JOIN all_cons_columns cc
  ON cc.owner = c.owner AND cc.constraint_name = c.constraint_name
JOIN all_users u ON u.username = c.owner
WHERE c.constraint_type = 'P' AND u.oracle_maintained = 'N'
  AND (:schema IS NULL OR c.owner = :schema)
"""

# r_constraint_name points at the referenced key; joining its columns by
# position pairs multi-column foreign keys correctly.
_FK_SQL = """
SELECT c.owner, c.table_name, cc.column_name, rc.owner, rc.table_name, rcc.column_name
FROM all_constraints c
JOIN all_cons_columns cc
  ON cc.owner = c.owner AND cc.constraint_name = c.constraint_name
JOIN all_constraints rc
  ON rc.owner = c.r_owner AND rc.constraint_name = c.r_constraint_name
JOIN all_cons_columns rcc
  ON rcc.owner = rc.owner AND rcc.constraint_name = rc.constraint_name
 AND rcc.position = cc.position
JOIN all_users u ON u.username = c.owner
WHERE c.constraint_type = 'R' AND u.oracle_maintained = 'N'
  AND (:schema IS NULL OR c.owner = :schema)
ORDER BY c.owner, c.table_name, c.constraint_name, cc.position
"""


def _qualified(owner: str, table: str) -> str:
    """Tables are always schema-qualified. A read-only login typically reads
    someone else's schema, so a bare name would be ambiguous the moment a
    second schema is visible."""
    return f"{owner}.{table}"


class OracleConnector(Connector):
    def _params(self) -> dict[str, Any]:
        dsn = self.config.get("dsn")
        if not dsn:
            raise ConnectorError("oracle config requires 'dsn'")
        parts = urlsplit(dsn)
        if parts.scheme != "oracle":
            raise ConnectorError(f"oracle dsn must start with oracle://, got '{parts.scheme}://'")
        service = unquote(parts.path.lstrip("/"))
        if not service:
            raise ConnectorError(
                "oracle dsn must name a service, e.g. oracle://user:pass@host:1521/FREEPDB1"
            )
        try:
            port = parts.port or 1521
        except ValueError as e:  # non-numeric port
            raise ConnectorError(f"invalid port in oracle dsn: {e}") from e
        return {
            "user": unquote(parts.username or ""),
            "password": unquote(parts.password or ""),
            "dsn": f"{parts.hostname or '127.0.0.1'}:{port}/{service}",
        }

    def _schema(self) -> str | None:
        """Optional narrowing. Unset means every non-Oracle schema the login can
        read, which is what a dedicated reader granted SELECT on one app schema
        actually wants — defaulting to the login's *own* schema would discover
        nothing at all."""
        schema = self.config.get("schema")
        return schema.upper() if schema else None

    async def _connect(self):
        try:
            conn = await oracledb.connect_async(
                **self._params(), tcp_connect_timeout=_CONNECT_TIMEOUT_SECONDS
            )
        except (oracledb.Error, OSError) as e:
            raise ConnectorError(f"connection failed: {e}") from e
        conn.call_timeout = _QUERY_TIMEOUT_SECONDS * 1000
        try:
            await self._assert_read_only(conn)
        except Exception:
            await conn.close()
            raise
        return conn

    @staticmethod
    async def _assert_read_only(conn) -> None:
        """Refuse a login that can write.

        Run on every connect, not once at registration: a privilege granted
        afterwards would otherwise silently remove the guarantee.
        """
        try:
            with conn.cursor() as cur:
                await cur.execute(_SESSION_PRIVS_SQL)
                system = {r[0] for r in await cur.fetchall()}
                await cur.execute(_OBJECT_PRIVS_SQL)
                objects = {r[0] for r in await cur.fetchall()}
        except (oracledb.Error, OSError) as e:
            raise ConnectorError(f"could not verify the login is read-only: {e}") from e

        unsafe = sorted((system - _READ_SAFE_SYSTEM_PRIVS) | (objects - _READ_SAFE_OBJECT_PRIVS))
        if unsafe:
            raise ConnectorError(
                f"datasource login can write ({', '.join(unsafe)}). eiye connects read-only; "
                "grant it CREATE SESSION and SELECT and nothing else, e.g. "
                "CREATE USER eiye IDENTIFIED BY '...'; GRANT CREATE SESSION TO eiye; "
                "GRANT SELECT ON app.customers TO eiye;"
            )

    async def test_connection(self) -> None:
        conn = await self._connect()
        try:
            with conn.cursor() as cur:
                await cur.execute("SELECT 1 FROM dual")
                await cur.fetchall()
        except (oracledb.Error, OSError) as e:
            raise ConnectorError(f"connection test failed: {e}") from e
        finally:
            await conn.close()

    async def _schema_rows(self) -> tuple[list[dict[str, Any]], list[tuple]]:
        schema = self._schema()
        conn = await self._connect()
        try:
            with conn.cursor() as cur:
                await cur.execute(_SCHEMA_SQL, schema=schema)
                columns = [(_qualified(o, t), c, d) for o, t, c, d in await cur.fetchall()]
                await cur.execute(_PK_SQL, schema=schema)
                pks = {(_qualified(o, t), c) for o, t, c in await cur.fetchall()}
                await cur.execute(_FK_SQL, schema=schema)
                fks = [
                    (_qualified(fo, ft), fc, _qualified(to, tt), tc)
                    for fo, ft, fc, to, tt, tc in await cur.fetchall()
                ]
        except (oracledb.Error, OSError) as e:
            raise ConnectorError(f"schema discovery failed: {e}") from e
        finally:
            await conn.close()
        fk_cols = {(ft, fc) for ft, fc, _tt, _tc in fks}
        return rows_to_tables(columns, pks, fk_cols), fks

    async def discover_schema(self) -> list[dict[str, Any]]:
        tables, fks = await self._schema_rows()
        # discover_relationships reuses this snapshot, so one discovery pass
        # costs one connection rather than two.
        self._fk_rows = fks
        return tables

    async def discover_relationships(self) -> list[dict[str, Any]]:
        fks = getattr(self, "_fk_rows", None)
        if fks is None:
            _tables, fks = await self._schema_rows()
        return [
            {"from_table": ft, "from_column": fc, "to_table": tt, "to_column": tc}
            for ft, fc, tt, tc in fks
        ]

    async def query(self, request: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        sql = request.get("sql")
        if not sql:
            raise ConnectorError("oracle query requires 'sql'")
        require_select(sql)
        # Bound the scan server-side so `limit` caps memory rather than just the
        # slice returned, and so DDL cannot be expressed at all. The alias has no
        # leading underscore on purpose: Oracle identifiers must start with a
        # letter, and `_eiye_q` is ORA-00911.
        bounded = f"SELECT * FROM ({sql.rstrip().rstrip(';')}) eiye_q FETCH FIRST :eiye_limit ROWS ONLY"
        conn = await self._connect()
        try:
            with conn.cursor() as cur:
                # No SET TRANSACTION READ ONLY on purpose — see the module
                # docstring. It would block only DML, which the wrapper already
                # makes unexpressible, and would fail valid reads with ORA-01466
                # for seconds after any DDL on the table.
                await cur.execute(bounded, eiye_limit=limit)
                names = [d[0] for d in cur.description]
                rows = [dict(zip(names, r)) for r in await cur.fetchall()]
            await conn.rollback()
            return rows
        except (oracledb.Error, OSError) as e:
            raise ConnectorError(f"query failed: {e}") from e
        finally:
            await conn.close()
