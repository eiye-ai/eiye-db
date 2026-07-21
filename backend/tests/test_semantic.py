"""Semantic layer Tier 1: relationship detection, governance, and exposure."""

import asyncio

from eiye_db import semantic
from eiye_db.semantic import _norm, detect_candidates


def test_norm():
    assert _norm("customer_id") == "customerid"
    assert _norm("customerId") == "customerid"
    assert _norm("Customer-ID") == "customerid"


CUSTOMERS = {"name": "customers.csv", "fields": [{"name": "id", "type": "integer"}, {"name": "city", "type": "string"}]}
ORDERS = {
    "name": "orders.csv",
    "fields": [{"name": "order_id", "type": "integer"}, {"name": "customer_id", "type": "integer"}],
}


def test_detect_table_key_pattern():
    # orders.customer_id names the customers table's key
    cands = detect_candidates([("ds1", "demo", [CUSTOMERS, ORDERS])])
    assert len(cands) == 1
    c = cands[0]
    assert {c["from_column"], c["to_column"]} == {"id", "customer_id"}
    assert c["confidence"] == 0.8
    assert c["kind"] == "candidate_join" and c["source"] == "heuristic"
    assert "customers" in c["rationale"]


def test_detect_exact_name_across_sources():
    a = {"name": "crm_customers", "fields": [{"name": "customer_id", "type": "integer"}]}
    b = {"name": "billing", "fields": [{"name": "customerId", "type": "bigint"}]}
    cands = detect_candidates([("ds1", "crm", [a]), ("ds2", "billing", [b])])
    assert len(cands) == 1
    assert cands[0]["confidence"] == 0.9


def test_detect_skips_generic_and_incompatible():
    # bare "id" vs bare "id" is too generic; boolean vs numeric is incompatible
    a = {"name": "alpha", "fields": [{"name": "id", "type": "integer"}]}
    b = {"name": "beta", "fields": [{"name": "id", "type": "integer"}]}
    assert detect_candidates([("ds1", "x", [a, b])]) == []
    c = {"name": "gamma", "fields": [{"name": "customer_id", "type": "boolean"}]}
    d = {"name": "delta", "fields": [{"name": "customer_id", "type": "integer"}]}
    assert detect_candidates([("ds1", "x", [c]), ("ds2", "y", [d])]) == []


def test_structural_sync_is_auto_approved_and_rebuilt():
    fks = [{"from_table": "orders", "from_column": "user_id", "to_table": "users", "to_column": "id"}]
    semantic.sync_structural("ds1", fks)
    rels = semantic.list_relationships(datasource_id="ds1")
    assert len(rels) == 1 and rels[0]["status"] == "approved" and rels[0]["source"] == "structural"
    # re-sync replaces rather than duplicates
    semantic.sync_structural("ds1", fks)
    assert len(semantic.list_relationships(datasource_id="ds1")) == 1


def test_upsert_preserves_human_decision():
    cand = detect_candidates([("ds1", "demo", [CUSTOMERS, ORDERS])])
    created = semantic.upsert(cand)
    assert created[0]["status"] == "candidate"
    rejected, previous = semantic.set_status(created[0]["id"], "rejected")
    assert rejected["status"] == "rejected" and previous == "candidate"
    # re-detection does not resurrect or duplicate the rejected link
    assert semantic.upsert(detect_candidates([("ds1", "demo", [CUSTOMERS, ORDERS])])) == []
    assert semantic.list_relationships()[0]["status"] == "rejected"


def test_structural_supersedes_rejected_heuristic():
    # A rejected heuristic guess must not veto the database's own FK metadata.
    created = semantic.upsert(detect_candidates([("ds1", "demo", [CUSTOMERS, ORDERS])]))
    semantic.set_status(created[0]["id"], "rejected")
    semantic.sync_structural(
        "ds1",
        [{"from_table": "orders.csv", "from_column": "customer_id", "to_table": "customers.csv", "to_column": "id"}],
    )
    rels = semantic.list_relationships()
    assert len(rels) == 1
    r = rels[0]
    assert r["source"] == "structural" and r["status"] == "approved" and r["confidence"] == 1.0
    assert r["id"] == created[0]["id"]  # upgraded in place, not duplicated
    # stored direction follows the FK (child -> parent)
    assert r["from_column"] == "customer_id" and r["to_column"] == "id"


