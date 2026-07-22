"""NL → governed query: deterministic matching, param extraction, LLM boundary."""

import pytest

from eiye_db import nl

M_REVENUE = {
    "id": "m1",
    "name": "revenue_by_city",
    "description": "Total order revenue grouped by customer city",
    "datasource_id": "ds1",
    "params": {"city": {"type": "string"}},
    "status": "approved",
}
M_COUNT = {
    "id": "m2",
    "name": "customer_count",
    "description": "Number of registered customers",
    "datasource_id": "ds1",
    "params": {},
    "status": "approved",
}
M_TOP = {
    "id": "m3",
    "name": "top_orders",
    "description": "Largest orders above a minimum amount",
    "datasource_id": "ds1",
    "params": {"min_amount": {"type": "number", "default": 0}},
    "status": "approved",
}


def test_rank_and_confidence():
    ranked = nl.rank("what is the revenue by city?", [M_REVENUE, M_COUNT, M_TOP])
    assert ranked[0]["metric"]["id"] == "m1" and nl.confident(ranked)
    # no token overlap: nothing ranked
    assert nl.rank("weather tomorrow?", [M_REVENUE, M_COUNT]) == []
    # ties are not confident
    a = {**M_COUNT, "id": "a", "name": "orders_alpha"}
    b = {**M_COUNT, "id": "b", "name": "orders_beta"}
    tied = nl.rank("orders", [a, b])
    assert not nl.confident(tied)


def test_rank_is_deterministic():
    metrics = [M_REVENUE, M_COUNT, M_TOP]
    q = "how many customers do we have? customer count please"
    assert nl.rank(q, metrics) == nl.rank(q, list(reversed(metrics)))


def test_extract_params():
    specs = {"city": {"type": "string"}, "min_amount": {"type": "number"}}
    # explicit pairs win
    assert nl.extract_params("revenue city=Boston min_amount=50", specs) == {"city": "Boston", "min_amount": 50}
    # quoted string -> first unbound string param; bare number -> number param
    assert nl.extract_params("revenue for 'New York' above 100", specs) == {"city": "New York", "min_amount": 100}
    # nothing extractable stays unbound
    assert nl.extract_params("revenue please", specs) == {}
    assert nl.missing_params({"params": specs}, {}) == ["city", "min_amount"]
    assert nl.missing_params({"params": {"x": {"type": "number", "default": 1}}}, {}) == []


def test_extract_params_hardening():
    specs = {"city": {"type": "string"}, "min_amount": {"type": "number"}}
    # apostrophes in possessives are not string delimiters
    assert nl.extract_params("customer's revenue in 'Boston'", specs) == {"city": "Boston"}
    # an explicit pair with an unknown name is consumed, not re-bound positionally
    assert nl.extract_params("limit=100 revenue", specs) == {}
    # digits inside a quoted literal don't leak into number binding
    assert nl.extract_params("city 'District 9'", specs) == {"city": "District 9"}
    # smart quotes fold to straight quotes
    assert nl.extract_params("revenue for “Boston”", specs) == {"city": "Boston"}


def test_single_token_is_never_confident():
    ranked = nl.rank("count", [M_COUNT, M_REVENUE])
    assert ranked and not nl.confident(ranked)  # shortlisted, but never executed


def test_plural_fold_matches():
    ranked = nl.rank("how many customers", [M_COUNT, M_REVENUE])
    assert ranked[0]["metric"]["id"] == "m2" and nl.confident(ranked)


def _make_catalog(client, tmp_path):
    from eiye_db import catalog

    d = tmp_path / "demo"
    d.mkdir()
    (d / "customers.csv").write_text("id,name,city\n1,Alice,Boston\n2,Bob,Berlin\n")
    ds = client.post(
        "/api/v1/datasources", json={"name": "demo", "type": "filesystem", "config": {"root": str(d)}}
    ).json()
    m = catalog.create(
        "customer_count", "Number of registered customers", ds["id"], {"path": "customers.csv"}, {}, source="human"
    )
    return ds, m


def test_ask_deterministic_end_to_end(client, tmp_path):
    from eiye_db import audit

    _make_catalog(client, tmp_path)
    res = client.post("/api/v1/semantic/ask", json={"question": "how many customers do we have? customer count"})
    assert res.status_code == 200
    body = res.json()
    assert body["answered"] is True and body["matcher"] == "deterministic"
    assert body["result"]["row_count"] == 2
    # lineage discloses the NL entry point and the governed definition used
    assert body["result"]["lineage"]["nl"]["metric"] == "customer_count"
    assert [a for a in audit.recent(10) if a["action"] == "ask"]


