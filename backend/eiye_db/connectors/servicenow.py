"""ServiceNow connector. Table API, GET-only, operator instance credentials.

Read-only is structural, as it is for the other HTTP sources: ServiceNow has no
read-only credential to verify, so the guarantee is that this module issues GET
and nothing else, enforced by the transport guard in
`tests/readonly_guards.py`. Auth is HTTP Basic with an instance account, and the
client plumbing is shared with the Atlassian connectors via `http_basic.py`.

**`tables` is required, and that is a governance decision rather than an
oversight.** A ServiceNow instance carries thousands of tables — `sys_user`,
`sys_user_has_role`, every custom `u_` table an admin ever made — and a governed
surface that exposes all of them by default has not governed anything. The
operator names the tables this datasource may read, and nothing outside that
list can be discovered or queried. It is the same idea as S3's `prefix` and
Confluence's `space_key`, made mandatory because the blast radius here is much
larger.

Two shapes worth knowing before changing anything:

- **Pagination lives in a header.** ServiceNow returns `Link` with `rel="next"`
  rather than a cursor in the body, which is why this connector reads the
  response rather than only its JSON. It is the fourth pagination scheme across
  five HTTP sources, after Confluence's `_links.next` and Jira's two.
- **Table names reach an encoded query.** Schema discovery asks `sys_dictionary`
  for `name={table}`, so a name is validated against the shape ServiceNow itself
  issues and refused otherwise — the same reasoning as the Jira connector's JQL
  handling, and for the same reason: this connector builds a query rather than
  only addressing a resource.
"""

import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from eiye_db.connectors.base import Connector, ConnectorError
from eiye_db.connectors.http_basic import basic_auth_client, get_json, get_response

#: Named on every 401/403. A ServiceNow account is far more likely to be locked
#: out or missing a role than mistyped, and the roles are the usual culprit.
AUTH_HINT = (
    "A ServiceNow account also needs read access to sys_db_object and sys_dictionary for schema "
    "discovery, and a read ACL on each table it should serve."
)

# ServiceNow table and column names: lower case, digits and underscores, and
# custom tables carry a `u_`/`x_` prefix that fits the same shape. Anchored, and
# applied before a name reaches either a URL path or an encoded query.
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,79}$")

_PAGE_SIZE = 100

# A ceiling on how many pages one call walks, so a table with a million incidents
# cannot turn a single query into an unbounded crawl.
_MAX_REQUESTS = 20

_DICTIONARY = "sys_dictionary"


def next_link(header: str | None) -> str | None:
    """Pull the `rel="next"` URL out of a Link header.

    ServiceNow emits several relations at once — first, prev, next, last — and
    documentation and real responses differ on whether the URL is wrapped in
    angle brackets, so both are accepted. Returns None on the last page, which
    is how a walk knows it is finished.
    """
    if not header:
        return None
    for part in header.split(","):
        section, _, params = part.partition(";")
        if 'rel="next"' in params.replace(" ", "") or "rel=next" in params.replace(" ", ""):
            return section.strip().strip("<>").strip()
    return None


def flatten(record: dict[str, Any]) -> dict[str, Any]:
    """Reduce a ServiceNow record to scalars.

    Reference fields come back as `{"link": ..., "value": ...}` objects. The
    link is an API URL that means nothing to a caller and the value is the
    sys_id they actually want, so the object collapses to the value rather than
    being passed through as a nested dict that no downstream PII scan or CSV
    rendering would handle.
    """
    out: dict[str, Any] = {}
    for key, value in record.items():
        if isinstance(value, dict):
            out[key] = value.get("value", "")
        else:
            out[key] = value
    return out