def test_undirected_identity_no_mirrored_duplicate():
    # Structural row stored child->parent; a reversed heuristic emission must dedup.
    semantic.sync_structural(
        "ds1",
        [{"from_table": "orders.csv", "from_column": "customer_id", "to_table": "customers.csv", "to_column": "id"}],
    )
    reversed_cand = {
        "from_datasource_id": "ds1",
        "from_table": "customers.csv",
        "from_column": "id",
        "to_datasource_id": "ds1",
        "to_table": "orders.csv",
        "to_column": "customer_id",
        "kind": "candidate_join",
        "source": "heuristic",
        "confidence": 0.8,
        "rationale": "reversed emission",
    }
    assert semantic.upsert([reversed_cand]) == []
    assert len(semantic.list_relationships()) == 1


def test_upsert_dedupes_within_batch():
    cand = detect_candidates([("ds1", "demo", [CUSTOMERS, ORDERS])])
    assert len(semantic.upsert(cand + cand)) == 1


def test_structural_survives_rediscovery_and_stale_fk_removed():
    fk = {"from_table": "orders", "from_column": "user_id", "to_table": "users", "to_column": "id"}
    semantic.sync_structural("ds1", [fk])
    first_id = semantic.list_relationships()[0]["id"]
    semantic.sync_structural("ds1", [fk])
    rels = semantic.list_relationships()
    assert len(rels) == 1 and rels[0]["id"] == first_id  # id stable across re-discovery
    semantic.sync_structural("ds1", [])  # FK dropped at the source
    assert semantic.list_relationships() == []


def test_prune_stale_candidates():
    created = semantic.upsert(detect_candidates([("ds1", "demo", [CUSTOMERS, ORDERS])]))
    assert len(created) == 1
    # customer_id column disappears from the schema
    orders_v2 = {"name": "orders.csv", "fields": [{"name": "order_id", "type": "integer"}]}
    assert semantic.prune_stale_candidates([("ds1", "demo", [CUSTOMERS, orders_v2])]) == 1
    assert semantic.list_relationships() == []


def test_export_yaml_quotes_metacharacters():
    semantic.sync_structural(
        "ds1",
        [{"from_table": "weird: name.csv", "from_column": "a #id", "to_table": "t", "to_column": "id"}],
    )
    text = semantic.export_yaml()
    assert '"ds1/weird: name.csv.a #id"' in text  # quoted scalar, not bare


def test_detect_type_families():
    a = {"name": "alpha", "fields": [{"name": "created_id", "type": "timestamp"}]}
    b = {"name": "beta", "fields": [{"name": "created_id", "type": "timestamp"}]}
    assert detect_candidates([("ds1", "x", [a]), ("ds2", "y", [b])]) == []  # temporal never joins
    c = {"name": "gamma", "fields": [{"name": "customer_id", "type": "integer"}]}
    d = {"name": "delta", "fields": [{"name": "customer_id", "type": "text"}]}
    assert len(detect_candidates([("ds1", "x", [c]), ("ds2", "y", [d])])) == 1  # numeric~text ids allowed


def test_detect_plural_table_stems():
    addresses = {"name": "addresses.csv", "fields": [{"name": "id", "type": "integer"}]}
    people = {"name": "people", "fields": [{"name": "address_id", "type": "integer"}]}
    cands = detect_candidates([("ds1", "x", [addresses, people])])
    assert len(cands) == 1 and cands[0]["confidence"] == 0.8


def _register_demo(client, tmp_path):
    (tmp_path / "customers.csv").write_text("id,name,city\n1,Alice,Boston\n")
    (tmp_path / "orders.csv").write_text("order_id,customer_id,amount\n10,1,99.5\n")
    ds = client.post(
        "/api/v1/datasources",
        json={"name": "demo", "type": "filesystem", "config": {"root": str(tmp_path)}},
    ).json()
    client.post(f"/api/v1/datasources/{ds['id']}/discover")
    return ds


def test_api_detect_review_expose(client, tmp_path):
    ds = _register_demo(client, tmp_path)

    created = client.post("/api/v1/semantic/detect").json()
    assert len(created) == 1 and created[0]["status"] == "candidate"

    # candidates appear in the schema surface, labeled
    schema = client.get(f"/api/v1/surface/schema/{ds['id']}").json()
    assert schema["relationships"][0]["status"] == "candidate"

    # approve -> becomes authoritative; export contains it
    rel_id = created[0]["id"]
    assert client.put(f"/api/v1/semantic/relationships/{rel_id}", json={"status": "approved"}).json()[
        "status"
    ] == "approved"
    schema = client.get(f"/api/v1/surface/schema/{ds['id']}").json()
    assert schema["relationships"][0]["status"] == "approved"
    assert "customers.csv" in client.get("/api/v1/semantic/export").text

    # re-detect creates nothing new
    assert client.post("/api/v1/semantic/detect").json() == []


