"""Helpers shared by the SQL connectors.

Only dialect-independent code belongs here. Read-only enforcement deliberately
does not: Postgres has a server-enforced read-only transaction that also covers
DDL, MySQL's covers DML but not DDL, and SQL Server has no read-only
transaction at all. Those are three different mechanisms, not one mechanism
with three parameters, so each connector states its own.
"""

import re
from typing import Any

from eiye_db.connectors.base import ConnectorError


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


# Leading whitespace and comments, so `require_select` sees the first real
# keyword. `--` only opens a comment when followed by whitespace: that is
# MySQL's rule, and erring this way is the safe direction — treating `--x` as a
# comment when the server does not would hide text from this check.
# `/*! ... */` is excluded on purpose: MySQL *executes* those, so they are not
# comments and must not be skipped over.
_LEADING_NOISE = re.compile(r"^(?:\s+|/\*(?!!).*?\*/|--[ \t\r\n][^\n]*|#[^\n]*)+", re.S)


def require_select(sql: str) -> None:
    """Reject statements that are plainly not a read.

    This is a usability guard, not the security boundary. It turns
    `DROP TABLE users` into a clear error instead of a confusing syntax error
    about the connector's own LIMIT wrapper. The actual enforcement is the
    read-only login verified at connect, the driver refusing to transmit
    multiple statements, the derived-table wrapper, and the read-only
    transaction — each of which holds whether or not this check is fooled.
    """
    head = _LEADING_NOISE.sub("", sql).lstrip("(")
    if not re.match(r"(?:select|with)\b", head, re.I):
        raise ConnectorError("only SELECT queries are supported")


def _blank_literals(sql: str) -> str:
    """Replace string literals, quoted/bracketed identifiers and comments with a space.

    Written for T-SQL, where `--` always opens a comment (MySQL requires a
    following space) and block comments nest. Only the SQL Server connector uses
    it; applying it to MySQL text would treat `--x` as a comment the server does
    not, which could hide a statement separator rather than reveal one.
    """
    out: list[str] = []
    i, n, depth = 0, len(sql), 0
    while i < n:
        if depth:
            if sql.startswith("/*", i):
                depth += 1
                i += 2
            elif sql.startswith("*/", i):
                depth -= 1
                i += 2
            else:
                i += 1
            continue
        if sql.startswith("/*", i):
            depth, i = 1, i + 2
            out.append(" ")
            continue
        if sql.startswith("--", i):
            while i < n and sql[i] != "\n":
                i += 1
            continue
        ch = sql[i]
        if ch in "'\"[":
            close = {"'": "'", '"': '"', "[": "]"}[ch]
            i += 1
            while i < n:
                if sql[i] == close:
                    # A doubled delimiter ('' "" ]]) is an escape, not the end.
                    if i + 1 < n and sql[i + 1] == close:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def reject_multiple_statements(sql: str) -> None:
    """Reject a batch of statements, allowing one optional trailing semicolon.

    This matters far more for SQL Server than for the other engines. TDS
    transmits multi-statement batches natively — there is no driver flag to turn
    that off the way PyMySQL leaves CLIENT_MULTI_STATEMENTS unset, and no
    prepared-statement protocol refusing it the way Postgres does. Verified: a
    batch appended after a closing paren executes, and drops the table when the
    login is allowed to.

    Still not the security boundary — a read-only login is. This is the layer
    that makes the boundary harder to reach.
    """
    _head, separator, tail = _blank_literals(sql).partition(";")
    if separator and tail.strip():
        raise ConnectorError("only a single statement is allowed")
