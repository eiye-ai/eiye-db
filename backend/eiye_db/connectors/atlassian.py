"""Shared base for the Atlassian Cloud connectors.

Extracted when the second one arrived, not the first. Confluence and Jira Cloud
authenticate identically — an operator-minted API token over HTTP Basic — and
return identical failures for an expired or under-privileged token, so those
belong in one place. Pagination does **not**: Confluence v2 returns
`_links.next` holding an opaque site-absolute URL, while Jira's issue search
returns a bare `nextPageToken`. Two different mechanisms, so each connector
walks its own.

Read-only across both is structural. Neither product offers a read-only
credential, nor any way to ask whether a token can write, so there is nothing to
verify at connect the way the SQL connectors do. What holds is that these
connectors issue GET and nothing else, enforced by the transport guard in
`tests/readonly_guards.py`.

The client construction and status-code mapping under all of this live in
`http_basic.py`, shared with the ServiceNow connector, which authenticates the
same way and shares none of the rest.
"""

from typing import Any
from urllib.parse import urlsplit

import httpx

from eiye_db.connectors.base import Connector, ConnectorError
from eiye_db.connectors.http_basic import basic_auth_client, get_json

#: Named on every 401/403 because an Atlassian credential that worked for months
#: has usually expired rather than been mistyped.
AUTH_HINT = "Atlassian API tokens expire after one year."

TOKEN_HELP = (
    "Mint the token at https://id.atlassian.com/manage-profile/security/api-tokens — it is the "
    "account's own credential, so give eiye an account with access to only what it should read."
)


class AtlassianCloudConnector(Connector):
    """Config, auth and GET plumbing common to Confluence and Jira Cloud."""

    #: Human name used in error messages, e.g. "confluence".
    PRODUCT = "atlassian"

    def __init__(self, config: dict[str, Any], transport: httpx.AsyncBaseTransport | None = None):
        super().__init__(config)
        self._transport = transport

    # --- config --------------------------------------------------------------

    def _site(self) -> str:
        """The site origin, with any path discarded.

        Operators paste whatever the browser was showing — `/wiki/spaces/ENG/...`
        for Confluence, `/jira/software/projects/ENG/boards/1` for Jira — and an
        Atlassian Cloud site is always served at the root of its own host, so
        everything after the host is a page the operator happened to be on
        rather than part of the address. Discarding it accepts every form of the
        setting and lands them all on the same origin, which also keeps the
        site-absolute cursor URLs Confluence returns from doubling their prefix.
        """
        base_url = self.config.get("base_url")
        if not base_url:
            raise ConnectorError(
                f"{self.PRODUCT} config requires 'base_url', e.g. https://your-site.atlassian.net"
            )
        parts = urlsplit(base_url.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ConnectorError(
                f"{self.PRODUCT} base_url must be an absolute http(s) URL, got '{base_url}'"
            )
        return f"{parts.scheme}://{parts.netloc}"

    def _auth(self) -> tuple[str, str]:
        email = self.config.get("email")
        token = self.config.get("api_token")
        if not email or not token:
            raise ConnectorError(f"{self.PRODUCT} config requires 'email' and 'api_token'. {TOKEN_HELP}")
        return email, token

    def _client(self) -> httpx.AsyncClient:
        return basic_auth_client(self._site(), self._auth(), self._transport)

    # --- HTTP ----------------------------------------------------------------

    async def _get(self, client: httpx.AsyncClient, path: str, params: dict | None = None) -> Any:
        return await get_json(client, path, params, auth_hint=AUTH_HINT)