def test_rejected_links_hidden_from_schema(client, tmp_path):
    ds = _register_demo(client, tmp_path)
    created = client.post("/api/v1/semantic/detect").json()
    client.put(f"/api/v1/semantic/relationships/{created[0]['id']}", json={"status": "rejected"})
    schema = client.get(f"/api/v1/surface/schema/{ds['id']}").json()
    assert schema["relationships"] == []


def test_mcp_get_schema_includes_relationships(client, tmp_path):
    ds = _register_demo(client, tmp_path)
    client.post("/api/v1/semantic/detect")
    from eiye_db import mcp_server

    schema = asyncio.run(mcp_server.get_schema(ds["id"]))
    assert schema["relationships"][0]["kind"] == "candidate_join"
    assert schema["relationships"][0]["status"] == "candidate"


def test_review_unknown_relationship_404(client):
    assert client.put("/api/v1/semantic/relationships/nope", json={"status": "approved"}).status_code == 404


def test_review_structural_conflict_409(client):
    semantic.sync_structural(
        "ds1", [{"from_table": "orders", "from_column": "user_id", "to_table": "users", "to_column": "id"}]
    )
    rel_id = semantic.list_relationships()[0]["id"]
    assert client.put(f"/api/v1/semantic/relationships/{rel_id}", json={"status": "rejected"}).status_code == 409
    assert semantic.list_relationships()[0]["status"] == "approved"


def test_review_requires_admin(client, monkeypatch):
    from eiye_db.config import settings

    created = semantic.upsert(detect_candidates([("ds1", "demo", [CUSTOMERS, ORDERS])]))
    monkeypatch.setattr(settings, "api_key", "secret")
    monkeypatch.setattr(settings, "admin_api_key", "root-secret")
    url = f"/api/v1/semantic/relationships/{created[0]['id']}"
    assert client.put(url, json={"status": "approved"}, headers={"X-API-Key": "secret"}).status_code == 403
    assert client.put(url, json={"status": "approved"}, headers={"X-API-Key": "root-secret"}).status_code == 200


def test_joins_in_sql_alias_resolution():
    sql = "SELECT * FROM orders o JOIN customers AS c ON o.customer_id = c.id WHERE o.total > 5"
    assert semantic._joins_in_sql(sql) == [("orders", "customer_id", "customers", "id")]
    # bare JOIN with ON keyword directly after the table must not treat ON as an alias
    sql2 = "SELECT * FROM orders JOIN customers ON orders.customer_id = customers.id"
    assert semantic._joins_in_sql(sql2) == [("orders", "customer_id", "customers", "id")]
    assert semantic._joins_in_sql("SELECT 1") == []
    # self-join produces nothing (same table both sides)
    assert semantic._joins_in_sql("SELECT * FROM t a JOIN t b ON a.x = b.x") == []


def _schemas_for(ds_id):
    from eiye_db import registry

    schema = registry.get_schema(ds_id)
    return [(ds_id, "demo", schema["tables"])]


def test_behavioral_mining_from_audit(client, tmp_path):
    from eiye_db import audit

    ds = _register_demo(client, tmp_path)
    # a user ran this JOIN twice; the audit trail remembers. The joined pair
    # (amount = city) is one the name heuristic would never propose — this edge
    # exists only because someone actually ran it.
    sql = "SELECT * FROM orders JOIN customers ON orders.amount = customers.city"
    for _ in range(2):
        audit.record("query", "datasource", ds["id"], "primary", ds["id"], details={"request": {"sql": sql}})
    cands = semantic.mine_audit_joins(_schemas_for(ds["id"]))
    assert len(cands) == 1
    c = cands[0]
    assert c["source"] == "behavioral" and c["confidence"] == 0.7
    assert "2 audited queries by 1 distinct caller" in c["rationale"]
    # bare "orders"/"customers" in SQL resolve to the canonical schema names
    assert {c["from_table"], c["to_table"]} == {"orders.csv", "customers.csv"}
    # flows through detect: filesystem heuristic candidate + behavioral candidate
    created = client.post("/api/v1/semantic/detect").json()
    sources = {r["source"] for r in created}
    assert sources == {"heuristic", "behavioral"}
    # failed queries are ignored
    audit.record("query", "datasource", ds["id"], "primary", ds["id"],
                 details={"request": {"sql": "SELECT * FROM a JOIN b ON a.x = b.y"}}, success=False)
    assert all(c["from_table"] != "a" for c in semantic.mine_audit_joins(_schemas_for(ds["id"])))


