"""SQL Server connector. Operator SQL-auth connection string; SELECT only.

**SQL Server has no read-only transaction.** Postgres has one that covers DDL,
MySQL has one that covers DML only, and SQL Server has nothing equivalent —
`SET TRANSACTION ISOLATION LEVEL` controls concurrency, not writability. So the
layering that protects the other two connectors does not transfer, and this
connector's guarantee rests almost entirely on one thing:

1. **A login that provably cannot write**, verified on every connect. This is
   the boundary. Everything else below is friction, not enforcement.
2. `require_select` and `reject_multiple_statements`, which make the boundary
   harder to reach and turn mistakes into clear errors.

Two behaviours were measured rather than assumed, and both shaped the design:

* **TDS transmits multi-statement batches, and there is no way to disable it.**
  `SELECT 1; DROP TABLE probe` executes as one batch; so does the same thing
  appended after a closing paren. Against a write-capable login the table was
  dropped. Against a read-only login the batch ran and the DROP was refused —
  permissions, and nothing else, were what saved it.
* **Wrapping the query in a derived table is net-negative here.** It is what
  bounds rows on Postgres and MySQL, but T-SQL forbids `ORDER BY` inside a
  derived table (error 1033), so wrapping breaks ordinary queries — and since
  batches escape it anyway, it buys nothing a text check does not. Rows are
  bounded with `SET ROWCOUNT` instead, which is server-side and leaves
  `ORDER BY` working.

Scope is deliberately SQL auth only: not SSPI, not Azure AD, not CDC.
"""

import asyncio
from typing import Any
from urllib.parse import unquote, urlsplit

import pymssql

from eiye_db.connectors.base import Connector, ConnectorError
from eiye_db.connectors.sql import reject_multiple_statements, require_select, rows_to_tables

# Mirrors service.QUERY_TIMEOUT_SECONDS; see the note in mysql.py for why it is
# duplicated rather than imported, and why the driver needs its own bound.
_QUERY_TIMEOUT_SECONDS = 30

_SCHEMA_SQL = """
SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = SCHEMA_NAME()
ORDER BY TABLE_NAME, ORDINAL_POSITION
"""

_PK_SQL = """
SELECT kcu.TABLE_NAME, kcu.COLUMN_NAME
FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
  ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME AND tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA
WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY' AND tc.TABLE_SCHEMA = SCHEMA_NAME()
"""

# sys.foreign_key_columns rather than INFORMATION_SCHEMA, so multi-column
# foreign keys pair positionally instead of by constraint name alone.
_FK_SQL = """
SELECT
  OBJECT_NAME(fkc.parent_object_id)     AS from_table,
  pc.name                               AS from_column,
  OBJECT_NAME(fkc.referenced_object_id) AS to_table,
  rc.name                               AS to_column
FROM sys.foreign_key_columns fkc
JOIN sys.columns pc
  ON pc.object_id = fkc.parent_object_id AND pc.column_id = fkc.parent_column_id
JOIN sys.columns rc
  ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id
WHERE SCHEMA_NAME(OBJECTPROPERTY(fkc.parent_object_id, 'SchemaId')) = SCHEMA_NAME()
"""

_ROLE_SQL = """
SELECT
  IS_SRVROLEMEMBER('sysadmin'),
  IS_ROLEMEMBER('db_owner'),
  CAST(DATABASEPROPERTYEX(DB_NAME(), 'Updateability') AS NVARCHAR(50))
"""

_PERMISSIONS_SQL = "SELECT permission_name FROM sys.fn_my_permissions(NULL, 'DATABASE')"

# Allowlist, not a denylist. A db_datareader login holds exactly four database
# permissions; sysadmin holds 87. Enumerating what is safe fails closed on the
# ones nobody thought to exclude, at the cost of refusing an unusual-but-benign
# grant — and the error names the offending permission, so that is diagnosable.
_READ_SAFE_PERMISSIONS = frozenset(
    {
        "CONNECT",
        "SELECT",
        "SHOWPLAN",
        "VIEW ANY COLUMN ENCRYPTION KEY DEFINITION",
        "VIEW ANY COLUMN MASTER KEY DEFINITION",
        "VIEW DATABASE STATE",
        "VIEW DEFINITION",
    }
)


