"""Confluence Cloud connector. GET-only, operator-held API token.

Read-only here is **structural**, not server-enforced: Confluence offers no
read-only credential and no way to ask whether a token can write, so there is no
equivalent of the login check the SQL connectors run. The guarantee is that this
module only ever issues GET requests, and the test suite enforces that with the
transport guard in `tests/readonly_guards.py` rather than leaving it to a
promise. See the README's "The two read-only claims" section for what that does
and does not prove.

**Cloud rather than Data Center**, which reverses the earlier plan. Data Center
was chosen to avoid operating an OAuth hop on the customer's behalf — but Cloud
authenticates REST with an operator-minted API token over HTTP Basic, so it
meets that constraint the same way a Postgres DSN or an S3 key pair does. Data
Center meanwhile has no official container image and needs a licence Atlassian
stopped self-serving in March 2026. Note for operators: **Atlassian API tokens
expire after one year.**

Scope is a first-class part of the config. `space_key` confines discovery and
every query to one space, the way `prefix` does for S3 and `root` does for the
filesystem — a governed surface should be able to expose one space without
exposing the site.

Pagination follows `_links.next`, which carries an opaque cursor and is absent
on the last page. Those cursor URLs are site-absolute and already carry `/wiki`,
which is why `AtlassianCloudConnector._site` reduces `base_url` to the bare
origin — basing the client any deeper would double the prefix on every paginated
request.

Auth, the HTTP client and error mapping live in `atlassian.py`, shared with the
Jira connector. Pagination does not: Jira's issue search returns a bare
`nextPageToken` rather than a URL, so the two walk differently.
"""

from html.parser import HTMLParser
from typing import Any

import httpx

from eiye_db.connectors import documents
from eiye_db.connectors.atlassian import AtlassianCloudConnector
from eiye_db.connectors.base import ConnectorError

# Confluence caps `limit` at 250 for these endpoints; ask for the most per
# round-trip and let the caller's own limit do the trimming.
_PAGE_SIZE = 250

# A ceiling on how many pages of results one call will walk, so a space with
# 50,000 pages cannot turn a single query into an unbounded crawl.
_MAX_REQUESTS = 20

# The metadata this connector reports for a page. Declared once because
# discover_schema advertises it and query has to produce exactly it — the two
# drifting apart is how a schema starts lying about its own rows.
_PAGE_FIELDS = [
    {"name": "id", "type": "string"},
    {"name": "title", "type": "string"},
    {"name": "status", "type": "string"},
    {"name": "space_id", "type": "string"},
    {"name": "parent_id", "type": "string"},
    {"name": "author_id", "type": "string"},
    {"name": "created_at", "type": "string"},
    {"name": "url", "type": "string"},
]


