"""S3-compatible connector tests.

Two layers. The offline ones drive a fake client, because what can go wrong in
this connector is mostly bookkeeping — which key gets fetched, what a folder
marker is, when a ranged read is truncated — and none of that needs a server.
The live ones are gated on EIYE_TEST_S3_ENDPOINT and run against MinIO, which
is what proves the boto3 configuration actually talks to an S3-compatible
server rather than merely being well-formed.
"""

import asyncio
import io
import os

import pytest

from eiye_db.connectors.base import ConnectorError

boto3 = pytest.importorskip("boto3")

from botocore.exceptions import ClientError  # noqa: E402  (after importorskip)

from eiye_db.connectors.s3 import S3Connector  # noqa: E402  (after importorskip)

CSV = b"name,age\nAlice,30\nBob,25\n"
NOTES = b"# Notes\nemail: x@y.com\n"


# --- config -------------------------------------------------------------------


def test_missing_bucket():
    with pytest.raises(ConnectorError, match="requires 'bucket'"):
        asyncio.run(S3Connector({}).test_connection())


def test_half_a_credential_rejected():
    with pytest.raises(ConnectorError, match="both"):
        asyncio.run(S3Connector({"bucket": "b", "access_key_id": "AKIA"}).test_connection())


def test_no_credential_is_allowed():
    # Omitting both keys is the instance-role / profile case, not an error.
    assert S3Connector({"bucket": "b"})._settings()["access_key_id"] == ""


def test_missing_key():
    with pytest.raises(ConnectorError, match="requires 'key'"):
        asyncio.run(S3Connector({"bucket": "b"}).query({}, limit=10))


def test_custom_endpoint_gets_path_addressing():
    # MinIO and friends have no wildcard DNS record per bucket, so virtual-host
    # addressing cannot resolve. This is the setting that makes them work.
    conn = S3Connector({"bucket": "b", "endpoint_url": "http://127.0.0.1:9000", "region": "us-east-1"})
    client = conn._client(conn._settings())
    try:
        assert client.meta.config.s3["addressing_style"] == "path"
        assert client.meta.config.signature_version == "s3v4"
        assert client.meta.config.read_timeout == 30
    finally:
        client.close()


def test_aws_keeps_its_default_addressing():
    conn = S3Connector({"bucket": "b", "region": "us-east-1"})
    client = conn._client(conn._settings())
    try:
        assert "addressing_style" not in (client.meta.config.s3 or {})
    finally:
        client.close()


# --- offline, against a fake client -------------------------------------------


class FakeS3:
    """The two calls this connector makes, and nothing else.

    Deliberately not a mock: `calls` is asserted on, so a test can state that
    discovery read three objects rather than three hundred.
    """

    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects
        self.calls: list[tuple[str, str]] = []

    def list_objects_v2(self, Bucket, Prefix="", MaxKeys=1000):
        self.calls.append(("list", Prefix))
        contents = [
            {"Key": k, "Size": len(v)} for k, v in sorted(self.objects.items()) if k.startswith(Prefix)
        ]
        return {"Contents": contents[:MaxKeys]}

    def get_object(self, Bucket, Key, Range):
        self.calls.append(("get", Key))
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "not found"}}, "GetObject")
        end = int(Range.split("-")[1])
        return {"Body": io.BytesIO(self.objects[Key][: end + 1])}

    def close(self):
        pass


@pytest.fixture
def fake(monkeypatch):
    """Point every S3Connector at one fake client for the duration of a test."""

    def build(objects, **config):
        client = FakeS3(objects)
        monkeypatch.setattr(S3Connector, "_client", lambda self, s: client)
        return S3Connector({"bucket": "b", **config}), client

    return build


def test_discover_maps_keys_to_tables(fake):
    conn, _ = fake({"people.csv": CSV, "notes.md": NOTES, "book.pdf": b"%PDF-1.4", "image.png": b"\x89PNG"})
    tables = {t["name"]: t for t in asyncio.run(conn.discover_schema())}
    assert tables["people.csv"]["fields"] == [
        {"name": "name", "type": "string"},
        {"name": "age", "type": "integer"},
    ]
    assert tables["notes.md"]["fields"] == [{"name": "content", "type": "text"}]
    assert tables["book.pdf"]["fields"] == [{"name": "content", "type": "text"}]
    # An extension with no extractor is not a table, same as on the filesystem.
    assert "image.png" not in tables


