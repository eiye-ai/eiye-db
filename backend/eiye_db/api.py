"""REST API routes."""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse

from eiye_db import __version__, audit, catalog, license, metrics, policy, registry, semantic, service
from eiye_db.config import settings
from eiye_db.connectors import ConnectorError
from eiye_db.models import (
    AskRequest,
    DataSource,
    DataSourceCreate,
    DataSourceUpdate,
    MetricCreate,
    MetricQuery,
    PolicyCreate,
    RelationshipUpdate,
    ResolveRequest,
    SourceQueryRequest,
    SourceQueryResponse,
)
from eiye_db.security import Identity, require_api_key

router = APIRouter(prefix="/api/v1")


@router.get("/status")
def status(identity: Identity = Depends(require_api_key)) -> dict:
    """Build info for an authenticated caller. The unauthenticated liveness
    probe is /health, on the app — this reports the version and debug flag, so
    it belongs inside the authenticated prefix it is named for."""
    return {
        "app": settings.app_name,
        "version": __version__,
        "debug": settings.debug,
        # Entitlement state is disclosed to any authenticated caller, not just
        # admins: an agent hitting a quota wall deserves to see why.
        "license": license.current().summary(),
        "usage": {"datasources": len(registry.list_all()), "queries_this_month": service.queries_this_month()},
    }


# Registrations are operator state, not agent-facing surface: `config` carries
# the connection secret (a Postgres DSN with its password, REST auth headers),
# and update/delete retarget or cascade the datasource id that policies, metrics
# and relationships are keyed on. The whole group is admin-only; agents read
# /surface/sources, which is policy-filtered and omits config entirely.
@router.post("/datasources", response_model=DataSource, status_code=201)
def create_datasource(req: DataSourceCreate, identity: Identity = Depends(require_api_key)):
    if not identity.is_admin:
        raise HTTPException(403, "registering a datasource requires the admin API key")
    # Admin bypasses ABAC but not the licence: an admin governs their data, they
    # do not license the software. LicenseLimitExceeded → 402 app-wide.
    service.check_datasource_quota()
    try:
        ds = registry.create(req)
    except ValueError as e:
        raise HTTPException(409, str(e))
    audit.record("create", "datasource", ds.id, identity.key_id, ds.id)
    return ds


@router.get("/datasources", response_model=list[DataSource])
def list_datasources(identity: Identity = Depends(require_api_key)):
    if not identity.is_admin:
        raise HTTPException(403, "listing raw registrations requires the admin API key")
    return registry.list_all()


@router.get("/datasources/{datasource_id}", response_model=DataSource)
def get_datasource(datasource_id: str, identity: Identity = Depends(require_api_key)):
    if not identity.is_admin:
        raise HTTPException(403, "reading a raw registration requires the admin API key")
    ds = registry.get(datasource_id)
    if ds is None:
        raise HTTPException(404, "datasource not found")
    return ds


@router.put("/datasources/{datasource_id}", response_model=DataSource)
def update_datasource(datasource_id: str, req: DataSourceUpdate, identity: Identity = Depends(require_api_key)):
    if not identity.is_admin:
        raise HTTPException(403, "updating a datasource requires the admin API key")
    ds = registry.update(datasource_id, req)
    if ds is None:
        raise HTTPException(404, "datasource not found")
    audit.record("update", "datasource", datasource_id, identity.key_id, datasource_id)
    return ds


@router.delete("/datasources/{datasource_id}", status_code=204)
def delete_datasource(datasource_id: str, identity: Identity = Depends(require_api_key)):
    if not identity.is_admin:
        raise HTTPException(403, "deleting a datasource requires the admin API key")
    if not registry.delete(datasource_id):
        raise HTTPException(404, "datasource not found")
    audit.record("delete", "datasource", datasource_id, identity.key_id, datasource_id)


@router.post("/datasources/{datasource_id}/test", response_model=DataSource)
async def test_datasource(datasource_id: str, identity: Identity = Depends(require_api_key)):
    if not identity.is_admin:
        raise HTTPException(403, "testing a connection requires the admin API key")
    try:
        return await service.test_connection(datasource_id, identity.key_id)
    except service.NotFoundError:
        raise HTTPException(404, "datasource not found")
    except ConnectorError as e:
        raise HTTPException(502, str(e))


@router.post("/datasources/{datasource_id}/discover")
async def discover_datasource(datasource_id: str, identity: Identity = Depends(require_api_key)):
    try:
        return await service.discover_schema(datasource_id, identity.key_id, is_admin=identity.is_admin)
    except service.NotFoundError:
        raise HTTPException(404, "datasource not found")
    except policy.PolicyDenied as e:
        raise HTTPException(403, str(e))
    except ConnectorError as e:
        raise HTTPException(502, str(e))


@router.get("/surface/sources")
def surface_sources(identity: Identity = Depends(require_api_key)):
    visible = service.visible_datasource_ids(identity.key_id, identity.is_admin)
    sources = []
    for ds in registry.list_all():
        if ds.id not in visible:
            continue
        schema = registry.get_schema(ds.id)
        sources.append(
            {
                "id": ds.id,
                "name": ds.name,
                "type": ds.type,
                "status": ds.status,
                "description": ds.description,
                "tables": len(schema["tables"]) if schema else None,
            }
        )
    return sources


