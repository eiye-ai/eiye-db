"""Google Drive connector tests.

Built on a `GetOnlyTransport` that allows POST to exactly `oauth2.googleapis.com`,
the same arrangement SharePoint uses for its token request.

The test that matters most is `test_the_assertion_never_carries_a_sub_claim`. A
`sub` claim is what turns a service-account token into an impersonation of a
named user — Google's domain-wide delegation — and an impersonating token would
see that person's entire Drive instead of only what was deliberately shared with
the service account. The whole reason this connector has a better scoping story
than SharePoint's rests on that claim being absent, so the fake decodes the
assertion it was actually sent and the suite fails if one appears.

The fake signs nothing and verifies nothing: it decodes the assertion to inspect
its claims, which is what a test of *what we send* needs. Verification is
Google's job and is not something this connector does.
"""

import asyncio
import base64
import json
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from eiye_db.connectors.base import ConnectorError
from eiye_db.connectors.gdrive import (
    READ_ONLY_SCOPE,
    GoogleDriveConnector,
    effective_name,
    export_target,
)
from eiye_db.connectors.http_bearer import service_account_claims, sign_rs256
from tests.readonly_guards import GetOnlyTransport, WriteAttempted

TOKEN_HOST = "oauth2.googleapis.com"
SA_EMAIL = "eiye-ro@acme-123456.iam.gserviceaccount.com"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_KEY_PEM = _KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()

SERVICE_ACCOUNT = {
    "type": "service_account",
    "project_id": "acme-123456",
    "client_email": SA_EMAIL,
    "private_key": PRIVATE_KEY_PEM,
}

CSV = "name,amount\nacme,10\nbeta,20\n"
SHEET_CSV = "quarter,total\nQ3,99\n"

ROOT = "folder-root"

#: The drive, as a parent-id -> children map. `folder-old` is two levels down,
#: so reaching its file proves the walk recurses.
DRIVE_TREE: dict[str, list[dict]] = {
    ROOT: [
        {"id": "f-reports", "name": "reports", "mimeType": "application/vnd.google-apps.folder"},
        {"id": "f-readme", "name": "readme.txt", "mimeType": "text/plain", "size": "12"},
        {"id": "f-sheet", "name": "Q3 numbers",
         "mimeType": "application/vnd.google-apps.spreadsheet"},
        {"id": "f-doc", "name": "Charter", "mimeType": "application/vnd.google-apps.document"},
    ],
    "f-reports": [
        {"id": "f-q3", "name": "q3.csv", "mimeType": "text/csv", "size": str(len(CSV))},
        {"id": "f-old", "name": "old", "mimeType": "application/vnd.google-apps.folder"},
    ],
    "f-old": [
        {"id": "f-q1", "name": "q1.csv", "mimeType": "text/csv", "size": str(len(CSV))},
    ],
}

CONTENT: dict[str, bytes] = {
    "f-readme": b"a plain note",
    "f-q3": CSV.encode(),
    "f-q1": CSV.encode(),
}

EXPORTS: dict[str, bytes] = {
    "f-sheet": SHEET_CSV.encode(),
    "f-doc": b"the charter text",
}


def decode_segment(segment: str) -> dict:
    segment += "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(segment))