def test_discover_names_are_relative_to_the_prefix(fake):
    conn, client = fake({"exports/2026/q1.csv": CSV, "other/skip.csv": CSV}, prefix="exports/")
    tables = {t["name"] for t in asyncio.run(conn.discover_schema())}
    assert tables == {"2026/q1.csv"}
    assert ("list", "exports/") in client.calls


def test_discover_skips_folder_markers(fake):
    conn, _ = fake({"reports/": b"", "reports/q1.csv": CSV})
    assert {t["name"] for t in asyncio.run(conn.discover_schema())} == {"reports/q1.csv"}


def test_discover_does_not_fetch_workbooks(fake):
    # A workbook is a zip; its first 64 KiB tells you nothing, and fetching
    # every one whole would turn discovery into a download of the bucket.
    conn, client = fake({"contacts.xlsx": b"PK\x03\x04not-really"})
    tables = {t["name"]: t for t in asyncio.run(conn.discover_schema())}
    assert tables["contacts.xlsx"]["fields"] == []
    assert [c for c in client.calls if c[0] == "get"] == []


def test_discover_caps_the_number_of_csvs_it_reads(fake):
    conn, client = fake({f"f{i:04}.csv": CSV for i in range(150)})
    tables = asyncio.run(conn.discover_schema())
    gets = [c for c in client.calls if c[0] == "get"]
    assert len(tables) == 150  # every object is still listed
    assert len(gets) == 100  # but only the first 100 are read for columns
    assert tables[-1]["fields"] == []


def test_query_reads_the_key_under_the_prefix(fake):
    conn, client = fake({"exports/people.csv": CSV}, prefix="exports/")
    rows = asyncio.run(conn.query({"key": "people.csv"}, limit=10))
    assert rows == [{"name": "Alice", "age": "30"}, {"name": "Bob", "age": "25"}]
    assert ("get", "exports/people.csv") in client.calls


def test_query_cannot_escape_the_prefix(fake):
    # S3 keys are opaque strings, so ".." addresses a key that literally
    # contains two dots. The concatenation is the whole boundary.
    conn, client = fake({"secret.csv": CSV, "exports/people.csv": CSV}, prefix="exports/")
    with pytest.raises(ConnectorError, match="cannot read"):
        asyncio.run(conn.query({"key": "../secret.csv"}, limit=10))
    assert ("get", "exports/../secret.csv") in client.calls


def test_query_limit_is_applied(fake):
    conn, _ = fake({"people.csv": CSV})
    assert asyncio.run(conn.query({"key": "people.csv"}, limit=1)) == [{"name": "Alice", "age": "30"}]


def test_query_text_object(fake):
    conn, _ = fake({"notes.md": NOTES})
    assert "x@y.com" in asyncio.run(conn.query({"key": "notes.md"}, limit=10))[0]["content"]


def test_query_unknown_extension_is_read_as_text(fake):
    conn, _ = fake({"data.yaml": b"key: value\n"})
    assert asyncio.run(conn.query({"key": "data.yaml"}, limit=10)) == [{"content": "key: value\n"}]


def test_missing_object_is_an_error(fake):
    conn, _ = fake({"people.csv": CSV})
    with pytest.raises(ConnectorError, match="cannot read"):
        asyncio.run(conn.query({"key": "ghost.csv"}, limit=10))


def test_oversized_object_is_refused_not_truncated(fake, monkeypatch):
    # Returning the first N rows of a file that has more is the kind of answer a
    # governed surface must not give, so the cap is an error, not a silent cut.
    monkeypatch.setattr("eiye_db.connectors.s3._MAX_OBJECT_BYTES", 8)
    conn, _ = fake({"people.csv": CSV})
    with pytest.raises(ConnectorError, match="exceeds"):
        asyncio.run(conn.query({"key": "people.csv"}, limit=10))


