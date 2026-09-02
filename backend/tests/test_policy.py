"""ABAC: policy validation, evaluation order, enforcement on every access path."""

import asyncio

import pytest

from eiye_db import policy

ADMIN = {"X-API-Key": "root-secret"}
PRIMARY = {"X-API-Key": "secret"}


@pytest.fixture
def keys(monkeypatch):
    """Leave open dev mode: 'primary' is non-admin, 'admin' is admin."""
    from eiye_db.config import settings

    monkeypatch.setattr(settings, "api_key", "secret")
    monkeypatch.setattr(settings, "admin_api_key", "root-secret")


def _register_demo(client, tmp_path, name="demo"):
    d = tmp_path / name
    d.mkdir()
    (d / "customers.csv").write_text("id,name,email,ssn\n1,Alice,alice@example.com,123-45-6789\n")
    (d / "orders.csv").write_text("order_id,customer_id,amount\n10,1,99.5\n")
    ds = client.post(
        "/api/v1/datasources",
        json={"name": name, "type": "filesystem", "config": {"root": str(d)}},
        headers=ADMIN,
    ).json()
    client.post(f"/api/v1/datasources/{ds['id']}/discover", headers=ADMIN)
    return ds


# --- validation ---


def test_policy_validation():
    with pytest.raises(policy.PolicyError):
        policy.create("p", "", "maybe", "*", ["read"], ["*"])  # bad effect
    with pytest.raises(policy.PolicyError):
        policy.create("p", "", "deny", "*", ["write"], ["*"])  # unknown action
    with pytest.raises(policy.PolicyError):
        policy.create("p", "", "deny", "*", [], ["*"])  # empty actions
    with pytest.raises(policy.PolicyError):
        policy.create("p", "", "deny", "*", ["read"], [])  # empty subjects
    with pytest.raises(policy.PolicyError):
        policy.create("p", "", "deny", "*", ["read"], ["*"], {"rows": []})  # unknown condition
    with pytest.raises(policy.PolicyError):
        policy.create("p", "", "allow", "*", ["read"], ["*"], {"columns": ["a"]})  # mask needs deny
    with pytest.raises(policy.PolicyError):
        # mask on discover would silently no-op (and silently NOT deny): refused
        policy.create("p", "", "deny", "*", ["read", "discover"], ["*"], {"columns": ["a"]})


def test_policy_unique_name():
    policy.create("dup", "", "deny", "*", ["read"], ["nobody"])
    with pytest.raises(policy.PolicyError):
        policy.create("dup", "", "deny", "*", ["read"], ["nobody"])


# --- evaluation ---


def test_check_deny_and_subject_scoping():
    policy.create("block-agent", "", "deny", "ds1", ["read"], ["mcp-stdio"])
    with pytest.raises(policy.PolicyDenied):
        policy.check("mcp-stdio", False, "read", "ds1")
    # other subjects, other datasources, other actions: unaffected
    assert policy.check("primary", False, "read", "ds1") == set()
    assert policy.check("mcp-stdio", False, "read", "ds2") == set()
    assert policy.check("mcp-stdio", False, "discover", "ds1") == set()
    # admin bypasses
    assert policy.check("admin", True, "read", "ds1") == set()


def test_check_column_mask_accumulates():
    policy.create("mask-a", "", "deny", "*", ["read"], ["*"], {"columns": ["ssn"]})
    policy.create("mask-b", "", "deny", "ds1", ["read"], ["*"], {"columns": ["salary"]})
    assert policy.check("primary", False, "read", "ds1") == {"ssn", "salary"}
    assert policy.check("primary", False, "read", "ds2") == {"ssn"}


def test_check_deny_wins_over_allow():
    policy.create("allow-all", "", "allow", "*", ["read"], ["*"])
    policy.create("deny-ds1", "", "deny", "ds1", ["read"], ["*"])
    with pytest.raises(policy.PolicyDenied):
        policy.check("primary", False, "read", "ds1")


def test_default_deny_flag(monkeypatch):
    from eiye_db.config import settings

    monkeypatch.setattr(settings, "abac_default_deny", True)
    with pytest.raises(policy.PolicyDenied):
        policy.check("primary", False, "read", "ds1")
    assert policy.check("admin", True, "read", "ds1") == set()  # admin unaffected
    policy.create("allow-primary", "", "allow", "*", ["read"], ["primary"])
    assert policy.check("primary", False, "read", "ds1") == set()
    with pytest.raises(policy.PolicyDenied):
        policy.check("primary", False, "discover", "ds1")  # allow is per-action


