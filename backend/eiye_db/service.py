"""Query/discovery orchestration shared by the REST API and the MCP server.

Every path through here enforces the governance chain:
connector (read-only) → PII redaction → audit trail.
"""

import asyncio
import re
import time
from datetime import datetime, timezone
from typing import Any

from fastapi.encoders import jsonable_encoder

from eiye_db import audit, catalog, nl, pii, policy, registry, resolution, semantic
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


def _check_policy(action: str, ds_id: str, key_id: str, is_admin: bool) -> set[str]:
    """Policy gate for one access; audited denial, returns columns to mask.

    The audit record carries the specific reason (e.detail, incl. the policy
    name); the exception the caller sees stays generic.
    """
    try:
        return policy.check(key_id, is_admin, action, ds_id)
    except policy.PolicyDenied as e:
        audit.record(
            "policy_deny", "datasource", ds_id, key_id, ds_id,
            details={"action": action, "reason": e.detail}, success=False,
        )
        raise


def check_schema_access(datasource_id: str, key_id: str, is_admin: bool = False) -> None:
    """Gate for reading an already-discovered schema (REST surface + MCP)."""
    _check_policy("discover", datasource_id, key_id, is_admin)
    # Schema reads are access too: audited like queries, not just their denials.
    audit.record("read_schema", "datasource", datasource_id, key_id, datasource_id)


def visible_datasource_ids(key_id: str, is_admin: bool = False) -> set[str]:
    """Datasources the subject may 'discover' — the filter every metadata
    listing (sources, relationships, metrics, export) applies so denied
    sources don't leak through side channels."""
    return {ds.id for ds in registry.list_all() if policy.permits(key_id, is_admin, "discover", ds.id)}


async def discover_schema(datasource_id: str, key_id: str, is_admin: bool = False) -> dict[str, Any]:
    ds = _get_or_raise(datasource_id)
    _check_policy("discover", ds.id, key_id, is_admin)
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


async def run_metric(
    metric_id: str, params: dict[str, Any], limit: int, key_id: str, is_admin: bool = False
) -> dict[str, Any]:
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
        result = await run_query(metric["datasource_id"], request, limit, key_id, is_admin=is_admin)
        # Lineage: these rows exist because of this governed definition.
        result.lineage["metric"] = {"id": metric["id"], "name": metric["name"], "params": safe_params}
    except (catalog.CatalogError, ConnectorError, TimeoutError, policy.PolicyDenied) as e:
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
        # A discover-denied source answers exactly like a missing one, so the
        # proposal tool can't be used as an existence/schema oracle.
        if registry.get(ds_id) is None or not policy.permits(key_id, False, "discover", ds_id):
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
    if registry.get(datasource_id) is None or not policy.permits(key_id, False, "discover", datasource_id):
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


async def resolve_entities(
    left: dict[str, Any], right: dict[str, Any], limit: int, key_id: str, is_admin: bool = False
) -> dict[str, Any]:
    """Cross-source entity matching over two governed query results.

    Each side is {"datasource_id", "request", "column"}. Both sides go through
    run_query — the full chain (read-only, PII redaction, audit) — so matching
    only ever sees values the caller could see. Matches are per-call analysis,
    never persisted, and carry tiered confidence, not authority.
    """
    # Column names are caller-supplied text: redact before they reach the
    # audit trail, same posture as query requests and proposal rationales.
    safe_details = pii.redact_structure(
        {
            "left": {"datasource_id": left["datasource_id"], "column": left["column"]},
            "right": {"datasource_id": right["datasource_id"], "column": right["column"]},
        }
    )[0]
    resource_id = f"{left['datasource_id']}:{right['datasource_id']}"
    try:
        results: dict[str, SourceQueryResponse] = {}
        values: dict[str, list] = {}
        for label, side in (("left", left), ("right", right)):
            res = await run_query(side["datasource_id"], side["request"], limit, key_id, is_admin=is_admin)
            col = side["column"]
            if res.rows and all(col not in row for row in res.rows):
                raise ValueError(f"column {col!r} not in the {label} result")
            results[label] = res
            values[label] = [row.get(col) for row in res.rows]
        # Matching is pure CPU over up to 1000 values per side; keep it off
        # the event loop so a large resolve can't stall other requests.
        matched = await asyncio.to_thread(resolution.match_values, values["left"], values["right"])
    except (ConnectorError, TimeoutError, ValueError, NotFoundError, policy.PolicyDenied) as e:
        # A failed resolve is still a cross-source correlation attempt; the
        # underlying query audits alone would make it look like plain queries.
        audit.record(
            "resolve_entities", "semantic", resource_id, key_id,
            details={**safe_details, "error": type(e).__name__}, success=False,
        )
        raise
    matches = matched["matches"]
    by_confidence: dict[str, int] = {}
    for m in matches:
        by_confidence[m["confidence"]] = by_confidence.get(m["confidence"], 0) + 1
    audit.record(
        "resolve_entities", "semantic", resource_id, key_id,
        details={**safe_details, "matches": by_confidence},
    )
    return {
        "matches": matches,
        "stats": {
            "left_rows": results["left"].row_count,
            "right_rows": results["right"].row_count,
            "left_distinct": matched["left_distinct"],
            "right_distinct": matched["right_distinct"],
            "matches": len(matches),
            "by_confidence": by_confidence,
        },
        "lineage": {"left": results["left"].lineage, "right": results["right"].lineage},
    }


