"""MySQL / MariaDB connector. One type covers both — MariaDB is a dialect, not a second SKU.

Read-only enforcement here is layered, because no single MySQL mechanism is
sufficient. Verified against MySQL 8.4 and MariaDB 11.8:

1. **A login that cannot write**, checked on every connect (`_assert_read_only`).
   This is the layer that actually holds; the rest are defence in depth.
2. **The driver refuses multiple statements.** PyMySQL leaves
   CLIENT_MULTI_STATEMENTS off, so a caller cannot close the wrapper's paren
   and append `; DROP TABLE ...`. aiomysql is unusable here for exactly this
   reason: it ORs the flag in unconditionally with no opt-out, which turns the
   wrapper into decoration.
3. **The derived-table wrapper**, which makes DDL unexpressible — `DROP`,
   `TRUNCATE` and `SELECT ... INTO OUTFILE` are all syntax errors inside
   `SELECT * FROM ( ... ) _eiye_q`.
4. **START TRANSACTION READ ONLY**, which rejects INSERT/UPDATE/DELETE.

Note what layer 4 does *not* do: unlike Postgres, a MySQL read-only
transaction does not cover DDL. CREATE, DROP, TRUNCATE, GRANT and SET GLOBAL
all execute inside one. That is why layers 1-3 exist rather than relying on the
transaction the way the Postgres connector does.
"""

import asyncio
from typing import Any
from urllib.parse import unquote, urlsplit

import pymysql

from eiye_db.connectors.base import Connector, ConnectorError
from eiye_db.connectors.sql import require_select, rows_to_tables

# Mirrors service.QUERY_TIMEOUT_SECONDS. Duplicated rather than imported
# because service imports connectors, and the value has to bound the driver's
# own socket read: queries run in a worker thread, and asyncio.timeout cancels
# the await without stopping the thread, so without this a runaway query would
# leak a thread until the server finished.
_READ_TIMEOUT_SECONDS = 30

_SCHEMA_SQL = """
SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
ORDER BY TABLE_NAME, ORDINAL_POSITION
"""

_PK_SQL = """
SELECT TABLE_NAME, COLUMN_NAME
FROM information_schema.KEY_COLUMN_USAGE
WHERE TABLE_SCHEMA = DATABASE() AND CONSTRAINT_NAME = 'PRIMARY'
"""

# Each row already carries both endpoints, so multi-column foreign keys need no
# positional pairing the way pg_constraint's parallel arrays do.
_FK_SQL = """
SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
FROM information_schema.KEY_COLUMN_USAGE
WHERE TABLE_SCHEMA = DATABASE() AND REFERENCED_TABLE_NAME IS NOT NULL
ORDER BY TABLE_NAME, CONSTRAINT_NAME, ORDINAL_POSITION
"""

# Tables that do not exist, so every probe below is inert.
_ABSENT = "_eiye_readonly_probe_absent"
_ABSENT_2 = "_eiye_readonly_probe_absent_src"

_WRITE_PROBES = (
    ("DELETE", f"DELETE FROM {_ABSENT} WHERE 1=0"),
    ("INSERT", f"INSERT INTO {_ABSENT} (i) VALUES (1)"),
    ("UPDATE", f"UPDATE {_ABSENT} SET i = 1 WHERE 1=0"),
    ("DROP", f"DROP TABLE {_ABSENT}"),
    ("ALTER", f"ALTER TABLE {_ABSENT} ADD COLUMN x INT"),
    ("CREATE", f"CREATE TABLE {_ABSENT} LIKE {_ABSENT_2}"),
)

_ER_ACCESS_DENIED = 1142