# --- API management ---


def test_policy_crud_admin_gated(client, keys):
    body = {"name": "p1", "effect": "deny", "resource_id": "*", "actions": ["read"], "subjects": ["nobody"]}
    assert client.post("/api/v1/policies", json=body, headers=PRIMARY).status_code == 403
    assert client.get("/api/v1/policies", headers=PRIMARY).status_code == 403
    created = client.post("/api/v1/policies", json=body, headers=ADMIN)
    assert created.status_code == 201
    pid = created.json()["id"]
    assert client.delete(f"/api/v1/policies/{pid}", headers=PRIMARY).status_code == 403
    listed = client.get("/api/v1/policies", headers=ADMIN).json()
    assert [p["name"] for p in listed] == ["p1"]
    assert client.delete(f"/api/v1/policies/{pid}", headers=ADMIN).status_code == 204
    assert client.get("/api/v1/policies", headers=ADMIN).json() == []


def test_policy_create_invalid_400(client, keys):
    body = {"name": "bad", "effect": "deny", "resource_id": "*", "actions": ["read"], "subjects": ["*"], "conditions": {"rows": 1}}
    assert client.post("/api/v1/policies", json=body, headers=ADMIN).status_code == 400


# --- enforcement: every access path ---


def test_query_denied_403_and_audited(client, keys, tmp_path):
    from eiye_db import audit

    ds = _register_demo(client, tmp_path)
    policy.create("block", "", "deny", ds["id"], ["read"], ["primary"])
    res = client.post(
        "/api/v1/query",
        json={"datasource_id": ds["id"], "request": {"path": "customers.csv"}},
        headers=PRIMARY,
    )
    # the caller gets a generic message — policy names reveal what's protected
    assert res.status_code == 403 and res.json()["detail"] == "access denied by policy"
    denials = [a for a in audit.recent(10) if a["action"] == "policy_deny"]
    assert denials and denials[0]["success"] is False and denials[0]["api_key_id"] == "primary"
    # ...while the audit trail keeps the specific policy for the admins
    assert "block" in denials[0]["details"]["reason"]
    # admin still passes
    assert (
        client.post(
            "/api/v1/query",
            json={"datasource_id": ds["id"], "request": {"path": "customers.csv"}},
            headers=ADMIN,
        ).status_code
        == 200
    )


def test_column_mask_end_to_end(client, keys, tmp_path):
    ds = _register_demo(client, tmp_path)
    policy.create("mask", "", "deny", "*", ["read"], ["*"], {"columns": ["ssn", "email"]})
    res = client.post(
        "/api/v1/query",
        json={"datasource_id": ds["id"], "request": {"path": "customers.csv"}},
        headers=PRIMARY,
    ).json()
    assert res["rows"] and all("ssn" not in row and "email" not in row for row in res["rows"])
    assert res["rows"][0]["id"] == "1"  # unmasked columns intact
    # masking disclosed in lineage; masked columns never reached PII redaction
    assert res["lineage"]["policy"]["masked_columns"] == ["email", "ssn"]
    assert res["pii_counts"] == {}


def test_discover_and_schema_denied(client, keys, tmp_path):
    ds = _register_demo(client, tmp_path)
    policy.create("no-schema", "", "deny", ds["id"], ["discover"], ["primary"])
    assert client.post(f"/api/v1/datasources/{ds['id']}/discover", headers=PRIMARY).status_code == 403
    assert client.get(f"/api/v1/surface/schema/{ds['id']}", headers=PRIMARY).status_code == 403
    assert client.get(f"/api/v1/surface/schema/{ds['id']}", headers=ADMIN).status_code == 200


def test_metric_inherits_datasource_policy(client, keys, tmp_path):
    from eiye_db import catalog

    ds = _register_demo(client, tmp_path)
    m = catalog.create("sample", "", ds["id"], {"path": "customers.csv"}, {}, source="human")
    policy.create("block", "", "deny", ds["id"], ["read"], ["primary"])
    res = client.post(f"/api/v1/semantic/metrics/{m['id']}/query", json={}, headers=PRIMARY)
    assert res.status_code == 403
    assert client.post(f"/api/v1/semantic/metrics/{m['id']}/query", json={}, headers=ADMIN).status_code == 200


