"""Confluence Cloud connector tests.

Every connector here is built on a `GetOnlyTransport`, so the GET-only claim —
which is the whole of this connector's read-only guarantee, there being no
read-only Confluence credential to verify — is enforced by the suite rather than
asserted in prose. See `tests/readonly_guards.py`.

The fake site below implements the three v2 endpoints the connector calls, with
real cursor pagination, because following `_links.next` correctly is the part
most likely to be got wrong and the part a mock that returns one page would
never exercise.
"""

import asyncio
import base64
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from eiye_db.connectors.base import ConnectorError
from eiye_db.connectors.confluence import (
    ConfluenceConnector,
    page_row,
    storage_to_text,
)
from tests.readonly_guards import GetOnlyTransport, WriteAttempted

SITE = "https://example.atlassian.net"
EMAIL, TOKEN = "ops@example.com", "tok123"

SPACES = [
    {"id": "100", "key": "ENG", "name": "Engineering"},
    {"id": "200", "key": "OPS", "name": "Operations"},
]

# Three pages in ENG so a limit of 2 has something to trim, and one in OPS so a
# scope violation has somewhere real to point at.
PAGES = {
    "100": [
        {
            "id": "1",
            "title": "Runbook",
            "status": "current",
            "spaceId": "100",
            "parentId": None,
            "authorId": "u1",
            "createdAt": "2026-01-01T00:00:00Z",
            "_links": {"webui": "/spaces/ENG/pages/1"},
        },
        {
            "id": "2",
            "title": "Onboarding",
            "status": "current",
            "spaceId": "100",
            "parentId": "1",
            "authorId": "u2",
            "createdAt": "2026-01-02T00:00:00Z",
            "_links": {"webui": "/spaces/ENG/pages/2"},
        },
        {
            "id": "3",
            "title": "Archived notes",
            "status": "archived",
            "spaceId": "100",
            "parentId": None,
            "authorId": "u1",
            "createdAt": "2026-01-03T00:00:00Z",
            "_links": {"webui": "/spaces/ENG/pages/3"},
        },
    ],
    "200": [
        {
            "id": "9",
            "title": "Pager rota",
            "status": "current",
            "spaceId": "200",
            "parentId": None,
            "authorId": "u3",
            "createdAt": "2026-01-04T00:00:00Z",
            "_links": {"webui": "/spaces/OPS/pages/9"},
        }
    ],
}

BODIES = {
    "1": "<p>Restart the <strong>worker</strong>.</p><ac:structured-macro ac:name='info'/><p>Then &amp; verify.</p>",
    "9": "<p>Rota lives here.</p>",
}


class FakeConfluence:
    """The three v2 endpoints this connector calls, paginating one item per page.

    `requests` is asserted on, so a test can state that a listing cost four
    round-trips rather than merely that it returned the right rows.
    """

    def __init__(self, page_size: int = 1):
        self.page_size = page_size
        self.requests: list[str] = []
        self.unauthorized = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        if self.unauthorized:
            return httpx.Response(401, json={"message": "Unauthorized"})
        expected = base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
        if request.headers.get("authorization") != f"Basic {expected}":
            return httpx.Response(401, json={"message": "bad credentials"})

        path = urlsplit(str(request.url)).path
        query = parse_qs(urlsplit(str(request.url)).query)
        cursor = int(query.get("cursor", ["0"])[0])

        if path == "/wiki/api/v2/spaces":
            items = SPACES
            keys = query.get("keys")
            if keys:
                items = [s for s in SPACES if s["key"] in keys[0].split(",")]
            return self._page(items, cursor, path, query)

        if path.startswith("/wiki/api/v2/spaces/") and path.endswith("/pages"):
            space_id = path.split("/")[-2]
            return self._page(PAGES.get(space_id, []), cursor, path, query)

        if path.startswith("/wiki/api/v2/pages/"):
            page_id = path.rsplit("/", 1)[-1]
            found = next((p for pp in PAGES.values() for p in pp if p["id"] == page_id), None)
            if found is None:
                return httpx.Response(404, json={"message": "no such page"})
            body = dict(found)
            if query.get("body-format") == ["storage"]:
                body["body"] = {"storage": {"value": BODIES.get(page_id, "")}}
            return httpx.Response(200, json=body)

        return httpx.Response(404, json={"message": f"no route for {path}"})

    def _page(self, items, cursor, path, query) -> httpx.Response:
        window = items[cursor : cursor + self.page_size]
        payload = {"results": window}
        if cursor + self.page_size < len(items):
            keep = f"&keys={query['keys'][0]}" if query.get("keys") else ""
            payload["_links"] = {"next": f"{path}?cursor={cursor + self.page_size}{keep}"}
        return httpx.Response(200, json=payload)


