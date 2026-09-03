"""REST connector tests using httpx.MockTransport.

Every connector here is built on a `GetOnlyTransport`, so the GET-only claim is
enforced by the test suite rather than asserted in prose: any non-GET request on
any exercised path raises `WriteAttempted` and fails the test. See
`tests/readonly_guards.py` for what that does and does not prove.
"""

import asyncio

import httpx
import pytest

from eiye_db.connectors.base import ConnectorError
from eiye_db.connectors.rest import RestConnector
from tests.readonly_guards import GetOnlyTransport, WriteAttempted

OPENAPI = {
    "paths": {
        "/users": {
            "get": {"parameters": [{"name": "limit", "schema": {"type": "integer"}}]},
            "post": {},
        },
        "/internal": {"post": {}},
    }
}


def handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/openapi.json":
        return httpx.Response(200, json=OPENAPI)
    if request.url.path == "/users":
        return httpx.Response(200, json=[{"id": 1, "email": "a@b.co"}, {"id": 2}])
    if request.url.path == "/single":
        return httpx.Response(200, json={"ok": True})
    if request.url.path == "/":
        return httpx.Response(200, text="root")
    return httpx.Response(404)


@pytest.fixture
def transport():
    return GetOnlyTransport(httpx.MockTransport(handler))


@pytest.fixture
def conn(transport):
    return RestConnector({"base_url": "http://test.local"}, transport=transport)


def test_test_connection(conn):
    asyncio.run(conn.test_connection())


def test_discover_openapi_get_only(conn):
    tables = asyncio.run(conn.discover_schema())
    assert tables == [{"name": "/users", "fields": [{"name": "limit", "type": "integer"}]}]


def test_query_list(conn):
    rows = asyncio.run(conn.query({"path": "/users"}, limit=1))
    assert rows == [{"id": 1, "email": "a@b.co"}]


def test_query_object_wrapped(conn):
    rows = asyncio.run(conn.query({"path": "/single"}, limit=10))
    assert rows == [{"ok": True}]


def test_query_404_raises(conn):
    with pytest.raises(ConnectorError, match="404"):
        asyncio.run(conn.query({"path": "/missing"}, limit=10))


def test_missing_base_url():
    conn = RestConnector({})
    with pytest.raises(ConnectorError, match="base_url"):
        asyncio.run(conn.test_connection())


# --- the guard itself ---------------------------------------------------------


def test_guard_is_not_inert(conn, transport):
    """A guard that never fires cannot be told apart from a guard that is
    broken. Prove the connector's ordinary work actually goes through it."""
    asyncio.run(conn.discover_schema())
    asyncio.run(conn.query({"path": "/users"}, limit=1))
    assert transport.methods_seen == ["GET", "GET"]


def test_guard_refuses_a_write(transport):
    """And prove it would catch one. This is the failure a future POST in the
    connector would produce — WriteAttempted is a BaseException precisely so
    the connector's own `except httpx.HTTPError` cannot turn it into a tidy
    ConnectorError."""

    async def post():
        async with httpx.AsyncClient(transport=transport, base_url="http://test.local") as client:
            await client.post("/users", json={})

    with pytest.raises(WriteAttempted, match="POST"):
        asyncio.run(post())
