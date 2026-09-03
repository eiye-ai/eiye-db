"""SharePoint connector tests.

Built on a `GetOnlyTransport` that allows POST to exactly
`login.microsoftonline.com`. That allowance is the whole reason this suite is
worth reading closely: SharePoint is the first connector that cannot be
literally GET-only, because OAuth2 client credentials are fetched with a POST.
`test_the_only_post_is_the_token_request` is what keeps the exception from
widening — it asserts on the URLs the guard actually recorded, so a POST to
Graph would fail the build rather than pass unnoticed.

The other tests worth reading are the scope ones. This is the only HTTP
connector that can inspect its own credential, and refusing a tenant-wide token
is the check that stops a datasource being pointed at every site in the tenant
by accident.

The fake refuses `/search` outright. Graph's search endpoint ignores
`Sites.Selected` in application-only mode, so a connector that reached for it
would silently escape its own scoping; that has to be a failing test rather than
a comment in the source.
"""

import asyncio
import base64
import json
import re
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import pytest

from eiye_db.connectors.base import ConnectorError
from eiye_db.connectors.http_bearer import LOGIN_HOST, token_roles
from eiye_db.connectors.sharepoint import (
    SELECTED_SCOPES,
    SharePointConnector,
    graph_path,
)
from tests.readonly_guards import GetOnlyTransport, WriteAttempted

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT_ID = "66666666-7777-8888-9999-000000000000"
SECRET = "s3cret"
HOSTNAME = "contoso.sharepoint.com"
SITE_PATH = "/sites/finance"
SITE_ID = f"{HOSTNAME},aaaa,bbbb"
DRIVE_ID = "b!drive"

CSV = "name,amount\nacme,10\nbeta,20\n"
TOP_CSV = "id,label\n1,top\n"

#: The library, as a folder -> entries map. `reports/old` is two levels down, so
#: reaching `old/q1.csv` proves the walk recurses rather than listing one level.
LIBRARY: dict[str, list[dict]] = {
    "": [
        {"name": "reports", "folder": {"childCount": 2}},
        {"name": "readme.txt", "file": {}, "size": 12},
        {"name": "top.csv", "file": {}, "size": len(TOP_CSV)},
    ],
    "reports": [
        {"name": "q3.csv", "file": {}, "size": len(CSV)},
        {"name": "old", "folder": {"childCount": 1}},
    ],
    "reports/old": [
        {"name": "q1.csv", "file": {}, "size": len(CSV)},
    ],
}

CONTENT: dict[str, bytes] = {
    "readme.txt": b"a plain note",
    "top.csv": TOP_CSV.encode(),
    "reports/q3.csv": CSV.encode(),
    "reports/old/q1.csv": CSV.encode(),
}

_ROOT = re.compile(r"^/v1\.0/drives/(?P<drive>[^/]+)/root(?P<rest>.*)$")


