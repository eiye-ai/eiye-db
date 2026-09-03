"""GET plumbing shared by the HTTP connectors that authenticate with a password.

Extracted at the third use — Confluence, Jira, ServiceNow — not on speculation.
What is genuinely the same across all three is narrow: build a client that sends
HTTP Basic credentials, issue a GET, and turn a status code into a
`ConnectorError`. What is *not* the same is what an auth failure means, so each
product supplies its own hint rather than inheriting a message that would be
wrong for it. Atlassian tokens expire after a year; a ServiceNow account is more
likely to be locked out or missing a role.

Everything here is GET-only by construction. These connectors sit in the
structural read-only tier, and the transport guard in
`tests/readonly_guards.py` is what enforces it.
"""

from typing import Any

import httpx

from eiye_db.connectors.base import ConnectorError

TIMEOUT_SECONDS = 30


def basic_auth_client(
    base_url: str,
    credentials: tuple[str, str],
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        auth=credentials,
        headers={"Accept": "application/json"},
        timeout=TIMEOUT_SECONDS,
        transport=transport,
    )


async def get_json(
    client: httpx.AsyncClient,
    path: str,
    params: dict | None = None,
    *,
    auth_hint: str = "",
) -> Any:
    """One GET, with the failures worth naming named.

    401 and 403 get their own message because the likeliest cause is rarely a
    typo — a credential that worked for months has usually expired or lost a
    role, and a bare status code sends the operator looking in the wrong place.
    """
    response = await get_response(client, path, params, auth_hint=auth_hint)
    try:
        return response.json()
    except ValueError as e:
        raise ConnectorError(f"{path} did not return JSON") from e


async def get_response(
    client: httpx.AsyncClient,
    path: str,
    params: dict | None = None,
    *,
    auth_hint: str = "",
) -> httpx.Response:
    """As `get_json`, but hands back the response.

    Needed where pagination lives in a header rather than the body — ServiceNow
    puts its next-page cursor in `Link`, so the body alone is not enough to walk
    a table.
    """
    try:
        response = await client.get(path, params=params)
    except httpx.HTTPError as e:
        raise ConnectorError(f"request to {path} failed: {e}") from e
    if response.status_code in (401, 403):
        raise ConnectorError(
            f"HTTP {response.status_code} from {path}: the credential was rejected, or the account "
            f"cannot see this content.{(' ' + auth_hint) if auth_hint else ''}"
        )
    if response.status_code >= 400:
        raise ConnectorError(f"HTTP {response.status_code} from {path}")
    return response
