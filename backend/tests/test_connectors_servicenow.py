"""ServiceNow connector tests.

Built on a `GetOnlyTransport`, so the GET-only claim — the whole of this
connector's read-only guarantee — is enforced by the suite rather than asserted.

The tests worth reading are the allowlist ones. A ServiceNow instance carries
thousands of tables including `sys_user`, so "which tables may this datasource
read" is the governance boundary here, and it has to hold on the query path as
well as at discovery. The fake records the paths and encoded queries it was
sent, so those tests assert on the request actually made rather than on the rows
that came back.
"""

import asyncio
import base64
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from eiye_db.connectors.base import ConnectorError
from eiye_db.connectors.servicenow import ServiceNowConnector, flatten, next_link
from tests.readonly_guards import GetOnlyTransport, WriteAttempted

INSTANCE = "https://acme.service-now.com"
USER, PASSWORD = "eiye_ro", "s3cret"

RECORDS = {
    "incident": [
        {
            "sys_id": "1",
            "number": "INC0001",
            "short_description": "Printer on fire",
            # A reference field, in the shape ServiceNow returns when the
            # exclude-reference-link parameter is not honoured.
            "assigned_to": {"link": f"{INSTANCE}/api/now/table/sys_user/u1", "value": "u1"},
        },
        {"sys_id": "2", "number": "INC0002", "short_description": "Coffee machine", "assigned_to": ""},
        {"sys_id": "3", "number": "INC0003", "short_description": "Lift stuck", "assigned_to": ""},
    ],
    "change_request": [{"sys_id": "9", "number": "CHG0001", "short_description": "Patch window"}],
    # Present on the instance but never in an allowlist below: reaching it is
    # the failure these tests exist to catch.
    "sys_user": [{"sys_id": "u1", "user_name": "admin", "email": "admin@acme.com"}],
}

DICTIONARY = {
    "incident": [
        {"element": "", "internal_type": ""},  # the row describing the table itself
        {"element": "number", "internal_type": "string"},
        {"element": "short_description", "internal_type": "string"},
        {"element": "assigned_to", "internal_type": {"link": "…", "value": "reference"}},
    ],
    "change_request": [{"element": "number", "internal_type": "string"}],
}


class FakeServiceNow:
    """The Table API, paginating one record per response via the Link header.

    `queries` is the point: a test can assert which table was actually
    requested, and with what encoded query, rather than only what came back.
    """

    def __init__(self, page_size: int = 1):
        self.page_size = page_size
        self.paths: list[str] = []
        self.queries: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        path = urlsplit(url).path
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        self.paths.append(path)
        if "sysparm_query" in query:
            self.queries.append(query["sysparm_query"])

        expected = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        if request.headers.get("authorization") != f"Basic {expected}":
            return httpx.Response(401, json={"error": {"message": "User Not Authenticated"}})

        table = path.rsplit("/", 1)[-1] if path.startswith("/api/now/table/") else None
        if table is None:
            return httpx.Response(404, json={"error": {"message": f"no route for {path}"}})

        if table == "sys_dictionary":
            name = ""
            for clause in query.get("sysparm_query", "").split("^"):
                if clause.startswith("name="):
                    name = clause[len("name=") :]
            return self._page(DICTIONARY.get(name, []), query, path)

        if table not in RECORDS:
            return httpx.Response(404, json={"error": {"message": f"no such table {table}"}})
        return self._page(RECORDS[table], query, path)

    def _page(self, items, query, path) -> httpx.Response:
        offset = int(query.get("sysparm_offset", "0"))
        window = items[offset : offset + self.page_size]
        headers = {}
        if offset + self.page_size < len(items):
            # The next link carries the *whole* original query, which is what a
            # real instance sends. An earlier version of this fake dropped it,
            # and page two of a dictionary walk then came back empty — which
            # would have hidden a pagination bug instead of revealing one.
            carried = {**query, "sysparm_offset": offset + self.page_size}
            rest = urlencode(carried)
            headers["Link"] = (
                f'<{INSTANCE}{path}?{urlencode({**query, "sysparm_offset": 0})}>;rel="first",'
                f'<{INSTANCE}{path}?{rest}>;rel="next"'
            )
        return httpx.Response(200, json={"result": window}, headers=headers)


