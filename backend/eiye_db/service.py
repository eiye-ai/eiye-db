"""Query/discovery orchestration shared by the REST API and the MCP server.

Every path through here enforces the governance chain:
connector (read-only) → PII redaction → audit trail.
"""

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from fastapi.encoders import jsonable_encoder

from eiye_db import audit, catalog, pii, registry, semantic
from eiye_db.connectors import ConnectorError, get_connector
from eiye_db.models import ConnectionStatus, DataSource, SourceQueryResponse

QUERY_TIMEOUT_SECONDS = 30


class NotFoundError(Exception):
    pass


def _get_or_raise(datasource_id: str) -> DataSource:
    ds = registry.get(datasource_id)
    if ds is None:
        raise NotFoundError(f"datasource not found: {datasource_id}")
    return ds


async def test_connection(datasource_id: str, key_id: str) -> DataSource:
    ds = _get_or_raise(datasource_id)
    connector = get_connector(ds.type, ds.config)
    try:
        await connector.test_connection()
    except ConnectorError:
        registry.set_status(ds.id, ConnectionStatus.ERROR)
        audit.record("test_connection", "datasource", ds.id, key_id, ds.id, success=False)
        raise
    finally:
        await connector.close()
    registry.set_status(ds.id, ConnectionStatus.CONNECTED, connected=True)
    audit.record("test_connection", "datasource", ds.id, key_id, ds.id)
    return registry.get(ds.id)


async def discover_schema(datasource_id: str, key_id: str) -> dict[str, Any]:
    ds = _get_or_raise(datasource_id)
    connector = get_connector(ds.type, ds.config)
    try:
        tables = await connector.discover_schema()
        fks = await connector.discover_relationships()
    except ConnectorError:
        audit.record("discover_schema", "datasource", ds.id, key_id, ds.id, success=False)
        raise
    finally:
        await connector.close()
    schema = {
        "datasource_id": ds.id,
        "tables": tables,
        "discovered_at": datetime.now(timezone.utc).isoformat(),
    }
    registry.set_schema(ds.id, schema)
    # Structural FKs are the source's own metadata: auto-approved ground truth.
    semantic.sync_structural(ds.id, fks)
    audit.record(
        "discover_schema", "datasource", ds.id, key_id, ds.id, details={"tables": len(tables), "foreign_keys": len(fks)}
    )
    return schema


def detect_relationships(key_id: str) -> list[dict[str, Any]]:
    """Candidate-join detection: heuristic (schema shape) + behavioral (audit mining).

    Candidates are never authoritative — they stay status="candidate" until a
    human approves them. Existing rows (and their approve/reject decisions)
    are preserved.
    """
    schemas = []
    for ds in registry.list_all():
        schema = registry.get_schema(ds.id)
        if schema and schema.get("tables"):
            schemas.append((ds.id, ds.name, schema["tables"]))
    pruned = semantic.prune_stale_candidates(schemas)
    heuristic = semantic.upsert(semantic.detect_candidates(schemas))
    # Behavioral candidates only fill the review queue's remaining budget,
    # strongest evidence first (mine_audit_joins pre-sorts by distinct callers).
    remaining = max(0, PROPOSAL_QUEUE_CAP - _outstanding_proposals())
    behavioral = semantic.upsert(semantic.mine_audit_joins(schemas)[:remaining]) if remaining else []
    audit.record(
        "detect_relationships",
        "semantic",
        "all",
        key_id,
        details={"heuristic": len(heuristic), "behavioral": len(behavioral), "pruned": pruned},
    )
    return heuristic + behavioral


