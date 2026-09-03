"""Jira Cloud connector. GET-only, operator-held API token.

Auth, the HTTP client and error mapping come from `atlassian.py`, shared with
the Confluence connector. Everything below is where Jira differs, and it differs
more than the shared auth suggests.

**Pagination is two mechanisms, not one.** Issue search returns a bare
`nextPageToken` — the old `startAt`/`total` model is gone, and the endpoint that
used it, `/rest/api/3/search`, now answers 410 on Cloud. Project search still
uses offsets and terminates on `isLast`. Confluence's `_links.next` is a third
shape again. Three schemes across two products is not something to paper over
with one clever helper, so each is walked where it is used.

**This connector builds a query language, which Confluence's does not**, and
that is the security-relevant difference between them. A project key reaches
JQL as `project = "ENG"`, so a key containing a quote would let a caller rewrite
the query. Keys are therefore validated against the shape Jira actually issues
before they are interpolated, and anything else is refused. Raw JQL is
deliberately **not** accepted from callers: it cannot write — JQL has no write
form — but it would step straight past `project_key`, which is the scope an
operator sets to expose one project without exposing the site.

Read-only is structural. Jira has no read-only credential and no way to ask
whether a token can write, so the guarantee is that this module issues GET and
nothing else, enforced by the transport guard in `tests/readonly_guards.py`.
Note that `/rest/api/3/search/jql` also has a POST form, for JQL too long to fit
in a query string; using it would read identically and break that guarantee, so
the queries here are kept small enough that GET is always sufficient.
"""

import re
from typing import Any

import httpx

from eiye_db.connectors import documents
from eiye_db.connectors.atlassian import AtlassianCloudConnector
from eiye_db.connectors.base import ConnectorError

# Jira caps issue search at 100 per request and project search at 50.
_ISSUE_PAGE_SIZE = 100
_PROJECT_PAGE_SIZE = 50

# A ceiling on how many pages one call will walk, so a project with 50,000
# issues cannot turn a single query into an unbounded crawl.
_MAX_REQUESTS = 20

# What Jira actually issues: a letter, then letters, digits or underscores.
# Anchored and applied before the value reaches JQL — see the module docstring.
_PROJECT_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,254}$")
_ISSUE_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,254}-[0-9]{1,19}$")

_ISSUE_FIELDS_PARAM = "summary,status,issuetype,priority,assignee,reporter,created,updated"

# Declared once because discover_schema advertises it and query has to produce
# exactly it — the two drifting apart is how a schema starts lying about rows.
_ISSUE_FIELDS = [
    {"name": "key", "type": "string"},
    {"name": "summary", "type": "string"},
    {"name": "status", "type": "string"},
    {"name": "issue_type", "type": "string"},
    {"name": "priority", "type": "string"},
    {"name": "assignee", "type": "string"},
    {"name": "reporter", "type": "string"},
    {"name": "created_at", "type": "string"},
    {"name": "updated_at", "type": "string"},
    {"name": "url", "type": "string"},
]


def adf_to_text(node: Any) -> str:
    """Flatten an Atlassian Document Format tree to plain text.

    Jira's v3 API returns rich text as ADF — nested JSON rather than the XHTML
    Confluence uses — so the two products need different extractors even though
    they share an account. Text lives in `text` nodes; everything else is
    structure. Paragraph-level nodes become line breaks so the result reads as
    prose and a PII scan sees realistic boundaries.
    """
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "\n".join(p for p in (adf_to_text(n) for n in node) if p)
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    inner = adf_to_text(node.get("content", []))
    return inner


def _named(field: Any, key: str = "name") -> str:
    """Pull a display value out of a Jira field that may be absent or null.

    `assignee` is null on an unassigned issue and `priority` is null on projects
    that disabled it, so this has to distinguish "missing" from "present but
    empty" without turning either into the string "None".
    """
    return (field or {}).get(key, "") if isinstance(field, dict) else ""


def issue_row(issue: dict[str, Any], site: str) -> dict[str, Any]:
    """Map one issue to the flat shape `_ISSUE_FIELDS` advertises."""
    fields = issue.get("fields") or {}
    key = issue.get("key", "")
    return {
        "key": key,
        "summary": fields.get("summary") or "",
        "status": _named(fields.get("status")),
        "issue_type": _named(fields.get("issuetype")),
        "priority": _named(fields.get("priority")),
        "assignee": _named(fields.get("assignee"), "displayName"),
        "reporter": _named(fields.get("reporter"), "displayName"),
        "created_at": fields.get("created") or "",
        "updated_at": fields.get("updated") or "",
        "url": f"{site}/browse/{key}" if key else "",
    }


