"""Jira Cloud connector tests.

Built on a `GetOnlyTransport`, so the GET-only claim — the whole of this
connector's read-only guarantee — is enforced by the suite rather than asserted.

The tests worth reading are the JQL ones. This connector is the first that
*builds* a query language rather than only addressing resources by name, so a
project key reaching JQL unchecked would let a caller rewrite the query. The
fake below records the JQL it was sent, which is what makes those assertions
about the query actually sent rather than about the rows that came back.
"""

import asyncio
import base64
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from eiye_db.connectors.base import ConnectorError
from eiye_db.connectors.jira import JiraConnector, adf_to_text, issue_row
from tests.readonly_guards import GetOnlyTransport, WriteAttempted

SITE = "https://example.atlassian.net"
EMAIL, TOKEN = "ops@example.com", "tok123"

PROJECTS = [{"id": "1", "key": "ENG"}, {"id": "2", "key": "OPS"}]

DESCRIPTION = {
    "type": "doc",
    "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "Restart the worker."}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "Contact a@b.co."}]},
    ],
}


def _issue(key, summary, project, assignee=None, priority=None):
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "status": {"name": "In Progress"},
            "issuetype": {"name": "Bug"},
            "priority": priority,
            "assignee": assignee,
            "reporter": {"displayName": "Ada"},
            "created": "2026-01-01T00:00:00Z",
            "updated": "2026-01-02T00:00:00Z",
            "project": {"key": project},
        },
    }


ISSUES = {
    "ENG": [
        _issue("ENG-1", "Worker crashes", "ENG", {"displayName": "Bob"}, {"name": "High"}),
        _issue("ENG-2", "Slow query", "ENG"),
        _issue("ENG-3", "Flaky test", "ENG"),
    ],
    "OPS": [_issue("OPS-9", "Rotate keys", "OPS")],
}


class FakeJira:
    """Jira's three relevant endpoints, with both of its pagination schemes.

    `jql_seen` is the point of the class: a test can assert what query was
    actually sent, not merely what rows came back.
    """

    def __init__(self, page_size: int = 1):
        self.page_size = page_size
        self.requests: list[str] = []
        self.jql_seen: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        expected = base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
        if request.headers.get("authorization") != f"Basic {expected}":
            return httpx.Response(401, json={"message": "bad credentials"})

        path = urlsplit(str(request.url)).path
        query = {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}

        if path == "/rest/api/3/myself":
            return httpx.Response(200, json={"accountId": "u1", "emailAddress": EMAIL})

        if path == "/rest/api/3/project/search":
            start = int(query.get("startAt", "0"))
            window = PROJECTS[start : start + self.page_size]
            return httpx.Response(
                200,
                json={"values": window, "isLast": start + self.page_size >= len(PROJECTS)},
            )

        if path.startswith("/rest/api/3/project/"):
            key = path.rsplit("/", 1)[-1]
            found = next((p for p in PROJECTS if p["key"] == key), None)
            return (
                httpx.Response(200, json=found)
                if found
                else httpx.Response(404, json={"message": "no such project"})
            )

        if path == "/rest/api/3/search/jql":
            jql = query.get("jql", "")
            self.jql_seen.append(jql)
            # Only the exact query this connector builds returns rows. Anything
            # else answers empty, so a mangled or injected JQL cannot silently
            # look like a success.
            project = None
            for key in ISSUES:
                if jql == f'project = "{key}" ORDER BY created DESC':
                    project = key
            if project is None:
                return httpx.Response(200, json={"issues": []})
            cursor = int(query.get("nextPageToken", "0"))
            rows = ISSUES[project][cursor : cursor + self.page_size]
            body: dict = {"issues": rows}
            if cursor + self.page_size < len(ISSUES[project]):
                body["nextPageToken"] = str(cursor + self.page_size)
            return httpx.Response(200, json=body)

        if path.startswith("/rest/api/3/issue/"):
            key = path.rsplit("/", 1)[-1]
            found = next((i for pp in ISSUES.values() for i in pp if i["key"] == key), None)
            if found is None:
                return httpx.Response(404, json={"message": "no such issue"})
            issue = {"key": found["key"], "fields": dict(found["fields"])}
            if "description" in query.get("fields", ""):
                issue["fields"]["description"] = DESCRIPTION
            return httpx.Response(200, json=issue)

        return httpx.Response(404, json={"message": f"no route for {path}"})


@pytest.fixture
def jira():
    return FakeJira()


@pytest.fixture
def transport(jira):
    return GetOnlyTransport(httpx.MockTransport(jira))


@pytest.fixture
def make(transport):
    def build(**config):
        return JiraConnector(
            {"base_url": SITE, "email": EMAIL, "api_token": TOKEN, **config}, transport=transport
        )

    return build


