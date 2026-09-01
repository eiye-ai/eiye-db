"""S3-compatible object storage: AWS S3, MinIO, and anything else speaking the S3 API.

Two calls, ever: `ListObjectsV2` to enumerate a prefix, and `GetObject` to read
one object. There is no third, and no request field through which a caller could
ask for one — `request` is `{"key": "..."}`, not a query language. So this
connector's read-only guarantee has a different shape from the SQL ones. Those
must defend a text channel wide enough to express `DROP TABLE`, which is why
each verifies at connect that its login cannot write. Here the write APIs are
simply never called, and that is checkable by reading this file.

What is *not* claimed: that the credential itself cannot write. AWS offers no
way to ask "is this key read-only" that a read-only key is permitted to call,
and MinIO implements no equivalent, so a probe would either lie or write. Give
eiye a key scoped to `s3:GetObject` and `s3:ListBucket` on this bucket; the
README shows the policy.

Object bytes are read into the request's memory, parsed, and dropped. Nothing is
written to `eiye.db` and nothing is cached on disk — this is a connector, not a
copy of the bucket.

Config:

    bucket             required
    prefix             optional; bounds what this datasource exposes
    endpoint_url       optional; set for MinIO and other S3-compatible servers
    region             optional
    access_key_id      optional pair; omit both to use the ambient AWS
    secret_access_key  credential chain (instance role, profile, environment)

Not a crawler, not a Bedrock knowledge base, not S3 Select, not Glacier restore.
"""

import asyncio
import io
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from eiye_db.connectors import documents
from eiye_db.connectors.base import Connector, ConnectorError

# Mirrors service.QUERY_TIMEOUT_SECONDS; see the note in mysql.py for why it is
# duplicated rather than imported, and why the driver needs its own bound.
_READ_TIMEOUT_SECONDS = 30

# Discovery stops here, the way the filesystem connector stops at 1000 files.
# Also the ListObjectsV2 page size, so discovery is one round-trip.
_MAX_OBJECTS = 1000

# Discovery reads the head of a CSV to name its columns. Ranged, so a large
# object costs the same as a small one — but it is still one GET per CSV, so
# only the first _MAX_SNIFFED_CSVS are read. Past that, discovery lists the
# object with no fields; querying it still returns its rows.
_CSV_SNIFF_BYTES = 64 * 1024
_MAX_SNIFFED_CSVS = 100

# A query reads the whole object, so it is bounded. Refused rather than
# truncated: silently returning the first N rows of a file that has more is the
# kind of answer a governed surface must not give.
_MAX_OBJECT_BYTES = 64 * 1024 * 1024