async def run_metric(metric_id: str, params: dict[str, Any], limit: int, key_id: str) -> dict[str, Any]:
    """Execute an approved metric: template + validated params → governed query.

    The underlying run_query applies the full chain (read-only connector, PII
    redaction, audit); this adds a metric-level audit record so results are
    traceable to the definition that produced them (lineage).
    """
    metric = catalog.get(metric_id)
    if metric is None:
        raise NotFoundError(f"metric not found: {metric_id}")
    safe_params = pii.redact_structure(params)[0]
    try:
        request = catalog.build_request(metric, params)  # raises CatalogError if not approved / bad params
        result = await run_query(metric["datasource_id"], request, limit, key_id)
        # Lineage: these rows exist because of this governed definition.
        result.lineage["metric"] = {"id": metric["id"], "name": metric["name"], "params": safe_params}
    except (catalog.CatalogError, ConnectorError, TimeoutError) as e:
        # Failures are lineage too: a denied or broken execution must be traceable.
        audit.record(
            "query_metric",
            "metric",
            metric_id,
            key_id,
            metric["datasource_id"],
            details={"name": metric["name"], "params": safe_params, "error": type(e).__name__},
            success=False,
        )
        raise
    audit.record(
        "query_metric",
        "metric",
        metric_id,
        key_id,
        metric["datasource_id"],
        details={"name": metric["name"], "params": safe_params, "rows": result.row_count},
    )
    return {
        # Redacted params in the envelope too — one consistent redaction posture
        # per response (params can legally contain emails: '@' passes the allowlist).
        "metric": {"id": metric["id"], "name": metric["name"], "params": safe_params},
        "result": result.model_dump(mode="json"),
    }


# Outstanding unreviewed proposals are bounded so a runaway (or injected) agent
# cannot flood the human review queue. Behavioral candidates share the budget:
# they are equally caller-influenceable (crafted queries), unlike heuristic
# candidates, which are bounded by schema size.
PROPOSAL_QUEUE_CAP = 100


def _outstanding_proposals() -> int:
    pending_rels = [r for r in semantic.list_relationships(status="candidate") if r["source"] in ("proposed", "behavioral")]
    pending_metrics = catalog.list_metrics(status="candidate")
    return len(pending_rels) + len(pending_metrics)


def _proposal_queue_full() -> bool:
    return _outstanding_proposals() >= PROPOSAL_QUEUE_CAP


def propose_relationship(
    from_datasource_id: str,
    from_table: str,
    from_column: str,
    to_datasource_id: str,
    to_table: str,
    to_column: str,
    rationale: str,
    key_id: str,
) -> dict[str, Any]:
    """An agent drafts a relationship. It lands as a candidate — never ground truth.

    The proposal is audited under the proposer's identity; a human approves or
    rejects it via the review endpoint (draft → approve → enforce).
    """
    for ds_id in (from_datasource_id, to_datasource_id):
        if registry.get(ds_id) is None:
            raise NotFoundError(f"datasource not found: {ds_id}")
    if _proposal_queue_full():
        audit.record("propose_relationship", "semantic", "queue-full", key_id, success=False)
        raise catalog.CatalogError("proposal queue is full; a human must review pending candidates first")
    # Rationale is agent-authored free text re-served to reviewers: cap + redact.
    rationale = pii.redact_text(rationale[:500])[0]
    # Annotate (don't block) endpoints missing from the discovered schema, so a
    # reviewer can't mistake a fabricated column for a real one. Schemas can be
    # stale, so this is a flag rather than a rejection.
    for ds_id, table, column in (
        (from_datasource_id, from_table, from_column),
        (to_datasource_id, to_table, to_column),
    ):
        schema = registry.get_schema(ds_id)
        tables = {t["name"]: {f["name"] for f in t.get("fields") or []} for t in (schema or {}).get("tables", [])}
        if schema and (table not in tables or column not in tables[table]):
            rationale += f" [note: {table}.{column} not found in the discovered schema]"
    created = semantic.upsert(
        [
            {
                "from_datasource_id": from_datasource_id,
                "from_table": from_table,
                "from_column": from_column,
                "to_datasource_id": to_datasource_id,
                "to_table": to_table,
                "to_column": to_column,
                "kind": "candidate_join",
                "source": "proposed",
                "confidence": 0.5,
                "rationale": rationale,
            }
        ]
    )
    outcome = created[0] if created else {"already_known": True}
    audit.record(
        "propose_relationship",
        "semantic",
        created[0]["id"] if created else "existing",
        key_id,
        details={"from": f"{from_table}.{from_column}", "to": f"{to_table}.{to_column}", "new": bool(created)},
    )
    return outcome


