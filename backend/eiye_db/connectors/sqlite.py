"""SQLite connector. An operator-named database file, opened read-only.

Unlike the other SQL engines here, the guarantee is neither a grant nor a
transaction — it is how the file is opened. `file:...?mode=ro` maps to
SQLITE_OPEN_READONLY, which the library refuses to write through at all, so
`DROP TABLE` fails exactly the way `INSERT` does. That is stronger than MySQL,
whose read-only transaction covers DML but not DDL, and far stronger than SQL
Server, which has no read-only transaction to offer.

The layers, in the order they hold:

1. **`mode=ro`.** Refuses DML and DDL alike, and refuses to create the file, so
   a typo in the path is an error rather than a new empty database.
2. **`PRAGMA query_only`.** `mode=ro` binds the main database only; a later
   `ATTACH` would open its target read-write. `query_only` covers the whole
   connection, attached databases included.
3. **One statement per `execute`.** Python's sqlite3 raises on a second
   statement, so a caller cannot close the LIMIT wrapper's paren and append
   their own — the same property PyMySQL gives the MySQL connector, and the one
   SQL Server cannot have.
4. **`require_select` and the derived-table wrapper**, which turn a rejected
   write into a clear message instead of a confusing one.

Extension loading is off (Python's sqlite3 default) and is never enabled, so
the `load_extension` path to arbitrary code is not open.

A `.db` file is a database, not a filesystem document: the filesystem connector
does not extract it, and this connector does not walk a directory.
"""

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

from eiye_db.connectors.base import Connector, ConnectorError
from eiye_db.connectors.sql import require_select, rows_to_tables

_BUSY_TIMEOUT_SECONDS = 10

# pragma_table_info / pragma_foreign_key_list as table-valued functions, joined
# against sqlite_master, so the whole schema is two statements rather than two
# per table. Available since SQLite 3.16 (2017); every CPython 3.11+ ships well
# past that. Views are included — they are what an analyst usually wants — and
# the sqlite_% internal tables are not.
_SCHEMA_SQL = """
SELECT m.name, p.name, p.type, p.pk
FROM sqlite_master AS m
JOIN pragma_table_info(m.name) AS p
WHERE m.type IN ('table', 'view') AND m.name NOT LIKE 'sqlite_%'
ORDER BY m.name, p.cid
"""

_FK_SQL = """
SELECT m.name, f."from", f."table", f."to"
FROM sqlite_master AS m
JOIN pragma_foreign_key_list(m.name) AS f
WHERE m.type = 'table' AND m.name NOT LIKE 'sqlite_%'
ORDER BY m.name, f.id, f.seq
"""


def _jsonable(value: Any) -> Any:
    """BLOB columns come back as bytes, which do not survive JSON.

    Reported by size rather than content: a governed answer has no use for
    inline binary, and base64 of a megabyte column is worse than useless.
    """
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    return value


class SQLiteConnector(Connector):
    def _uri(self) -> str:
        raw = self.config.get("path")
        if not raw:
            raise ConnectorError("sqlite config requires 'path'")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise ConnectorError(f"sqlite path must be absolute, got '{raw}'")
        # as_uri percent-encodes, so a '?' or '#' in the path cannot be read as
        # the start of the query string and smuggle in a different open mode.
        return f"{path.as_uri()}?mode=ro"

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(
                self._uri(),
                uri=True,
                timeout=_BUSY_TIMEOUT_SECONDS,
                isolation_level=None,
            )
        except sqlite3.Error as e:
            raise ConnectorError(f"connection failed: {e}") from e
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
        except sqlite3.Error as e:
            conn.close()
            raise ConnectorError(f"connection failed: {e}") from e
        return conn

    def _test_sync(self) -> None:
        conn = self._connect()
        try:
            # sqlite_master, not SELECT 1: opening is lazy and SELECT 1 never
            # touches the file, so a text file with a .db suffix passed that
            # test and then failed at discovery. Reading the catalogue forces
            # the header to be parsed, which is the thing being tested.
            conn.execute("SELECT count(*) FROM sqlite_master").fetchall()
        except sqlite3.Error as e:
            raise ConnectorError(f"connection test failed: {e}") from e
        finally:
            conn.close()

    def _schema_sync(self) -> tuple[list[dict[str, Any]], list[tuple]]:
        conn = self._connect()
        try:
            rows = [tuple(r) for r in conn.execute(_SCHEMA_SQL).fetchall()]
            fk_rows = [tuple(r) for r in conn.execute(_FK_SQL).fetchall()]
        except sqlite3.Error as e:
            raise ConnectorError(f"schema discovery failed: {e}") from e
        finally:
            conn.close()

        # A column with no declared type is legal — SQLite is dynamically typed
        # — but an empty string in the catalogue reads as a bug, so name it.
        columns = [(table, column, dtype or "any") for table, column, dtype, _pk in rows]
        pks = {(table, column) for table, column, _dtype, pk in rows if pk}
        fks = _resolve_implicit_targets(fk_rows, pks)
        fk_cols = {(ft, fc) for ft, fc, _tt, _tc in fks}
        return rows_to_tables(columns, pks, fk_cols), fks

    def _query_sync(self, sql: str, limit: int) -> list[dict[str, Any]]:
        # Bound the scan in the engine so `limit` caps work, not just the
        # returned slice, and so DDL cannot be expressed inside the wrapper.
        bounded = f"SELECT * FROM ({sql.rstrip().rstrip(';')}) _eiye_q LIMIT ?"
        conn = self._connect()
        try:
            rows = conn.execute(bounded, (limit,)).fetchall()
        except sqlite3.Error as e:
            raise ConnectorError(f"query failed: {e}") from e
        finally:
            conn.close()
        return [{k: _jsonable(v) for k, v in dict(r).items()} for r in rows]

    async def test_connection(self) -> None:
        await asyncio.to_thread(self._test_sync)

    async def discover_schema(self) -> list[dict[str, Any]]:
        tables, fks = await asyncio.to_thread(self._schema_sync)
        # discover_relationships on this instance reuses the same snapshot, so
        # one discovery pass costs one open of the file, not two.
        self._fk_rows = fks
        return tables

    async def discover_relationships(self) -> list[dict[str, Any]]:
        fks = getattr(self, "_fk_rows", None)
        if fks is None:
            _tables, fks = await asyncio.to_thread(self._schema_sync)
        return [
            {"from_table": ft, "from_column": fc, "to_table": tt, "to_column": tc}
            for ft, fc, tt, tc in fks
        ]

    async def query(self, request: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        sql = request.get("sql")
        if not sql:
            raise ConnectorError("sqlite query requires 'sql'")
        require_select(sql)
        return await asyncio.to_thread(self._query_sync, sql, limit)


def _resolve_implicit_targets(fk_rows: list[tuple], pks: set[tuple]) -> list[tuple]:
    """Fill in the target column SQLite leaves NULL.

    `REFERENCES users` without a column list is legal and means "the parent's
    primary key", which `pragma_foreign_key_list` reports as a NULL `to`.
    Resolving it needs the parent's primary key, which the schema query already
    collected. A composite primary key cannot be paired this way from one row,
    so those are dropped rather than guessed at.
    """
    single_pk = {}
    for table, column in pks:
        single_pk[table] = None if table in single_pk else column
    out = []
    for from_table, from_column, to_table, to_column in fk_rows:
        if to_column is None:
            to_column = single_pk.get(to_table)
            if to_column is None:
                continue
        out.append((from_table, from_column, to_table, to_column))
    return out
