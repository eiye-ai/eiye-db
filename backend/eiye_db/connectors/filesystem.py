"""Filesystem connector: CSV, text, PDF, and XLSX files under a configured root.

Parsing lives in `documents`, shared with the S3 connector — the two differ in
where the bytes come from, not in how they are read. What stays here is what is
specific to a directory tree: the root, the traversal guard, and the walk.
"""

import asyncio
from pathlib import Path
from typing import Any

from eiye_db.connectors import documents
from eiye_db.connectors.base import Connector, ConnectorError
from eiye_db.connectors.documents import infer_type  # noqa: F401  (re-exported; imported by tests)

_MAX_FILES = 1000
_MAX_XLSX_INSPECT_BYTES = 5_000_000  # skip xlsx field inference above this during discovery


class FilesystemConnector(Connector):
    def _root(self) -> Path:
        root = self.config.get("root")
        if not root:
            raise ConnectorError("filesystem config requires 'root'")
        return Path(root).resolve()

    def _resolve(self, rel_path: str) -> Path:
        root = self._root()
        target = (root / rel_path).resolve()
        if root != target and root not in target.parents:
            raise ConnectorError(f"path escapes datasource root: {rel_path}")
        return target

    async def test_connection(self) -> None:
        root = self._root()
        if not root.is_dir():
            raise ConnectorError(f"root is not a directory: {root}")

    async def discover_schema(self) -> list[dict[str, Any]]:
        root = self._root()
        if not root.is_dir():
            raise ConnectorError(f"root is not a directory: {root}")
        tables = []
        count = 0
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            count += 1
            if count > _MAX_FILES:
                break
            rel = str(path.relative_to(root))
            kind = documents.kind_for(path.name)
            if kind == "csv":
                tables.append({"name": rel, "fields": self._csv_fields(path)})
            elif kind == "xlsx":
                tables.append({"name": rel, "fields": self._xlsx_fields(path)})
            elif kind in ("pdf", "text"):
                tables.append({"name": rel, "fields": [{"name": "content", "type": "text"}]})
        return tables

    def _csv_fields(self, path: Path) -> list[dict[str, Any]]:
        try:
            with path.open(newline="", errors="replace") as f:
                return documents.csv_fields(f)
        except OSError as e:
            raise ConnectorError(f"cannot read {path.name}: {e}") from e

    def _xlsx_fields(self, path: Path) -> list[dict[str, Any]]:
        # Skip very large workbooks so discovery never has to parse a huge file.
        try:
            if path.stat().st_size > _MAX_XLSX_INSPECT_BYTES:
                return []
        except OSError:
            return []
        return documents.xlsx_fields(str(path))

    async def query(self, request: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        rel_path = request.get("path")
        if not rel_path:
            raise ConnectorError("filesystem query requires 'path'")
        target = self._resolve(rel_path)
        if not target.is_file():
            raise ConnectorError(f"no such file: {rel_path}")
        kind = documents.kind_for(target.name)
        try:
            if kind == "csv":
                with target.open(newline="", errors="replace") as f:
                    return documents.csv_rows(f, limit)
            # PDF/XLSX parsing is blocking CPU work; run it off the event loop so a slow or
            # huge file cannot stall other requests (and the query timeout can still fire).
            if kind == "xlsx":
                return await asyncio.to_thread(documents.xlsx_rows, str(target), limit, target.name)
            if kind == "pdf":
                return await asyncio.to_thread(documents.pdf_rows, str(target), target.name)
            return documents.text_rows(target.read_text(errors="replace"))
        except OSError as e:
            raise ConnectorError(f"cannot read {rel_path}: {e}") from e
