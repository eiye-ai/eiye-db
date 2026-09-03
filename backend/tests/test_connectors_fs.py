"""Filesystem connector tests.

Every connector built in this module is wrapped so its calls run under the
file-write guard: an `open` asking for write access on any exercised path raises
`WriteAttempted` and fails the test. That makes "this connector only ever reads"
a property the suite enforces rather than one the docstring asserts. See
`tests/readonly_guards.py` for what that does and does not prove.
"""

import asyncio

import pytest
from openpyxl import Workbook

from eiye_db.connectors.base import ConnectorError
from eiye_db.connectors.filesystem import FilesystemConnector, infer_type
from tests.readonly_guards import WriteAttempted, guard_connector_files, guard_file_writes


@pytest.fixture(autouse=True)
def modes_seen(monkeypatch):
    """Guard every FilesystemConnector this module constructs.

    Patched at the class rather than applied per call site: there are a dozen
    places building a connector here and one that forgets would be a silent
    hole. The guard is active only *inside* connector calls, which is what lets
    the fixtures below still write their sample files.
    """
    seen: list[str] = []
    original_init = FilesystemConnector.__init__

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        guard_connector_files(self, seen)

    monkeypatch.setattr(FilesystemConnector, "__init__", init)
    return seen


@pytest.fixture
def data_dir(tmp_path):
    (tmp_path / "people.csv").write_text(
        "name,age,score\nAlice,30,91.5\nBob,25,88.0\n"
    )
    (tmp_path / "notes.md").write_text("# Notes\nemail: x@y.com\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "empty.csv").write_text("")
    return tmp_path


def test_infer_type():
    assert infer_type(["1", "2"]) == "integer"
    assert infer_type(["1.5", "2"]) == "number"
    assert infer_type(["a", "1"]) == "string"
    assert infer_type([]) == "string"


def test_discover(data_dir):
    conn = FilesystemConnector({"root": str(data_dir)})
    tables = asyncio.run(conn.discover_schema())
    by_name = {t["name"]: t for t in tables}
    assert by_name["people.csv"]["fields"] == [
        {"name": "name", "type": "string"},
        {"name": "age", "type": "integer"},
        {"name": "score", "type": "number"},
    ]
    assert by_name["notes.md"]["fields"] == [{"name": "content", "type": "text"}]
    assert by_name["sub/empty.csv"]["fields"] == []


def test_query_csv(data_dir):
    conn = FilesystemConnector({"root": str(data_dir)})
    rows = asyncio.run(conn.query({"path": "people.csv"}, limit=1))
    assert rows == [{"name": "Alice", "age": "30", "score": "91.5"}]


def test_query_text(data_dir):
    conn = FilesystemConnector({"root": str(data_dir)})
    rows = asyncio.run(conn.query({"path": "notes.md"}, limit=10))
    assert "x@y.com" in rows[0]["content"]


def test_path_traversal_blocked(data_dir):
    conn = FilesystemConnector({"root": str(data_dir)})
    with pytest.raises(ConnectorError, match="escapes"):
        asyncio.run(conn.query({"path": "../../etc/passwd"}, limit=10))


def test_missing_root_rejected(tmp_path):
    conn = FilesystemConnector({"root": str(tmp_path / "nope")})
    with pytest.raises(ConnectorError):
        asyncio.run(conn.test_connection())


def test_missing_file_rejected(data_dir):
    conn = FilesystemConnector({"root": str(data_dir)})
    with pytest.raises(ConnectorError, match="no such file"):
        asyncio.run(conn.query({"path": "ghost.csv"}, limit=10))


def _make_pdf(path, text: str) -> None:
    """Write a minimal single-page PDF whose Helvetica text pypdf can extract.

    Dependency-free: object byte offsets are computed for a valid xref table.
    `text` must avoid '(', ')' and '\\' (PDF string metacharacters).
    """
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        None,  # content stream, filled below
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    stream = b"BT /F1 24 Tf 72 700 Td (" + text.encode("latin-1") + b") Tj ET"
    objs[3] = b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"

    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += str(i).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref_pos = len(out)
    n = len(objs) + 1
    out += b"xref\n0 " + str(n).encode() + b"\n0000000000 65535 f \n"
    for off in offsets:
        out += ("%010d 00000 n \n" % off).encode()
    out += b"trailer\n<< /Size " + str(n).encode() + b" /Root 1 0 R >>\nstartxref\n"
    out += str(xref_pos).encode() + b"\n%%EOF\n"
    path.write_bytes(out)


@pytest.fixture
def doc_dir(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.append(["name", "email", "phone"])
    ws.append(["Alice", "alice@example.com", "617-555-1234"])
    ws.append(["Bob", "bob@corp.io", "415-555-9876"])
    wb.save(str(tmp_path / "contacts.xlsx"))
    _make_pdf(tmp_path / "receipt.pdf", "Customer Dave dave@example.com phone 555-867-5309")
    return tmp_path


def test_discover_documents(doc_dir):
    conn = FilesystemConnector({"root": str(doc_dir)})
    tables = {t["name"]: t for t in asyncio.run(conn.discover_schema())}
    assert tables["contacts.xlsx"]["fields"] == [
        {"name": "name", "type": "string"},
        {"name": "email", "type": "string"},
        {"name": "phone", "type": "string"},
    ]
    assert tables["receipt.pdf"]["fields"] == [{"name": "content", "type": "text"}]


def test_query_xlsx(doc_dir):
    conn = FilesystemConnector({"root": str(doc_dir)})
    rows = asyncio.run(conn.query({"path": "contacts.xlsx"}, limit=1))
    assert rows == [{"name": "Alice", "email": "alice@example.com", "phone": "617-555-1234"}]


def test_query_pdf(doc_dir):
    conn = FilesystemConnector({"root": str(doc_dir)})
    rows = asyncio.run(conn.query({"path": "receipt.pdf"}, limit=10))
    assert "dave@example.com" in rows[0]["content"]
    assert "555-867-5309" in rows[0]["content"]


def test_xlsx_unreadable_skipped_in_discover(tmp_path):
    # A non-xlsx file with an .xlsx suffix must not crash discovery.
    (tmp_path / "broken.xlsx").write_text("not really a spreadsheet")
    conn = FilesystemConnector({"root": str(tmp_path)})
    tables = {t["name"]: t for t in asyncio.run(conn.discover_schema())}
    assert tables["broken.xlsx"]["fields"] == []


def test_query_broken_xlsx_raises_connector_error(tmp_path):
    (tmp_path / "broken.xlsx").write_text("not really a spreadsheet")
    conn = FilesystemConnector({"root": str(tmp_path)})
    with pytest.raises(ConnectorError, match="xlsx"):
        asyncio.run(conn.query({"path": "broken.xlsx"}, limit=5))


def test_query_broken_pdf_raises_connector_error(tmp_path):
    (tmp_path / "broken.pdf").write_text("not really a pdf")
    conn = FilesystemConnector({"root": str(tmp_path)})
    with pytest.raises(ConnectorError, match="PDF"):
        asyncio.run(conn.query({"path": "broken.pdf"}, limit=5))


# --- the guard itself ---------------------------------------------------------


def test_guard_is_not_inert(data_dir, modes_seen):
    """A guard that never fires cannot be told apart from a guard that is
    broken. Prove the connector's ordinary work actually opens files through
    it."""
    conn = FilesystemConnector({"root": str(data_dir)})
    asyncio.run(conn.discover_schema())
    asyncio.run(conn.query({"path": "people.csv"}, limit=10))
    assert modes_seen, "the guard saw no file opens at all"
    assert all("w" not in m and "a" not in m and "+" not in m for m in modes_seen)


@pytest.mark.parametrize("mode", ["w", "a", "x", "r+", "wb"])
def test_guard_refuses_a_write(tmp_path, mode):
    """And prove it would catch one. This is the failure a future
    `open(..., "w")` in the connector or an extractor would produce."""
    with pytest.raises(WriteAttempted), guard_file_writes():
        open(tmp_path / "scratch", mode)


def test_guard_survives_the_extractors_swallowing_exceptions(tmp_path):
    """documents.py catches `Exception` in six places so one malformed file
    cannot end a discovery pass. WriteAttempted is a BaseException so those
    handlers cannot quietly absorb it — pin that, because the guard is worthless
    if they can."""
    with pytest.raises(WriteAttempted), guard_file_writes():
        try:
            open(tmp_path / "scratch", "w")
        except Exception:  # noqa: BLE001 - deliberately as broad as documents.py
            pytest.fail("an `except Exception:` swallowed the guard")