def test_behavioral_mining_counts_distinct_callers(client, tmp_path):
    from eiye_db import audit

    ds = _register_demo(client, tmp_path)
    sql = "SELECT * FROM orders JOIN customers ON orders.customer_id = customers.id"
    # one caller hammering a join 5x is weaker evidence than 2 distinct callers
    for _ in range(5):
        audit.record("query", "datasource", ds["id"], "loud-key", ds["id"], details={"request": {"sql": sql}})
    audit.record("query", "datasource", ds["id"], "other-key", ds["id"], details={"request": {"sql": sql}})
    cands = semantic.mine_audit_joins(_schemas_for(ds["id"]))
    assert len(cands) == 1
    assert "6 audited queries by 2 distinct callers" in cands[0]["rationale"]


def test_behavioral_mining_drops_phantom_endpoints(client, tmp_path):
    from eiye_db import audit

    ds = _register_demo(client, tmp_path)
    # CTE / fabricated table names and unknown columns must not become candidates
    for sql in (
        "WITH evil AS (SELECT 1 AS id) SELECT * FROM evil JOIN customers ON evil.id = customers.id",
        "SELECT * FROM orders JOIN customers ON orders.no_such_col = customers.id",
        "SELECT * FROM public.orders o JOIN public.customers c ON o.customer_id = c.id",
    ):
        audit.record("query", "datasource", ds["id"], "k", ds["id"], details={"request": {"sql": sql}})
    cands = semantic.mine_audit_joins(_schemas_for(ds["id"]))
    # only the schema-qualified real join survives (qualifier stripped, endpoints validated)
    assert len(cands) == 1
    assert {cands[0]["from_table"], cands[0]["to_table"]} == {"orders.csv", "customers.csv"}


def test_behavioral_mining_excludes_dead_datasources(client, tmp_path):
    from eiye_db import audit

    ds = _register_demo(client, tmp_path)
    sql = "SELECT * FROM orders JOIN customers ON orders.customer_id = customers.id"
    audit.record("query", "datasource", ds["id"], "k", ds["id"], details={"request": {"sql": sql}})
    # audit rows referencing a datasource not in the live schema list are skipped
    assert semantic.mine_audit_joins([]) == []
    audit.record("query", "datasource", "gone-ds", "k", "gone-ds", details={"request": {"sql": sql}})
    cands = semantic.mine_audit_joins(_schemas_for(ds["id"]))
    assert all(c["from_datasource_id"] == ds["id"] for c in cands)


def test_behavioral_candidates_respect_proposal_queue_cap(client, tmp_path, monkeypatch):
    from eiye_db import audit, service

    ds = _register_demo(client, tmp_path)
    sql = "SELECT * FROM orders JOIN customers ON orders.customer_id = customers.id"
    audit.record("query", "datasource", ds["id"], "k", ds["id"], details={"request": {"sql": sql}})
    # queue already full -> behavioral candidates are not created
    monkeypatch.setattr(service, "_outstanding_proposals", lambda: service.PROPOSAL_QUEUE_CAP)
    created = service.detect_relationships("primary")
    assert all(r["source"] != "behavioral" for r in created)


def test_lineage_on_query_and_metric(client, tmp_path):
    from eiye_db import catalog

    ds = _register_demo(client, tmp_path)
    res = client.post(
        "/api/v1/query", json={"datasource_id": ds["id"], "request": {"path": "customers.csv"}}
    ).json()
    lin = res["lineage"]
    assert lin["datasource"]["name"] == "demo" and lin["request"] == {"path": "customers.csv"}
    assert lin["executed_at"]

    m = catalog.create("sample", "", ds["id"], {"path": "customers.csv"}, {}, source="human")
    out = client.post(f"/api/v1/semantic/metrics/{m['id']}/query", json={}).json()
    assert out["result"]["lineage"]["metric"]["name"] == "sample"
    assert out["result"]["lineage"]["datasource"]["id"] == ds["id"]


def test_metric_envelope_redacts_param_pii(client, tmp_path):
    from eiye_db import catalog

    ds = _register_demo(client, tmp_path)
    m = catalog.create(
        "by-owner", "", ds["id"], {"path": "customers.csv", "owner": "{owner}"},
        {"owner": {"type": "string"}}, source="human",
    )
    # '@' passes the param allowlist, so an email can legally reach the params —
    # the envelope and lineage must re-serve it redacted
    out = client.post(
        f"/api/v1/semantic/metrics/{m['id']}/query", json={"params": {"owner": "alice@example.com"}}
    ).json()
    assert "alice@example.com" not in str(out["metric"]["params"])
    assert "alice@example.com" not in str(out["result"]["lineage"])


def test_datasource_delete_cascades_relationships(client, tmp_path):
    ds = _register_demo(client, tmp_path)
    client.post("/api/v1/semantic/detect")
    assert len(semantic.list_relationships()) == 1
    client.delete(f"/api/v1/datasources/{ds['id']}")
    assert semantic.list_relationships() == []