def _metric_summary(m: dict[str, Any]) -> dict[str, Any]:
    return {k: m[k] for k in ("id", "name", "description", "params")}


async def ask(question: str, limit: int, key_id: str, is_admin: bool = False) -> dict[str, Any]:
    """NL → governed query. Deterministic serving path: match the question
    against approved (and policy-visible) metrics, extract parameters with
    fixed patterns, execute through run_metric. When the deterministic path
    falls short and EIYE_NL_LLM_ENABLED is on, an LLM drafts the metric
    choice/parameters — but the draft still executes through the same typed
    validation, policy checks, and audit as any caller-supplied request.

    Never guesses: with no confident match (and no LLM), the answer is an
    explicit non-answer listing the closest governed metrics.
    """
    from eiye_db.config import settings

    question = str(question)[:500]
    safe_question = pii.redact_text(question)[0]
    visible = visible_datasource_ids(key_id, is_admin)
    approved = [m for m in catalog.list_metrics(status="approved") if m["datasource_id"] in visible]
    ranked = nl.rank(question, approved)
    candidates = [_metric_summary(r["metric"]) for r in ranked[:5]]

    chosen: dict[str, Any] | None = None
    params: dict[str, Any] = {}
    matcher = "deterministic"
    no_answer_reason: str | None = None

    if nl.confident(ranked):
        top = ranked[0]["metric"]
        extracted = nl.extract_params(question, top["params"])
        missing = nl.missing_params(top, extracted)
        if not missing:
            chosen, params = top, extracted
        else:
            # Fallback reason if the LLM (when enabled) can't rescue this.
            no_answer_reason = f"metric '{top['name']}' matches but needs parameters: {missing}"
            candidates = [_metric_summary(top)]

    if chosen is None and ranked and settings.nl_llm_enabled:
        try:
            draft = await nl.llm_bind(question, [r["metric"] for r in ranked[:5]])
            outcome = "draft" if draft else "no-draft"
        except Exception as e:  # LLM assist must fail closed to a non-answer, never a 500
            draft, outcome = None, f"error:{type(e).__name__}"
        # Calling llm_bind at all sends the question to the Anthropic API:
        # the egress is audited on EVERY outcome, not just success/error — an
        # operator must be able to reconstruct exactly which questions left.
        audit.record(
            "ask_llm", "semantic", "egress", key_id,
            details={"question": safe_question, "outcome": outcome, "shortlist": len(ranked[:5])},
            success=not outcome.startswith("error"),
        )
        if draft:
            chosen = next(r["metric"] for r in ranked if r["metric"]["id"] == draft["metric_id"])
            params = draft["params"]
            matcher = "llm-assisted"

    if chosen is None:
        if no_answer_reason is None:
            if not ranked:
                no_answer_reason = "no approved metric matches this question"
            elif len(ranked) == 1:
                no_answer_reason = f"metric '{ranked[0]['metric']['name']}' matches only weakly — not executing"
            else:
                no_answer_reason = "no clear best match among the candidate metrics"
        audit.record(
            "ask", "semantic", "no-answer", key_id,
            details={"question": safe_question, "reason": no_answer_reason, "candidates": len(candidates)},
        )
        return {"answered": False, "reason": no_answer_reason, "candidates": candidates}

    try:
        # Full governance chain: approved-only, typed param validation +
        # injection allowlist, policy checks, redaction, audit — identical
        # whether params came from patterns or the LLM.
        executed = await run_metric(chosen["id"], params, limit, key_id, is_admin)
    except (catalog.CatalogError, NotFoundError) as e:
        # NotFoundError covers the delete race between rank and execution.
        # Either way this ask completes as a structured non-answer — audited
        # as an ask (with matcher disclosure), not just as a metric failure.
        audit.record(
            "ask", "semantic", chosen["id"], key_id, chosen["datasource_id"],
            details={"question": safe_question, "matcher": matcher, "error": type(e).__name__}, success=False,
        )
        # Validation messages can echo caller/LLM-influenced text: cap + redact
        # before re-serving, same posture as every other free-text response.
        reason = pii.redact_text(str(e)[:300])[0] or "metric no longer exists"
        return {"answered": False, "reason": reason, "candidates": [_metric_summary(chosen)]}
    except (ConnectorError, TimeoutError, policy.PolicyDenied) as e:
        # These keep their transport error contract (502/504/403) but the ask
        # attempt itself is still traced.
        audit.record(
            "ask", "semantic", chosen["id"], key_id, chosen["datasource_id"],
            details={"question": safe_question, "matcher": matcher, "error": type(e).__name__}, success=False,
        )
        raise
    executed["result"]["lineage"]["nl"] = {"question": safe_question, "matcher": matcher, "metric": chosen["name"]}
    audit.record(
        "ask", "semantic", chosen["id"], key_id, chosen["datasource_id"],
        details={"question": safe_question, "matcher": matcher, "metric": chosen["name"]},
    )
    return {"answered": True, "matcher": matcher, **executed}