@router.get("/surface/schema/{datasource_id}")
def surface_schema(datasource_id: str, identity: Identity = Depends(require_api_key)):
    if registry.get(datasource_id) is None:
        raise HTTPException(404, "datasource not found")
    try:
        service.check_schema_access(datasource_id, identity.key_id, identity.is_admin)
    except policy.PolicyDenied as e:
        raise HTTPException(403, str(e))
    schema = registry.get_schema(datasource_id)
    if schema is None:
        raise HTTPException(404, "no schema discovered yet; POST /datasources/{id}/discover first")
    return {
        **schema,
        "relationships": service.relationships_for_schema(datasource_id, identity.key_id, identity.is_admin),
    }


@router.post("/semantic/detect")
def semantic_detect(identity: Identity = Depends(require_api_key)):
    """Run heuristic candidate-join detection across all discovered schemas."""
    # Detection is a curation operation: it reads every schema and mutates the
    # global candidate set, so it is admin-gated like review.
    if not identity.is_admin:
        raise HTTPException(403, "relationship detection requires the admin API key")
    return service.detect_relationships(identity.key_id)


@router.get("/semantic/relationships")
def semantic_relationships(
    status: str | None = None,
    datasource_id: str | None = None,
    identity: Identity = Depends(require_api_key),
):
    rels = semantic.list_relationships(status=status, datasource_id=datasource_id)
    if identity.is_admin:
        return rels
    # Non-admin view matches the gated schema surface: no rejected rows, and
    # nothing touching a source the subject cannot 'discover'.
    visible = service.visible_datasource_ids(identity.key_id)
    return [
        r for r in rels
        if r["status"] != "rejected"
        and r["from_datasource_id"] in visible
        and r["to_datasource_id"] in visible
    ]


@router.put("/semantic/relationships/{relationship_id}")
def semantic_review(
    relationship_id: str,
    req: RelationshipUpdate,
    identity: Identity = Depends(require_api_key),
):
    # The human-approval gate is what makes candidates trustworthy, so it is a
    # technical boundary, not a procedural one: admin key required (agents may
    # legitimately hold the primary key).
    if not identity.is_admin:
        raise HTTPException(403, "relationship review requires the admin API key")
    rel, previous = semantic.set_status(relationship_id, req.status)
    if rel is None:
        if previous == "structural":
            raise HTTPException(409, "structural relationships come from the source database and cannot be reviewed")
        raise HTTPException(404, "relationship not found")
    audit.record(
        "review_relationship",
        "semantic",
        relationship_id,
        identity.key_id,
        details={
            "old_status": previous,
            "new_status": req.status,
            "from": f"{rel['from_datasource_id']}/{rel['from_table']}.{rel['from_column']}",
            "to": f"{rel['to_datasource_id']}/{rel['to_table']}.{rel['to_column']}",
        },
    )
    return rel


@router.get("/semantic/export", response_class=PlainTextResponse)
def semantic_export(identity: Identity = Depends(require_api_key)):
    """The approved semantic model as YAML (semantic-layer-as-code), scoped to
    the sources the caller may see."""
    visible = None if identity.is_admin else service.visible_datasource_ids(identity.key_id)
    return semantic.export_yaml(visible) + "\n".join(catalog.export_yaml_lines(visible)) + "\n"


@router.post("/semantic/metrics", status_code=201)
def metric_create(req: MetricCreate, identity: Identity = Depends(require_api_key)):
    # Human authorship of governed definitions is the trust anchor: admin only,
    # and the result is approved (executable) immediately.
    if not identity.is_admin:
        raise HTTPException(403, "metric creation requires the admin API key")
    if registry.get(req.datasource_id) is None:
        raise HTTPException(404, "datasource not found")
    try:
        metric = catalog.create(
            req.name, req.description, req.datasource_id, req.request_template, req.params, source="human"
        )
    except catalog.CatalogError as e:
        raise HTTPException(400, str(e))
    audit.record("create", "metric", metric["id"], identity.key_id, req.datasource_id, details={"name": req.name})
    return metric


@router.get("/semantic/metrics")
def metric_list(status: str | None = None, identity: Identity = Depends(require_api_key)):
    metrics_all = catalog.list_metrics(status=status)
    if identity.is_admin:
        return metrics_all
    # Metric templates embed table/column names (and SQL) of their source:
    # scope the listing to sources the subject may 'discover'.
    visible = service.visible_datasource_ids(identity.key_id)
    return [m for m in metrics_all if m["datasource_id"] in visible]


@router.put("/semantic/metrics/{metric_id}/review")
def metric_review(metric_id: str, req: RelationshipUpdate, identity: Identity = Depends(require_api_key)):
    if not identity.is_admin:
        raise HTTPException(403, "metric review requires the admin API key")
    metric, previous = catalog.set_status(metric_id, req.status)
    if metric is None:
        raise HTTPException(404, "metric not found")
    audit.record(
        "review_metric",
        "metric",
        metric_id,
        identity.key_id,
        details={"old_status": previous, "new_status": req.status, "name": metric["name"]},
    )
    return metric


