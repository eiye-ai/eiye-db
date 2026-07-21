"""PostgreSQL connector. Queries run inside read-only transactions."""

from typing import Any

import asyncpg

from eiye_db.connectors.base import Connector, ConnectorError

_SCHEMA_SQL = """
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'public'
ORDER BY table_name, ordinal_position
"""

_PK_SQL = """
SELECT kcu.table_name, kcu.column_name
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = 'public'
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


def rows_to_tables(columns: list[tuple], pks: set[tuple], fk_cols: set[tuple] | None = None) -> list[dict[str, Any]]:
    """Group (table, column, type) rows into the connector schema shape."""
    fk_cols = fk_cols or set()
    tables: dict[str, list[dict[str, Any]]] = {}
    for table, column, dtype in columns:
        tables.setdefault(table, []).append(
            {
                "name": column,
                "type": dtype,
                "is_primary_key": (table, column) in pks,
                "is_foreign_key": (table, column) in fk_cols,
            }
        )
    return [{"name": name, "fields": fields} for name, fields in tables.items()]


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
            return await asyncpg.connect(self._dsn(), timeout=10)
        except (OSError, asyncpg.PostgresError, ValueError) as e:
            raise ConnectorError(f"connection failed: {e}") from e

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
        # Bound the scan server-side so `limit` caps memory, not just the
        # returned slice (fetch() otherwise materializes the whole result set).
        bounded = f"SELECT * FROM ({sql.rstrip().rstrip(';')}) _eiye_q LIMIT $1"
        conn = await self._connect()
        try:
            # readonly transaction makes writes fail server-side regardless of the SQL text
            async with conn.transaction(readonly=True):
                records = await conn.fetch(bounded, limit)
        except asyncpg.PostgresError as e:
            raise ConnectorError(f"query failed: {e}") from e
        finally:
            await conn.close()
        return [dict(r) for r in records]
