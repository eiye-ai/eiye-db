"""SharePoint / OneDrive document libraries, over Microsoft Graph.

Read-only is structural, as it is for the other HTTP sources: Graph offers no
"is this app read-only" probe, so the guarantee is that this module issues GET
and nothing else. The one exception is the OAuth2 token request, which is a
POST by protocol; `GetOnlyTransport` is given exactly `login.microsoftonline.com`
in the tests and records what was posted there, so the exception is bounded and
visible rather than hidden behind a second HTTP client.

Two things are unusual enough to read before changing anything.

**This connector inspects its own credential, which no other HTTP source can.**
Entra application-only tokens carry a `roles` claim listing the granted
application permissions, so `_assert_selected_scope` refuses a token carrying
`Sites.Read.All`, `Files.Read.All` or `Sites.FullControl.All` and insists on one
of the `*.Selected` scopes. That is deliberately a check on *breadth*, not on
read-versus-write: the read/write role in the Selected model lives on the
resource rather than in the token, so it is not visible here. What the check
buys is that a datasource cannot be pointed at a tenant-wide credential by
accident — the failure mode where an app consented `Files.Read.All` for some
other integration quietly gains eiye a view of every file in the tenant.

**Item-level ACLs are not applied, and cannot be.** Microsoft's access
calculation grants an application-only token access to a resource if a
permission record exists on it *or on a securable hierarchical parent*. A grant
on a site or a library is that parent, so this connector reads everything
beneath the grant regardless of unique permissions set on individual files. Only
the delegated flow intersects app permissions with a user's, and eiye's ABAC
subject is an API key id, not an Entra user, so there is nothing to intersect
with. `GET /permissions` is also documented as not returning the full permission
set app-only, so eiye cannot even reliably report the ACLs it is not applying.

  The consequence, and it belongs in the README rather than only here: **every
  file under the configured library and folder is visible to any agent whose
  ABAC policy allows this datasource.** That is the same contract as a Postgres
  datasource — the login's grants define the data, and ABAC decides who may
  query it — but SharePoint carries an expectation of per-user ACLs that a
  purpose-provisioned database login does not, so it has to be said out loud.
  Scope the grant to a single document library, not a site collection, and put
  nothing in that library you would not show every agent that can reach it.

**`/search/query` is never called and must never be.** It does not enforce
`Sites.Selected` in application-only mode — it runs against the tenant-wide
search index — so the obvious implementation of "find a document" would defeat
the entire scoping model. The test suite fails the build if any request path
starts with `/search`.

Config:

    tenant_id      required   Entra directory (tenant) id
    client_id      required   the app registration's client id
    client_secret  required   a secret on that app registration
    site_url       required   https://contoso.sharepoint.com/sites/finance
    library        optional   document library display name; defaults to Documents
    folder         optional   path within the library; bounds what this datasource exposes

Not a crawler, not a Graph search front-end, not a copy of the library.
"""

import io
import time
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from eiye_db.connectors import documents
from eiye_db.connectors.base import Connector, ConnectorError
from eiye_db.connectors.http_bearer import (
    GRAPH_SCOPE,
    fetch_token,
    get_json,
    token_roles,
)

GRAPH = "https://graph.microsoft.com/v1.0"

AUTH_HINT = (
    "A Selected scope grants nothing until an administrator also runs the matching "
    "POST /sites/{id}/permissions (or /lists/{id}/permissions) for this app. Consent alone is not access."
)

#: Tenant-wide scopes. Any one of these makes the Selected model meaningless,
#: so a token carrying one is refused rather than used narrowly by convention.
TENANT_WIDE_SCOPES = frozenset(
    {
        "Sites.Read.All",
        "Sites.ReadWrite.All",
        "Sites.Manage.All",
        "Sites.FullControl.All",
        "Files.Read.All",
        "Files.ReadWrite.All",
    }
)

#: The scopes this connector will accept. All four default to no access at all
#: until an administrator grants the app a role on a specific resource.
SELECTED_SCOPES = frozenset(
    {
        "Sites.Selected",
        "Lists.SelectedOperations.Selected",
        "ListItems.SelectedOperations.Selected",
        "Files.SelectedOperations.Selected",
    }
)

_DEFAULT_LIBRARY = "Documents"

