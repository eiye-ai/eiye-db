"""MCP server exposing the semantic surface over stdio.

Run: python -m eiye_db.mcp_server

Same governance chain as the REST API (read-only connectors → PII redaction →
audit trail), via the shared service layer. The stdio principal is the local
operator who launched the process; it is audited as api_key_id="mcp-stdio".
PII redaction is always on for MCP callers — there is no include_pii here.
"""

from typing import Any

from mcp.server.fastmcp import FastMCP

from eiye_db import db, pii, registry, service
from eiye_db.config import settings

MCP_KEY_ID = "mcp-stdio"

mcp = FastMCP("eiye_db")


@mcp.tool()
def list_datasources() -> list[dict[str, Any]]:
    """List all registered datasources: id, name, type, status, description,
    and whether a schema has been discovered."""
    return [
        {
            "id": ds.id,
            "name": ds.name,
            "type": ds.type,
            "status": ds.status,
            "description": ds.description,
            "schema_discovered": registry.get_schema(ds.id) is not None,
        }
        for ds in registry.list_all()
    ]


@mcp.tool()
async def get_schema(datasource_id: str) -> dict[str, Any]:
    """Get the schema of a datasource: its tables/files, their fields, and known
    relationships (joins). Relationships with status "approved" are governed
    ground truth (e.g. real foreign keys); status "candidate" means a detected
    but unreviewed guess — treat candidates as hints, not facts.
    Runs live discovery if no schema has been cached yet."""
    schema = registry.get_schema(datasource_id)
    if schema is None:
        schema = await service.discover_schema(datasource_id, MCP_KEY_ID)
    return {**schema, "relationships": service.relationships_for_schema(datasource_id)}


@mcp.tool()
async def query_datasource(
    datasource_id: str, request: dict[str, Any], limit: int = 100
) -> dict[str, Any]:
    """Run a read-only query against a datasource. PII in results is always
    redacted. `request` is connector-specific:
    postgresql: {"sql": "SELECT ..."} (runs in a read-only transaction) ·
    filesystem: {"path": "relative/file.csv"} ·
    rest_api: {"path": "/endpoint", "params": {...}} (GET only)."""
    result = await service.run_query(
        datasource_id, request, min(max(limit, 1), 1000), MCP_KEY_ID
    )
    return result.model_dump(mode="json")


def main() -> None:
    db.configure()
    if settings.pii_ner_enabled:
        pii._load_ner()  # fail loud at boot if the NER model is missing
    mcp.run()


if __name__ == "__main__":
    main()