class S3Connector(Connector):
    def _settings(self) -> dict[str, Any]:
        bucket = self.config.get("bucket")
        if not bucket:
            raise ConnectorError("s3 config requires 'bucket'")
        key_id = self.config.get("access_key_id") or ""
        secret = self.config.get("secret_access_key") or ""
        if bool(key_id) != bool(secret):
            raise ConnectorError(
                "s3 config needs both 'access_key_id' and 'secret_access_key', or neither "
                "(to use the ambient AWS credential chain)"
            )
        return {
            "bucket": bucket,
            "prefix": self.config.get("prefix") or "",
            "endpoint_url": self.config.get("endpoint_url") or None,
            "region": self.config.get("region") or None,
            "access_key_id": key_id,
            "secret_access_key": secret,
        }

    def _client(self, s: dict[str, Any]):
        credentials = {}
        if s["access_key_id"]:
            credentials = {
                "aws_access_key_id": s["access_key_id"],
                "aws_secret_access_key": s["secret_access_key"],
            }
        return boto3.client(
            "s3",
            endpoint_url=s["endpoint_url"],
            region_name=s["region"],
            config=BotoConfig(
                connect_timeout=10,
                read_timeout=_READ_TIMEOUT_SECONDS,
                retries={"max_attempts": 3, "mode": "standard"},
                signature_version="s3v4",
                # Virtual-host addressing needs a wildcard DNS record per
                # bucket, which a self-hosted MinIO will not have. AWS keeps its
                # default (virtual-host), which is what AWS prefers.
                s3={"addressing_style": "path"} if s["endpoint_url"] else {},
            ),
            **credentials,
        )

    def _list(self, client, s: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            resp = client.list_objects_v2(Bucket=s["bucket"], Prefix=s["prefix"], MaxKeys=_MAX_OBJECTS)
        except (BotoCoreError, ClientError) as e:
            raise ConnectorError(f"listing failed: {e}") from e
        # Zero-byte keys ending in "/" are the console's folder markers, not objects.
        return [o for o in resp.get("Contents", []) if not (o["Key"].endswith("/") and o["Size"] == 0)]

    def _fetch(self, client, s: dict[str, Any], key: str, max_bytes: int) -> tuple[bytes, bool]:
        """Read up to max_bytes of an object; also report whether more remained.

        The range asks for one byte past the cap, so a full-length response is
        proof the object is longer rather than a coincidence.
        """
        try:
            resp = client.get_object(Bucket=s["bucket"], Key=key, Range=f"bytes=0-{max_bytes}")
            body = resp["Body"]
            try:
                data = body.read()
            finally:
                body.close()
        except (BotoCoreError, ClientError) as e:
            raise ConnectorError(f"cannot read {key}: {e}") from e
        return data[:max_bytes], len(data) > max_bytes

    # --- discovery -----------------------------------------------------------

    def _discover_sync(self) -> list[dict[str, Any]]:
        s = self._settings()
        client = self._client(s)
        try:
            tables = []
            sniffed = 0
            for obj in self._list(client, s):
                key = obj["Key"]
                name = key[len(s["prefix"]) :]
                kind = documents.kind_for(key)
                if kind == "csv":
                    fields = []
                    if sniffed < _MAX_SNIFFED_CSVS:
                        sniffed += 1
                        fields = self._csv_fields(client, s, key)
                    tables.append({"name": name, "fields": fields})
                elif kind == "xlsx":
                    # A workbook is a zip: nothing can be inferred from its first
                    # 64 KiB, and fetching every workbook whole would make
                    # discovery download the bucket. Columns appear on query.
                    tables.append({"name": name, "fields": []})
                elif kind in ("pdf", "text"):
                    tables.append({"name": name, "fields": [{"name": "content", "type": "text"}]})
            return tables
        finally:
            client.close()

    def _csv_fields(self, client, s: dict[str, Any], key: str) -> list[dict[str, Any]]:
        data, truncated = self._fetch(client, s, key, _CSV_SNIFF_BYTES)
        text = data.decode("utf-8", errors="replace")
        if truncated:
            # Drop the partial last line so a half-read row cannot skew the
            # inferred types. No newline in 64 KiB means no usable header.
            text = text[: text.rfind("\n") + 1]
        return documents.csv_fields(io.StringIO(text, newline=""))

    # --- query ---------------------------------------------------------------

    def _query_sync(self, key: str, limit: int) -> list[dict[str, Any]]:
        s = self._settings()
        client = self._client(s)
        try:
            # Prefix scoping by construction: the caller names a key relative to
            # the configured prefix and the two are concatenated, so there is no
            # traversal to check for. S3 keys are opaque strings — ".." is a
            # literal two characters to the server, not a parent directory.
            full_key = s["prefix"] + key
            data, truncated = self._fetch(client, s, full_key, _MAX_OBJECT_BYTES)
        finally:
            client.close()
        if truncated:
            raise ConnectorError(
                f"object exceeds the {_MAX_OBJECT_BYTES // (1024 * 1024)} MiB eiye reads in one query: {key}"
            )
        kind = documents.kind_for(key)
        if kind == "csv":
            return documents.csv_rows(io.StringIO(data.decode("utf-8", errors="replace"), newline=""), limit)
        if kind == "xlsx":
            return documents.xlsx_rows(io.BytesIO(data), limit, key)
        if kind == "pdf":
            return documents.pdf_rows(io.BytesIO(data), key)
        return documents.text_rows(data.decode("utf-8", errors="replace"))

    # --- interface -----------------------------------------------------------

    async def test_connection(self) -> None:
        await asyncio.to_thread(self._test_sync)

    def _test_sync(self) -> None:
        s = self._settings()
        client = self._client(s)
        try:
            # ListObjectsV2 rather than HeadBucket: it exercises the exact
            # permission the connector needs, on the exact prefix it will use.
            try:
                client.list_objects_v2(Bucket=s["bucket"], Prefix=s["prefix"], MaxKeys=1)
            except (BotoCoreError, ClientError) as e:
                raise ConnectorError(f"connection failed: {e}") from e
        finally:
            client.close()

    async def discover_schema(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._discover_sync)

    async def query(self, request: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        key = request.get("key")
        if not key:
            raise ConnectorError("s3 query requires 'key'")
        return await asyncio.to_thread(self._query_sync, key, limit)
