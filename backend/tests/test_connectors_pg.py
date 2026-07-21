"""PostgreSQL connector tests. Live tests gated on EIYE_TEST_PG_DSN."""

import asyncio
import os

import pytest

from eiye_db.connectors.base import ConnectorError
from eiye_db.connectors.postgres import PostgresConnector, fk_rows_to_relationships, rows_to_tables

PG_DSN = os.environ.get("EIYE_TEST_PG_DSN")


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


@pytest.mark.skipif(not PG_DSN, reason="EIYE_TEST_PG_DSN not set")
def test_live_roundtrip():
    conn = PostgresConnector({"dsn": PG_DSN})
    asyncio.run(conn.test_connection())
    rows = asyncio.run(conn.query({"sql": "SELECT 1 AS one"}, limit=10))
    assert rows == [{"one": 1}]


@pytest.mark.skipif(not PG_DSN, reason="EIYE_TEST_PG_DSN not set")
def test_live_write_blocked():
    # A write is rejected — either by the read-only transaction or (once the
    # SQL is wrapped in a bounding subquery) as a syntax error. Either way the
    # write never executes.
    conn = PostgresConnector({"dsn": PG_DSN})
    with pytest.raises(ConnectorError):
        asyncio.run(conn.query({"sql": "CREATE TABLE eiye_smoke (id int)"}, limit=10))