def test_ask_no_match_is_explicit_non_answer(client, tmp_path):
    _make_catalog(client, tmp_path)
    body = client.post("/api/v1/semantic/ask", json={"question": "weather on Mars tomorrow"}).json()
    assert body["answered"] is False and body["candidates"] == []


def test_ask_missing_params_reports_them(client, tmp_path):
    from eiye_db import catalog

    ds, _ = _make_catalog(client, tmp_path)
    catalog.create(
        "customers_in_city", "Customers filtered by city", ds["id"],
        {"path": "customers.csv", "city": "{city}"}, {"city": {"type": "string"}}, source="human",
    )
    body = client.post("/api/v1/semantic/ask", json={"question": "customers in city please"}).json()
    assert body["answered"] is False and "city" in body["reason"]
    assert body["candidates"][0]["name"] == "customers_in_city"


def test_ask_question_redacted_in_audit_and_lineage(client, tmp_path):
    from eiye_db import audit

    _make_catalog(client, tmp_path)
    q = "customer count for alice@example.com"
    body = client.post("/api/v1/semantic/ask", json={"question": q}).json()
    assert "alice@example.com" not in str(body["result"]["lineage"])
    assert all("alice@example.com" not in str(a["details"]) for a in audit.recent(10))


def test_ask_respects_policy_visibility(client, tmp_path, monkeypatch):
    from eiye_db import policy
    from eiye_db.config import settings

    ds, _ = _make_catalog(client, tmp_path)
    monkeypatch.setattr(settings, "api_key", "secret")
    monkeypatch.setattr(settings, "admin_api_key", "root")
    policy.create("hide", "", "deny", ds["id"], ["read", "discover"], ["primary"])
    body = client.post(
        "/api/v1/semantic/ask",
        json={"question": "customer count"},
        headers={"X-API-Key": "secret"},
    ).json()
    # the denied source's metrics are not even candidates — no name leakage
    assert body["answered"] is False and body["candidates"] == []


def test_llm_disabled_by_default(client, tmp_path):
    from eiye_db.config import settings

    assert settings.nl_llm_enabled is False


def test_ask_llm_draft_still_validated(client, tmp_path, monkeypatch):
    """The LLM boundary: a drafted param that violates the catalog allowlist
    must produce a structured non-answer, never execute."""
    from eiye_db import catalog, service
    from eiye_db.config import settings

    ds, _ = _make_catalog(client, tmp_path)
    m = catalog.create(
        "customers_in_city", "Customers filtered by city", ds["id"],
        {"path": "customers.csv", "city": "{city}"}, {"city": {"type": "string"}}, source="human",
    )
    monkeypatch.setattr(settings, "nl_llm_enabled", True)

    async def evil_draft(question, shortlist):
        return {"metric_id": m["id"], "params": {"city": "x'; DROP TABLE customers; --"}, "reason": "r"}

    monkeypatch.setattr(nl, "llm_bind", evil_draft)
    body = client.post("/api/v1/semantic/ask", json={"question": "customers in city please"}).json()
    assert body["answered"] is False and "disallowed" in body["reason"]

    async def good_draft(question, shortlist):
        return {"metric_id": m["id"], "params": {"city": "Boston"}, "reason": "r"}

    monkeypatch.setattr(nl, "llm_bind", good_draft)
    body = client.post("/api/v1/semantic/ask", json={"question": "customers in city please"}).json()
    assert body["answered"] is True and body["matcher"] == "llm-assisted"
    # llm use is disclosed in lineage
    assert body["result"]["lineage"]["nl"]["matcher"] == "llm-assisted"
    # and the drafted params still went through the redacted envelope
    assert body["metric"]["params"] == {"city": "Boston"}

    # an LLM failure fails closed to a non-answer, never a 500
    async def broken(question, shortlist):
        raise RuntimeError("api down")

    monkeypatch.setattr(nl, "llm_bind", broken)
    res = client.post("/api/v1/semantic/ask", json={"question": "customers in city please"})
    assert res.status_code == 200 and res.json()["answered"] is False
    # service module resolves nl.llm_bind dynamically for monkeypatching
    assert service is not None


def _fake_anthropic(monkeypatch, response_text):
    import sys
    import types

    class FakeMsg:
        content = [type("B", (), {"type": "text", "text": response_text})()]

    class FakeMessages:
        async def create(self, **kwargs):
            return FakeMsg()

    class FakeClient:
        def __init__(self, api_key=None):
            self.messages = FakeMessages()

    fake_mod = types.ModuleType("anthropic")
    fake_mod.AsyncAnthropic = FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)


