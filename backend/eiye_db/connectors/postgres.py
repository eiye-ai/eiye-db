"""PostgreSQL connector.

Read-only enforcement is layered here too. This connector was written first,
when the read-only transaction was believed to be the whole boundary; probing
it against a live server showed it is not, so the layers are now spelled out.
Verified against PostgreSQL 16:

1. **A login that cannot write**, checked on every connect
   (`_assert_read_only`). This is the layer that actually holds. Postgres needs
   only one question where Oracle needs three, because `has_table_privilege`
   answers the *effective* one: a privilege held directly, through a role,
   through `PUBLIC`, or through `pg_write_all_data` all come back true.
   Superuser is asked separately — a superuser passes every privilege test, but
   an empty database has no table to test, so the flag cannot be left implicit.
2. **`require_select`**, which turns a write into a clear error rather than a
   confusing one about the wrapper below.
3. **The derived-table wrapper**, which makes `COPY`, DDL and data-modifying
   CTEs syntax errors. Load-bearing, not cosmetic: a read-only transaction
   permits `COPY ... TO PROGRAM`, and the wrapper is the only layer that stops
   it.
4. **asyncpg's extended query protocol**, which refuses to parse more than one
   statement, so a caller cannot close the wrapper's paren and append
   `; DROP TABLE ...`. This holds because the query path uses `fetch`, which
   always prepares — see the comment on that call.
5. **The read-only transaction**, which rejects every DML and DDL statement,
   `nextval`, `SELECT INTO` and writes performed inside a function.

Layer 5 is stronger than MySQL's, which does not cover DDL, but it is still not
the boundary. It is scoped to the session, so a function opening a *second*
connection escapes it entirely: as a superuser, `SELECT dblink_exec(...)` runs
an INSERT through the query path above, and `SELECT pg_read_file(...)` returns
the contents of any file the server account can read. Both are plain SELECTs,
so the wrapper permits them. What refuses both is the login's privileges, which
is why layer 1 exists and why a superuser DSN is rejected outright.
"""

from typing import Any

import asyncpg

from eiye_db.connectors.base import Connector, ConnectorError
from eiye_db.connectors.sql import require_select, rows_to_tables

_SCHEMA_SQL = """
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'public'
ORDER BY table_name, ordinal_position
"""

# pg_constraint, not information_schema, and for a reason that only shows up
# against a real deployment: information_schema.table_constraints is restricted
# to tables the login *owns*, so a SELECT-only login — the one this connector
# now requires — saw an empty result and every table came back with no primary
# key at all. The catalog is not privilege-filtered.
_PK_SQL = """
SELECT c.relname, a.attname
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN unnest(con.conkey) AS ck(attnum) ON TRUE
JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = ck.attnum
WHERE con.contype = 'p' AND n.nspname = 'public'
"""

# pg_constraint (not information_schema) so multi-column FKs pair positionally.
_FK_SQL = """
SELECT
  src.relname  AS from_table,
  a.attname    AS from_column,
  tgt.relname  AS to_table,
  b.attname    AS to_column
FROM pg_constraint c
JOIN pg_class src ON src.oid = c.conrelid
JOIN pg_class tgt ON tgt.oid = c.confrelid
JOIN pg_namespace n ON n.oid = c.connamespace
JOIN unnest(c.conkey)  WITH ORDINALITY AS ck(attnum, ord) ON TRUE
JOIN unnest(c.confkey) WITH ORDINALITY AS cf(attnum, ord) ON cf.ord = ck.ord
JOIN pg_attribute a ON a.attrelid = c.conrelid  AND a.attnum = ck.attnum
JOIN pg_attribute b ON b.attrelid = c.confrelid AND b.attnum = cf.attnum
WHERE c.contype = 'f' AND n.nspname = 'public'
"""


# Every relation the login could write to, named so the error can say which.
# `has_table_privilege` reports the effective privilege, so this one query
# covers direct grants, role membership, PUBLIC and pg_write_all_data alike.
# Views are included because an auto-updatable view is a write path to the
# table underneath it. LIMIT bounds the cost: 6.6 ms across 2,069 tables.
_WRITABLE_SQL = r"""
SELECT n.nspname || '.' || c.relname
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'f', 'v')
  AND n.nspname <> 'information_schema'
  AND n.nspname NOT LIKE 'pg\_%'
  AND (has_table_privilege(c.oid, 'INSERT')
    OR has_table_privilege(c.oid, 'UPDATE')
    OR has_table_privilege(c.oid, 'DELETE')
    OR has_table_privilege(c.oid, 'TRUNCATE'))
LIMIT 5
"""

_GRANT_HINT = (
    "eiye connects read-only; grant it SELECT and nothing else, e.g. "
    "CREATE ROLE eiye LOGIN PASSWORD '...'; GRANT CONNECT ON DATABASE mydb TO eiye; "
    "GRANT USAGE ON SCHEMA public TO eiye; "
    "GRANT SELECT ON ALL TABLES IN SCHEMA public TO eiye;"
)


