"""Metric catalog: definitions, strict substitution, governed execution, proposals."""

import asyncio

import pytest

from eiye_db import catalog
from eiye_db.catalog import CatalogError, substitute, validate_definition

TEMPLATE = {"sql": "SELECT city, count(*) FROM customers WHERE plan = '{plan}' LIMIT {n}"}
PARAMS = {"plan": {"type": "string"}, "n": {"type": "number", "default": 10}}


def test_validate_definition_rejects_undeclared_and_unused():
    with pytest.raises(CatalogError, match="undeclared"):
        validate_definition(TEMPLATE, {"plan": {"type": "string"}})  # {n} undeclared
    with pytest.raises(CatalogError, match="never used"):
        validate_definition({"sql": "SELECT 1"}, {"ghost": {"type": "string"}})
    with pytest.raises(CatalogError, match="type"):
        validate_definition({"sql": "{x}"}, {"x": {"type": "list"}})


def test_substitute_happy_path_and_default():
    out = substitute(TEMPLATE, PARAMS, {"plan": "pro"})
    assert out == {"sql": "SELECT city, count(*) FROM customers WHERE plan = 'pro' LIMIT 10"}
    out = substitute(TEMPLATE, PARAMS, {"plan": "pro", "n": 5})
    assert out["sql"].endswith("LIMIT 5")


def test_substitute_strictness():
    with pytest.raises(CatalogError, match="missing"):
        substitute(TEMPLATE, PARAMS, {})
    with pytest.raises(CatalogError, match="unknown"):
        substitute(TEMPLATE, PARAMS, {"plan": "pro", "extra": 1})
    with pytest.raises(CatalogError, match="finite number"):
        substitute(TEMPLATE, PARAMS, {"plan": "pro", "n": True})
    with pytest.raises(CatalogError, match="finite number"):
        substitute(TEMPLATE, PARAMS, {"plan": "pro", "n": "10"})


def test_substitute_blocks_injection():
    for evil in ["pro'; DROP TABLE customers; --", 'a"b', "x{y}", "a;b", "a/b", "x" * 201, "a--b", "pro\n", "a\nb"]:
        with pytest.raises(CatalogError, match="disallowed"):
            substitute(TEMPLATE, PARAMS, {"plan": evil})


def test_substitute_rejects_nonfinite_numbers():
    for bad in [float("inf"), float("-inf"), float("nan")]:
        with pytest.raises(CatalogError, match="finite"):
            substitute(TEMPLATE, PARAMS, {"plan": "pro", "n": bad})


def test_validate_definition_checks_defaults():
    with pytest.raises(CatalogError, match="disallowed"):
        validate_definition({"sql": "x = '{p}'"}, {"p": {"type": "string", "default": "bad'quote"}})
    with pytest.raises(CatalogError, match="finite"):
        validate_definition({"sql": "LIMIT {n}"}, {"n": {"type": "number", "default": float("inf")}})


def test_description_is_capped_and_redacted():
    m = catalog.create("m-desc", "contact alice@example.com " + "x" * 600, "ds1", {"sql": "SELECT 1"}, {}, "human")
    assert "[REDACTED:email]" in m["description"]
    assert len(m["description"]) <= 520  # capped before redaction tokens expand it


def test_create_human_is_approved_and_names_unique():
    m = catalog.create("m1", "", "ds1", TEMPLATE, PARAMS, source="human")
    assert m["status"] == "approved" and m["source"] == "human"
    with pytest.raises(CatalogError, match="already exists"):
        catalog.create("m1", "", "ds1", TEMPLATE, PARAMS, source="human")


def test_proposed_is_candidate_and_cannot_run():
    m = catalog.create("m2", "", "ds1", {"sql": "SELECT 1"}, {}, source="proposed")
    assert m["status"] == "candidate"
    with pytest.raises(CatalogError, match="not approved"):
        catalog.build_request(m, {})


@pytest.fixture
def fs_metric(client, tmp_path):
    (tmp_path / "customers.csv").write_text("id,plan,email\n1,pro,a@x.com\n2,free,b@y.io\n")
    ds = client.post(
        "/api/v1/datasources",
        json={"name": "demo", "type": "filesystem", "config": {"root": str(tmp_path)}},
    ).json()
    metric = client.post(
        "/api/v1/semantic/metrics",
        json={
            "name": "customer-sample",
            "description": "governed sample of customers",
            "datasource_id": ds["id"],
            "request_template": {"path": "customers.csv"},
            "params": {},
        },
    ).json()
    return ds, metric


def test_api_create_and_governed_execution(client, fs_metric):
    _ds, metric = fs_metric
    assert metric["status"] == "approved"
    out = client.post(f"/api/v1/semantic/metrics/{metric['id']}/query", json={"params": {}, "limit": 5}).json()
    assert out["metric"]["name"] == "customer-sample"
    rows = out["result"]["rows"]
    assert out["result"]["pii_filtered"] is True
    assert rows[0]["email"] == "[REDACTED:email]"  # governance chain applies to metrics