@pytest.fixture
def conn(make):
    return make()


# --- config (shared base) ------------------------------------------------------


def test_missing_base_url():
    with pytest.raises(ConnectorError, match="base_url"):
        asyncio.run(JiraConnector({}).test_connection())


@pytest.mark.parametrize("bad", ["example.atlassian.net", "ftp://example.atlassian.net", "/jira"])
def test_base_url_must_be_absolute_http(bad):
    with pytest.raises(ConnectorError, match="absolute http"):
        asyncio.run(JiraConnector({"base_url": bad}).test_connection())


def test_credentials_are_required():
    with pytest.raises(ConnectorError, match="email"):
        asyncio.run(JiraConnector({"base_url": SITE}).test_connection())


@pytest.mark.parametrize(
    "configured",
    [SITE, f"{SITE}/", f"{SITE}/jira", f"{SITE}/jira/software/projects/ENG/boards/1"],
)
def test_site_url_normalisation(configured):
    """Operators paste whatever the browser was showing. A Cloud site is always
    served at the root of its own host, so everything after the host is a page
    they happened to be on rather than part of the address."""
    assert JiraConnector({"base_url": configured})._site() == SITE


# --- pure helpers -------------------------------------------------------------


def test_adf_to_text_flattens_paragraphs():
    assert adf_to_text(DESCRIPTION) == "Restart the worker.\nContact a@b.co."


@pytest.mark.parametrize("empty", [None, {}, [], "", {"type": "doc"}])
def test_adf_to_text_handles_an_absent_description(empty):
    """`description` is null on most issues, so this runs far more often than
    the happy path."""
    assert adf_to_text(empty) == ""


def test_issue_row_maps_null_fields_to_empty_strings():
    """Unassigned issues have a null assignee and projects can disable priority.
    Neither may become the string 'None'."""
    row = issue_row(ISSUES["ENG"][1], SITE)
    assert row["assignee"] == ""
    assert row["priority"] == ""
    assert row["reporter"] == "Ada"
    assert row["url"] == f"{SITE}/browse/ENG-2"


def test_issue_row_tolerates_an_issue_with_no_fields():
    assert issue_row({}, SITE)["url"] == ""


# --- JQL construction ---------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        'ENG" OR project = "OPS',
        "ENG' OR 1=1",
        "ENG ORDER BY created",
        'ENG"',
        "ENG OR project=OPS",
        "1ENG",
        "ENG-1",
    ],
)
def test_a_hostile_project_key_never_reaches_jql(conn, jira, hostile):
    """The reason this connector validates and the Confluence one does not: a
    project key is interpolated into a query language. Assert on what was sent,
    not on the rows — an injected query that happened to return nothing would
    otherwise look like a pass."""
    with pytest.raises(ConnectorError, match="not a valid Jira project key"):
        asyncio.run(conn.query({"project": hostile}, limit=10))
    assert jira.jql_seen == [], "a rejected key still reached the API"


def test_the_built_jql_is_exactly_what_is_expected(conn, jira):
    asyncio.run(conn.query({"project": "ENG"}, limit=10))
    # One distinct query, repeated once per page of results.
    assert set(jira.jql_seen) == {'project = "ENG" ORDER BY created DESC'}


def test_raw_jql_is_not_a_supported_request(conn, jira):
    """Deliberately unsupported. JQL cannot write, but it would step straight
    past `project_key`, which is the scope an operator sets."""
    with pytest.raises(ConnectorError, match="requires 'project'"):
        asyncio.run(conn.query({"jql": "project = OPS"}, limit=10))
    assert jira.jql_seen == []


@pytest.mark.parametrize("bad", ["ENG", "ENG-", "-1", "ENG-1-2", 'ENG-1" OR "x'])
def test_a_malformed_issue_key_is_refused(conn, bad):
    with pytest.raises(ConnectorError, match="not a valid Jira issue key"):
        asyncio.run(conn.query({"issue_key": bad}, limit=10))


# --- connection & discovery ----------------------------------------------------


def test_test_connection(conn):
    asyncio.run(conn.test_connection())


def test_test_connection_reports_a_rejected_token(make):
    with pytest.raises(ConnectorError, match="expire after one year"):
        asyncio.run(make(api_token="wrong").test_connection())


def test_test_connection_rejects_an_unknown_project(make):
    with pytest.raises(ConnectorError, match="404"):
        asyncio.run(make(project_key="NOPE").test_connection())


def test_discover_lists_one_table_per_project(conn):
    tables = asyncio.run(conn.discover_schema())
    assert [t["name"] for t in tables] == ["ENG", "OPS"]
    assert [f["name"] for f in tables[0]["fields"]][:3] == ["key", "summary", "status"]