def test_resolve_inherits_datasource_policy(client, keys, tmp_path):
    ds = _register_demo(client, tmp_path)
    policy.create("block", "", "deny", ds["id"], ["read"], ["primary"])
    side = {"datasource_id": ds["id"], "request": {"path": "customers.csv"}, "column": "name"}
    res = client.post("/api/v1/semantic/resolve", json={"left": side, "right": side}, headers=PRIMARY)
    assert res.status_code == 403


def test_mcp_denied_by_policy(client, keys, tmp_path):
    from eiye_db import mcp_server

    ds = _register_demo(client, tmp_path)
    policy.create("no-agents", "", "deny", ds["id"], ["read", "discover"], ["mcp-stdio"])
    with pytest.raises(policy.PolicyDenied):
        asyncio.run(mcp_server.get_schema(ds["id"]))
    with pytest.raises(policy.PolicyDenied):
        asyncio.run(mcp_server.query_datasource(ds["id"], {"path": "customers.csv"}))


def test_default_deny_end_to_end(client, keys, tmp_path, monkeypatch):
    from eiye_db.config import settings

    ds = _register_demo(client, tmp_path)
    monkeypatch.setattr(settings, "abac_default_deny", True)
    body = {"datasource_id": ds["id"], "request": {"path": "customers.csv"}}
    assert client.post("/api/v1/query", json=body, headers=PRIMARY).status_code == 403
    assert client.post("/api/v1/query", json=body, headers=ADMIN).status_code == 200
    policy.create("allow-primary", "", "allow", "*", ["read"], ["primary"])
    assert client.post("/api/v1/query", json=body, headers=PRIMARY).status_code == 200


# --- side channels: metadata surfaces respect the discover gate ---


def _deny_discover(ds_id, subject="primary"):
    policy.create(f"hide-{ds_id[:8]}", "", "deny", ds_id, ["read", "discover"], [subject])


def test_relationships_listing_filtered(client, keys, tmp_path):
    ds = _register_demo(client, tmp_path)
    client.post("/api/v1/semantic/detect", headers=ADMIN)
    assert client.get("/api/v1/semantic/relationships", headers=PRIMARY).json() != []
    _deny_discover(ds["id"])
    # denied source's table/column names no longer leak through the listing
    assert client.get("/api/v1/semantic/relationships", headers=PRIMARY).json() == []
    assert client.get("/api/v1/semantic/relationships", headers=ADMIN).json() != []


def test_rejected_relationships_hidden_from_non_admin(client, keys, tmp_path):
    _register_demo(client, tmp_path)
    created = client.post("/api/v1/semantic/detect", headers=ADMIN).json()
    client.put(
        f"/api/v1/semantic/relationships/{created[0]['id']}", json={"status": "rejected"}, headers=ADMIN
    )
    assert all(r["status"] != "rejected" for r in client.get("/api/v1/semantic/relationships", headers=PRIMARY).json())
    assert any(r["status"] == "rejected" for r in client.get("/api/v1/semantic/relationships", headers=ADMIN).json())


def test_export_filtered(client, keys, tmp_path):
    from eiye_db import catalog

    ds = _register_demo(client, tmp_path)
    catalog.create("secret-metric", "", ds["id"], {"path": "customers.csv"}, {}, source="human")
    assert "secret-metric" in client.get("/api/v1/semantic/export", headers=PRIMARY).text
    _deny_discover(ds["id"])
    exported = client.get("/api/v1/semantic/export", headers=PRIMARY).text
    assert "secret-metric" not in exported and "customers" not in exported
    assert "secret-metric" in client.get("/api/v1/semantic/export", headers=ADMIN).text


def test_detect_requires_admin(client, keys):
    assert client.post("/api/v1/semantic/detect", headers=PRIMARY).status_code == 403


def test_metric_listing_filtered(client, keys, tmp_path):
    from eiye_db import catalog

    ds = _register_demo(client, tmp_path)
    catalog.create("m1", "", ds["id"], {"path": "customers.csv"}, {}, source="human")
    _deny_discover(ds["id"])
    assert client.get("/api/v1/semantic/metrics", headers=PRIMARY).json() == []
    assert client.get("/api/v1/semantic/metrics", headers=ADMIN).json() != []


