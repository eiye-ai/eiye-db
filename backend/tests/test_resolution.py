"""Entity resolution: normalization, tiered matching, governed endpoint."""

from eiye_db.resolution import match_values, normalize_entity


def _matches(left, right):
    return match_values(left, right)["matches"]


def test_normalize_entity():
    assert normalize_entity("Acme Widgets, LLC") == "ACME WIDGETS"
    assert normalize_entity("ACME WIDGETS INC.") == "ACME WIDGETS"
    assert normalize_entity("  acme   widgets  ") == "ACME WIDGETS"
    assert normalize_entity("Acme-Widgets/Co") == "ACME WIDGETS"
    # a name that is nothing but legal boilerplate normalizes to empty
    assert normalize_entity("LLC Inc.") == ""
    # descriptive words are NOT stripped — they distinguish entities
    assert normalize_entity("Bank of America") == "BANK OF AMERICA"
    assert normalize_entity("Acme Holdings") == "ACME HOLDINGS"


def test_match_tiers():
    left = ["Acme Widgets LLC", "Widgets Acme", "Acme Widgets North", "Unrelated Foods"]
    right = ["ACME WIDGETS, INC.", "Acme Widgets Global Corp", "Beta Industrial"]
    matches = _matches(left, right)
    by_type = {m["match_type"]: m for m in matches}
    assert by_type["exact"]["left_value"] == "Acme Widgets LLC"
    assert by_type["exact"]["confidence"] == "high"
    # "Widgets Acme" == "Acme Widgets" once word order is ignored
    assert by_type["token_set"]["left_value"] == "Widgets Acme"
    assert by_type["token_set"]["confidence"] == "medium"
    # "Acme Widgets North" shares 2 of 3 significant tokens with "Acme Widgets Global"
    assert by_type["token_overlap"]["left_value"] == "Acme Widgets North"
    assert by_type["token_overlap"]["confidence"] == "low"
    # nothing matched "Unrelated Foods"
    assert len(matches) == 3
    # best-first ordering: high before medium before low
    assert [m["confidence"] for m in matches] == ["high", "medium", "low"]


def test_descriptive_suffixes_never_high():
    # "Acme Holdings" vs "Acme Partners" may be different firms: the match
    # survives only at the low "core" tier, with the ignored words disclosed
    matches = _matches(["Acme Holdings"], ["Acme Partners"])
    assert len(matches) == 1
    m = matches[0]
    assert m["match_type"] == "core" and m["confidence"] == "low"
    assert "HOLDINGS" in m["rationale"] and "PARTNERS" in m["rationale"]
    # names differing by descriptive words mid-name stay distinct entirely
    assert _matches(["General Services Administration"], ["General Administration"])[0]["confidence"] == "low"
    assert _matches(["America First Insurance"], ["First Insurance"])[0]["confidence"] == "low"


def test_token_set_respects_multiplicity():
    # "New York New York" (the casino) is not "New York" reordered
    assert _matches(["New York New York"], ["New York"]) == []
    # genuine reorder still matches
    assert _matches(["Widgets Acme"], ["Acme Widgets"])[0]["match_type"] == "token_set"


def test_overlap_ignores_stopword_tokens():
    # only PACIFIC is a significant shared token; OF/THE must not pad the count
    assert _matches(["University of the Pacific"], ["Bank of the Pacific"]) == []
    assert _matches(["The Acme Fund"], ["The Bethesda Fund"]) == []


def test_match_skips_unusable_values():
    left = [None, "", "AB", "[REDACTED:email]", {"name": "Acme"}, ["Acme"], 100, "12345", "Acme Widgets"]
    right = ["[REDACTED:email]", {"name": "Acme"}, 100, "12345", "acme widgets"]
    matches = _matches(left, right)
    # redaction markers, dicts/lists, numbers, and numeric strings never match
    assert len(matches) == 1 and matches[0]["left_value"] == "Acme Widgets"


def test_colliding_variants_are_surfaced_not_dropped():
    out = match_values(["Acme LLC", "ACME, Inc.", "acme corp"], ["Acme"])
    assert out["left_distinct"] == 1 and out["right_distinct"] == 1
    m = out["matches"][0]
    assert m["left_value"] == "Acme LLC"
    # every colliding spelling is reported, not silently discarded
    assert m["left_variants"] == ["Acme LLC", "ACME, Inc.", "acme corp"]


def test_match_output_is_deterministic():
    # collision winners come from sorted order, not dict iteration order
    a = match_values(["Widgets Acme Widgets"], ["Widgets Acme Widgets B", "Acme Widgets Widgets B"])
    b = match_values(["Widgets Acme Widgets"], ["Acme Widgets Widgets B", "Widgets Acme Widgets B"])
    assert a == b


def test_pathological_values_bounded():
    # a single enormous cell must not blow up matching (values are truncated)
    big = " ".join(f"tok{i}" for i in range(50_000))
    out = match_values([big], [big])
    assert out["left_distinct"] == 1  # truncated but usable, and fast