class FakeDrive:
    """Google's token endpoint and the slice of the Drive API this touches.

    `assertions` holds every decoded JWT payload the connector sent, which is
    what lets a test assert on the claims rather than on the effect of them.
    """

    def __init__(
        self,
        *,
        granted_scope: str | None = None,
        page_size: int = 50,
        shared_drive_expected: str | None = None,
    ):
        self.granted_scope = READ_ONLY_SCOPE if granted_scope is None else granted_scope
        self.page_size = page_size
        self.shared_drive_expected = shared_drive_expected
        self.assertions: list[dict] = []
        self.queries: list[str] = []
        self.paths: list[str] = []
        self.token_requests = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        self.paths.append(url.path)
        if url.host == TOKEN_HOST:
            return self._token(request)
        if request.headers.get("authorization") != "Bearer drive-token":
            return httpx.Response(401, json={"error": {"code": 401, "message": "Invalid Credentials"}})
        if url.path == "/drive/v3/files":
            return self._list(request)
        if url.path.endswith("/export"):
            return self._export(request, url.path.split("/")[-2])
        if url.path.startswith("/drive/v3/files/"):
            return self._media(request, url.path.rsplit("/", 1)[-1])
        return httpx.Response(404, json={"error": {"message": f"no route for {url.path}"}})

    # --- token ---------------------------------------------------------------

    def _token(self, request: httpx.Request) -> httpx.Response:
        self.token_requests += 1
        form = parse_qs(request.content.decode())
        assertion = form.get("assertion", [""])[0]
        header, payload, _ = assertion.split(".")
        assert decode_segment(header)["alg"] == "RS256"
        self.assertions.append(decode_segment(payload))
        return httpx.Response(
            200,
            json={"access_token": "drive-token", "expires_in": 3599, "token_type": "Bearer",
                  "scope": self.granted_scope},
        )

    # --- files ---------------------------------------------------------------

    def _list(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        query = params.get("q", "")
        self.queries.append(query)
        if self.shared_drive_expected is not None:
            assert params.get("driveId") == self.shared_drive_expected
            assert params.get("includeItemsFromAllDrives") == "true"
            assert params.get("supportsAllDrives") == "true"

        parent = ""
        if " in parents" in query:
            parent = query.split("'")[1]
        entries = DRIVE_TREE.get(parent, [])
        skip = int(params.get("pageToken") or 0)
        page = entries[skip : skip + self.page_size]
        body: dict = {"files": page}
        if skip + self.page_size < len(entries):
            body["nextPageToken"] = str(skip + self.page_size)
        return httpx.Response(200, json=body)

    def _media(self, request: httpx.Request, file_id: str) -> httpx.Response:
        if dict(request.url.params).get("alt") != "media":
            return httpx.Response(400, json={"error": {"message": "alt=media required"}})
        if file_id not in CONTENT:
            return httpx.Response(404, json={"error": {"message": f"no file {file_id}"}})
        return httpx.Response(200, content=CONTENT[file_id])

    def _export(self, request: httpx.Request, file_id: str) -> httpx.Response:
        if file_id not in EXPORTS:
            # Real Drive refuses to export a non-native file, and a connector
            # that asked would be reading the dispatch table backwards.
            return httpx.Response(403, json={"error": {"message": "not exportable"}})
        return httpx.Response(200, content=EXPORTS[file_id])


def build(fake: FakeDrive, **config) -> tuple[GoogleDriveConnector, GetOnlyTransport]:
    guard = GetOnlyTransport(httpx.MockTransport(fake), post_allowed_hosts=frozenset({TOKEN_HOST}))
    settings: dict = {"service_account_json": SERVICE_ACCOUNT, "folder_id": ROOT}
    settings.update(config)
    return GoogleDriveConnector(settings, transport=guard), guard


# --- domain-wide delegation ---------------------------------------------------


def test_the_assertion_never_carries_a_sub_claim():
    """The single most important test in this file.

    `sub` is domain-wide delegation. With it, the token impersonates a named
    user and sees that person's whole Drive; without it, the service account is
    an ordinary principal and Drive's own sharing rules are the boundary. The
    entire scoping story of this connector is that claim's absence.
    """
    fake = FakeDrive()
    connector, _ = build(fake)
    asyncio.run(connector.discover_schema())

    assert fake.assertions, "no assertion was sent, so this proves nothing"
    for claims in fake.assertions:
        assert "sub" not in claims
        assert claims["iss"] == SA_EMAIL
        assert claims["scope"] == READ_ONLY_SCOPE


def test_service_account_claims_has_no_delegation_field():
    """Asserted on the builder too, so the guarantee survives a caller that
    mints an assertion without going through the connector."""
    claims = service_account_claims(SA_EMAIL, READ_ONLY_SCOPE)
    assert set(claims) == {"iss", "scope", "aud", "iat", "exp"}


def test_the_assertion_is_signed_with_the_service_account_key():
    """Verified against the public half, which is the only party that could —
    so a corrupted signing path fails here rather than at Google."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    token = sign_rs256({"iss": SA_EMAIL}, PRIVATE_KEY_PEM)
    signing_input, _, signature = token.rpartition(".")
    raw = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    _KEY.public_key().verify(raw, signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())


def test_an_unreadable_private_key_is_reported_plainly():
    connector, _ = build(FakeDrive(), service_account_json={**SERVICE_ACCOUNT, "private_key": "nope"})
    with pytest.raises(ConnectorError) as e:
        asyncio.run(connector.test_connection())
    assert "private_key" in str(e.value)


# --- the read-only claim ------------------------------------------------------


def test_the_only_post_is_the_token_request():
    fake = FakeDrive()
    connector, guard = build(fake)
    asyncio.run(connector.discover_schema())
    assert guard.posts_seen == [f"https://{TOKEN_HOST}/token"]
    assert guard.methods_seen.count("POST") == 1


def test_a_post_to_drive_would_fail_the_build():
    _, guard = build(FakeDrive())

    async def post() -> None:
        async with httpx.AsyncClient(transport=guard) as client:
            await client.post("https://www.googleapis.com/drive/v3/files", json={})

    with pytest.raises(WriteAttempted):
        asyncio.run(post())


@pytest.mark.parametrize(
    "granted",
    [
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/drive.file",
        f"{READ_ONLY_SCOPE} https://www.googleapis.com/auth/drive",
    ],
)
def test_a_writable_credential_is_refused(granted):
    """Google states the granted scope in the token response, so this is the
    credential's own account of itself rather than an assumption."""
    connector, _ = build(FakeDrive(granted_scope=granted))
    with pytest.raises(ConnectorError) as e:
        asyncio.run(connector.test_connection())
    assert "read-only" in str(e.value)
    assert READ_ONLY_SCOPE in str(e.value)


def test_a_token_response_with_no_scope_is_refused():
    connector, _ = build(FakeDrive(granted_scope=""))
    with pytest.raises(ConnectorError) as e:
        asyncio.run(connector.test_connection())
    assert "did not say what scope" in str(e.value)


def test_the_metadata_only_scope_is_accepted():
    connector, _ = build(
        FakeDrive(granted_scope="https://www.googleapis.com/auth/drive.metadata.readonly")
    )
    asyncio.run(connector.test_connection())


def test_the_token_is_cached_across_calls():
    fake = FakeDrive()
    connector, _ = build(fake)
    asyncio.run(connector.discover_schema())
    asyncio.run(connector.query({"path": "readme.txt"}, 10))
    assert fake.token_requests == 1


# --- config -------------------------------------------------------------------


def test_the_key_is_accepted_as_a_json_string():
    """An operator pasting the downloaded file into a form produces a string."""
    connector, _ = build(FakeDrive(), service_account_json=json.dumps(SERVICE_ACCOUNT))
    asyncio.run(connector.test_connection())


def test_a_missing_key_says_the_account_is_the_operators():
    connector, _ = build(FakeDrive(), service_account_json=None)
    with pytest.raises(ConnectorError) as e:
        asyncio.run(connector.test_connection())
    assert "service_account_json" in str(e.value)


def test_malformed_key_json_is_reported_as_such():
    connector, _ = build(FakeDrive(), service_account_json="{not json")
    with pytest.raises(ConnectorError) as e:
        asyncio.run(connector.test_connection())
    assert "not valid JSON" in str(e.value)


def test_a_key_missing_client_email_is_refused():
    sa = {k: v for k, v in SERVICE_ACCOUNT.items() if k != "client_email"}
    connector, _ = build(FakeDrive(), service_account_json=sa)
    with pytest.raises(ConnectorError) as e:
        asyncio.run(connector.test_connection())
    assert "client_email" in str(e.value)


def test_a_shared_drive_sets_both_flags_and_the_corpus():
    """Omitting either flag returns nothing for a shared-drive datasource, which
    looks exactly like a permission problem. The fake asserts on all three."""
    connector, _ = build(FakeDrive(shared_drive_expected="sd-1"), shared_drive_id="sd-1")
    asyncio.run(connector.test_connection())


# --- the q clause -------------------------------------------------------------


def test_the_folder_id_bounds_every_listing():
    fake = FakeDrive()
    connector, _ = build(fake)
    asyncio.run(connector.discover_schema())
    assert fake.queries, "no query was recorded"
    # Every listing is parented, so nothing outside the shared tree is asked for.
    assert all("in parents" in q and "trashed = false" in q for q in fake.queries)
    assert f"'{ROOT}' in parents" in fake.queries[0]


def test_a_hostile_folder_id_cannot_break_out_of_the_quoted_clause():
    """A `q` literal is single-quoted, so an id carrying a quote would end it
    early and let the remainder be read as query syntax."""
    fake = FakeDrive()
    connector, _ = build(fake, folder_id="x' or name != '")
    asyncio.run(connector.test_connection())
    assert fake.queries == ["'x\\' or name != \\'' in parents and trashed = false"]


def test_there_is_no_raw_query_passthrough():
    """`q` is a query language with an `in parents` operator; accepting one from
    a caller would address files outside the configured folder."""
    fake = FakeDrive()
    connector, _ = build(fake)
    with pytest.raises(ConnectorError):
        asyncio.run(connector.query({"q": "name contains 'salary'"}, 10))
    assert not any("salary" in q for q in fake.queries)


# --- native documents ---------------------------------------------------------


def test_google_native_files_get_an_extension_matching_their_export():
    assert effective_name("Q3 numbers", "application/vnd.google-apps.spreadsheet") == "Q3 numbers.csv"
    assert effective_name("Charter", "application/vnd.google-apps.document") == "Charter.txt"
    # An ordinary file is left alone, extension and all.
    assert effective_name("q3.csv", "text/csv") == "q3.csv"
    assert export_target("text/csv") is None


def test_a_google_sheet_is_exported_and_read_as_csv():
    connector, _ = build(FakeDrive())
    rows = asyncio.run(connector.query({"path": "Q3 numbers.csv"}, 10))
    assert rows == [{"quarter": "Q3", "total": "99"}]


def test_a_google_doc_is_exported_and_read_as_text():
    connector, _ = build(FakeDrive())
    rows = asyncio.run(connector.query({"path": "Charter.txt"}, 10))
    assert "the charter text" in str(rows)


def test_an_ordinary_file_takes_the_media_path_not_export():
    """Drive refuses to export a non-native file, so reading the dispatch table
    backwards would 403 rather than fail quietly."""
    connector, _ = build(FakeDrive())
    rows = asyncio.run(connector.query({"path": "reports/q3.csv"}, 10))
    assert rows == [{"name": "acme", "amount": "10"}, {"name": "beta", "amount": "20"}]


# --- discovery ----------------------------------------------------------------


def test_discovery_walks_the_whole_tree():
    connector, _ = build(FakeDrive())
    names = {t["name"] for t in asyncio.run(connector.discover_schema())}
    assert names == {"readme.txt", "Q3 numbers.csv", "Charter.txt", "reports/q3.csv",
                     "reports/old/q1.csv"}


def test_discovery_sniffs_csv_columns():
    connector, _ = build(FakeDrive())
    tables = {t["name"]: t for t in asyncio.run(connector.discover_schema())}
    assert [f["name"] for f in tables["reports/q3.csv"]["fields"]] == ["name", "amount"]
    assert tables["readme.txt"]["fields"] == [{"name": "content", "type": "text"}]


def test_pagination_follows_the_next_page_token():
    connector, _ = build(FakeDrive(page_size=1))
    names = {t["name"] for t in asyncio.run(connector.discover_schema())}
    assert names == {"readme.txt", "Q3 numbers.csv", "Charter.txt", "reports/q3.csv",
                     "reports/old/q1.csv"}


# --- query --------------------------------------------------------------------


def test_a_query_without_a_path_is_refused():
    connector, _ = build(FakeDrive())
    with pytest.raises(ConnectorError) as e:
        asyncio.run(connector.query({}, 10))
    assert "path" in str(e.value)


def test_an_unknown_path_says_what_the_account_can_see():
    connector, _ = build(FakeDrive())
    with pytest.raises(ConnectorError) as e:
        asyncio.run(connector.query({"path": "nope.csv"}, 10))
    assert "nope.csv" in str(e.value) and "shared" in str(e.value)


def test_a_query_cannot_reach_a_file_discovery_would_not_list():
    """`_resolve` walks rather than asking Drive to look the name up, so a path
    naming something outside the folder resolves to nothing at all."""
    fake = FakeDrive()
    connector, _ = build(fake, folder_id="f-old")
    with pytest.raises(ConnectorError):
        asyncio.run(connector.query({"path": "reports/q3.csv"}, 10))
    assert asyncio.run(_names(connector)) == {"q1.csv"}


async def _names(connector: GoogleDriveConnector) -> set[str]:
    return {t["name"] for t in await connector.discover_schema()}


def test_throttling_names_google_not_graph():
    """`get_json` is shared with SharePoint now, and a message naming the wrong
    vendor sends an operator to the wrong console."""
    def throttle(request: httpx.Request) -> httpx.Response:
        if request.url.host == TOKEN_HOST:
            return httpx.Response(200, json={"access_token": "drive-token", "expires_in": 3599,
                                             "scope": READ_ONLY_SCOPE})
        return httpx.Response(429, headers={"Retry-After": "23"},
                              json={"error": {"message": "Rate Limit Exceeded"}})

    guard = GetOnlyTransport(httpx.MockTransport(throttle),
                             post_allowed_hosts=frozenset({TOKEN_HOST}))
    connector = GoogleDriveConnector(
        {"service_account_json": SERVICE_ACCOUNT, "folder_id": ROOT}, transport=guard
    )
    with pytest.raises(ConnectorError) as e:
        asyncio.run(connector.test_connection())
    assert "Google Drive is throttling" in str(e.value) and "23" in str(e.value)