def test_surface_sources_filtered(client, keys, tmp_path):
    ds = _register_demo(client, tmp_path)
    _deny_discover(ds["id"])
    assert client.get("/api/v1/surface/sources", headers=PRIMARY).json() == []
    assert client.get("/api/v1/surface/sources", headers=ADMIN).json() != []


def test_mcp_listings_filtered(client, keys, tmp_path):
    from eiye_db import catalog, mcp_server

    ds = _register_demo(client, tmp_path)
    catalog.create("m1", "", ds["id"], {"path": "customers.csv"}, {}, source="human")
    assert mcp_server.list_datasources() != []
    _deny_discover(ds["id"], subject="mcp-stdio")
    assert mcp_server.list_datasources() == []
    assert mcp_server.list_metrics() == []


def test_propose_is_not_an_existence_oracle(client, keys, tmp_path):
    from eiye_db import service

    ds = _register_demo(client, tmp_path)
    _deny_discover(ds["id"], subject="mcp-stdio")
    # a denied source answers exactly like a missing one
    for target in (ds["id"], "no-such-ds"):
        with pytest.raises(service.NotFoundError) as e:
            service.propose_relationship(target, "t", "c", target, "t2", "c2", "why", "mcp-stdio")
        assert str(e.value) == f"datasource not found: {target}"
        with pytest.raises(service.NotFoundError):
            service.propose_metric("m", "", target, {"path": "x"}, {}, "mcp-stdio")


# --- the two-surface contract: raw registrations are admin-only ---


def test_datasources_group_is_admin_only(client, keys, tmp_path):
    """`/datasources` serves DataSource.config verbatim — a Postgres DSN with its
    password, REST auth headers — and its mutations retarget or cascade-delete the
    very ids policies are keyed on. Agents get `/surface/sources` instead."""
    ds = _register_demo(client, tmp_path)
    body = {"name": "other", "type": "filesystem", "config": {"root": str(tmp_path)}}
    assert client.post("/api/v1/datasources", json=body, headers=PRIMARY).status_code == 403
    assert client.get("/api/v1/datasources", headers=PRIMARY).status_code == 403
    assert client.get(f"/api/v1/datasources/{ds['id']}", headers=PRIMARY).status_code == 403
    assert client.put(f"/api/v1/datasources/{ds['id']}", json={"name": "x"}, headers=PRIMARY).status_code == 403
    assert client.post(f"/api/v1/datasources/{ds['id']}/test", headers=PRIMARY).status_code == 403
    assert client.delete(f"/api/v1/datasources/{ds['id']}", headers=PRIMARY).status_code == 403
    # nothing was renamed, retargeted or cascaded away; admin keeps the full surface
    assert client.get(f"/api/v1/datasources/{ds['id']}", headers=ADMIN).json()["name"] == "demo"
    assert len(client.get("/api/v1/datasources", headers=ADMIN).json()) == 1


def test_surface_sources_omits_config(client, keys, tmp_path):
    """The agent-facing half of the contract: policy-filtered, and credentials
    are not in the shape at all."""
    _register_demo(client, tmp_path)
    sources = client.get("/api/v1/surface/sources", headers=PRIMARY).json()
    assert sources and all("config" not in s for s in sources)


def test_schema_relationships_do_not_leak_denied_far_side(client, keys, tmp_path):
    """A cross-source link names the counterpart's datasource, table and column.
    `check_schema_access` gates the near side only, so the far side needs the
    same visibility filter every other metadata listing applies."""
    visible = _register_demo(client, tmp_path, name="visible")
    hidden = _register_demo(client, tmp_path, name="hidden")
    client.post("/api/v1/semantic/detect", headers=ADMIN)
    all_rels = client.get("/api/v1/semantic/relationships", headers=ADMIN).json()
    assert any(r["from_datasource_id"] != r["to_datasource_id"] for r in all_rels), "expected a cross-source candidate"

    _deny_discover(hidden["id"])
    rels = client.get(f"/api/v1/surface/schema/{visible['id']}", headers=PRIMARY).json()["relationships"]
    assert all(hidden["id"] not in (r["from_datasource_id"], r["to_datasource_id"]) for r in rels)
    # the near source's own links survive the filter — this is scoping, not blanking
    assert rels
    # admin still sees the whole picture
    admin_rels = client.get(f"/api/v1/surface/schema/{visible['id']}", headers=ADMIN).json()["relationships"]
    assert any(hidden["id"] in (r["from_datasource_id"], r["to_datasource_id"]) for r in admin_rels)


