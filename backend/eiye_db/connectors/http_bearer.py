"""OAuth2 client-credentials plumbing, and the token inspection that goes with it.

Written for SharePoint and kept separate from `http_basic.py` because the two
share nothing but the word "auth". A Basic credential is a static string the
operator pastes in; a client-credentials token is fetched, expires, and — the
part that matters here — *carries its own scope list*, which is something eiye
can check.

**The only non-GET request any connector built on this module makes is the token
request.** That is a real hole in a GET-only claim, so it is not left implicit:
`GetOnlyTransport` refuses POST unless the host is explicitly allowed, and the
SharePoint suite allows exactly `login.microsoftonline.com` and asserts on what
was posted there. A second, unguarded HTTP client for token acquisition would
have been easier and would have quietly made the read-only claim untestable.

`token_roles` is the other reason this module exists. Application-only tokens
from Entra carry a `roles` claim listing the granted application permissions, so
a connector can read what it was handed and refuse a credential that is broader
than the product is willing to accept. That is the HTTP analogue of the SQL
connectors' privilege probe: for the first time on an HTTP source, the
credential itself can be inspected rather than trusted.
"""

import base64
import binascii
import json
import time
from typing import Any

import httpx

from eiye_db.connectors.base import ConnectorError

#: `.default` asks for whatever application permissions the app was consented,
#: which is the only scope form client credentials accepts.
GRAPH_SCOPE = "https://graph.microsoft.com/.default"

LOGIN_HOST = "login.microsoftonline.com"

TIMEOUT_SECONDS = 30

#: Refresh this far before expiry, so a token cannot lapse mid-request.
_EXPIRY_MARGIN_SECONDS = 120


def token_endpoint(tenant_id: str) -> str:
    return f"https://{LOGIN_HOST}/{tenant_id}/oauth2/v2.0/token"


async def fetch_token(
    tenant_id: str,
    client_id: str,
    client_secret: str,
    *,
    scope: str = GRAPH_SCOPE,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[str, float]:
    """Client-credentials grant. Returns the token and the epoch it expires at.

    Hand-rolled rather than taking `msal`. The grant is one form POST and one
    JSON response; a dependency that pulls in a token cache, a broker and a
    device-code flow to do that is a lot of code we do not control for no gain.
    """
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, transport=transport) as client:
        try:
            response = await client.post(
                token_endpoint(tenant_id),
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "scope": scope,
                },
            )
        except httpx.HTTPError as e:
            raise ConnectorError(f"could not reach the Entra token endpoint: {e}") from e

    if response.status_code >= 400:
        # Entra's error body is the useful part — AADSTS7000215 (bad secret) and
        # AADSTS700016 (unknown app) send an operator to different places, and a
        # bare 401 sends them to neither.
        detail = ""
        try:
            body = response.json()
            detail = body.get("error_description") or body.get("error") or ""
        except ValueError:
            detail = response.text[:200]
        raise ConnectorError(
            f"Entra refused the client credentials (HTTP {response.status_code}). {detail.splitlines()[0] if detail else ''}"
        )

    try:
        payload = response.json()
    except ValueError as e:
        raise ConnectorError("the Entra token endpoint did not return JSON") from e

    token = payload.get("access_token")
    if not token:
        raise ConnectorError("the Entra token response carried no access_token")
    expires_in = payload.get("expires_in")
    lifetime = float(expires_in) if isinstance(expires_in, (int, float, str)) and str(expires_in).isdigit() else 3600.0
    return token, time.time() + lifetime - _EXPIRY_MARGIN_SECONDS


def token_roles(access_token: str) -> list[str]:
    """Read the `roles` claim out of a JWT without verifying it.

    Not verifying is correct here rather than lazy: eiye is not the audience of
    this token and holds none of the signing keys. Microsoft verifies it, and if
    the signature were bad the very next Graph call would 401. What the claim is
    used for is a *narrowing* check — refusing a credential that is broader than
    we want — so a forged token could only ever cause eiye to refuse itself.

    Returns `[]` for anything unreadable, which callers treat as "unknown"
    rather than "no permissions": an opaque token is a reason to say so, not to
    silently allow.
    """
    parts = access_token.split(".")
    if len(parts) != 3:
        return []
    segment = parts[1]
    segment += "=" * (-len(segment) % 4)  # JWTs strip base64 padding
    try:
        claims = json.loads(base64.urlsafe_b64decode(segment))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return []
    roles = claims.get("roles")
    return [str(r) for r in roles] if isinstance(roles, list) else []


async def get_json(
    client: httpx.AsyncClient,
    url: str,
    params: dict | None = None,
    *,
    auth_hint: str = "",
) -> Any:
    """One GET against Graph, with Graph's own error message preserved.

    Graph puts a genuinely useful string in `error.message` — "Item not found",
    "Access denied" and "The site was not found" are three different operator
    problems behind what would otherwise be one 403.
    """
    try:
        response = await client.get(url, params=params)
    except httpx.HTTPError as e:
        raise ConnectorError(f"request to {url} failed: {e}") from e

    if response.status_code == 429:
        retry = response.headers.get("retry-after", "")
        raise ConnectorError(
            "Microsoft Graph is throttling this app"
            + (f"; it asked for a {retry}s pause" if retry else "")
            + ". Reduce the query limit or retry later — eiye does not retry on your behalf, "
            "because a connector that silently sleeps turns a governed query into an unbounded one."
        )
    if response.status_code >= 400:
        raise ConnectorError(f"HTTP {response.status_code} from {url}: {_graph_error(response)}"
                             + ((" " + auth_hint) if response.status_code in (401, 403) and auth_hint else ""))
    try:
        return response.json()
    except ValueError as e:
        raise ConnectorError(f"{url} did not return JSON") from e


def _graph_error(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "")
    return str(error or "")
