"""REST API routes."""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse

from eiye_db import audit, catalog, metrics, registry, semantic, service
from eiye_db.connectors import ConnectorError
from eiye_db.models import (
    DataSource,
    DataSourceCreate,
    DataSourceUpdate,
    MetricCreate,
    MetricQuery,
    RelationshipUpdate,
    ResolveRequest,
    SourceQueryRequest,
    SourceQueryResponse,
)
from eiye_db.security import Identity, require_api_key

router = APIRouter(prefix="/api/v1")


@router.post("/datasources", response_model=DataSource, status_code=201)
def create_datasource(req: DataSourceCreate, identity: Identity = Depends(require_api_key)):
    try:
        ds = registry.create(req)
    except ValueError as e:
        raise HTTPException(409, str(e))
    audit.record("create", "datasource", ds.id, identity.key_id, ds.id)
    return ds


@router.get("/datasources", response_model=list[DataSource])
def list_datasources(identity: Identity = Depends(require_api_key)):
    return registry.list_all()


@router.get("/datasources/{datasource_id}", response_model=DataSource)
def get_datasource(datasource_id: str, identity: Identity = Depends(require_api_key)):
    ds = registry.get(datasource_id)
    if ds is None:
        raise HTTPException(404, "datasource not found")
    return ds


@router.put("/datasources/{datasource_id}", response_model=DataSource)
def update_datasource(datasource_id: str, req: DataSourceUpdate, identity: Identity = Depends(require_api_key)):
    ds = registry.update(datasource_id, req)
    if ds is None:
        raise HTTPException(404, "datasource not found")
    audit.record("update", "datasource", datasource_id, identity.key_id, datasource_id)
    return ds


@router.delete("/datasources/{datasource_id}", status_code=204)
def delete_datasource(datasource_id: str, identity: Identity = Depends(require_api_key)):
    if not registry.delete(datasource_id):
        raise HTTPException(404, "datasource not found")
    audit.record("delete", "datasource", datasource_id, identity.key_id, datasource_id)


@router.post("/datasources/{datasource_id}/test", response_model=DataSource)
async def test_datasource(datasource_id: str, identity: Identity = Depends(require_api_key)):
    try:
        return await service.test_connection(datasource_id, identity.key_id)
    except service.NotFoundError:
        raise HTTPException(404, "datasource not found")
    except ConnectorError as e:
        raise HTTPException(502, str(e))


@router.post("/datasources/{datasource_id}/discover")
async def discover_datasource(datasource_id: str, identity: Identity = Depends(require_api_key)):
    try:
        return await service.discover_schema(datasource_id, identity.key_id)
    except service.NotFoundError:
        raise HTTPException(404, "datasource not found")
    except ConnectorError as e:
        raise HTTPException(502, str(e))


@router.get("/surface/sources")
def surface_sources(identity: Identity = Depends(require_api_key)):
    sources = []
    for ds in registry.list_all():
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
    schema = registry.get_schema(datasource_id)
    if schema is None:
        raise HTTPException(404, "no schema discovered yet; POST /datasources/{id}/discover first")
    return {**schema, "relationships": service.relationships_for_schema(datasource_id)}


@router.post("/semantic/detect")
def semantic_detect(identity: Identity = Depends(require_api_key)):
    """Run heuristic candidate-join detection across all discovered schemas."""
    return service.detect_relationships(identity.key_id)


@router.get("/semantic/relationships")
def semantic_relationships(
    status: str | None = None,
    datasource_id: str | None = None,
    identity: Identity = Depends(require_api_key),
):
    return semantic.list_relationships(status=status, datasource_id=datasource_id)


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
    """The approved semantic model as YAML (semantic-layer-as-code)."""
    return semantic.export_yaml() + "\n".join(catalog.export_yaml_lines()) + "\n"


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
    return catalog.list_metrics(status=status)


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
        return await service.run_metric(metric_id, req.params, req.limit, identity.key_id)
    except service.NotFoundError:
        raise HTTPException(404, "metric not found")
    except catalog.MetricNotApproved as e:
        raise HTTPException(409, str(e))
    except catalog.CatalogError as e:
        raise HTTPException(400, str(e))
    except ConnectorError as e:
        raise HTTPException(502, str(e))
    except TimeoutError:
        raise HTTPException(504, "query timed out")


@router.post("/semantic/resolve")
async def semantic_resolve(req: ResolveRequest, identity: Identity = Depends(require_api_key)):
    try:
        return await service.resolve_entities(
            req.left.model_dump(), req.right.model_dump(), req.limit, identity.key_id
        )
    except service.NotFoundError:
        raise HTTPException(404, "datasource not found")
    except ValueError as e:
        raise HTTPException(400, str(e))
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
            req.datasource_id, req.request, req.limit, identity.key_id, include_pii=req.include_pii
        )
    except service.NotFoundError:
        raise HTTPException(404, "datasource not found")
    except ConnectorError as e:
        raise HTTPException(502, str(e))
    except TimeoutError:
        raise HTTPException(504, "query timed out")


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