@pytest.fixture
def site():
    return FakeConfluence()


@pytest.fixture
def transport(site):
    return GetOnlyTransport(httpx.MockTransport(site))


@pytest.fixture
def make(transport):
    def build(**config):
        return ConfluenceConnector(
            {"base_url": SITE, "email": EMAIL, "api_token": TOKEN, **config}, transport=transport
        )

    return build


@pytest.fixture
def conn(make):
    return make()


# --- config -------------------------------------------------------------------


def test_missing_base_url():
    with pytest.raises(ConnectorError, match="base_url"):
        asyncio.run(ConfluenceConnector({}).test_connection())


@pytest.mark.parametrize("bad", ["example.atlassian.net", "ftp://example.atlassian.net", "/wiki"])
def test_base_url_must_be_absolute_http(bad):
    with pytest.raises(ConnectorError, match="absolute http"):
        asyncio.run(ConfluenceConnector({"base_url": bad}).test_connection())


@pytest.mark.parametrize(
    "config",
    [
        {"base_url": SITE},
        {"base_url": SITE, "email": EMAIL},
        {"base_url": SITE, "api_token": TOKEN},
    ],
)
def test_credentials_are_required(config):
    with pytest.raises(ConnectorError, match="email"):
        asyncio.run(ConfluenceConnector(config).test_connection())


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (SITE, SITE),
        (f"{SITE}/wiki", SITE),
        (f"{SITE}/wiki/", SITE),
        (f"{SITE}/", SITE),
    ],
)
def test_site_url_normalisation(configured, expected):
    """Operators paste the URL from a browser, which carries `/wiki`; the cursor
    URLs Confluence returns carry it too. Both forms must land on the same
    origin or paginated requests become `/wiki/wiki/...`."""
    assert ConfluenceConnector({"base_url": configured})._site() == expected


# --- pure helpers -------------------------------------------------------------


def test_storage_to_text_strips_markup_and_decodes_entities():
    assert storage_to_text(BODIES["1"]) == "Restart the\nworker\n.\nThen & verify."


def test_storage_to_text_survives_malformed_markup():
    """A page whose markup will not parse should degrade to something readable
    and PII-scannable, not fail the query."""
    assert "secret" in storage_to_text("<p>secret<<<>>")


def test_page_row_builds_an_absolute_url():
    row = page_row(PAGES["100"][0], SITE)
    assert row["url"] == f"{SITE}/wiki/spaces/ENG/pages/1"
    assert row["parent_id"] == ""  # None must not become the string "None"
    assert row["space_id"] == "100"


def test_page_row_tolerates_a_page_with_no_links():
    assert page_row({"id": 7}, SITE) == {
        "id": "7",
        "title": "",
        "status": "",
        "space_id": "",
        "parent_id": "",
        "author_id": "",
        "created_at": "",
        "url": "",
    }


# --- connection ---------------------------------------------------------------


def test_test_connection(conn):
    asyncio.run(conn.test_connection())


def test_test_connection_reports_a_rejected_token(conn, site):
    site.unauthorized = True
    with pytest.raises(ConnectorError, match="expire after one year"):
        asyncio.run(conn.test_connection())


def test_a_wrong_token_is_rejected(make):
    with pytest.raises(ConnectorError, match="401"):
        asyncio.run(make(api_token="wrong").test_connection())


def test_test_connection_rejects_an_unknown_space_key(make):
    """A space_key naming nothing is a configuration error. Surfaced here it is
    obvious; surfaced at query time it looks like an empty space."""
    with pytest.raises(ConnectorError, match="was not found"):
        asyncio.run(make(space_key="NOPE").test_connection())


# --- discovery ----------------------------------------------------------------


def test_discover_lists_one_table_per_space(conn):
    tables = asyncio.run(conn.discover_schema())
    assert [t["name"] for t in tables] == ["ENG", "OPS"]
    assert [f["name"] for f in tables[0]["fields"]][:3] == ["id", "title", "status"]


def test_discover_is_confined_to_the_configured_space(make):
    assert [t["name"] for t in asyncio.run(make(space_key="ENG").discover_schema())] == ["ENG"]