def relationships_for_schema(datasource_id: str) -> list[dict[str, Any]]:
    """Relationships to attach to a schema response: approved first, then candidates.

    Rejected links are excluded — an agent should never see them. Candidates are
    included but explicitly labeled so a client can distinguish ground truth
    from unreviewed proposals.
    """
    rels = semantic.list_relationships(datasource_id=datasource_id)
    return [r for r in rels if r["status"] == "approved"] + [r for r in rels if r["status"] == "candidate"]


def _strip_masked(obj: Any, masked_lower: set[str]) -> Any:
    """Drop masked keys recursively (case-insensitive): nested/composite
    results (row_to_json, REST objects) must not smuggle a masked column."""
    if isinstance(obj, dict):
        return {k: _strip_masked(v, masked_lower) for k, v in obj.items() if str(k).lower() not in masked_lower}
    if isinstance(obj, list):
        return [_strip_masked(v, masked_lower) for v in obj]
    return obj


def _sql_references_masked(sql: str, masked_lower: set[str]) -> str | None:
    """First masked column the SQL text names, else None.

    A column cannot be projected without writing its name (aliases rename the
    output but the source identifier still appears), so a word-boundary scan
    closes the `SELECT ssn AS x` bypass. Fail-closed by design: a match in a
    string literal or comment also denies.
    """
    for col in sorted(masked_lower):
        if re.search(rf"\b{re.escape(col)}\b", sql, re.IGNORECASE):
            return col
    return None


async def run_query(
    datasource_id: str,
    request: dict[str, Any],
    limit: int,
    key_id: str,
    include_pii: bool = False,
    is_admin: bool = False,
) -> SourceQueryResponse:
    ds = _get_or_raise(datasource_id)
    masked = _check_policy("read", ds.id, key_id, is_admin)
    masked_lower = {m.lower() for m in masked}
    if masked_lower and isinstance(request.get("sql"), str):
        col = _sql_references_masked(request["sql"], masked_lower)
        if col is not None:
            audit.record(
                "policy_deny", "datasource", ds.id, key_id, ds.id,
                details={"action": "read", "reason": f"query text references masked column '{col}'"},
                success=False,
            )
            raise policy.PolicyDenied(f"query text references masked column '{col}'")
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
    if masked_lower:
        # Policy-masked columns are dropped before anything else sees them —
        # they never reach redaction, the response, or the caller.
        rows = _strip_masked(rows, masked_lower)
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
            # Disclose masking so a missing column reads as policy, not absence.
            **({"policy": {"masked_columns": sorted(masked)}} if masked else {}),
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
