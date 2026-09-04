"""Google Drive, via a customer-owned service account.

Read-only is structural, as it is for the other HTTP sources: this module issues
GET and nothing else. The one exception is the OAuth2 token request, which is a
POST by protocol; the test guard allows POST to exactly `oauth2.googleapis.com`
and asserts nothing else was posted, the same arrangement SharePoint uses.

**Drive does not have SharePoint's ACL problem, and the reason is worth stating
because the two connectors look alike.** A SharePoint application-only token is
granted access to everything beneath its grant, so item-level permissions do not
apply to it. A Google service account without domain-wide delegation is not a
special principal at all — it is an ordinary account with its own email address,
and Drive's normal sharing rules apply to it exactly as they would to a person.
It sees a file if, and only if, someone shared that file (or a folder, or a
shared drive) with it. Even a document shared with "everyone in the
organisation" is invisible to it, because a service account is not a member of
the Workspace domain.

That makes the access boundary the operator's own sharing decisions, expressed
in the Drive UI they already know, and revocable there. It is the best scoping
story of any connector in this repo.

**All of which depends on never using domain-wide delegation, so this module
cannot express it.** Delegation is enabled by putting a `sub` claim naming a
user into the signed assertion, which turns the token into an impersonation of
that person and would expose their entire Drive. `service_account_claims` in
`http_bearer.py` never sets `sub`, there is no configuration key that would, and
the test suite decodes the assertion actually sent and fails if one appears.

**The credential is checked, not assumed.** Google's token response states the
scope it granted, and `_assert_read_only_scope` refuses anything outside the two
read-only Drive scopes. `drive.readonly` is enforced by Google — a write with
that token is refused at their end — which is a stronger property than the
structural tier claims. It is still labelled structural, because the tier's
other half is verification against a live server in CI and there is no Drive to
run against. The check that *is* possible is made; the claim that cannot be
verified is not made.

**There is no `q` passthrough.** Drive's `q` is a query language with an `in
parents` operator, so accepting one from a caller would let them address files
outside the configured folder — the same reason the Jira connector refuses raw
JQL and the ServiceNow one refuses `sysparm_query`.

Config:

    service_account_json  required  the downloaded key, as a JSON string or an object
    folder_id             optional  bounds this datasource to one folder and its children
    shared_drive_id       optional  set when the content lives in a shared drive

Not a crawler, not a Drive search front-end, not a copy of the drive.
"""

import io
import json
import time
from typing import Any

import httpx

from eiye_db.connectors import documents
from eiye_db.connectors.base import Connector, ConnectorError
from eiye_db.connectors.http_bearer import fetch_service_account_token, get_json

DRIVE = "https://www.googleapis.com/drive/v3"

PRODUCT = "Google Drive"

#: The only scope this connector ever asks for.
READ_ONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

#: Scopes a granted token may carry. `drive.metadata.readonly` is narrower than
#: what this connector needs but cannot write, so it is accepted rather than
#: refused — an operator who granted it will find discovery works and content
#: reads fail, which is a clearer signal than a refusal at connect.
READ_ONLY_SCOPES = frozenset(
    {
        READ_ONLY_SCOPE,
        "https://www.googleapis.com/auth/drive.metadata.readonly",
    }
)

AUTH_HINT = (
    "A service account sees only what was explicitly shared with its client_email — sharing with "
    "the whole organisation does not reach it, because a service account is not a member of the "
    "domain. Share the folder with that address, or add the account to the shared drive."
)

_FOLDER_MIME = "application/vnd.google-apps.folder"