def test_llm_bind_rejects_out_of_shortlist(monkeypatch):
    """llm_bind must drop a metric id the model was never shown."""
    import asyncio

    _fake_anthropic(monkeypatch, '{"metric_id": "evil", "params": [], "reason": "r"}')
    assert asyncio.run(nl.llm_bind("q", [M_COUNT])) is None


def test_llm_bind_converts_param_pairs(monkeypatch):
    """Params arrive as strict-schema {name, value} pairs and become a dict."""
    import asyncio

    _fake_anthropic(
        monkeypatch,
        '{"metric_id": "m2", "params": [{"name": "city", "value": "Boston"}, {"name": "n", "value": 5}], "reason": "r"}',
    )
    out = asyncio.run(nl.llm_bind("q", [M_COUNT]))
    assert out == {"metric_id": "m2", "params": {"city": "Boston", "n": 5}, "reason": "r"}


def test_llm_egress_always_audited(client, tmp_path, monkeypatch):
    """Every llm_bind call is question egress — audited even on a no-draft."""
    from eiye_db import audit
    from eiye_db.config import settings

    _make_catalog(client, tmp_path)
    monkeypatch.setattr(settings, "nl_llm_enabled", True)

    async def declines(question, shortlist):
        return None

    monkeypatch.setattr(nl, "llm_bind", declines)
    # weak single-token match: shortlisted but not confident -> LLM consulted
    body = client.post("/api/v1/semantic/ask", json={"question": "count for alice@example.com"}).json()
    assert body["answered"] is False
    egress = [a for a in audit.recent(10) if a["action"] == "ask_llm"]
    assert egress and egress[0]["details"]["outcome"] == "no-draft"
    assert "alice@example.com" not in str(egress[0]["details"])  # audit copy stays redacted


def test_llm_bind_drops_non_identifier_param_names(monkeypatch):
    import asyncio

    _fake_anthropic(
        monkeypatch,
        '{"metric_id": "m2", "params": [{"name": "evil name leak@x.com <s>", "value": "v"},'
        ' {"name": "city", "value": "Boston"}], "reason": "r"}',
    )
    out = asyncio.run(nl.llm_bind("q", [M_COUNT]))
    assert out["params"] == {"city": "Boston"}


def test_ask_failure_reason_is_redacted(client, tmp_path, monkeypatch):
    """A hostile draft must not reflect un-redacted text into the reason."""
    from eiye_db import catalog
    from eiye_db.config import settings

    ds, _ = _make_catalog(client, tmp_path)
    m = catalog.create(
        "customers_in_city", "Customers filtered by city", ds["id"],
        {"path": "customers.csv", "city": "{city}"}, {"city": {"type": "string"}}, source="human",
    )
    monkeypatch.setattr(settings, "nl_llm_enabled", True)

    async def hostile(question, shortlist):
        return {"metric_id": m["id"], "params": {"city": "ok", "leak@secret.com": "x"}, "reason": "r"}

    monkeypatch.setattr(nl, "llm_bind", hostile)
    body = client.post("/api/v1/semantic/ask", json={"question": "customers in city please"}).json()
    assert body["answered"] is False and "leak@secret.com" not in body["reason"]


def test_mcp_ask_rejects_long_question(client):
    import asyncio

    from eiye_db import mcp_server

    with pytest.raises(ValueError):
        asyncio.run(mcp_server.ask("q" * 501))


def test_ask_failure_is_audited_as_ask(client, tmp_path, monkeypatch):
    """Execution-phase failures still leave an ask-level audit trace."""
    from eiye_db import audit, catalog
    from eiye_db.config import settings

    ds, _ = _make_catalog(client, tmp_path)
    m = catalog.create(
        "customers_in_city", "Customers filtered by city", ds["id"],
        {"path": "customers.csv", "city": "{city}"}, {"city": {"type": "string"}}, source="human",
    )
    monkeypatch.setattr(settings, "nl_llm_enabled", True)

    async def evil_draft(question, shortlist):
        return {"metric_id": m["id"], "params": {"city": "x'; DROP"}, "reason": "r"}

    monkeypatch.setattr(nl, "llm_bind", evil_draft)
    body = client.post("/api/v1/semantic/ask", json={"question": "customers in city please"}).json()
    assert body["answered"] is False
    failures = [a for a in audit.recent(10) if a["action"] == "ask" and not a["success"]]
    assert failures and failures[0]["details"]["matcher"] == "llm-assisted"
    assert failures[0]["details"]["error"] == "CatalogError"


def test_ensure_llm_ready_fails_loud(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "anthropic", None)
    with pytest.raises((RuntimeError, ImportError)):
        nl.ensure_llm_ready()