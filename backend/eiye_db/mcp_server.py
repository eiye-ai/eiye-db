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
    """List the registered datasources this agent may see: id, name, type,
    status, description, and whether a schema has been discovered."""
    visible = service.visible_datasource_ids(MCP_KEY_ID)
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
        if ds.id in visible
    ]


@mcp.tool()
async def get_schema(datasource_id: str) -> dict[str, Any]:
    """Get the schema of a datasource: its tables/files, their fields, and known
    relationships (joins). Relationships with status "approved" are governed
    ground truth (e.g. real foreign keys); status "candidate" means a detected
    but unreviewed guess — treat candidates as hints, not facts.
    Runs live discovery if no schema has been cached yet."""
    service.check_schema_access(datasource_id, MCP_KEY_ID)
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
    """List governed metrics (named, parameterized query templates) over the
    datasources this agent may see. Only metrics with status "approved" can be
    executed; "candidate" ones await human review."""
    visible = service.visible_datasource_ids(MCP_KEY_ID)
    return [
        {k: m[k] for k in ("id", "name", "description", "datasource_id", "params", "status")}
        for m in catalog.list_metrics()
        if m["datasource_id"] in visible
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


@mcp.tool()
async def ask(question: str, limit: int = 100) -> dict[str, Any]:
    """Answer a natural-language question through the GOVERNED metric catalog.
    Prefer this over ad-hoc query_datasource when a metric may exist: answers
    come only from approved metric definitions, so they are consistent across
    callers and never improvised. Returns {"answered": true, result...} or
    {"answered": false, "reason", "candidates": [closest metrics]} — on a
    non-answer, call query_metric directly with one of the candidates, or
    propose_metric to draft a new definition for human approval.
    Questions are limited to 500 characters (rejected, not truncated — a
    silent cut could drop or mangle a parameter binding)."""
    question = str(question)
    if len(question) > 500:
        raise ValueError("question too long (max 500 characters)")
    return await service.ask(question, min(max(limit, 1), 1000), MCP_KEY_ID)


@mcp.tool()
async def resolve_entities(
    left_datasource_id: str,
    left_request: dict[str, Any],
    left_column: str,
    right_datasource_id: str,
    right_request: dict[str, Any],
    right_column: str,
    limit: int = 100,
) -> dict[str, Any]:
    """Match entity names across two query results — e.g. does "vendor_name" in
    one source refer to the same organizations as "employer" in another?
    Each request is connector-specific (same shapes as query_datasource); the
    named column's values are matched after normalization (suffixes like LLC/
    INC stripped). Matches are tiered: "high" = identical after normalization,
    "medium" = same words reordered, "low" = strong token overlap. These are
    heuristic ANALYSIS, not governed truth — if a match reveals a real join,
    propose_relationship it for human review."""
    return await service.resolve_entities(
        {"datasource_id": left_datasource_id, "request": left_request, "column": left_column},
        {"datasource_id": right_datasource_id, "request": right_request, "column": right_column},
        min(max(limit, 1), 1000),
        MCP_KEY_ID,
    )


def main() -> None:
    db.configure()
    if settings.pii_ner_enabled:
        pii._load_ner()  # fail loud at boot if the NER model is missing
    if settings.nl_llm_enabled:
        from eiye_db import nl

        nl.ensure_llm_ready()  # fail loud at boot, not on the first ask
    mcp.run()


if __name__ == "__main__":
    main()