#: Google-native documents have no bytes to download; they are exported to a
#: format that does. The targets are chosen to match what the extractors in
#: `documents.py` already read, so a Doc arrives as text and a Sheet as CSV.
_EXPORT_AS = {
    "application/vnd.google-apps.document": ("text/plain", ".txt"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
    "application/vnd.google-apps.presentation": ("text/plain", ".txt"),
}

_PAGE_SIZE = 200

# A ceiling on Drive calls per discovery pass. A folder tree costs one request
# per folder, so without this a deeply nested share turns discovery into a crawl.
_MAX_REQUESTS = 40

_MAX_FILE_BYTES = 32 * 1024 * 1024
_CSV_SNIFF_BYTES = 64 * 1024
_MAX_SNIFFED_CSVS = 20

_FIELDS = "nextPageToken,files(id,name,mimeType,size)"


def export_target(mime_type: str) -> tuple[str, str] | None:
    """The export format for a Google-native file, or None for an ordinary one."""
    return _EXPORT_AS.get(mime_type)


def effective_name(name: str, mime_type: str) -> str:
    """The name eiye reports for a file, with an extension that matches its bytes.

    A Google Doc called "Q3 notes" has no extension, and `documents.kind_for`
    works off one. Appending the exported format's suffix is what lets a Doc be
    read as text and a Sheet as CSV without a second dispatch table.
    """
    target = export_target(mime_type)
    if target and not name.lower().endswith(target[1]):
        return name + target[1]
    return name


class GoogleDriveConnector(Connector):
    def __init__(self, config: dict[str, Any], transport: httpx.AsyncBaseTransport | None = None):
        super().__init__(config)
        self._transport = transport
        self._token: str | None = None
        self._token_expiry: float = 0.0

    # --- config --------------------------------------------------------------

    def _service_account(self) -> dict[str, Any]:
        """The downloaded key, accepted as an object or as the raw JSON string.

        Both, because an operator pasting the file into a form produces a string
        and an operator writing config produces an object, and refusing either
        would be a papercut with no governance value.
        """
        raw = self.config.get("service_account_json")
        if not raw:
            raise ConnectorError(
                "gdrive config requires 'service_account_json': the key file downloaded from the "
                "Google Cloud console, as JSON. eiye never operates a Google app on your behalf — "
                "the service account is yours, in your project."
            )
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ConnectorError(f"service_account_json is not valid JSON: {e}") from e
        if not isinstance(raw, dict):
            raise ConnectorError("service_account_json must be a JSON object")
        return raw

    def _folder_id(self) -> str | None:
        value = self.config.get("folder_id")
        return str(value).strip() if value else None

    def _shared_drive_id(self) -> str | None:
        value = self.config.get("shared_drive_id")
        return str(value).strip() if value else None

    # --- auth ----------------------------------------------------------------

    async def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry:
            return self._token
        token, granted, expiry = await fetch_service_account_token(
            self._service_account(), READ_ONLY_SCOPE, transport=self._transport
        )
        self._assert_read_only_scope(granted)
        self._token, self._token_expiry = token, expiry
        return token

    @staticmethod
    def _assert_read_only_scope(granted: str) -> None:
        """Refuse a token that can write.

        Google states the granted scope in the token response, so this is the
        credential's own account of itself rather than an assumption about what
        was asked for. A response naming no scope is refused too: it would make
        the one check available here into a check that sometimes runs.
        """
        scopes = {s for s in granted.split() if s}
        if not scopes:
            raise ConnectorError(
                "Google's token response did not say what scope it granted, so eiye cannot confirm "
                "this credential is read-only."
            )
        writable = sorted(scopes - READ_ONLY_SCOPES)
        if writable:
            raise ConnectorError(
                f"this service account's token carries {', '.join(writable)}, which can modify "
                f"Drive. eiye requires a read-only credential — grant it {READ_ONLY_SCOPE} and "
                "nothing else."
            )

    async def _client(self) -> httpx.AsyncClient:
        token = await self._access_token()
        return httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=30,
            transport=self._transport,
            follow_redirects=True,
        )

    # --- Drive ---------------------------------------------------------------

    def _shared_drive_params(self) -> dict[str, Any]:
        """Shared drives are invisible to `files.list` unless asked for.

        Two flags rather than one, and both are needed: `supportsAllDrives` says
        the client understands shared-drive semantics, `includeItemsFromAllDrives`
        says results may include them. Omitting either silently returns nothing
        for a shared-drive datasource, which looks like a permission problem.
        """
        params: dict[str, Any] = {"supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
        shared_drive = self._shared_drive_id()
        if shared_drive:
            params["corpora"] = "drive"
            params["driveId"] = shared_drive
        return params

    async def _list(self, client: httpx.AsyncClient, parent: str | None) -> list[dict]:
        """One folder's children, following `nextPageToken`.

        The sixth pagination scheme across the HTTP connectors. Unlike Graph's,
        the cursor is a bare token rather than a URL, so the query is rebuilt
        each page — which is safe here only because every other parameter is
        constructed by this method rather than carried from the last response.
        """
        query = f"'{_escape(parent)}' in parents and trashed = false" if parent else "trashed = false"
        params: dict[str, Any] = {
            "q": query,
            "fields": _FIELDS,
            "pageSize": _PAGE_SIZE,
            **self._shared_drive_params(),
        }
        out: list[dict] = []
        for _ in range(_MAX_REQUESTS):
            page = await get_json(
                client, f"{DRIVE}/files", params, auth_hint=AUTH_HINT, product=PRODUCT
            )
            out.extend(f for f in (page.get("files") or []) if isinstance(f, dict))
            cursor = page.get("nextPageToken")
            if not cursor:
                break
            params = {**params, "pageToken": cursor}
        return out

    async def _walk(self, client: httpx.AsyncClient) -> list[tuple[str, str, str]]:
        """Every file under the configured root, as (path, id, mimeType).

        Breadth-first with a request budget shared across the tree. Folders are
        recursed; Google-native files are kept and exported at read time; a file
        with a type nothing can extract is still listed, because a name in the
        surface is more useful than a silent omission.
        """
        root = self._folder_id()
        queue: list[tuple[str, str | None]] = [("", root)]
        found: list[tuple[str, str, str]] = []
        requests = 0
        while queue and requests < _MAX_REQUESTS:
            prefix, parent = queue.pop(0)
            requests += 1
            for entry in await self._list(client, parent):
                name, file_id = str(entry.get("name") or ""), str(entry.get("id") or "")
                mime = str(entry.get("mimeType") or "")
                if not name or not file_id:
                    continue
                child = f"{prefix}/{name}" if prefix else name
                if mime == _FOLDER_MIME:
                    queue.append((child, file_id))
                else:
                    found.append((f"{prefix}/{effective_name(name, mime)}" if prefix
                                  else effective_name(name, mime), file_id, mime))
        return found

    async def _fetch(
        self, client: httpx.AsyncClient, file_id: str, mime_type: str, max_bytes: int
    ) -> tuple[bytes, bool]:
        """Read at most `max_bytes` of one file, and say whether it was truncated.

        Google-native files go through `export`, which produces bytes that do not
        exist until asked for and therefore ignores a Range header — so those are
        capped after the fact rather than at the request. Ordinary files take the
        ranged path, asking for one byte past the cap so a full-length response
        proves the file is longer.
        """
        target = export_target(mime_type)
        if target:
            url = f"{DRIVE}/files/{file_id}/export"
            params: dict[str, Any] = {"mimeType": target[0], **self._shared_drive_params()}
            headers: dict[str, str] = {}
        else:
            url = f"{DRIVE}/files/{file_id}"
            params = {"alt": "media", **self._shared_drive_params()}
            headers = {"Range": f"bytes=0-{max_bytes}"}
        try:
            response = await client.get(url, params=params, headers=headers)
        except httpx.HTTPError as e:
            raise ConnectorError(f"cannot read {file_id}: {e}") from e
        if response.status_code >= 400:
            raise ConnectorError(
                f"cannot read {file_id}: HTTP {response.status_code}. {AUTH_HINT}"
                if response.status_code in (401, 403)
                else f"cannot read {file_id}: HTTP {response.status_code}"
            )
        data = response.content
        return data[:max_bytes], len(data) > max_bytes

    async def _resolve(self, client: httpx.AsyncClient, path: str) -> tuple[str, str]:
        """Map a path from `query` back to a file id and mime type.

        Resolved by walking rather than by asking Drive to look the name up:
        a name lookup would need a `q` naming the file, and the walk is already
        bounded by the configured folder, so this cannot address anything
        discovery would not have listed.
        """
        wanted = path.strip("/")
        for name, file_id, mime in await self._walk(client):
            if name == wanted:
                return file_id, mime
        raise ConnectorError(
            f"no file '{path}' under this datasource. Discovery lists what the service account can "
            f"see; {AUTH_HINT[0].lower()}{AUTH_HINT[1:]}"
        )

    # --- contract ------------------------------------------------------------

    async def test_connection(self) -> None:
        """Mint a token, check its scope, and list the configured root.

        Listing matters: a service account with a perfectly good key that nobody
        shared anything with authenticates fine and sees nothing, which is the
        most common way this connector is misconfigured.
        """
        client = await self._client()
        try:
            await self._list(client, self._folder_id())
        finally:
            await client.aclose()

    async def discover_schema(self) -> list[dict[str, Any]]:
        client = await self._client()
        try:
            tables: list[dict[str, Any]] = []
            sniffed = 0
            for name, file_id, mime in await self._walk(client):
                kind = documents.kind_for(name)
                if kind == "csv":
                    fields: list[dict[str, Any]] = []
                    if sniffed < _MAX_SNIFFED_CSVS:
                        sniffed += 1
                        fields = await self._csv_fields(client, file_id, mime)
                    tables.append({"name": name, "fields": fields})
                elif kind == "xlsx":
                    tables.append({"name": name, "fields": []})
                elif kind in ("pdf", "text"):
                    tables.append({"name": name, "fields": [{"name": "content", "type": "text"}]})
            return tables
        finally:
            await client.aclose()

    async def _csv_fields(
        self, client: httpx.AsyncClient, file_id: str, mime: str
    ) -> list[dict[str, Any]]:
        data, truncated = await self._fetch(client, file_id, mime, _CSV_SNIFF_BYTES)
        text = data.decode("utf-8", errors="replace")
        if truncated:
            text = text[: text.rfind("\n") + 1]
        return documents.csv_fields(io.StringIO(text, newline=""))

    async def query(self, request: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        """`{"path": "reports/q3.csv"}` — a path as discovery reported it."""
        path = request.get("path")
        if not path:
            raise ConnectorError("gdrive query requires 'path', as discovery reported it")
        client = await self._client()
        try:
            file_id, mime = await self._resolve(client, str(path))
            data, truncated = await self._fetch(client, file_id, mime, _MAX_FILE_BYTES)
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


def _escape(file_id: str) -> str:
    """Close the one hole a file id could open in a `q` clause.

    Drive ids are opaque and in practice URL-safe, but this connector builds a
    quoted `q` string around one, and a value carrying a quote or a backslash
    would end the literal early and let the rest be read as query syntax. Ids
    reaching here come from Drive itself or from operator config, neither of
    which is a reason to skip it.
    """
    return file_id.replace("\\", "\\\\").replace("'", "\\'")