def jwt(roles: list[str] | None, *, malformed: bool = False) -> str:
    """A token the right shape for `token_roles` to read.

    Unsigned on purpose: the connector deliberately does not verify, because
    eiye is not the audience and holds none of Entra's keys. A test that signed
    it would be testing something the product does not do.
    """
    if malformed:
        return "not-a-jwt"

    def seg(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    claims: dict = {"aud": "https://graph.microsoft.com", "appid": CLIENT_ID}
    if roles is not None:
        claims["roles"] = roles
    return f"{seg({'alg': 'RS256', 'typ': 'JWT'})}.{seg(claims)}.signature"


class FakeGraph:
    """Entra's token endpoint and the slice of Graph this connector touches.

    `paths` is the point: a test can assert which Graph endpoints were actually
    called — and, just as importantly, which were not.
    """

    def __init__(
        self,
        roles: list[str] | None = None,
        *,
        page_size: int = 50,
        libraries: tuple[str, ...] = ("Documents", "Archive"),
        malformed_token: bool = False,
        throttle: bool = False,
        redirect_content: bool = False,
    ):
        self.roles = ["Sites.Selected"] if roles is None else roles
        self.page_size = page_size
        self.libraries = libraries
        self.malformed_token = malformed_token
        self.throttle = throttle
        self.redirect_content = redirect_content
        self.paths: list[str] = []
        self.token_requests = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        path = url.path
        self.paths.append(path)

        if url.host == LOGIN_HOST:
            return self._token(request)

        if path.startswith("/v1.0/search") or "/search/" in path:
            # Never reached by a correct connector. Raising rather than
            # returning an error makes this impossible to swallow.
            raise WriteAttempted(f"the connector called the Graph search endpoint: {url}")

        if url.host == "download.sharepoint.example":
            # A pre-authenticated download URL, checked before the bearer test
            # because httpx correctly drops Authorization across hosts.
            return self._content(request, unquote(path.lstrip("/")))

        if request.headers.get("authorization") != f"Bearer {self._access_token()}":
            return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken",
                                                       "message": "Access token is empty."}})
        if self.throttle:
            return httpx.Response(429, headers={"Retry-After": "17"},
                                  json={"error": {"code": "activityLimitReached",
                                                  "message": "Too many requests"}})

        if path == f"/v1.0/sites/{HOSTNAME}:{SITE_PATH}":
            return httpx.Response(200, json={"id": SITE_ID, "webUrl": f"https://{HOSTNAME}{SITE_PATH}"})
        if path == f"/v1.0/sites/{SITE_ID}/drives":
            return httpx.Response(
                200,
                json={"value": [{"id": DRIVE_ID if n == "Documents" else f"b!{n}", "name": n}
                                for n in self.libraries]},
            )

        match = _ROOT.match(path)
        if match:
            if match.group("drive") != DRIVE_ID:
                return httpx.Response(404, json={"error": {"message": "drive not found"}})
            return self._drive(request, match.group("rest"))

        return httpx.Response(404, json={"error": {"message": f"no route for {path}"}})

    # --- Entra ---------------------------------------------------------------

    def _access_token(self) -> str:
        return jwt(self.roles, malformed=self.malformed_token)

    def _token(self, request: httpx.Request) -> httpx.Response:
        self.token_requests += 1
        form = parse_qs(request.content.decode())
        if form.get("client_secret", [""])[0] != SECRET:
            return httpx.Response(
                401,
                json={"error": "invalid_client",
                      "error_description": "AADSTS7000215: Invalid client secret provided."},
            )
        return httpx.Response(
            200, json={"access_token": self._access_token(), "expires_in": 3599,
                       "token_type": "Bearer"}
        )

    # --- drive ---------------------------------------------------------------

    def _drive(self, request: httpx.Request, rest: str) -> httpx.Response:
        if rest == "/children":
            return self._children(request, "")
        listing = re.match(r"^:/(?P<path>.*):/children$", rest)
        if listing:
            return self._children(request, unquote(listing.group("path")))
        content = re.match(r"^:/(?P<path>.*):/content$", rest)
        if content:
            path = unquote(content.group("path"))
            if self.redirect_content:
                return httpx.Response(
                    302, headers={"Location": f"https://download.sharepoint.example/{path}"}
                )
            return self._content(request, path)
        return httpx.Response(404, json={"error": {"message": f"no route for root{rest}"}})

    def _children(self, request: httpx.Request, folder: str) -> httpx.Response:
        if folder not in LIBRARY:
            return httpx.Response(404, json={"error": {"code": "itemNotFound",
                                                       "message": f"no folder {folder}"}})
        entries = LIBRARY[folder]
        skip = int(parse_qs(urlsplit(str(request.url)).query).get("$skiptoken", ["0"])[0])
        page = entries[skip : skip + self.page_size]
        body: dict = {"value": page}
        if skip + self.page_size < len(entries):
            # Graph hands back a fully-formed URL, query string included. The
            # connector must use it verbatim; rebuilding it from an offset is
            # how a paginator starts repeating a page.
            body["@odata.nextLink"] = str(request.url.copy_set_param("$skiptoken",
                                                                     str(skip + self.page_size)))
        return httpx.Response(200, json=body)

    def _content(self, request: httpx.Request, path: str) -> httpx.Response:
        if path not in CONTENT:
            return httpx.Response(404, json={"error": {"message": f"no file {path}"}})
        data = CONTENT[path]
        header = request.headers.get("range", "")
        span = re.match(r"bytes=0-(\d+)$", header)
        if span:
            data = data[: int(span.group(1)) + 1]
        return httpx.Response(200, content=data)


def build(fake: FakeGraph, **config) -> tuple[SharePointConnector, GetOnlyTransport]:
    guard = GetOnlyTransport(httpx.MockTransport(fake), post_allowed_hosts=frozenset({LOGIN_HOST}))
    settings = {
        "tenant_id": TENANT,
        "client_id": CLIENT_ID,
        "client_secret": SECRET,
        "site_url": f"https://{HOSTNAME}{SITE_PATH}",
    }
    settings.update(config)
    return SharePointConnector(settings, transport=guard), guard


# --- the read-only claim ------------------------------------------------------


def test_the_only_post_is_the_token_request():
    """The exception to GET-only, pinned to one URL.

    If this ever records a second host, the connector has started writing
    somewhere and the structural read-only claim is no longer true.
    """
    fake = FakeGraph()
    connector, guard = build(fake)
    asyncio.run(connector.discover_schema())

    assert guard.posts_seen == [f"https://{LOGIN_HOST}/{TENANT}/oauth2/v2.0/token"]
    assert set(guard.methods_seen) == {"GET", "POST"}
    assert guard.methods_seen.count("POST") == 1