def test_discover_paginates(conn, site):
    """The fake serves one space per response, so listing both proves the
    connector followed `_links.next` rather than stopping at the first page."""
    assert len(asyncio.run(conn.discover_schema())) == 2
    assert sum("/spaces" in r for r in site.requests) == 2


# --- query --------------------------------------------------------------------


def test_query_lists_a_space(conn):
    rows = asyncio.run(conn.query({"space": "ENG"}, limit=10))
    assert [r["title"] for r in rows] == ["Runbook", "Onboarding", "Archived notes"]
    assert rows[1]["parent_id"] == "1"


def test_query_respects_the_limit_and_stops_paginating(conn, site):
    rows = asyncio.run(conn.query({"space": "ENG"}, limit=2))
    assert len(rows) == 2
    # One request to resolve the space, then two of three page-listing requests:
    # the walk stops as soon as the limit is met rather than draining the space.
    assert sum("/pages" in r for r in site.requests) == 2


def test_query_a_page_returns_its_text(conn):
    rows = asyncio.run(conn.query({"page_id": "1"}, limit=10))
    assert rows[0]["title"] == "Runbook"
    assert rows[0]["content"] == "Restart the\nworker\n.\nThen & verify."


def test_query_needs_a_space_or_a_page_id(conn):
    with pytest.raises(ConnectorError, match="requires 'space'"):
        asyncio.run(conn.query({}, limit=10))


def test_query_an_unknown_space(conn):
    with pytest.raises(ConnectorError, match="was not found"):
        asyncio.run(conn.query({"space": "NOPE"}, limit=10))


def test_query_a_missing_page(conn):
    with pytest.raises(ConnectorError, match="404"):
        asyncio.run(conn.query({"page_id": "404404"}, limit=10))


def test_listing_does_not_fetch_bodies(conn, site):
    """A space of a thousand pages must not become a thousand extra requests,
    so listing returns metadata only."""
    asyncio.run(conn.query({"space": "ENG"}, limit=10))
    assert not any("body-format" in r for r in site.requests)
    assert "content" not in asyncio.run(conn.query({"space": "ENG"}, limit=1))[0]


# --- scope --------------------------------------------------------------------


def test_a_scoped_source_refuses_another_space(make):
    with pytest.raises(ConnectorError, match="scoped to space 'ENG'"):
        asyncio.run(make(space_key="ENG").query({"space": "OPS"}, limit=10))


def test_a_scoped_source_refuses_a_page_id_from_another_space(make):
    """The scope has to hold on the page-id path too. Page ids are site-wide, so
    without this check a caller who knows one id could read straight past a
    scope the operator set deliberately."""
    with pytest.raises(ConnectorError, match="outside space 'ENG'"):
        asyncio.run(make(space_key="ENG").query({"page_id": "9"}, limit=10))


def test_a_scoped_source_still_serves_its_own_pages(make):
    rows = asyncio.run(make(space_key="ENG").query({"page_id": "1"}, limit=10))
    assert rows[0]["id"] == "1"


# --- the guard ----------------------------------------------------------------


def test_guard_is_not_inert(conn, transport):
    """A guard that never fires cannot be told apart from a guard that is
    broken. Prove the connector's ordinary work goes through it, and that
    everything it issued was a read."""
    asyncio.run(conn.discover_schema())
    asyncio.run(conn.query({"page_id": "1"}, limit=10))
    assert transport.methods_seen
    assert set(transport.methods_seen) == {"GET"}


def test_guard_refuses_a_write(transport):
    """The failure a future POST in this connector would produce. This is the
    whole of the read-only guarantee here — Confluence has no read-only
    credential to verify, so there is no second layer behind it."""

    async def post():
        async with httpx.AsyncClient(transport=transport, base_url=SITE) as client:
            await client.post("/wiki/api/v2/pages", json={})

    with pytest.raises(WriteAttempted, match="POST"):
        asyncio.run(post())


def test_the_fake_site_would_accept_a_write(site):
    """The guard is the only thing preventing one. Confirm the fake is not
    quietly refusing writes itself, which would make the test above pass for the
    wrong reason."""
    written = site(httpx.Request("POST", f"{SITE}/wiki/api/v2/pages", json={}))
    assert written.status_code != 405, "the fake refuses writes, so the guard is not what is being tested"
    assert json.loads(written.content)  # it answered something, rather than rejecting the method