class SQLServerConnector(Connector):
    def _params(self) -> dict[str, Any]:
        dsn = self.config.get("dsn")
        if not dsn:
            raise ConnectorError("sqlserver config requires 'dsn'")
        parts = urlsplit(dsn)
        if parts.scheme not in ("sqlserver", "mssql"):
            raise ConnectorError(f"sqlserver dsn must start with sqlserver:// or mssql://, got '{parts.scheme}://'")
        database = unquote(parts.path.lstrip("/"))
        if not database:
            raise ConnectorError("sqlserver dsn must name a database, e.g. sqlserver://user:pass@host:1433/dbname")
        try:
            port = parts.port or 1433
        except ValueError as e:
            raise ConnectorError(f"invalid port in sqlserver dsn: {e}") from e
        return {
            "server": parts.hostname or "127.0.0.1",
            "port": port,
            "user": unquote(parts.username or ""),
            "password": unquote(parts.password or ""),
            "database": database,
        }

    def _connect(self):
        try:
            conn = pymssql.connect(
                **self._params(),
                login_timeout=10,
                timeout=_QUERY_TIMEOUT_SECONDS,
                # No transaction is opened: SQL Server has no read-only one, so
                # a transaction would imply a guarantee that does not exist.
                autocommit=True,
            )
        except (pymssql.Error, OSError) as e:
            raise ConnectorError(f"connection failed: {e}") from e
        try:
            self._assert_read_only(conn)
        except Exception:
            conn.close()
            raise
        return conn

    @staticmethod
    def _assert_read_only(conn) -> None:
        """Refuse a login that can write.

        Unlike MySQL, probing with a write aimed at a missing table cannot work
        here: SQL Server resolves the object first, so a write-capable login and
        a read-only one both get 208 'Invalid object name' and the two are
        indistinguishable. Verified. So this reads permission metadata instead.

        A database in READ_ONLY state is accepted regardless of the login's
        grants, because the engine will refuse every write anyway.
        """
        try:
            with conn.cursor() as cur:
                cur.execute(_ROLE_SQL)
                sysadmin, db_owner, updateability = cur.fetchone()
                cur.execute(_PERMISSIONS_SQL)
                held = {row[0] for row in cur.fetchall()}
        except pymssql.Error as e:
            raise ConnectorError(f"could not verify the login is read-only: {e}") from e

        if str(updateability).upper() == "READ_ONLY":
            return

        reasons: list[str] = []
        if sysadmin:
            reasons.append("member of the sysadmin server role")
        if db_owner:
            reasons.append("member of the db_owner database role")
        unsafe = sorted(held - _READ_SAFE_PERMISSIONS)
        if unsafe:
            shown = ", ".join(unsafe[:6]) + (f", +{len(unsafe) - 6} more" if len(unsafe) > 6 else "")
            reasons.append(f"holds non-read permissions ({shown})")
        if reasons:
            raise ConnectorError(
                f"datasource login can write: {'; '.join(reasons)}. SQL Server has no read-only "
                "transaction, so eiye relies on the login itself. Create a reader, e.g. "
                "CREATE LOGIN eiye WITH PASSWORD='...'; CREATE USER eiye FOR LOGIN eiye; "
                "ALTER ROLE db_datareader ADD MEMBER eiye;"
            )

    def _test_sync(self) -> None:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchall()
        except pymssql.Error as e:
            raise ConnectorError(f"connection test failed: {e}") from e
        finally:
            conn.close()

    def _schema_sync(self) -> tuple[list[dict[str, Any]], list[tuple]]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(_SCHEMA_SQL)
                columns = [tuple(r) for r in cur.fetchall()]
                cur.execute(_PK_SQL)
                pks = {tuple(r) for r in cur.fetchall()}
                cur.execute(_FK_SQL)
                fks = [tuple(r) for r in cur.fetchall()]
        except pymssql.Error as e:
            raise ConnectorError(f"schema discovery failed: {e}") from e
        finally:
            conn.close()
        fk_cols = {(ft, fc) for ft, fc, _tt, _tc in fks}
        return rows_to_tables(columns, pks, fk_cols), fks

    def _query_sync(self, sql: str, limit: int) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            with conn.cursor(as_dict=True) as cur:
                # Bounds the result server-side without a derived table, so a
                # caller's ORDER BY still parses. Reset afterwards is
                # unnecessary — the connection is closed below.
                cur.execute("SET ROWCOUNT %d", (limit,))
                cur.execute(sql)
                rows = list(cur.fetchall())
                # Defence in depth against a batch that slipped past
                # reject_multiple_statements. pymssql hands back the first
                # result set and never surfaces errors from later statements
                # unless nextset() is called — verified: a refused DROP inside
                # a batch raises 3701 here, not at execute(). Without this, a
                # batch would run its trailing statements and still be reported
                # as a successful single query.
                if cur.nextset():
                    raise ConnectorError("only a single statement is allowed")
                return rows
        except pymssql.Error as e:
            raise ConnectorError(f"query failed: {e}") from e
        finally:
            conn.close()

    async def test_connection(self) -> None:
        await asyncio.to_thread(self._test_sync)

    async def discover_schema(self) -> list[dict[str, Any]]:
        tables, fks = await asyncio.to_thread(self._schema_sync)
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
            raise ConnectorError("sqlserver query requires 'sql'")
        require_select(sql)
        reject_multiple_statements(sql)
        return await asyncio.to_thread(self._query_sync, sql, limit)