class _TextExtractor(HTMLParser):
    """Collect visible text from Confluence storage format.

    Storage format is XHTML with Confluence's own `ac:`/`ri:` elements mixed in.
    Those carry macro parameters rather than prose, and `convert_charrefs`
    handles the entity decoding, so collecting character data and dropping tags
    gives readable text without a markup dependency.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.parts.append(text)

    def text(self) -> str:
        return "\n".join(self.parts)


def storage_to_text(storage: str) -> str:
    """Flatten Confluence storage format to plain text.

    Extraction is best-effort by design: a page whose markup cannot be parsed
    should degrade to something a caller can still read and a PII scan can still
    inspect, rather than failing the whole query.
    """
    parser = _TextExtractor()
    try:
        parser.feed(storage)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup must not fail the query
        return storage[: documents.MAX_TEXT_CHARS]
    return parser.text()[: documents.MAX_TEXT_CHARS]


def page_row(page: dict[str, Any], site: str) -> dict[str, Any]:
    """Map one v2 page object to the flat shape `_PAGE_FIELDS` advertises."""
    links = page.get("_links") or {}
    webui = links.get("webui") or ""
    return {
        "id": str(page.get("id", "")),
        "title": page.get("title", ""),
        "status": page.get("status", ""),
        "space_id": str(page.get("spaceId", "")),
        "parent_id": str(page.get("parentId") or ""),
        "author_id": page.get("authorId", ""),
        "created_at": page.get("createdAt", ""),
        "url": f"{site}/wiki{webui}" if webui else "",
    }


class ConfluenceConnector(AtlassianCloudConnector):
    PRODUCT = "confluence"

    def _space_key(self) -> str | None:
        key = self.config.get("space_key")
        return key.strip() if isinstance(key, str) and key.strip() else None

    # --- HTTP ----------------------------------------------------------------

    async def _paginate(
        self, client: httpx.AsyncClient, path: str, params: dict, limit: int
    ) -> list[dict[str, Any]]:
        """Walk `_links.next` until it is absent, `limit` is reached, or the
        request ceiling is hit.

        The cursor is opaque and site-absolute, so each subsequent request uses
        it verbatim rather than reconstructing the query string — reconstructing
        it is how a paginator starts silently repeating page one.
        """
        results: list[dict[str, Any]] = []
        next_path: str | None = path
        next_params: dict | None = params
        for _ in range(_MAX_REQUESTS):
            if next_path is None:
                break
            body = await self._get(client, next_path, next_params)
            results.extend(body.get("results") or [])
            if len(results) >= limit:
                break
            next_path = (body.get("_links") or {}).get("next")
            next_params = None  # the cursor URL already carries them
        return results[:limit]

    # --- contract ------------------------------------------------------------

    async def test_connection(self) -> None:
        async with self._client() as client:
            # The cheapest authenticated read. A space_key that names nothing is
            # a configuration error worth surfacing here rather than at query
            # time, when it would look like an empty space.
            key = self._space_key()
            body = await self._get(
                client, "/wiki/api/v2/spaces", {"limit": 1, **({"keys": key} if key else {})}
            )
        if key and not (body.get("results") or []):
            raise ConnectorError(f"space '{key}' was not found, or this account cannot see it")

    async def discover_schema(self) -> list[dict[str, Any]]:
        """One table per space. A space is the unit an operator grants access
        to, so it is the unit the semantic surface should show."""
        key = self._space_key()
        async with self._client() as client:
            spaces = await self._paginate(
                client,
                "/wiki/api/v2/spaces",
                {"limit": _PAGE_SIZE, **({"keys": key} if key else {})},
                limit=_PAGE_SIZE,
            )
        return [
            {"name": s.get("key") or str(s.get("id", "")), "fields": list(_PAGE_FIELDS)}
            for s in spaces
        ]

    async def query(self, request: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        """Two shapes, mirroring how the object-store connectors work.

        `{"space": "ENG"}` lists a space's pages as metadata; `{"page_id": "123"}`
        returns one page with its text. Listing deliberately does not fetch
        bodies: a space of a thousand pages would be a thousand extra requests
        and a payload nobody asked for.
        """
        page_id = request.get("page_id")
        space = request.get("space")
        if not page_id and not space:
            raise ConnectorError(
                "confluence query requires 'space' (list a space's pages) or 'page_id' (one page's text)"
            )
        scope = self._space_key()
        if space and scope and space != scope:
            raise ConnectorError(f"this datasource is scoped to space '{scope}'")

        site = self._site()
        async with self._client() as client:
            if page_id:
                return await self._page_with_body(client, str(page_id), site, scope)
            return await self._space_pages(client, str(space), site, limit)

    async def _space_pages(
        self, client: httpx.AsyncClient, space: str, site: str, limit: int
    ) -> list[dict[str, Any]]:
        spaces = await self._paginate(client, "/wiki/api/v2/spaces", {"keys": space, "limit": 1}, limit=1)
        if not spaces:
            raise ConnectorError(f"space '{space}' was not found, or this account cannot see it")
        space_id = spaces[0].get("id")
        pages = await self._paginate(
            client, f"/wiki/api/v2/spaces/{space_id}/pages", {"limit": _PAGE_SIZE}, limit=limit
        )
        return [page_row(p, site) for p in pages]

    async def _page_with_body(
        self, client: httpx.AsyncClient, page_id: str, site: str, scope: str | None
    ) -> list[dict[str, Any]]:
        page = await self._get(client, f"/wiki/api/v2/pages/{page_id}", {"body-format": "storage"})
        row = page_row(page, site)
        if scope:
            # A scoped datasource must not become a site-wide reader just
            # because the caller knows a page id from another space.
            allowed = await self._paginate(
                client, "/wiki/api/v2/spaces", {"keys": scope, "limit": 1}, limit=1
            )
            if not allowed or str(allowed[0].get("id", "")) != row["space_id"]:
                raise ConnectorError(f"page {page_id} is outside space '{scope}'")
        storage = ((page.get("body") or {}).get("storage") or {}).get("value") or ""
        row["content"] = storage_to_text(storage)
        return [row]
