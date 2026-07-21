"""MCP server exposing the semantic surface over stdio.

Run: python -m eiye_db.mcp_server

Same governance chain as the REST API (read-only connectors → PII redaction →
audit trail), via the shared service layer. The stdio principal is the local
operator who launched the process; it is audited as api_key_id="mcp-stdio".
PII redaction is always on for MCP callers — there is no include_pii here.
"""

from typing import Any

from mcp.server.fastmcp import FastMCP

from eiye_db import catalog, db, pii, registry, service
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


@mcp.tool()
def list_metrics() -> list[dict[str, Any]]:
    """List governed metrics (named, parameterized query templates). Only metrics
    with status "approved" can be executed; "candidate" ones await human review."""
    return [
        {k: m[k] for k in ("id", "name", "description", "datasource_id", "params", "status")}
        for m in catalog.list_metrics()
    ]


@mcp.tool()
async def query_metric(metric_id: str, params: dict[str, Any] | None = None, limit: int = 100) -> dict[str, Any]:
    """Execute an approved metric with concrete parameter values. Prefer this
    over ad-hoc query_datasource when a metric exists for the question — the
    definition is governed, so results are consistent across callers. PII in
    results is always redacted; every execution is audited.
    Returns {"metric": {id, name, params}, "result": <same shape as query_datasource>}."""
    return await service.run_metric(metric_id, params or {}, min(max(limit, 1), 1000), MCP_KEY_ID)


@mcp.tool()
def propose_relationship(
    from_datasource_id: str,
    from_table: str,
    from_column: str,
    to_datasource_id: str,
    to_table: str,
    to_column: str,
    rationale: str,
) -> dict[str, Any]:
    """Propose that two columns are joinable (e.g. you noticed matching values
    while querying). The proposal is recorded as a CANDIDATE for human review —
    it does not become ground truth until approved. Give a concrete rationale."""
    return service.propose_relationship(
        from_datasource_id, from_table, from_column, to_datasource_id, to_table, to_column, rationale, MCP_KEY_ID
    )


@mcp.tool()
def propose_metric(
    name: str,
    description: str,
    datasource_id: str,
    request_template: dict[str, Any],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Propose a reusable metric definition (a connector-specific request template
    with {placeholder} parameters, e.g. {"sql": "SELECT city, count(*) FROM
    customers WHERE plan = '{plan}' GROUP BY city"} with params {"plan":
    {"type": "string"}}). It is recorded as a CANDIDATE and cannot execute until
    a human approves it. An invalid definition raises a tool error (same channel
    as every other tool failure)."""
    return service.propose_metric(name, description, datasource_id, request_template, params or {}, MCP_KEY_ID)


def main() -> None:
    db.configure()
    if settings.pii_ner_enabled:
        pii._load_ner()  # fail loud at boot if the NER model is missing
    mcp.run()


if __name__ == "__main__":
    main()