class ServiceNowConnector(Connector):
    def __init__(self, config: dict[str, Any], transport: httpx.AsyncBaseTransport | None = None):
        super().__init__(config)
        self._transport = transport

    # --- config --------------------------------------------------------------

    def _instance(self) -> str:
        base_url = self.config.get("base_url")
        if not base_url:
            raise ConnectorError(
                "servicenow config requires 'base_url', e.g. https://acme.service-now.com"
            )
        parts = urlsplit(base_url.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ConnectorError(
                f"servicenow base_url must be an absolute http(s) URL, got '{base_url}'"
            )
        return f"{parts.scheme}://{parts.netloc}"

    def _auth(self) -> tuple[str, str]:
        username = self.config.get("username")
        password = self.config.get("password")
        if not username or not password:
            raise ConnectorError(
                "servicenow config requires 'username' and 'password'. Use a dedicated integration "
                "account with read roles only — this connector never writes, but ServiceNow will "
                "happily hand a write-capable account's data to anyone who can reach it."
            )
        return username, password

    def _tables(self) -> list[str]:
        """The allowlist. Required, non-empty, and every entry validated.

        Defaulting to "every table on the instance" was considered and rejected:
        it would put sys_user and every credential-adjacent table into a
        governed surface by accident, which is the opposite of the product.
        """
        tables = self.config.get("tables")
        if isinstance(tables, str):
            tables = [t.strip() for t in tables.split(",") if t.strip()]
        if not tables or not isinstance(tables, list):
            raise ConnectorError(
                "servicenow config requires 'tables': the list of tables this datasource may read, "
                "e.g. [\"incident\", \"change_request\"]. There is deliberately no default — an "
                "instance has thousands of tables and exposing all of them is not a governed surface."
            )
        return [self._checked(str(t)) for t in tables]

    @staticmethod
    def _checked(name: str) -> str:
        if not _NAME.match(name):
            raise ConnectorError(
                f"'{name}' is not a valid ServiceNow table name. Names are lower case letters, "
                "digits and underscores; anything else is refused rather than placed into a URL "
                "path or an encoded query."
            )
        return name

    def _client(self) -> httpx.AsyncClient:
        return basic_auth_client(self._instance(), self._auth(), self._transport)

    # --- HTTP ----------------------------------------------------------------

    async def _paginate(
        self, client: httpx.AsyncClient, path: str, params: dict, limit: int
    ) -> list[dict[str, Any]]:
        """Walk `Link: rel="next"` until it is absent or `limit` is reached.

        The next URL is absolute and already carries its own query string, so it
        is used verbatim; rebuilding it from an offset is how a paginator starts
        silently repeating a page.
        """
        out: list[dict[str, Any]] = []
        next_url: str | None = path
        next_params: dict | None = params
        for _ in range(_MAX_REQUESTS):
            if next_url is None:
                break
            response = await get_response(client, next_url, next_params, auth_hint=AUTH_HINT)
            try:
                out.extend(response.json().get("result") or [])
            except ValueError as e:
                raise ConnectorError(f"{next_url} did not return JSON") from e
            if len(out) >= limit:
                break
            next_url = next_link(response.headers.get("link"))
            next_params = None
        return out[:limit]

    # --- contract ------------------------------------------------------------

    async def test_connection(self) -> None:
        """Prove the credential works *and* that every allowed table is readable.

        Checking each one matters: a typo in the allowlist, or a missing read
        ACL on one table out of five, would otherwise surface later as a table
        that silently returns nothing.
        """
        # Order matters: resolving the instance and credential first means a
        # config missing several things reports the most fundamental one, rather
        # than complaining about `tables` to someone who has not set a URL yet.
        self._instance()
        self._auth()
        tables = self._tables()
        async with self._client() as client:
            for table in tables:
                await get_json(
                    client,
                    f"/api/now/table/{table}",
                    {"sysparm_limit": 1, "sysparm_fields": "sys_id"},
                    auth_hint=AUTH_HINT,
                )

    async def discover_schema(self) -> list[dict[str, Any]]:
        """One table per allowlist entry, with columns from `sys_dictionary`."""
        tables = self._tables()
        out = []
        async with self._client() as client:
            for table in tables:
                rows = await self._paginate(
                    client,
                    f"/api/now/table/{_DICTIONARY}",
                    {
                        # `element` is empty on the row describing the table
                        # itself, so those are filtered below rather than being
                        # reported as a nameless column.
                        "sysparm_query": f"name={table}^active=true^ORDERBYelement",
                        "sysparm_fields": "element,internal_type",
                        "sysparm_limit": _PAGE_SIZE,
                        "sysparm_exclude_reference_link": "true",
                    },
                    limit=_PAGE_SIZE * 4,
                )
                fields = [
                    {"name": r["element"], "type": flatten(r).get("internal_type") or "string"}
                    for r in rows
                    if r.get("element")
                ]
                out.append({"name": table, "fields": fields})
        return out

    async def query(self, request: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        """`{"table": "incident"}` returns that table's records.

        There is deliberately no encoded-query passthrough. `sysparm_query` is a
        query language, and accepting one from a caller would let them read past
        the allowlist by joining or dot-walking to another table — the same
        reason the Jira connector refuses raw JQL.
        """
        table = request.get("table")
        if not table:
            raise ConnectorError(
                "servicenow query requires 'table', naming one of the tables this datasource allows"
            )
        allowed = self._tables()
        table = self._checked(str(table))
        if table not in allowed:
            raise ConnectorError(
                f"table '{table}' is not in this datasource's allowlist ({', '.join(allowed)})"
            )
        async with self._client() as client:
            rows = await self._paginate(
                client,
                f"/api/now/table/{table}",
                {
                    "sysparm_limit": min(_PAGE_SIZE, limit),
                    # Reference fields come back as plain values rather than
                    # {link, value} objects; `flatten` covers whatever slips past.
                    "sysparm_exclude_reference_link": "true",
                },
                limit=limit,
            )
        return [flatten(r) for r in rows]