def test_sniff_drops_a_partial_trailing_row(fake, monkeypatch):
    # The header must survive a ranged read that lands mid-row; a half-read
    # value would otherwise be sampled and could flip an inferred type.
    monkeypatch.setattr("eiye_db.connectors.s3._CSV_SNIFF_BYTES", len(b"name,age\nAlice,3"))
    conn, _ = fake({"people.csv": CSV})
    tables = {t["name"]: t for t in asyncio.run(conn.discover_schema())}
    assert tables["people.csv"]["fields"] == [
        {"name": "name", "type": "string"},
        {"name": "age", "type": "string"},  # no complete data row survived the cut
    ]


def test_only_list_and_get_are_ever_called(fake):
    # The read-only claim in one assertion: the connector has no code path to a
    # write API, and this fails if one is ever added.
    conn, client = fake({"people.csv": CSV})
    asyncio.run(conn.discover_schema())
    asyncio.run(conn.query({"key": "people.csv"}, limit=10))
    assert {c[0] for c in client.calls} == {"list", "get"}


# --- live (MinIO) --------------------------------------------------------------

# Read at import, like the other connector suites: conftest's autouse
# _clear_eiye_env deletes every EIYE_* variable before fixtures run.
LIVE_ENDPOINT = os.environ.get("EIYE_TEST_S3_ENDPOINT")
LIVE_KEY = os.environ.get("EIYE_TEST_S3_ACCESS_KEY_ID", "minioadmin")
LIVE_SECRET = os.environ.get("EIYE_TEST_S3_SECRET_ACCESS_KEY", "minioadmin")
BUCKET = "eiye-test"


@pytest.fixture
def live_config():
    if not LIVE_ENDPOINT:
        pytest.skip("EIYE_TEST_S3_ENDPOINT not set")
    return {
        "bucket": BUCKET,
        "prefix": "exports/",
        "endpoint_url": LIVE_ENDPOINT,
        "region": "us-east-1",
        "access_key_id": LIVE_KEY,
        "secret_access_key": LIVE_SECRET,
    }


@pytest.fixture
def live_bucket(live_config):
    """Create the bucket and seed it. Uses the same credentials as the
    connector, which is the operator's key — the connector never writes."""
    admin = boto3.client(
        "s3",
        endpoint_url=live_config["endpoint_url"],
        region_name="us-east-1",
        aws_access_key_id=live_config["access_key_id"],
        aws_secret_access_key=live_config["secret_access_key"],
        config=boto3.session.Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    try:
        admin.create_bucket(Bucket=BUCKET)
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            raise
    admin.put_object(Bucket=BUCKET, Key="exports/people.csv", Body=CSV)
    admin.put_object(Bucket=BUCKET, Key="exports/notes.md", Body=NOTES)
    admin.put_object(Bucket=BUCKET, Key="elsewhere/secret.csv", Body=b"secret\n1\n")
    yield admin
    admin.close()


def test_live_test_connection(live_bucket, live_config):
    asyncio.run(S3Connector(live_config).test_connection())


def test_live_test_connection_fails_on_a_missing_bucket(live_bucket, live_config):
    with pytest.raises(ConnectorError, match="connection failed"):
        asyncio.run(S3Connector({**live_config, "bucket": "eiye-no-such-bucket"}).test_connection())


def test_live_discover(live_bucket, live_config):
    tables = {t["name"]: t for t in asyncio.run(S3Connector(live_config).discover_schema())}
    # The prefix is the boundary: nothing outside it is listed.
    assert set(tables) == {"people.csv", "notes.md"}
    assert tables["people.csv"]["fields"] == [
        {"name": "name", "type": "string"},
        {"name": "age", "type": "integer"},
    ]


def test_live_query(live_bucket, live_config):
    rows = asyncio.run(S3Connector(live_config).query({"key": "people.csv"}, limit=1))
    assert rows == [{"name": "Alice", "age": "30"}]


def test_live_query_outside_the_prefix_finds_nothing(live_bucket, live_config):
    with pytest.raises(ConnectorError, match="cannot read"):
        asyncio.run(S3Connector(live_config).query({"key": "../elsewhere/secret.csv"}, limit=10))