class MySQLConnector(Connector):
    def _params(self) -> dict[str, Any]:
        dsn = self.config.get("dsn")
        if not dsn:
            raise ConnectorError("mysql config requires 'dsn'")
        parts = urlsplit(dsn)
        if parts.scheme not in ("mysql", "mariadb"):
            raise ConnectorError(f"mysql dsn must start with mysql:// or mariadb://, got '{parts.scheme}://'")
        database = unquote(parts.path.lstrip("/"))
        if not database:
            raise ConnectorError("mysql dsn must name a database, e.g. mysql://user:pass@host:3306/dbname")
        try:
            port = parts.port or 3306
        except ValueError as e:  # non-numeric port in the DSN
            raise ConnectorError(f"invalid port in mysql dsn: {e}") from e
        return {
            "host": parts.hostname or "127.0.0.1",
            "port": port,
            "user": unquote(parts.username or ""),
            "password": unquote(parts.password or ""),
            "database": database,
        }

    def _connect(self):
        try:
            conn = pymysql.connect(
                **self._params(),
                # Load-bearing, not decorative: this is what keeps
                # CLIENT_MULTI_STATEMENTS off, so the LIMIT wrapper cannot be
                # escaped with `; DROP TABLE ...`. PyMySQL's default is already
                # 0; it is passed explicitly so a future default cannot quietly
                # remove the protection.
                client_flag=0,
                connect_timeout=10,
                read_timeout=_READ_TIMEOUT_SECONDS,
                write_timeout=_READ_TIMEOUT_SECONDS,
                cursorclass=pymysql.cursors.DictCursor,
            )
        except (pymysql.MySQLError, OSError) as e:
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

        Each probe aims a write at a table that does not exist. A login without
        the privilege is stopped by authorization (1142) before the missing
        table is ever resolved; a login that holds the privilege gets "unknown
        table" (1146/1051) instead. Nothing is written in either case, because
        the table is not there — verified on MySQL 8.4 and MariaDB 11.8.

        Run on every connect rather than once at registration: a privilege
        granted after the datasource was registered would otherwise silently
        remove the guarantee. The cost is six trivial round-trips against a
        connection that already paid for TCP setup and auth.
        """
        writable: list[str] = []
        try:
            with conn.cursor() as cur:
                for privilege, probe in _WRITE_PROBES:
                    try:
                        cur.execute(probe)
                    except pymysql.MySQLError as e:
                        if e.args[0] == _ER_ACCESS_DENIED:
                            continue
                        # Any other error means authorization let the statement
                        # through and it failed later — the login holds this
                        # privilege.
                        writable.append(privilege)
                        continue
                    writable.append(privilege)
            conn.rollback()
        except (pymysql.MySQLError, OSError) as e:
            raise ConnectorError(f"could not verify the login is read-only: {e}") from e

        if writable:
            raise ConnectorError(
                f"datasource login can write ({', '.join(writable)}). eiye connects read-only; "
                "grant it SELECT and nothing else, e.g. "
                "CREATE USER 'eiye'@'%' IDENTIFIED BY '...'; GRANT SELECT ON mydb.* TO 'eiye'@'%';"
            )

    def _test_sync(self) -> None:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchall()
        except pymysql.MySQLError as e:
            raise ConnectorError(f"connection test failed: {e}") from e
        finally:
            conn.close()

    def _schema_sync(self) -> tuple[list[dict[str, Any]], list[tuple]]:
        conn = self._connect()
        try:
            with conn.cursor(pymysql.cursors.Cursor) as cur:
                cur.execute(_SCHEMA_SQL)
                columns = [tuple(r) for r in cur.fetchall()]
                cur.execute(_PK_SQL)
                pks = {tuple(r) for r in cur.fetchall()}
                cur.execute(_FK_SQL)
                fks = [tuple(r) for r in cur.fetchall()]
        except pymysql.MySQLError as e:
            raise ConnectorError(f"schema discovery failed: {e}") from e
        finally:
            conn.close()
        fk_cols = {(ft, fc) for ft, fc, _tt, _tc in fks}
        return rows_to_tables(columns, pks, fk_cols), fks

    def _query_sync(self, sql: str, limit: int) -> list[dict[str, Any]]:
        # Bound the scan server-side so `limit` caps memory, not just the
        # returned slice, and so DDL cannot be expressed at all.
        bounded = f"SELECT * FROM ({sql.rstrip().rstrip(';')}) _eiye_q LIMIT %s"
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                # Rejects INSERT/UPDATE/DELETE server-side. Does not cover DDL
                # on MySQL — see the module docstring.
                cur.execute("START TRANSACTION READ ONLY")
                cur.execute(bounded, (limit,))
                rows = list(cur.fetchall())
            conn.rollback()
            return rows
        except pymysql.MySQLError as e:
            raise ConnectorError(f"query failed: {e}") from e
        finally:
            conn.close()

    async def test_connection(self) -> None:
        await asyncio.to_thread(self._test_sync)

    async def discover_schema(self) -> list[dict[str, Any]]:
        tables, fks = await asyncio.to_thread(self._schema_sync)
        # discover_relationships on this instance reuses the same snapshot, so
        # one discovery pass costs one connection round-trip, not two.
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
            raise ConnectorError("mysql query requires 'sql'")
        require_select(sql)
        return await asyncio.to_thread(self._query_sync, sql, limit)