def test_discover_paginates_projects_by_offset(conn, jira):
    """Project search still pages by `startAt` and ends on `isLast` — a
    different mechanism from issue search, in the same product."""
    assert len(asyncio.run(conn.discover_schema())) == 2
    assert sum("project/search" in r for r in jira.requests) == 2


def test_discover_is_confined_to_the_configured_project(make, jira):
    assert [t["name"] for t in asyncio.run(make(project_key="ENG").discover_schema())] == ["ENG"]
    assert not any("project/search" in r for r in jira.requests), "a scoped source listed every project"


# --- query --------------------------------------------------------------------


def test_query_lists_a_project(conn):
    rows = asyncio.run(conn.query({"project": "ENG"}, limit=10))
    assert [r["key"] for r in rows] == ["ENG-1", "ENG-2", "ENG-3"]
    assert rows[0]["status"] == "In Progress"


def test_query_paginates_issues_by_token(conn, jira):
    """Issue search returns an opaque `nextPageToken` and no total. The fake
    serves one issue per response, so three rows proves the token was followed."""
    assert len(asyncio.run(conn.query({"project": "ENG"}, limit=10))) == 3
    assert sum("search/jql" in r for r in jira.requests) == 3


def test_query_respects_the_limit_and_stops_paginating(conn, jira):
    assert len(asyncio.run(conn.query({"project": "ENG"}, limit=2))) == 2
    assert sum("search/jql" in r for r in jira.requests) == 2


def test_query_an_issue_returns_its_description(conn):
    rows = asyncio.run(conn.query({"issue_key": "ENG-1"}, limit=10))
    assert rows[0]["summary"] == "Worker crashes"
    assert rows[0]["content"] == "Restart the worker.\nContact a@b.co."


def test_listing_does_not_fetch_descriptions(conn, jira):
    rows = asyncio.run(conn.query({"project": "ENG"}, limit=10))
    assert "content" not in rows[0]
    assert not any("description" in r for r in jira.requests)


def test_query_needs_a_project_or_an_issue_key(conn):
    with pytest.raises(ConnectorError, match="requires 'project'"):
        asyncio.run(conn.query({}, limit=10))


def test_query_a_missing_issue(conn):
    with pytest.raises(ConnectorError, match="404"):
        asyncio.run(conn.query({"issue_key": "ENG-404"}, limit=10))


# --- scope --------------------------------------------------------------------


def test_a_scoped_source_refuses_another_project(make):
    with pytest.raises(ConnectorError, match="scoped to project 'ENG'"):
        asyncio.run(make(project_key="ENG").query({"project": "OPS"}, limit=10))


def test_a_scoped_source_refuses_an_issue_from_another_project(make, jira):
    """Issue keys carry their project, so this is checkable without a round
    trip — and it must be checked, or a scoped source becomes a site-wide reader
    for anyone who knows a key."""
    with pytest.raises(ConnectorError, match="outside project 'ENG'"):
        asyncio.run(make(project_key="ENG").query({"issue_key": "OPS-9"}, limit=10))
    assert not any("/issue/" in r for r in jira.requests), "the out-of-scope issue was fetched anyway"


def test_a_scoped_source_still_serves_its_own_issues(make):
    rows = asyncio.run(make(project_key="ENG").query({"issue_key": "ENG-1"}, limit=10))
    assert rows[0]["key"] == "ENG-1"


# --- the guard ----------------------------------------------------------------


def test_guard_is_not_inert(conn, transport):
    asyncio.run(conn.discover_schema())
    asyncio.run(conn.query({"issue_key": "ENG-1"}, limit=10))
    assert transport.methods_seen
    assert set(transport.methods_seen) == {"GET"}


def test_guard_refuses_a_write(transport):
    """`/rest/api/3/search/jql` also has a POST form, for JQL too long for a
    query string. It reads identically and would break the GET-only guarantee,
    so this is the failure a future switch to it would produce."""

    async def post():
        async with httpx.AsyncClient(transport=transport, base_url=SITE) as client:
            await client.post("/rest/api/3/search/jql", json={"jql": "project = ENG"})

    with pytest.raises(WriteAttempted, match="POST"):
        asyncio.run(post())


def test_the_fake_would_answer_a_write(jira):
    """The guard is the only thing preventing one; confirm the fake is not
    refusing writes itself, which would make the test above pass for the wrong
    reason."""
    credential = base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
    answered = jira(
        httpx.Request("POST", f"{SITE}/rest/api/3/myself", headers={"Authorization": f"Basic {credential}"})
    )
    assert answered.status_code == 200