def test_mcp_schema_relationships_do_not_leak_denied_far_side(client, keys, tmp_path):
    from eiye_db import mcp_server

    visible = _register_demo(client, tmp_path, name="visible")
    hidden = _register_demo(client, tmp_path, name="hidden")
    client.post("/api/v1/semantic/detect", headers=ADMIN)
    _deny_discover(hidden["id"], subject="mcp-stdio")
    rels = asyncio.run(mcp_server.get_schema(visible["id"]))["relationships"]
    assert rels and all(hidden["id"] not in (r["from_datasource_id"], r["to_datasource_id"]) for r in rels)


def test_mcp_principal_is_the_configured_key_id(client, keys, tmp_path):
    """The MCP subject comes from config (EIYE_KEY_ID), so policies can target a
    named agent. It is not a credential — stdio already trusts whoever spawned
    the process — but it restores per-agent ABAC scoping and audit attribution."""
    from eiye_db import mcp_server
    from eiye_db.config import settings

    assert mcp_server.MCP_KEY_ID == settings.key_id == "mcp-stdio"


# --- audit: why was this permitted? ---


def test_audit_records_basis_of_every_permit(client, keys, tmp_path):
    """Admins bypass ABAC entirely (policy.check returns before evaluating), so
    an allow in the trail must distinguish 'a policy permitted this' from
    'nobody asked policy'."""
    from eiye_db import audit

    ds = _register_demo(client, tmp_path)
    body = {"datasource_id": ds["id"], "request": {"path": "customers.csv"}}

    assert client.post("/api/v1/query", json=body, headers=PRIMARY).status_code == 200
    assert _latest(audit, "query")["details"]["basis"] == "policy"
    assert client.post("/api/v1/query", json=body, headers=ADMIN).status_code == 200
    assert _latest(audit, "query")["details"]["basis"] == "admin-bypass"

    # the same distinction on the metadata paths
    client.get(f"/api/v1/surface/schema/{ds['id']}", headers=PRIMARY)
    assert _latest(audit, "read_schema")["details"]["basis"] == "policy"
    client.post(f"/api/v1/datasources/{ds['id']}/discover", headers=ADMIN)
    assert _latest(audit, "discover_schema")["details"]["basis"] == "admin-bypass"


def _latest(audit, action):
    return next(a for a in audit.recent(50) if a["action"] == action)


# --- masking hardening ---


def test_mask_is_case_insensitive(client, keys, tmp_path):
    d = tmp_path / "up"
    d.mkdir()
    (d / "people.csv").write_text("ID,Name,SSN\n1,Alice,123-45-6789\n")
    ds = client.post(
        "/api/v1/datasources",
        json={"name": "up", "type": "filesystem", "config": {"root": str(d)}},
        headers=ADMIN,
    ).json()
    policy.create("mask", "", "deny", "*", ["read"], ["*"], {"columns": ["ssn"]})
    res = client.post(
        "/api/v1/query", json={"datasource_id": ds["id"], "request": {"path": "people.csv"}}, headers=PRIMARY
    ).json()
    assert res["rows"] and all("SSN" not in row and "ssn" not in row for row in res["rows"])


def test_strip_masked_is_recursive():
    from eiye_db.service import _strip_masked

    rows = [{"id": 1, "detail": {"SSN": "x", "ok": [{"ssn": "y", "keep": 1}]}}]
    assert _strip_masked(rows, {"ssn"}) == [{"id": 1, "detail": {"ok": [{"keep": 1}]}}]


def test_sql_referencing_masked_column_denied():
    from eiye_db.service import _sql_references_masked

    # aliasing can't hide the source identifier
    assert _sql_references_masked("SELECT ssn AS x FROM people", {"ssn"}) == "ssn"
    assert _sql_references_masked('SELECT "SSN" FROM people', {"ssn"}) == "ssn"
    assert _sql_references_masked("SELECT p.ssn FROM people p", {"ssn"}) == "ssn"
    # no false hit on substrings of longer identifiers
    assert _sql_references_masked("SELECT ssn_hash FROM people", {"ssn"}) is None
    assert _sql_references_masked("SELECT name FROM people", {"ssn"}) is None


# --- policy store integrity ---