@router.delete("/semantic/metrics/{metric_id}", status_code=204)
def metric_delete(metric_id: str, identity: Identity = Depends(require_api_key)):
    if not identity.is_admin:
        raise HTTPException(403, "metric deletion requires the admin API key")
    if not catalog.delete(metric_id):
        raise HTTPException(404, "metric not found")
    audit.record("delete", "metric", metric_id, identity.key_id)


@router.post("/semantic/metrics/{metric_id}/query")
async def metric_query(metric_id: str, req: MetricQuery, identity: Identity = Depends(require_api_key)):
    try:
        return await service.run_metric(metric_id, req.params, req.limit, identity.key_id, is_admin=identity.is_admin)
    except service.NotFoundError:
        raise HTTPException(404, "metric not found")
    except catalog.MetricNotApproved as e:
        raise HTTPException(409, str(e))
    except catalog.CatalogError as e:
        raise HTTPException(400, str(e))
    except policy.PolicyDenied as e:
        raise HTTPException(403, str(e))
    except ConnectorError as e:
        raise HTTPException(502, str(e))
    except TimeoutError:
        raise HTTPException(504, "query timed out")


@router.post("/semantic/ask")
async def semantic_ask(req: AskRequest, identity: Identity = Depends(require_api_key)):
    try:
        return await service.ask(req.question, req.limit, identity.key_id, identity.is_admin)
    except policy.PolicyDenied as e:
        raise HTTPException(403, str(e))
    except ConnectorError as e:
        raise HTTPException(502, str(e))
    except TimeoutError:
        raise HTTPException(504, "query timed out")


@router.post("/semantic/resolve")
async def semantic_resolve(req: ResolveRequest, identity: Identity = Depends(require_api_key)):
    try:
        return await service.resolve_entities(
            req.left.model_dump(), req.right.model_dump(), req.limit, identity.key_id, is_admin=identity.is_admin
        )
    except service.NotFoundError:
        raise HTTPException(404, "datasource not found")
    except ValueError as e:
        raise HTTPException(400, str(e))
    except policy.PolicyDenied as e:
        raise HTTPException(403, str(e))
    except ConnectorError as e:
        raise HTTPException(502, str(e))
    except TimeoutError:
        raise HTTPException(504, "query timed out")


@router.post("/query", response_model=SourceQueryResponse)
async def query(req: SourceQueryRequest, identity: Identity = Depends(require_api_key)):
    if req.include_pii and not identity.is_admin:
        raise HTTPException(403, "include_pii requires the admin API key")
    try:
        return await service.run_query(
            req.datasource_id, req.request, req.limit, identity.key_id,
            include_pii=req.include_pii, is_admin=identity.is_admin,
        )
    except service.NotFoundError:
        raise HTTPException(404, "datasource not found")
    except policy.PolicyDenied as e:
        raise HTTPException(403, str(e))
    except ConnectorError as e:
        raise HTTPException(502, str(e))
    except TimeoutError:
        raise HTTPException(504, "query timed out")


# Policies are the governance levers themselves: every operation, including
# reading them, is admin-only (a policy list reveals what is being protected).
@router.post("/policies", status_code=201)
def policy_create(req: PolicyCreate, identity: Identity = Depends(require_api_key)):
    if not identity.is_admin:
        raise HTTPException(403, "policy management requires the admin API key")
    if req.resource_id != "*" and registry.get(req.resource_id) is None:
        raise HTTPException(400, f"resource_id must be '*' or an existing datasource id: {req.resource_id}")
    try:
        created = policy.create(
            req.name, req.description, req.effect, req.resource_id, req.actions, req.subjects, req.conditions
        )
    except policy.PolicyError as e:
        raise HTTPException(400, str(e))
    # The full definition goes in the audit record: who was granted/denied
    # what must be reconstructable from the trail alone.
    audit.record("create_policy", "policy", created["id"], identity.key_id, details=created)
    return created


@router.get("/policies")
def policy_list(identity: Identity = Depends(require_api_key)):
    if not identity.is_admin:
        raise HTTPException(403, "policy management requires the admin API key")
    return policy.list_policies()


@router.delete("/policies/{policy_id}", status_code=204)
def policy_delete(policy_id: str, identity: Identity = Depends(require_api_key)):
    if not identity.is_admin:
        raise HTTPException(403, "policy management requires the admin API key")
    removed = policy.delete(policy_id)
    if removed is None:
        raise HTTPException(404, "policy not found")
    # Deleting a policy changes who can access what: record what was removed.
    audit.record("delete_policy", "policy", policy_id, identity.key_id, details=removed)


@router.get("/audit")
def audit_log(limit: int = 100, identity: Identity = Depends(require_api_key)):
    if not identity.is_admin:
        raise HTTPException(403, "audit log requires the admin API key")
    return audit.recent(min(limit, 1000))


@router.get("/metrics")
def metrics_summary(identity: Identity = Depends(require_api_key)):
    if not identity.is_admin:
        raise HTTPException(403, "metrics requires the admin API key")
    return metrics.collect()