def _register(client, tmp_path, name, filename, header, rows):
    d = tmp_path / name
    d.mkdir()
    (d / filename).write_text(header + "\n" + "\n".join(rows) + ("\n" if rows else ""))
    ds = client.post(
        "/api/v1/datasources",
        json={"name": name, "type": "filesystem", "config": {"root": str(d)}},
    ).json()
    return ds


def test_resolve_endpoint_governed(client, tmp_path):
    from eiye_db import audit

    vendors = _register(
        client, tmp_path, "vendors", "vendors.csv",
        "vendor_name,total", ["Acme Widgets LLC,100", "Beta Industrial,50"],
    )
    donors = _register(
        client, tmp_path, "donors", "donors.csv",
        "donor,employer", ["Alice,ACME WIDGETS INC.", "Bob,Gamma Foods"],
    )
    res = client.post(
        "/api/v1/semantic/resolve",
        json={
            "left": {"datasource_id": vendors["id"], "request": {"path": "vendors.csv"}, "column": "vendor_name"},
            "right": {"datasource_id": donors["id"], "request": {"path": "donors.csv"}, "column": "employer"},
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["stats"]["matches"] == 1
    assert body["stats"]["left_distinct"] == 2 and body["stats"]["right_distinct"] == 2
    m = body["matches"][0]
    assert m["left_value"] == "Acme Widgets LLC" and m["confidence"] == "high"
    # both sides carry lineage from their governed queries
    assert body["lineage"]["left"]["datasource"]["id"] == vendors["id"]
    assert body["lineage"]["right"]["datasource"]["id"] == donors["id"]
    # the resolution itself is audited, beyond the two underlying query audits
    actions = [a["action"] for a in audit.recent(10)]
    assert "resolve_entities" in actions and actions.count("query") >= 2


def test_resolve_unknown_column_400_and_audited(client, tmp_path):
    from eiye_db import audit

    vendors = _register(client, tmp_path, "v2", "v.csv", "vendor_name", ["Acme"])
    res = client.post(
        "/api/v1/semantic/resolve",
        json={
            "left": {"datasource_id": vendors["id"], "request": {"path": "v.csv"}, "column": "nope"},
            "right": {"datasource_id": vendors["id"], "request": {"path": "v.csv"}, "column": "vendor_name"},
        },
    )
    assert res.status_code == 400 and "nope" in res.json()["detail"]
    # the failed resolve attempt is audited as a resolve, not just as queries
    failed = [a for a in audit.recent(10) if a["action"] == "resolve_entities"]
    assert failed and failed[0]["success"] is False


def test_resolve_unknown_datasource_404_and_audited(client):
    from eiye_db import audit

    side = {"datasource_id": "missing", "request": {"path": "x.csv"}, "column": "a"}
    assert client.post("/api/v1/semantic/resolve", json={"left": side, "right": side}).status_code == 404
    failed = [a for a in audit.recent(10) if a["action"] == "resolve_entities"]
    assert failed and failed[0]["success"] is False


def test_resolve_audit_redacts_column_names(client, tmp_path):
    from eiye_db import audit

    ds = _register(client, tmp_path, "v4", "v.csv", "vendor_name", [])
    # empty result: no rows to validate against, so a PII-valued column name
    # reaches the audit path — it must be redacted there
    res = client.post(
        "/api/v1/semantic/resolve",
        json={
            "left": {"datasource_id": ds["id"], "request": {"path": "v.csv"}, "column": "alice@example.com"},
            "right": {"datasource_id": ds["id"], "request": {"path": "v.csv"}, "column": "vendor_name"},
        },
    )
    assert res.status_code == 200
    rec = [a for a in audit.recent(10) if a["action"] == "resolve_entities"][0]
    assert "alice@example.com" not in str(rec["details"])


def test_mcp_resolve_entities(client, tmp_path):
    import asyncio

    from eiye_db import mcp_server

    vendors = _register(client, tmp_path, "v3", "v.csv", "vendor_name", ["Acme Widgets LLC"])
    out = asyncio.run(
        mcp_server.resolve_entities(
            vendors["id"], {"path": "v.csv"}, "vendor_name",
            vendors["id"], {"path": "v.csv"}, "vendor_name",
        )
    )
    assert out["stats"]["matches"] == 1 and out["matches"][0]["confidence"] == "high"


def test_redacted_values_cannot_match(client, tmp_path):
    # emails are redacted by the governed query path; resolution must not
    # "match" two redaction markers across sources
    a = _register(client, tmp_path, "maila", "a.csv", "contact", ["alice@example.com"])
    b = _register(client, tmp_path, "mailb", "b.csv", "contact", ["bob@other.org"])
    res = client.post(
        "/api/v1/semantic/resolve",
        json={
            "left": {"datasource_id": a["id"], "request": {"path": "a.csv"}, "column": "contact"},
            "right": {"datasource_id": b["id"], "request": {"path": "b.csv"}, "column": "contact"},
        },
    )
    assert res.status_code == 200 and res.json()["stats"]["matches"] == 0


def test_normalize_keeps_distinct_entities_apart():
    # suffix stripping must not collapse genuinely different names
    assert _matches(["Acme Widgets"], ["Acme Foods"]) == []