def test_api_metric_gates(client, fs_metric, monkeypatch):
    from eiye_db.config import settings

    ds, metric = fs_metric
    monkeypatch.setattr(settings, "api_key", "secret")
    monkeypatch.setattr(settings, "admin_api_key", "root-secret")
    body = {
        "name": "x",
        "datasource_id": ds["id"],
        "request_template": {"path": "customers.csv"},
        "params": {},
    }
    assert client.post("/api/v1/semantic/metrics", json=body, headers={"X-API-Key": "secret"}).status_code == 403
    # execution is allowed for the primary key (it's a governed read)
    r = client.post(
        f"/api/v1/semantic/metrics/{metric['id']}/query", json={}, headers={"X-API-Key": "secret"}
    )
    assert r.status_code == 200


def test_api_unapproved_metric_409(client, fs_metric):
    ds, _metric = fs_metric
    proposed = catalog.create("draft", "", ds["id"], {"path": "customers.csv"}, {}, source="proposed")
    r = client.post(f"/api/v1/semantic/metrics/{proposed['id']}/query", json={})
    assert r.status_code == 409
    # approve via review endpoint -> executes
    assert client.put(f"/api/v1/semantic/metrics/{proposed['id']}/review", json={"status": "approved"}).status_code == 200
    assert client.post(f"/api/v1/semantic/metrics/{proposed['id']}/query", json={}).status_code == 200


def test_api_bad_definition_400(client, fs_metric):
    ds, _ = fs_metric
    body = {
        "name": "broken",
        "datasource_id": ds["id"],
        "request_template": {"path": "{file}"},
        "params": {},
    }
    assert client.post("/api/v1/semantic/metrics", json=body).status_code == 400


def test_datasource_delete_cascades_metrics(client, fs_metric):
    ds, metric = fs_metric
    client.delete(f"/api/v1/datasources/{ds['id']}")
    assert catalog.get(metric["id"]) is None


def test_export_includes_metrics(client, fs_metric):
    text = client.get("/api/v1/semantic/export").text
    assert "metrics:" in text and '"customer-sample"' in text


def test_mcp_metric_tools(client, fs_metric):
    from eiye_db import mcp_server

    ds, metric = fs_metric
    listed = mcp_server.list_metrics()
    assert listed[0]["name"] == "customer-sample" and "request_template" not in listed[0]

    out = asyncio.run(mcp_server.query_metric(metric["id"], {}, 5))
    assert out["result"]["rows"][0]["email"] == "[REDACTED:email]"  # MCP always redacts

    prop = mcp_server.propose_metric(
        "pro-count", "count pro customers", ds["id"], {"path": "customers.csv"}, {}
    )
    assert prop["status"] == "candidate"
    with pytest.raises(catalog.MetricNotApproved):
        asyncio.run(mcp_server.query_metric(prop["id"], {}, 5))  # candidate can't run
    with pytest.raises(catalog.CatalogError):  # invalid definition raises a tool error
        mcp_server.propose_metric("bad", "", ds["id"], {"path": "{ghost}"}, {})

    rel = mcp_server.propose_relationship(ds["id"], "customers.csv", "id", ds["id"], "orders.csv", "customer_id", "seen matching values")
    assert rel["status"] == "candidate" and rel["source"] == "proposed"
    again = mcp_server.propose_relationship(ds["id"], "orders.csv", "customer_id", ds["id"], "customers.csv", "id", "reversed duplicate")
    assert again == {"already_known": True}  # undirected identity holds for proposals too


def test_proposal_rationale_redacted_and_unknown_endpoint_flagged(client, fs_metric):
    from eiye_db import mcp_server, semantic

    ds, _ = fs_metric
    client.post(f"/api/v1/datasources/{ds['id']}/discover")
    mcp_server.propose_relationship(
        ds["id"], "customers.csv", "id", ds["id"], "ghost_table", "ghost_col", "email me at spy@evil.com"
    )
    rel = [r for r in semantic.list_relationships() if r["source"] == "proposed"][0]
    assert "[REDACTED:email]" in rel["rationale"]
    assert "not found in the discovered schema" in rel["rationale"]


def test_proposal_queue_cap(client, fs_metric, monkeypatch):
    from eiye_db import mcp_server, service

    ds, _ = fs_metric
    monkeypatch.setattr(service, "PROPOSAL_QUEUE_CAP", 1)
    mcp_server.propose_metric("q1", "", ds["id"], {"path": "customers.csv"}, {})
    with pytest.raises(catalog.CatalogError, match="queue is full"):
        mcp_server.propose_metric("q2", "", ds["id"], {"path": "customers.csv"}, {})
    with pytest.raises(catalog.CatalogError, match="queue is full"):
        mcp_server.propose_relationship(ds["id"], "a", "b", ds["id"], "c", "d", "r")