def test_policy_resource_must_exist(client, keys, tmp_path):
    body = {"name": "ghost", "effect": "deny", "resource_id": "no-such-ds", "actions": ["read"], "subjects": ["*"]}
    assert client.post("/api/v1/policies", json=body, headers=ADMIN).status_code == 400
    ds = _register_demo(client, tmp_path)
    body["resource_id"] = ds["id"]
    assert client.post("/api/v1/policies", json=body, headers=ADMIN).status_code == 201


def test_datasource_delete_cascades_policies(client, keys, tmp_path):
    ds = _register_demo(client, tmp_path)
    policy.create("scoped", "", "deny", ds["id"], ["read"], ["nobody"])
    policy.create("global", "", "deny", "*", ["read"], ["nobody"])
    client.delete(f"/api/v1/datasources/{ds['id']}", headers=ADMIN)
    assert [p["name"] for p in policy.list_policies()] == ["global"]


def test_policy_audit_carries_full_definition(client, keys):
    from eiye_db import audit

    body = {
        "name": "traceable", "effect": "deny", "resource_id": "*",
        "actions": ["read"], "subjects": ["primary"], "conditions": {"columns": ["ssn"]},
    }
    pid = client.post("/api/v1/policies", json=body, headers=ADMIN).json()["id"]
    client.delete(f"/api/v1/policies/{pid}", headers=ADMIN)
    by_action = {a["action"]: a for a in audit.recent(10)}
    for action in ("create_policy", "delete_policy"):
        d = by_action[action]["details"]
        assert d["subjects"] == ["primary"] and d["actions"] == ["read"]
        assert d["conditions"] == {"columns": ["ssn"]} and d["effect"] == "deny"


def test_schema_reads_audited(client, keys, tmp_path):
    from eiye_db import audit

    ds = _register_demo(client, tmp_path)
    client.get(f"/api/v1/surface/schema/{ds['id']}", headers=PRIMARY)
    reads = [a for a in audit.recent(10) if a["action"] == "read_schema"]
    assert reads and reads[0]["api_key_id"] == "primary" and reads[0]["datasource_id"] == ds["id"]


def test_example_policies_are_valid():
    """The shipped examples must always load (with placeholder substitution)."""
    import json
    from pathlib import Path

    examples = json.loads(
        (Path(__file__).resolve().parents[2] / "examples" / "policies" / "example_policies.json").read_text()
    )
    assert len(examples) >= 4
    for p in examples:
        created = policy.create(
            p["name"],
            p["description"],
            p["effect"],
            p["resource_id"].replace("REPLACE-WITH-DATASOURCE-ID", "some-ds"),
            p["actions"],
            p["subjects"],
            p.get("conditions", {}),
        )
        assert created["id"]


def test_every_example_placeholder_is_substitutable():
    """A placeholder the seed script does not know about gets POSTed verbatim,
    creating a policy whose subject or resource matches nothing. In an
    access-control file that reads as configured and is inert, which is the
    worst of the available states."""
    import importlib.util
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    examples = json.loads((root / "examples" / "policies" / "example_policies.json").read_text())
    spec = importlib.util.spec_from_file_location("seed", root / "scripts" / "seed_example_policies.py")
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)
    known = {seed.PLACEHOLDER, seed.SUBJECT_PLACEHOLDER}
    for p in examples:
        found = {v for v in [p["resource_id"], *p["subjects"]] if "REPLACE-WITH" in v}
        assert found <= known, f"{p['name']} carries an unhandled placeholder: {sorted(found - known)}"


# --- operating the hardened posture (default-deny) ---


@pytest.fixture
def hardened(monkeypatch):
    from eiye_db.config import settings

    monkeypatch.setattr(settings, "abac_default_deny", True)


def test_explain_reports_read_discover_and_masks(client, keys, tmp_path):
    """The review is decided by check() and permits() themselves. An
    explanation that could drift from enforcement would be worse than none."""
    ds = _register_demo(client, tmp_path)
    policy.create("mask-ssn", "", "deny", ds["id"], ["read"], ["primary"], {"columns": ["ssn"]})
    reviewed = policy.explain("primary", False, [ds["id"]])
    assert reviewed == [
        {"datasource_id": ds["id"], "read": True, "discover": True, "masked_columns": ["ssn"]}
    ]

    policy.create("no-read", "", "deny", ds["id"], ["read"], ["primary"])
    blocked = policy.explain("primary", False, [ds["id"]])[0]
    assert blocked["read"] is False and blocked["discover"] is True and blocked["masked_columns"] == []