def test_a_post_to_graph_would_fail_the_build():
    """Proof the guard is armed rather than merely configured. Posting to Graph
    through the connector's own transport must raise, not be recorded."""
    _, guard = build(FakeGraph())

    async def post() -> None:
        async with httpx.AsyncClient(transport=guard) as client:
            await client.post("https://graph.microsoft.com/v1.0/sites/x/permissions", json={})

    with pytest.raises(WriteAttempted):
        asyncio.run(post())


def test_the_search_endpoint_is_never_called():
    """Graph search ignores Sites.Selected app-only, so reaching it would defeat
    the entire scoping model. Asserted on the recorded paths, not on intent."""
    fake = FakeGraph()
    connector, _ = build(fake)
    asyncio.run(connector.discover_schema())
    asyncio.run(connector.query({"path": "reports/q3.csv"}, 10))
    assert fake.paths, "the fake recorded nothing, so this assertion proves nothing"
    assert not any("search" in p for p in fake.paths)


# --- credential scope ---------------------------------------------------------


@pytest.mark.parametrize("scope", sorted(SELECTED_SCOPES))
def test_every_selected_scope_is_accepted(scope):
    connector, _ = build(FakeGraph(roles=[scope]))
    asyncio.run(connector.test_connection())


@pytest.mark.parametrize(
    "roles",
    [
        ["Sites.Read.All"],
        ["Files.Read.All"],
        ["Sites.FullControl.All"],
        # Broad *and* narrow: the broad one still wins, because it still grants
        # every site in the tenant.
        ["Sites.Selected", "Files.ReadWrite.All"],
    ],
)
def test_a_tenant_wide_credential_is_refused(roles):
    connector, _ = build(FakeGraph(roles=roles))
    with pytest.raises(ConnectorError) as e:
        asyncio.run(connector.test_connection())
    assert "tenant" in str(e.value)
    # The message has to say what to do instead, or an operator is stuck.
    assert "Sites.Selected" in str(e.value)


def test_a_token_with_no_roles_is_refused():
    """Consent without admin approval yields a token with no roles claim. That
    is unverifiable rather than harmless, so it is refused."""
    connector, _ = build(FakeGraph(roles=[]))
    with pytest.raises(ConnectorError) as e:
        asyncio.run(connector.test_connection())
    assert "roles" in str(e.value)


def test_an_unreadable_token_is_refused():
    """An opaque token would turn the one check this connector can make into a
    check it only sometimes makes."""
    connector, _ = build(FakeGraph(malformed_token=True))
    with pytest.raises(ConnectorError) as e:
        asyncio.run(connector.test_connection())
    assert "could not read" in str(e.value)


def test_an_unrelated_scope_is_refused():
    connector, _ = build(FakeGraph(roles=["User.Read.All"]))
    with pytest.raises(ConnectorError) as e:
        asyncio.run(connector.test_connection())
    assert "Selected scope" in str(e.value)


def test_token_roles_reads_an_entra_shaped_token():
    assert token_roles(jwt(["Sites.Selected"])) == ["Sites.Selected"]
    assert token_roles("opaque") == []
    assert token_roles(jwt(None)) == []


def test_the_token_is_cached_across_calls():
    """Discovery makes tens of requests; a token per request would make every
    pass tens of round trips to Entra as well."""
    fake = FakeGraph()
    connector, _ = build(fake)
    asyncio.run(connector.discover_schema())
    asyncio.run(connector.query({"path": "top.csv"}, 10))
    assert fake.token_requests == 1


def test_a_bad_secret_reports_entras_own_message():
    fake = FakeGraph()
    connector, _ = build(fake, client_secret="wrong")
    with pytest.raises(ConnectorError) as e:
        asyncio.run(connector.test_connection())
    assert "AADSTS7000215" in str(e.value)


# --- config -------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["tenant_id", "client_id", "client_secret", "site_url"])
def test_every_required_setting_is_named_when_absent(missing):
    fake = FakeGraph()
    connector, _ = build(fake, **{missing: ""})
    with pytest.raises(ConnectorError) as e:
        asyncio.run(connector.test_connection())
    assert missing in str(e.value)


def test_a_relative_site_url_is_refused():
    connector, _ = build(FakeGraph(), site_url="contoso.sharepoint.com/sites/finance")
    with pytest.raises(ConnectorError) as e:
        asyncio.run(connector.test_connection())
    assert "absolute" in str(e.value)


def test_an_unknown_library_names_the_ones_that_exist():
    """The likeliest cause is a renamed library or a missing permission grant,
    and a bare 404 sends an operator looking in the wrong place."""
    connector, _ = build(FakeGraph(), library="Nope")
    with pytest.raises(ConnectorError) as e:
        asyncio.run(connector.test_connection())
    assert "Documents" in str(e.value) and "Archive" in str(e.value)