class JiraConnector(AtlassianCloudConnector):
    PRODUCT = "jira"

    def _project_key(self) -> str | None:
        key = self.config.get("project_key")
        return key.strip() if isinstance(key, str) and key.strip() else None

    @staticmethod
    def _checked_project(key: str) -> str:
        if not _PROJECT_KEY.match(key):
            raise ConnectorError(
                f"'{key}' is not a valid Jira project key. Keys are a letter followed by letters, "
                "digits or underscores; anything else is refused rather than placed into JQL."
            )
        return key

    @staticmethod
    def _checked_issue(key: str) -> str:
        if not _ISSUE_KEY.match(key):
            raise ConnectorError(f"'{key}' is not a valid Jira issue key, e.g. ENG-123")
        return key

    # --- pagination ----------------------------------------------------------

    async def _projects(self, client: httpx.AsyncClient, limit: int) -> list[dict[str, Any]]:
        """Walk `/project/search`, which still pages by offset and ends on `isLast`."""
        out: list[dict[str, Any]] = []
        start = 0
        for _ in range(_MAX_REQUESTS):
            body = await self._get(
                client,
                "/rest/api/3/project/search",
                {"startAt": start, "maxResults": _PROJECT_PAGE_SIZE},
            )
            values = body.get("values") or []
            out.extend(values)
            if body.get("isLast", True) or not values or len(out) >= limit:
                break
            start += len(values)
        return out[:limit]

    async def _issues(self, client: httpx.AsyncClient, jql: str, limit: int, fields: str) -> list[dict]:
        """Walk `/search/jql`, which pages by an opaque `nextPageToken`.

        There is no `total` any more, and no `startAt`: the only way to know a
        walk is finished is the token being absent from the response.
        """
        out: list[dict[str, Any]] = []
        token: str | None = None
        for _ in range(_MAX_REQUESTS):
            params: dict[str, Any] = {
                "jql": jql,
                "maxResults": min(_ISSUE_PAGE_SIZE, limit - len(out)),
                "fields": fields,
            }
            if token:
                params["nextPageToken"] = token
            body = await self._get(client, "/rest/api/3/search/jql", params)
            out.extend(body.get("issues") or [])
            token = body.get("nextPageToken")
            if not token or len(out) >= limit:
                break
        return out[:limit]

    # --- contract ------------------------------------------------------------

    async def test_connection(self) -> None:
        scope = self._project_key()
        async with self._client() as client:
            if scope is None:
                await self._get(client, "/rest/api/3/myself")
                return
            # A project_key naming nothing is a configuration error worth
            # surfacing here rather than at query time, when it would be
            # indistinguishable from a project with no issues.
            self._checked_project(scope)
            await self._get(client, f"/rest/api/3/project/{scope}")

    async def discover_schema(self) -> list[dict[str, Any]]:
        """One table per project. A project is the unit an operator grants
        access to, so it is the unit the semantic surface should show."""
        scope = self._project_key()
        async with self._client() as client:
            if scope:
                project = await self._get(client, f"/rest/api/3/project/{self._checked_project(scope)}")
                projects = [project]
            else:
                projects = await self._projects(client, limit=_PROJECT_PAGE_SIZE * _MAX_REQUESTS)
        return [
            {"name": p.get("key") or str(p.get("id", "")), "fields": list(_ISSUE_FIELDS)}
            for p in projects
        ]

    async def query(self, request: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        """Two shapes, mirroring the Confluence connector.

        `{"project": "ENG"}` lists a project's issues as metadata;
        `{"issue_key": "ENG-1"}` returns one issue with its description text.
        Listing does not fetch descriptions: a project of a thousand issues
        would otherwise carry a payload nobody asked for.
        """
        issue_key = request.get("issue_key")
        project = request.get("project")
        if not issue_key and not project:
            raise ConnectorError(
                "jira query requires 'project' (list a project's issues) or 'issue_key' (one issue)"
            )
        scope = self._project_key()
        if project and scope and project != scope:
            raise ConnectorError(f"this datasource is scoped to project '{scope}'")

        site = self._site()
        async with self._client() as client:
            if issue_key:
                return await self._one_issue(client, self._checked_issue(str(issue_key)), site, scope)
            jql = f'project = "{self._checked_project(str(project))}" ORDER BY created DESC'
            issues = await self._issues(client, jql, limit, _ISSUE_FIELDS_PARAM)
            return [issue_row(i, site) for i in issues]

    async def _one_issue(
        self, client: httpx.AsyncClient, key: str, site: str, scope: str | None
    ) -> list[dict[str, Any]]:
        if scope and key.rsplit("-", 1)[0].upper() != scope.upper():
            # Issue keys carry their project, so this is checkable without a
            # round-trip — and it has to be checked, or a scoped datasource
            # becomes a site-wide reader for anyone who knows a key.
            raise ConnectorError(f"issue {key} is outside project '{scope}'")
        issue = await self._get(
            client, f"/rest/api/3/issue/{key}", {"fields": f"{_ISSUE_FIELDS_PARAM},description"}
        )
        row = issue_row(issue, site)
        description = (issue.get("fields") or {}).get("description")
        row["content"] = adf_to_text(description)[: documents.MAX_TEXT_CHARS]
        return [row]