_PAGE_SIZE = 200

# A ceiling on Graph calls per discovery pass. A document library is a tree and
# listing it costs one request per folder, so without this a deep library turns
# a single discovery into an unbounded crawl.
_MAX_REQUESTS = 40

_MAX_FILE_BYTES = 32 * 1024 * 1024
_CSV_SNIFF_BYTES = 64 * 1024
_MAX_SNIFFED_CSVS = 20


def graph_path(*segments: str) -> str:
    """Percent-encode a user-supplied path for Graph's `root:/{path}:` syntax.

    Unlike an S3 key, a Graph path is genuinely hierarchical — the server
    resolves `..` — so traversal is a real escape from the configured folder
    rather than a literal two characters. Segments are checked here rather than
    at the call sites so there is one place to get it right.
    """
    parts: list[str] = []
    for segment in segments:
        for part in str(segment).replace("\\", "/").split("/"):
            if not part or part == ".":
                continue
            if part == "..":
                raise ConnectorError(
                    "'..' is not allowed in a SharePoint path: it would resolve outside the folder "
                    "this datasource is scoped to."
                )
            parts.append(part)
    # Colon is Graph's own delimiter in the `root:/path:` form and slash is the
    # separator we are building, so neither may survive from inside a segment.
    return "/".join(quote(p, safe="") for p in parts)