def test_the_library_defaults_to_documents():
    connector, _ = build(FakeGraph())
    asyncio.run(connector.test_connection())


# --- path handling ------------------------------------------------------------


def test_graph_path_encodes_and_normalises():
    assert graph_path("reports", "q3 report.csv") == "reports/q3%20report.csv"
    assert graph_path("", "a//b/./c.txt") == "a/b/c.txt"
    # Graph's own delimiter must not survive from inside a segment.
    assert graph_path("a:b.csv") == "a%3Ab.csv"


def test_graph_path_refuses_traversal():
    """A Graph path is genuinely hierarchical — unlike an S3 key, the server
    resolves `..` — so this is an escape from the configured folder."""
    with pytest.raises(ConnectorError) as e:
        graph_path("reports", "../../etc/secrets.csv")
    assert ".." in str(e.value)


def test_a_query_cannot_traverse_out_of_the_folder():
    connector, _ = build(FakeGraph(), folder="reports")
    with pytest.raises(ConnectorError):
        asyncio.run(connector.query({"path": "../top.csv"}, 10))


def test_a_query_without_a_path_is_refused():
    connector, _ = build(FakeGraph())
    with pytest.raises(ConnectorError) as e:
        asyncio.run(connector.query({}, 10))
    assert "path" in str(e.value)


# --- discovery ----------------------------------------------------------------


def test_discovery_walks_the_whole_tree():
    connector, _ = build(FakeGraph())
    tables = asyncio.run(connector.discover_schema())
    names = {t["name"] for t in tables}
    assert names == {"readme.txt", "top.csv", "reports/q3.csv", "reports/old/q1.csv"}


def test_discovery_is_bounded_by_the_folder_setting():
    """`folder` is this connector's `prefix`: nothing above it is discoverable."""
    connector, _ = build(FakeGraph(), folder="reports")
    tables = asyncio.run(connector.discover_schema())
    assert {t["name"] for t in tables} == {"q3.csv", "old/q1.csv"}


def test_discovery_sniffs_csv_columns():
    connector, _ = build(FakeGraph())
    tables = {t["name"]: t for t in asyncio.run(connector.discover_schema())}
    assert [f["name"] for f in tables["reports/q3.csv"]["fields"]] == ["name", "amount"]
    assert tables["readme.txt"]["fields"] == [{"name": "content", "type": "text"}]


def test_pagination_follows_the_odata_next_link():
    """One entry per page, so every folder in the fixture needs at least one
    follow-up request. The assertion is on the files found, because a paginator
    that repeats a page still returns rows."""
    connector, _ = build(FakeGraph(page_size=1))
    tables = asyncio.run(connector.discover_schema())
    assert {t["name"] for t in tables} == {"readme.txt", "top.csv", "reports/q3.csv",
                                           "reports/old/q1.csv"}


# --- query --------------------------------------------------------------------


def test_query_returns_csv_rows():
    connector, _ = build(FakeGraph())
    rows = asyncio.run(connector.query({"path": "reports/q3.csv"}, 10))
    assert rows == [{"name": "acme", "amount": "10"}, {"name": "beta", "amount": "20"}]


def test_query_is_relative_to_the_folder():
    connector, _ = build(FakeGraph(), folder="reports")
    rows = asyncio.run(connector.query({"path": "q3.csv"}, 10))
    assert len(rows) == 2


def test_query_reads_text():
    connector, _ = build(FakeGraph())
    rows = asyncio.run(connector.query({"path": "readme.txt"}, 10))
    assert "a plain note" in str(rows)


def test_a_content_redirect_is_followed():
    """Graph answers /content with a 302 to a pre-authenticated URL on another
    host. The guard still sees the follow-up, and it is still a GET."""
    fake = FakeGraph(redirect_content=True)
    connector, guard = build(fake)
    rows = asyncio.run(connector.query({"path": "reports/q3.csv"}, 10))
    assert len(rows) == 2
    assert "download.sharepoint.example" in " ".join(fake.paths) or any(
        p.endswith("q3.csv") for p in fake.paths
    )
    assert guard.methods_seen.count("POST") == 1


def test_a_missing_file_is_reported_by_name():
    connector, _ = build(FakeGraph())
    with pytest.raises(ConnectorError) as e:
        asyncio.run(connector.query({"path": "nope.csv"}, 10))
    assert "nope.csv" in str(e.value)


def test_throttling_says_what_to_do():
    """Graph throttles far harder than the other HTTP sources. eiye does not
    retry on its own — a connector that silently sleeps turns a governed query
    into an unbounded one — so the error has to be actionable."""
    connector, _ = build(FakeGraph(throttle=True))
    with pytest.raises(ConnectorError) as e:
        asyncio.run(connector.test_connection())
    assert "throttling" in str(e.value) and "17" in str(e.value)