def propose_metric(
    name: str,
    description: str,
    datasource_id: str,
    request_template: dict[str, Any],
    params: dict[str, Any],
    key_id: str,
) -> dict[str, Any]:
    """An agent drafts a metric definition. Lands as a candidate; cannot execute
    until a human approves it."""
    if registry.get(datasource_id) is None:
        raise NotFoundError(f"datasource not found: {datasource_id}")
    if _proposal_queue_full():
        audit.record("propose_metric", "metric", "queue-full", key_id, success=False)
        raise catalog.CatalogError("proposal queue is full; a human must review pending candidates first")
    try:
        metric = catalog.create(name, description, datasource_id, request_template, params, source="proposed")
    except catalog.CatalogError:
        audit.record("propose_metric", "metric", "invalid", key_id, datasource_id, details={"name": name}, success=False)
        raise
    audit.record("propose_metric", "metric", metric["id"], key_id, datasource_id, details={"name": name})
    return metric


def relationships_for_schema(datasource_id: str) -> list[dict[str, Any]]:
    """Relationships to attach to a schema response: approved first, then candidates.

    Rejected links are excluded — an agent should never see them. Candidates are
    included but explicitly labeled so a client can distinguish ground truth
    from unreviewed proposals.
    """
    rels = semantic.list_relationships(datasource_id=datasource_id)
    return [r for r in rels if r["status"] == "approved"] + [r for r in rels if r["status"] == "candidate"]


async def run_query(
    datasource_id: str,
    request: dict[str, Any],
    limit: int,
    key_id: str,
    include_pii: bool = False,
) -> SourceQueryResponse:
    ds = _get_or_raise(datasource_id)
    connector = get_connector(ds.type, ds.config)
    # Query text (SQL predicates, REST params) can itself contain PII — redact
    # before it is persisted to the audit trail.
    safe_request = pii.redact_structure(request)[0]
    started_at = datetime.now(timezone.utc)
    start = time.monotonic()
    try:
        async with asyncio.timeout(QUERY_TIMEOUT_SECONDS):
            rows = await connector.query(request, limit)
    except (ConnectorError, TimeoutError):
        audit.record("query", "datasource", ds.id, key_id, ds.id, details={"request": safe_request}, success=False)
        raise
    finally:
        await connector.close()

    rows = jsonable_encoder(rows)
    pii_counts: dict[str, int] = {}
    if not include_pii:
        # NER redaction is CPU-heavy; run it off the event loop so it can't stall
        # other requests. The regex-only path is cheap enough to stay inline.
        from eiye_db.config import settings

        if settings.pii_ner_enabled:
            rows, pii_counts = await asyncio.to_thread(pii.redact_structure, rows)
        else:
            rows, pii_counts = pii.redact_structure(rows)

    # Build the response before auditing success so a serialization failure
    # is recorded as a failure, not a phantom success.
    response = SourceQueryResponse(
        datasource_id=ds.id,
        rows=rows,
        row_count=len(rows),
        pii_filtered=not include_pii,
        pii_counts=pii_counts,
        execution_time_ms=(time.monotonic() - start) * 1000,
        lineage={
            "datasource": {"id": ds.id, "name": ds.name, "type": str(ds.type)},
            "request": safe_request,  # already PII-redacted
            "executed_at": started_at.isoformat(),
        },
    )
    audit.record(
        "query",
        "datasource",
        ds.id,
        key_id,
        ds.id,
        details={
            "request": safe_request,
            "rows": len(rows),
            "pii_redactions": sum(pii_counts.values()),
            "pii_counts": pii_counts,
            "include_pii": include_pii,
        },
    )
    return response
