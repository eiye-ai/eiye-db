"""Connector interface. All connectors are read-only by design."""

from abc import ABC, abstractmethod
from typing import Any


class ConnectorError(Exception):
    """Raised for connection, discovery, or query failures."""


class Connector(ABC):
    def __init__(self, config: dict[str, Any]):
        self.config = config

    @abstractmethod
    async def test_connection(self) -> None:
        """Raise ConnectorError if the source is unreachable/misconfigured."""

    @abstractmethod
    async def discover_schema(self) -> list[dict[str, Any]]:
        """Return tables: [{"name": str, "fields": [{"name", "type", ...}]}]."""

    @abstractmethod
    async def query(self, request: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        """Execute a read-only, connector-specific request; return rows."""

    async def discover_relationships(self) -> list[dict[str, Any]]:
        """Structural links the source itself declares (e.g. SQL foreign keys).

        Shape: [{"from_table", "from_column", "to_table", "to_column"}].
        Sources without declared relationships return [].
        """
        return []

    async def close(self) -> None:
        pass