def test_explain_under_default_deny_needs_an_allow(client, keys, hardened, tmp_path):
    ds = _register_demo(client, tmp_path)
    before = policy.explain("support-agent", False, [ds["id"]])[0]
    assert before["read"] is False and before["discover"] is False

    policy.create("allow-support", "", "allow", ds["id"], ["read", "discover"], ["support-agent"])
    after = policy.explain("support-agent", False, [ds["id"]])[0]
    assert after["read"] is True and after["discover"] is True


def test_explain_shows_admin_bypass(client, keys, hardened, tmp_path):
    """Admins bypass ABAC entirely, so a review of an admin subject must say
    'everything', not repeat the policy table back."""
    ds = _register_demo(client, tmp_path)
    reviewed = policy.explain("admin", True, [ds["id"]])[0]
    assert reviewed["read"] is True and reviewed["discover"] is True


def test_access_review_is_admin_only(client, keys, tmp_path):
    _register_demo(client, tmp_path)
    assert client.get("/api/v1/access/primary", headers=PRIMARY).status_code == 403
    assert client.get("/api/v1/access/primary", headers=ADMIN).status_code == 200


def test_access_review_names_the_credential(client, keys, tmp_path):
    """Which setting configures a subject is the first thing an operator needs:
    a typo'd key id reviews as 'none' rather than as a locked-out agent."""
    ds = _register_demo(client, tmp_path)
    body = client.get("/api/v1/access/primary", headers=ADMIN).json()
    assert body["credential"] == "EIYE_API_KEY" and body["is_admin"] is False
    assert body["dev_mode"] is False and body["default_deny"] is False
    assert body["datasources"][0]["name"] == "demo"
    assert body["datasources"][0]["datasource_id"] == ds["id"]

    unknown = client.get("/api/v1/access/typo-agent", headers=ADMIN).json()
    assert unknown["credential"] == "none" and unknown["is_admin"] is False

    admin_view = client.get("/api/v1/access/admin", headers=ADMIN).json()
    assert admin_view["credential"] == "EIYE_ADMIN_API_KEY" and admin_view["is_admin"] is True


def test_access_review_reflects_a_grant(client, keys, hardened, tmp_path):
    ds = _register_demo(client, tmp_path)
    denied = client.get("/api/v1/access/support-agent", headers=ADMIN).json()
    assert denied["default_deny"] is True
    assert denied["datasources"][0]["read"] is False

    client.post(
        "/api/v1/policies",
        json={
            "name": "allow-support-demo",
            "description": "",
            "effect": "allow",
            "resource_id": ds["id"],
            "actions": ["read", "discover"],
            "subjects": ["support-agent"],
        },
        headers=ADMIN,
    )
    granted = client.get("/api/v1/access/support-agent", headers=ADMIN).json()
    assert granted["datasources"][0]["read"] is True and granted["datasources"][0]["discover"] is True


def test_boot_warns_when_default_deny_has_no_allow(monkeypatch, tmp_path, caplog):
    """Silent until every agent fails. Warn, never refuse: policies are created
    through the API, so refusing to start without one would deadlock."""
    from fastapi.testclient import TestClient

    from eiye_db.config import settings
    from eiye_db.main import app

    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path}/deny.db")
    monkeypatch.setattr(settings, "abac_default_deny", True)
    with caplog.at_level("WARNING", logger="eiye_db"):
        with TestClient(app):
            pass
    assert any("no allow policy exists" in r.message for r in caplog.records)


def test_boot_is_quiet_when_an_allow_exists(monkeypatch, tmp_path, caplog):
    from fastapi.testclient import TestClient

    from eiye_db import db
    from eiye_db.config import settings
    from eiye_db.main import app

    # The policy must live in the database the lifespan will configure, which
    # it reads from settings — not the one the fresh_db fixture pointed at.
    url = f"sqlite:///{tmp_path}/allow.db"
    monkeypatch.setattr(settings, "database_url", url)
    monkeypatch.setattr(settings, "abac_default_deny", True)
    db.configure(url)
    policy.create("allow-any", "", "allow", "*", ["read", "discover"], ["primary"])
    with caplog.at_level("WARNING", logger="eiye_db"):
        with TestClient(app):
            pass
    assert not any("no allow policy exists" in r.message for r in caplog.records)