@pytest.fixture
def snow():
    return FakeServiceNow()


@pytest.fixture
def transport(snow):
    return GetOnlyTransport(httpx.MockTransport(snow))


@pytest.fixture
def make(transport):
    def build(**config):
        base = {
            "base_url": INSTANCE,
            "username": USER,
            "password": PASSWORD,
            "tables": ["incident", "change_request"],
        }
        return ServiceNowConnector({**base, **config}, transport=transport)

    return build


@pytest.fixture
def conn(make):
    return make()


# --- pure helpers -------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ('<https://x/api?a=1>;rel="next"', "https://x/api?a=1"),
        ('<https://x/api?a=0>;rel="first",<https://x/api?a=1>;rel="next"', "https://x/api?a=1"),
        # Documentation and real responses differ on the angle brackets, so both
        # shapes have to parse.
        ('https://x/api?a=1;rel="next"', "https://x/api?a=1"),
        ('<https://x/api?a=0>;rel="first",<https://x/api?a=9>;rel="last"', None),
        ("", None),
        (None, None),
    ],
)
def test_next_link(header, expected):
    assert next_link(header) == expected


def test_flatten_collapses_reference_fields():
    """A reference comes back as {link, value}; the link is an API URL that
    means nothing downstream and the value is what the caller wants."""
    assert flatten(RECORDS["incident"][0])["assigned_to"] == "u1"
    assert flatten({"a": "plain", "b": {"value": "v"}, "c": {}}) == {"a": "plain", "b": "v", "c": ""}


# --- config -------------------------------------------------------------------


def test_missing_base_url():
    with pytest.raises(ConnectorError, match="base_url"):
        asyncio.run(ServiceNowConnector({}).test_connection())


@pytest.mark.parametrize("bad", ["acme.service-now.com", "ftp://acme.service-now.com"])
def test_base_url_must_be_absolute_http(bad):
    with pytest.raises(ConnectorError, match="absolute http"):
        asyncio.run(ServiceNowConnector({"base_url": bad}).test_connection())


@pytest.mark.parametrize("config", [{}, {"username": USER}, {"password": PASSWORD}])
def test_credentials_are_required(config):
    with pytest.raises(ConnectorError, match="username"):
        asyncio.run(ServiceNowConnector({"base_url": INSTANCE, **config}).test_connection())


@pytest.mark.parametrize("empty", [None, [], "", "   "])
def test_tables_is_required(make, empty):
    """No default, deliberately: an instance has thousands of tables and
    exposing all of them by accident is the opposite of a governed surface."""
    with pytest.raises(ConnectorError, match="requires 'tables'"):
        asyncio.run(make(tables=empty).test_connection())


def test_tables_accepts_a_comma_separated_string(make):
    """Config arrives as JSON from the API and as a form field from the console;
    a comma-separated string is what the second one produces."""
    assert make(tables="incident, change_request")._tables() == ["incident", "change_request"]


@pytest.mark.parametrize(
    "hostile",
    [
        "../sys_user",
        "incident^ORname=sys_user",
        "incident,sys_user",
        "Incident",
        "sys user",
        "1incident",
        "incident?sysparm_query=x",
    ],
)
def test_a_hostile_table_name_is_refused(make, snow, hostile):
    """Names reach a URL path *and* an encoded query, so they are validated
    against ServiceNow's own shape and refused rather than escaped. Asserting
    nothing was requested matters: a name that produced an empty result would
    otherwise look like a pass."""
    with pytest.raises(ConnectorError, match="not a valid ServiceNow table name"):
        asyncio.run(make(tables=[hostile]).test_connection())
    assert snow.paths == [], "a rejected name still reached the instance"


# --- connection & discovery ----------------------------------------------------


def test_test_connection_checks_every_allowed_table(conn, snow):
    """A missing read ACL on one table out of several would otherwise surface
    later as a table that silently returns nothing."""
    asyncio.run(conn.test_connection())
    assert snow.paths == ["/api/now/table/incident", "/api/now/table/change_request"]


def test_test_connection_reports_a_rejected_credential(make):
    with pytest.raises(ConnectorError, match="sys_dictionary"):
        asyncio.run(make(password="wrong").test_connection())


def test_test_connection_fails_on_a_table_that_does_not_exist(make):
    with pytest.raises(ConnectorError, match="404"):
        asyncio.run(make(tables=["nope"]).test_connection())


