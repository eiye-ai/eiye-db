"""Mechanical proof for the structural read-only tier.

The structural-tier connectors — REST, S3, filesystem, Confluence, Jira,
ServiceNow and SharePoint — do not get their read-only guarantee the way the SQL
connectors do. There is no server refusing them a write: they simply never ask
for one. That claim is real but it was only ever provable by reading the code,
which is a weaker thing than the SQL connectors offer and was documented as such.

The guards here close that gap. Each one sits at the boundary the connector
actually crosses — an HTTP transport, a botocore event, a file open — and turns
an attempted write into a test failure. Wired into a connector's whole suite
they make the claim mechanical: add a POST, a PutObject or an `open(..., "w")`
anywhere on an exercised path and the build goes red.

What this does *not* prove: paths the tests never take. A branch with no
coverage could still hide a write, so this is a coverage-bounded proof, not an
exhaustive one. It is still a long way past "read the code and trust it".
"""

from __future__ import annotations

import builtins
import pathlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx

# S3 operations the connector is allowed to issue. ListObjectsV2 and GetObject
# are the two it actually calls; the Head* pair is allowed because botocore may
# issue them while resolving a bucket, and neither writes.
S3_READ_OPERATIONS = frozenset(
    {"ListObjectsV2", "ListObjects", "GetObject", "HeadObject", "HeadBucket", "ListBuckets"}
)

# The HTTP methods a GET-only connector may use. HEAD is included because it is
# a GET without a body and some clients issue one during redirect handling.
READ_METHODS = frozenset({"GET", "HEAD"})

_WRITE_MODE_FLAGS = frozenset("wxa+")


class WriteAttempted(BaseException):
    """A structurally-read-only connector tried to write.

    Deliberately a `BaseException` rather than an `Exception`. The connectors
    under test catch broad exception types on purpose — `documents.py` alone has
    six `except Exception:` handlers, because a malformed spreadsheet must not
    take down a discovery pass. A guard that application code can swallow proves
    nothing, so this is raised outside the hierarchy those handlers catch.
    """


class GetOnlyTransport(httpx.AsyncBaseTransport):
    """Wrap an httpx transport and refuse anything that is not a read.

    Records what it saw, so a test can assert the guard was actually exercised.
    A guard that never fires is indistinguishable from a guard that is broken,
    and `methods_seen` is what tells the two apart.

    `post_allowed_hosts` exists for one case and should stay that narrow:
    OAuth2 client credentials. Fetching a bearer token is a POST, so a connector
    that authenticates that way cannot be literally GET-only. The honest options
    were to give the token client its own unguarded transport — which would make
    the claim untestable, because the guard would no longer see every request
    the connector makes — or to allow POST to exactly the identity provider and
    record it. This is the second. `posts_seen` carries the URLs, so a test can
    assert the only POST that happened was the token request it expected.
    """

    def __init__(
        self,
        delegate: httpx.AsyncBaseTransport,
        post_allowed_hosts: frozenset[str] = frozenset(),
    ):
        self.delegate = delegate
        self.post_allowed_hosts = post_allowed_hosts
        self.methods_seen: list[str] = []
        self.posts_seen: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method not in READ_METHODS:
            if request.method == "POST" and request.url.host in self.post_allowed_hosts:
                self.posts_seen.append(str(request.url))
            else:
                raise WriteAttempted(f"{request.method} {request.url}")
        self.methods_seen.append(request.method)
        return await self.delegate.handle_async_request(request)


def guard_s3_client(client: Any, operations_seen: list[str] | None = None) -> Any:
    """Refuse any S3 operation outside the read-only allowlist.

    Hooks `before-call`, which botocore fires once per operation with the
    resolved model and before the request goes out — so a write is refused
    rather than merely observed.
    """

    def _check(model: Any = None, **_kwargs: Any) -> None:
        name = getattr(model, "name", None)
        if name is None:  # pragma: no cover - botocore always supplies one
            return
        if name not in S3_READ_OPERATIONS:
            raise WriteAttempted(f"S3 {name}")
        if operations_seen is not None:
            operations_seen.append(name)

    client.meta.events.register("before-call.s3", _check)
    return client


def guard_connector_files(connector: Any, modes_seen: list[str] | None = None) -> Any:
    """Run every public call on `connector` under the file-write guard.

    Wrapping the connector rather than the whole test is deliberate. The guard
    cannot be active for the entire test, because the fixtures have to *write*
    the sample files first; and it cannot be left to each call site, because one
    forgotten `with` is a silent hole. Binding it to the object the tests are
    handed makes it impossible to hold wrong.
    """
    for name in ("test_connection", "discover_schema", "discover_relationships", "query"):
        original = getattr(connector, name, None)
        if original is None:
            continue

        def wrap(call):
            async def guarded(*args: Any, **kwargs: Any) -> Any:
                with guard_file_writes(modes_seen):
                    return await call(*args, **kwargs)

            return guarded

        setattr(connector, name, wrap(original))
    return connector


@contextmanager
def guard_file_writes(modes_seen: list[str] | None = None) -> Iterator[None]:
    """Refuse any file open that asks for write access.

    Patches both `builtins.open` and `Path.open`, because the filesystem
    connector uses the latter and the document extractors it delegates to
    (openpyxl, the PDF reader) use the former.

    Checked by mode string rather than by making the directory read-only:
    permissions are ignored when the tests run as root, which would leave the
    guard silently inert in a container. This proves the property of the code
    instead of the property of one machine's filesystem.
    """
    real_open = builtins.open
    real_path_open = pathlib.Path.open

    def _check(mode: Any) -> None:
        text = mode if isinstance(mode, str) else ""
        if _WRITE_MODE_FLAGS & set(text):
            raise WriteAttempted(f"open(mode={mode!r})")
        if modes_seen is not None:
            modes_seen.append(text or "r")

    def _open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        _check(mode)
        return real_open(file, mode, *args, **kwargs)

    def _path_open(self: pathlib.Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        _check(mode)
        return real_path_open(self, mode, *args, **kwargs)

    builtins.open = _open
    pathlib.Path.open = _path_open
    try:
        yield
    finally:
        builtins.open = real_open
        pathlib.Path.open = real_path_open