def fk_rows_to_relationships(rows: list[tuple]) -> list[dict[str, Any]]:
    """Map (from_table, from_column, to_table, to_column) rows to the base shape."""
    return [
        {"from_table": ft, "from_column": fc, "to_table": tt, "to_column": tc}
        for ft, fc, tt, tc in rows
    ]


class PostgresConnector(Connector):
    def _dsn(self) -> str:
        dsn = self.config.get("dsn")
        if not dsn:
            raise ConnectorError("postgres config requires 'dsn'")
        return dsn

    async def _connect(self):
        try:
            conn = await asyncpg.connect(self._dsn(), timeout=10)
        except (OSError, asyncpg.PostgresError, ValueError) as e:
            raise ConnectorError(f"connection failed: {e}") from e
        try:
            await self._assert_read_only(conn)
        except Exception:
            await conn.close()
            raise
        return conn

    @staticmethod
    async def _assert_read_only(conn) -> None:
        """Refuse a login that can write.

        Run on every connect rather than once at registration: a privilege
        granted after the datasource was registered would otherwise silently
        remove the guarantee. Two cheap queries on a connection that has
        already paid for TCP setup and auth.

        The superuser check is not redundant with the privilege query. A
        superuser holds every privilege, so the query catches one wherever a
        table exists — but it also reaches past table privileges entirely, to
        `pg_read_file` and `dblink`, both of which run inside the read-only
        transaction. Naming it separately also makes the error say the useful
        thing rather than listing five arbitrary tables.
        """
        try:
            if await conn.fetchval("SELECT current_setting('is_superuser') = 'on'"):
                raise ConnectorError(
                    f"datasource login is a superuser. A superuser bypasses the read-only "
                    f"transaction — pg_read_file reads any file the server account can, and "
                    f"dblink opens a second session that the transaction does not cover. {_GRANT_HINT}"
                )
            writable = [r[0] for r in await conn.fetch(_WRITABLE_SQL)]
        except asyncpg.PostgresError as e:
            raise ConnectorError(f"could not verify the login is read-only: {e}") from e

        if writable:
            raise ConnectorError(
                f"datasource login can write to {', '.join(writable)}. {_GRANT_HINT}"
            )

    async def test_connection(self) -> None:
        conn = await self._connect()
        try:
            await conn.fetchval("SELECT 1")
        finally:
            await conn.close()

    async def discover_schema(self) -> list[dict[str, Any]]:
        conn = await self._connect()
        try:
            columns = [tuple(r) for r in await conn.fetch(_SCHEMA_SQL)]
            pks = {tuple(r) for r in await conn.fetch(_PK_SQL)}
            fks = [tuple(r) for r in await conn.fetch(_FK_SQL)]
        except asyncpg.PostgresError as e:
            raise ConnectorError(f"schema discovery failed: {e}") from e
        finally:
            await conn.close()
        # discover_relationships on this instance reuses the same snapshot, so
        # one discovery pass costs one connection round-trip, not two.
        self._fk_rows = fks
        fk_cols = {(ft, fc) for ft, fc, _tt, _tc in fks}
        return rows_to_tables(columns, pks, fk_cols)

    async def discover_relationships(self) -> list[dict[str, Any]]:
        fks = getattr(self, "_fk_rows", None)
        if fks is None:
            conn = await self._connect()
            try:
                fks = [tuple(r) for r in await conn.fetch(_FK_SQL)]
            except asyncpg.PostgresError as e:
                raise ConnectorError(f"relationship discovery failed: {e}") from e
            finally:
                await conn.close()
        return fk_rows_to_relationships(fks)

    async def query(self, request: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        sql = request.get("sql")
        if not sql:
            raise ConnectorError("postgres query requires 'sql'")
        require_select(sql)
        # Bound the scan server-side so `limit` caps memory, not just the
        # returned slice (fetch() otherwise materializes the whole result set),
        # and so COPY and DDL cannot be expressed at all.
        bounded = f"SELECT * FROM ({sql.rstrip().rstrip(';')}) _eiye_q LIMIT $1"
        conn = await self._connect()
        try:
            # readonly transaction makes writes fail server-side regardless of the SQL text
            async with conn.transaction(readonly=True):
                # `fetch` is load-bearing, not just convenient: it always
                # prepares, and the extended query protocol cannot carry a
                # second statement, so a caller who closes the wrapper's paren
                # and appends `; DROP TABLE ...` gets a parse error. The unsafe
                # neighbour is `execute()` with no arguments, which takes the
                # simple protocol and runs both halves — verified against
                # PostgreSQL 16, table gone. Passing `limit` as a bind
                # parameter would force the extended protocol even through
                # `execute`, but it is `fetch` that holds the line here.
                records = await conn.fetch(bounded, limit)
        except asyncpg.PostgresError as e:
            raise ConnectorError(f"query failed: {e}") from e
        finally:
            await conn.close()
        return [dict(r) for r in records]