def test_discover_returns_one_table_per_allowlist_entry(conn):
    tables = {t["name"]: t for t in asyncio.run(conn.discover_schema())}
    assert set(tables) == {"incident", "change_request"}
    assert [f["name"] for f in tables["incident"]["fields"]] == [
        "number",
        "short_description",
        "assigned_to",
    ]


def test_discover_drops_the_row_describing_the_table_itself(conn):
    """sys_dictionary carries one row per table with an empty `element`;
    reporting it would put a nameless column in the schema."""
    fields = asyncio.run(conn.discover_schema())[0]["fields"]
    assert all(f["name"] for f in fields)


def test_discover_flattens_a_reference_typed_column(conn):
    fields = {f["name"]: f for f in asyncio.run(conn.discover_schema())[0]["fields"]}
    assert fields["assigned_to"]["type"] == "reference"


def test_discover_scopes_the_dictionary_query_to_the_table(conn, snow):
    asyncio.run(conn.discover_schema())
    assert snow.queries[0].startswith("name=incident^")


# --- query --------------------------------------------------------------------


def test_query_returns_records(conn):
    rows = asyncio.run(conn.query({"table": "incident"}, limit=10))
    assert [r["number"] for r in rows] == ["INC0001", "INC0002", "INC0003"]
    assert rows[0]["assigned_to"] == "u1"


def test_query_paginates_through_the_link_header(conn, snow):
    """The fake serves one record per response, so three rows proves the Link
    header was followed rather than the first page being taken as the whole."""
    assert len(asyncio.run(conn.query({"table": "incident"}, limit=10))) == 3
    assert sum(p == "/api/now/table/incident" for p in snow.paths) == 3


def test_query_respects_the_limit_and_stops_paginating(conn, snow):
    assert len(asyncio.run(conn.query({"table": "incident"}, limit=2))) == 2
    assert sum(p == "/api/now/table/incident" for p in snow.paths) == 2


def test_query_needs_a_table(conn):
    with pytest.raises(ConnectorError, match="requires 'table'"):
        asyncio.run(conn.query({}, limit=10))


# --- the allowlist is the boundary ---------------------------------------------


def test_query_refuses_a_table_outside_the_allowlist(conn, snow):
    """`sys_user` exists on the instance and is readable by the credential. The
    allowlist is the only thing stopping it, so this is the test that says the
    scope is real."""
    with pytest.raises(ConnectorError, match="not in this datasource's allowlist"):
        asyncio.run(conn.query({"table": "sys_user"}, limit=10))
    assert not any("sys_user" in p for p in snow.paths), "the table was fetched anyway"


def test_the_instance_would_serve_that_table(conn, snow):
    """Confirm the fake is not refusing sys_user on its own, which would make
    the test above pass for the wrong reason."""
    rows = asyncio.run(conn.__class__(
        {"base_url": INSTANCE, "username": USER, "password": PASSWORD, "tables": ["sys_user"]},
        transport=conn._transport,
    ).query({"table": "sys_user"}, limit=10))
    assert rows[0]["user_name"] == "admin"


def test_no_encoded_query_passthrough(conn, snow):
    """`sysparm_query` is a query language; accepting one from a caller would
    let them dot-walk past the allowlist. Not supported, and the extra key is
    ignored rather than forwarded."""
    asyncio.run(conn.query({"table": "incident", "sysparm_query": "ORname=sys_user"}, limit=10))
    assert not any("sys_user" in q for q in snow.queries)


# --- the guard ----------------------------------------------------------------


def test_guard_is_not_inert(conn, transport):
    asyncio.run(conn.discover_schema())
    asyncio.run(conn.query({"table": "incident"}, limit=10))
    assert transport.methods_seen
    assert set(transport.methods_seen) == {"GET"}


def test_guard_refuses_a_write(transport):
    """The Table API takes POST, PUT, PATCH and DELETE on the same paths this
    connector GETs. This is the failure any of them would produce."""

    async def post():
        async with httpx.AsyncClient(transport=transport, base_url=INSTANCE) as client:
            await client.post("/api/now/table/incident", json={"short_description": "x"})

    with pytest.raises(WriteAttempted, match="POST"):
        asyncio.run(post())