class SharePointConnector(Connector):
    def __init__(self, config: dict[str, Any], transport: httpx.AsyncBaseTransport | None = None):
        super().__init__(config)
        self._transport = transport
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self._drive_id: str | None = None

    # --- config --------------------------------------------------------------

    def _required(self, key: str, example: str) -> str:
        value = self.config.get(key)
        if not value:
            raise ConnectorError(f"sharepoint config requires '{key}', e.g. {example}")
        return str(value).strip()

    def _site(self) -> tuple[str, str]:
        """Split the site URL into the hostname and server-relative path Graph wants.

        `https://contoso.sharepoint.com/sites/finance` becomes
        `contoso.sharepoint.com` and `/sites/finance`, which addresses the site
        as `/sites/{hostname}:{path}`. The root site of a tenant has an empty
        path, and Graph accepts that form too.
        """
        site_url = self._required("site_url", "https://contoso.sharepoint.com/sites/finance")
        parts = urlsplit(site_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ConnectorError(
                f"sharepoint site_url must be an absolute http(s) URL, got '{site_url}'"
            )
        return parts.hostname, parts.path.rstrip("/")

    def _library(self) -> str:
        return str(self.config.get("library") or _DEFAULT_LIBRARY).strip()

    def _folder(self) -> str:
        """The prefix this datasource is bounded to, already encoded."""
        return graph_path(self.config.get("folder") or "")

    # --- auth ----------------------------------------------------------------

    async def _access_token(self) -> str:
        """Cached for the token's own lifetime, minus a safety margin.

        Discovery makes tens of requests and a fresh token per request would
        turn every pass into tens of round trips to Entra as well.
        """
        if self._token and time.time() < self._token_expiry:
            return self._token
        token, expiry = await fetch_token(
            self._required("tenant_id", "a-guid"),
            self._required("client_id", "a-guid"),
            self._required("client_secret", "the app registration's secret"),
            scope=GRAPH_SCOPE,
            transport=self._transport,
        )
        self._assert_selected_scope(token)
        self._token, self._token_expiry = token, expiry
        return token

    @staticmethod
    def _assert_selected_scope(token: str) -> None:
        """Refuse a credential broader than the Selected model.

        An opaque token is refused too. Graph tokens are JWTs today, and
        treating an unreadable one as "probably fine" would turn the one check
        this connector can make into a check it only sometimes makes.
        """
        roles = set(token_roles(token))
        if not roles:
            raise ConnectorError(
                "could not read the application permissions from the Entra token, so eiye cannot "
                "confirm the credential is scoped. Check the app registration has an application "
                "permission granted and admin-consented — a token with no roles claim usually means "
                "none was."
            )
        overbroad = sorted(roles & TENANT_WIDE_SCOPES)
        if overbroad:
            raise ConnectorError(
                f"this app is consented {', '.join(overbroad)}, which grants it every site in the "
                "tenant. eiye refuses tenant-wide SharePoint credentials. Consent one of "
                f"{', '.join(sorted(SELECTED_SCOPES))} instead, then grant this app a 'read' role on "
                "the one library this datasource should serve."
            )
        if not roles & SELECTED_SCOPES:
            raise ConnectorError(
                f"this app's token carries {', '.join(sorted(roles))}, none of which is a SharePoint "
                f"Selected scope. Consent one of {', '.join(sorted(SELECTED_SCOPES))}."
            )

    async def _client(self) -> httpx.AsyncClient:
        token = await self._access_token()
        return httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=30,
            transport=self._transport,
            # Content downloads answer with a redirect to a pre-authenticated
            # URL on another host. httpx drops the Authorization header across
            # hosts, which is what we want — that URL carries its own.
            follow_redirects=True,
        )

    # --- Graph ---------------------------------------------------------------

    async def _drive(self, client: httpx.AsyncClient) -> str:
        """Resolve the configured library to a drive id, once per connector.

        Two calls rather than one: Graph addresses a site by hostname and path,
        and only then can its drives be listed. The library is matched by
        display name because that is what an operator can see in SharePoint;
        the drive id is a guid nobody has to hand.
        """
        if self._drive_id:
            return self._drive_id
        hostname, path = self._site()
        site = await get_json(client, f"{GRAPH}/sites/{hostname}:{path}", auth_hint=AUTH_HINT)
        site_id = site.get("id")
        if not site_id:
            raise ConnectorError(f"Graph returned no id for site {hostname}{path}")

        library = self._library()
        drives = await get_json(
            client, f"{GRAPH}/sites/{site_id}/drives", {"$select": "id,name"}, auth_hint=AUTH_HINT
        )
        available = [d for d in (drives.get("value") or []) if isinstance(d, dict)]
        for drive in available:
            if str(drive.get("name", "")).casefold() == library.casefold():
                self._drive_id = str(drive["id"])
                return self._drive_id
        names = ", ".join(sorted(str(d.get("name", "")) for d in available)) or "none"
        raise ConnectorError(
            f"no document library named '{library}' on {hostname}{path}. Libraries visible to this "
            f"app: {names}."
        )

    async def _children(self, client: httpx.AsyncClient, drive_id: str, folder: str) -> list[dict]:
        """List one folder, following `@odata.nextLink` — the fifth pagination
        scheme across the HTTP connectors, and the only one that hands back a
        fully-formed URL including its own query string."""
        if folder:
            url = f"{GRAPH}/drives/{drive_id}/root:/{folder}:/children"
        else:
            url = f"{GRAPH}/drives/{drive_id}/root/children"
        params: dict | None = {"$top": _PAGE_SIZE, "$select": "name,size,folder,file"}
        out: list[dict] = []
        for _ in range(_MAX_REQUESTS):
            page = await get_json(client, url, params, auth_hint=AUTH_HINT)
            out.extend(v for v in (page.get("value") or []) if isinstance(v, dict))
            next_url = page.get("@odata.nextLink")
            if not next_url:
                break
            url, params = str(next_url), None
        return out

    async def _walk(self, client: httpx.AsyncClient, drive_id: str) -> list[str]:
        """Every file under the configured folder, as paths relative to it.

        Breadth-first with a request budget shared across the whole tree, so a
        library with a thousand folders truncates rather than crawling. The
        budget is not an error: a datasource scoped that widely is a
        configuration problem, and the folder setting is how it is fixed.
        """
        root = self._folder()
        queue: list[str] = [""]
        files: list[str] = []
        requests = 0
        while queue and requests < _MAX_REQUESTS:
            relative = queue.pop(0)
            requests += 1
            absolute = graph_path(root, relative)
            for entry in await self._children(client, drive_id, absolute):
                name = str(entry.get("name") or "")
                if not name:
                    continue
                child = f"{relative}/{name}" if relative else name
                # Presence, not truthiness. `folder` and `file` are facets, and
                # Graph is entitled to return an empty one — `"file": {}` is a
                # file, and testing it for truth silently drops it.
                if "folder" in entry:
                    queue.append(child)
                elif "file" in entry:
                    files.append(child)
        return files

    async def _fetch(
        self, client: httpx.AsyncClient, drive_id: str, path: str, max_bytes: int
    ) -> tuple[bytes, bool]:
        """Read at most `max_bytes` of one file, and say whether it was truncated.

        The range asks for one byte past the cap, so a full-length response is
        proof the file is longer rather than a coincidence — the same trick the
        S3 connector uses, and for the same reason.
        """
        url = f"{GRAPH}/drives/{drive_id}/root:/{path}:/content"
        try:
            response = await client.get(url, headers={"Range": f"bytes=0-{max_bytes}"})
        except httpx.HTTPError as e:
            raise ConnectorError(f"cannot read {path}: {e}") from e
        if response.status_code >= 400:
            raise ConnectorError(f"cannot read {path}: HTTP {response.status_code}")
        data = response.content
        return data[:max_bytes], len(data) > max_bytes

    # --- contract ------------------------------------------------------------

    async def test_connection(self) -> None:
        """Prove the credential is scoped, the site resolves, and the library exists.

        Resolving the drive is the real check: consent without the matching
        `POST /permissions` grant is the most common way a Selected-scope app is
        misconfigured, and it fails exactly here rather than later on a query.
        """
        client = await self._client()
        try:
            drive_id = await self._drive(client)
            await self._children(client, drive_id, self._folder())
        finally:
            await client.aclose()

    async def discover_schema(self) -> list[dict[str, Any]]:
        """One table per file, as the S3 and filesystem connectors do."""
        client = await self._client()
        try:
            drive_id = await self._drive(client)
            root = self._folder()
            tables: list[dict[str, Any]] = []
            sniffed = 0
            for relative in await self._walk(client, drive_id):
                kind = documents.kind_for(relative)
                if kind == "csv":
                    fields: list[dict[str, Any]] = []
                    if sniffed < _MAX_SNIFFED_CSVS:
                        sniffed += 1
                        fields = await self._csv_fields(client, drive_id, graph_path(root, relative))
                    tables.append({"name": relative, "fields": fields})
                elif kind == "xlsx":
                    # A workbook is a zip; its first 64 KiB says nothing, and
                    # fetching every workbook whole would make discovery a
                    # download of the library. Columns appear on query.
                    tables.append({"name": relative, "fields": []})
                elif kind in ("pdf", "text"):
                    tables.append({"name": relative, "fields": [{"name": "content", "type": "text"}]})
            return tables
        finally:
            await client.aclose()

    async def _csv_fields(
        self, client: httpx.AsyncClient, drive_id: str, path: str
    ) -> list[dict[str, Any]]:
        data, truncated = await self._fetch(client, drive_id, path, _CSV_SNIFF_BYTES)
        text = data.decode("utf-8", errors="replace")
        if truncated:
            # Drop the partial last line so a half-read row cannot skew the
            # inferred types.
            text = text[: text.rfind("\n") + 1]
        return documents.csv_fields(io.StringIO(text, newline=""))

    async def query(self, request: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        """`{"path": "reports/q3.csv"}` — a path relative to the configured folder.

        There is deliberately no search, no `$filter` passthrough and no item-id
        form. Graph's search endpoint ignores this app's site scoping entirely
        (see the module docstring), and a filter expression is a query language
        that would let a caller address content the folder setting excludes.
        """
        path = request.get("path")
        if not path:
            raise ConnectorError("sharepoint query requires 'path', relative to the configured folder")
        full = graph_path(self._folder(), str(path))
        client = await self._client()
        try:
            drive_id = await self._drive(client)
            data, truncated = await self._fetch(client, drive_id, full, _MAX_FILE_BYTES)
        finally:
            await client.aclose()
        if truncated:
            raise ConnectorError(
                f"file exceeds the {_MAX_FILE_BYTES // (1024 * 1024)} MiB eiye reads in one query: {path}"
            )
        kind = documents.kind_for(str(path))
        if kind == "csv":
            return documents.csv_rows(
                io.StringIO(data.decode("utf-8", errors="replace"), newline=""), limit
            )
        if kind == "xlsx":
            return documents.xlsx_rows(io.BytesIO(data), limit, str(path))
        if kind == "pdf":
            return documents.pdf_rows(io.BytesIO(data), str(path))
        return documents.text_rows(data.decode("utf-8", errors="replace"))
