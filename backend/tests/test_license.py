"""Entitlements: signature verification, quota enforcement, and what expiry
must never take away."""

import base64
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from eiye_db import license
from eiye_db.config import settings

ADMIN = {"X-API-Key": "root-secret"}
PRIMARY = {"X-API-Key": "secret"}


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "secret")
    monkeypatch.setattr(settings, "admin_api_key", "root-secret")

# Test-only signing key. The real one never enters the repo; this pair exists so
# the suite can mint licenses without it.
TEST_SIGNING_KEY = None
TEST_PUBLIC_KEY = None


def setup_module():
    """Mint a throwaway keypair and point the verifier at it for this module."""
    global TEST_SIGNING_KEY, TEST_PUBLIC_KEY
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    TEST_SIGNING_KEY = base64.b64encode(
        priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    ).decode()
    TEST_PUBLIC_KEY = base64.b64encode(
        priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).decode()


@pytest.fixture
def signed(monkeypatch):
    """Return a factory that writes a validly signed license and activates it."""
    monkeypatch.setattr(license, "LICENSOR_PUBLIC_KEY", TEST_PUBLIC_KEY)

    def _make(tmp_path, **overrides):
        claims = {
            "license_id": "lic-test-1",
            "customer": "Acme Corp",
            "tier": "pro",
            "max_datasources": 50,
            "max_queries_per_month": 250_000,
            "features": ["sso"],
            "issued_at": _now().isoformat(),
            "expires_at": (_now() + timedelta(days=365)).isoformat(),
        }
        claims.update(overrides)
        path = tmp_path / "eiye.license"
        path.write_text(_sign(claims))
        monkeypatch.setattr(settings, "license_file", str(path))
        license.current.cache_clear()
        return path

    yield _make
    license.current.cache_clear()


def _now():
    return datetime.now(timezone.utc)


def _sign(claims: dict) -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(TEST_SIGNING_KEY))
    payload = base64.b64encode(json.dumps(claims, sort_keys=True).encode()).decode()
    return json.dumps({"claims": payload, "signature": base64.b64encode(key.sign(payload.encode())).decode()})


@pytest.fixture(autouse=True)
def _clear_license_cache():
    license.current.cache_clear()
    yield
    license.current.cache_clear()


# --- signature verification ---


def test_no_license_file_is_the_free_grant():
    ent = license.current()
    assert not ent.licensed and ent.tier == "free"
    # These must equal the BSL Additional Use Grant in /LICENSE.
    assert ent.max_datasources == 5 and ent.max_queries_per_month == 1000


def test_valid_license_loads(signed, tmp_path):
    signed(tmp_path)
    ent = license.current()
    assert ent.licensed and ent.customer == "Acme Corp" and ent.tier == "pro"
    assert ent.max_datasources == 50 and ent.has_feature("sso")


def test_tampered_claims_are_rejected(signed, tmp_path, monkeypatch):
    """Raising your own limits by editing the file must not work — that is the
    whole point of signing rather than reading a plain config value."""
    path = signed(tmp_path)
    envelope = json.loads(path.read_text())
    claims = json.loads(base64.b64decode(envelope["claims"]))
    claims["max_datasources"] = 100_000
    envelope["claims"] = base64.b64encode(json.dumps(claims, sort_keys=True).encode()).decode()
    path.write_text(json.dumps(envelope))
    license.current.cache_clear()
    with pytest.raises(license.LicenseError, match="signature"):
        license.current()


def test_license_signed_by_the_wrong_key_is_rejected(signed, tmp_path, monkeypatch):
    signed(tmp_path)
    monkeypatch.setattr(license, "LICENSOR_PUBLIC_KEY", base64.b64encode(b"\x00" * 32).decode())
    license.current.cache_clear()
    with pytest.raises(license.LicenseError):
        license.current()


def test_malformed_and_missing_files_raise(tmp_path, monkeypatch):
    bad = tmp_path / "bad.license"
    bad.write_text("not json at all")
    monkeypatch.setattr(settings, "license_file", str(bad))
    license.current.cache_clear()
    with pytest.raises(license.LicenseError, match="malformed"):
        license.current()

    monkeypatch.setattr(settings, "license_file", str(tmp_path / "nope.license"))
    license.current.cache_clear()
    with pytest.raises(license.LicenseError, match="cannot read"):
        license.current()


# --- expiry ladder ---


def test_expiry_grace_then_degrade(signed, tmp_path):
    signed(tmp_path, expires_at=(_now() - timedelta(days=1)).isoformat())
    ent = license.current()
    assert ent.expired and ent.in_grace and not ent.degraded  # still fully working

    signed(tmp_path, expires_at=(_now() - timedelta(days=license.GRACE_PERIOD_DAYS + 1)).isoformat())
    ent = license.current()
    assert ent.degraded and not ent.has_feature("sso")


# --- enforcement ---


def test_free_tier_caps_datasource_registration(client, keys, tmp_path):
    body = {"type": "filesystem", "config": {"root": str(tmp_path)}}
    for i in range(license.FREE_MAX_DATASOURCES):
        assert client.post("/api/v1/datasources", json={"name": f"ds{i}", **body}, headers=ADMIN).status_code == 201
    over = client.post("/api/v1/datasources", json={"name": "one-too-many", **body}, headers=ADMIN)
    assert over.status_code == 402 and "5 datasources" in over.json()["detail"]


def test_license_raises_the_datasource_cap(signed, client, keys, tmp_path):
    signed(tmp_path, max_datasources=7)
    body = {"type": "filesystem", "config": {"root": str(tmp_path)}}
    for i in range(6):
        assert client.post("/api/v1/datasources", json={"name": f"ds{i}", **body}, headers=ADMIN).status_code == 201
    assert license.current().max_datasources == 7


def test_expired_license_blocks_registration_but_not_reads(signed, client, keys, tmp_path):
    """The ladder: expiry stops growth, never severs access to what exists — a
    customer may have a retention obligation against that audit trail."""
    d = tmp_path / "demo"
    d.mkdir()
    (d / "c.csv").write_text("id,name\n1,Alice\n")
    ds = client.post(
        "/api/v1/datasources", json={"name": "demo", "type": "filesystem", "config": {"root": str(d)}}, headers=ADMIN
    ).json()
    client.post(f"/api/v1/datasources/{ds['id']}/discover", headers=ADMIN)

    signed(tmp_path, expires_at=(_now() - timedelta(days=license.GRACE_PERIOD_DAYS + 1)).isoformat())

    blocked = client.post(
        "/api/v1/datasources", json={"name": "new", "type": "filesystem", "config": {"root": str(d)}}, headers=ADMIN
    )
    assert blocked.status_code == 402 and "expired" in blocked.json()["detail"]

    # everything already in place keeps working
    q = client.post("/api/v1/query", json={"datasource_id": ds["id"], "request": {"path": "c.csv"}}, headers=PRIMARY)
    assert q.status_code == 200
    assert client.get("/api/v1/audit", headers=ADMIN).status_code == 200
    assert client.get("/api/v1/semantic/export", headers=ADMIN).status_code == 200
    assert client.get(f"/api/v1/surface/schema/{ds['id']}", headers=PRIMARY).status_code == 200


def test_free_tier_query_quota_blocks_and_licensed_overage_does_not(signed, client, keys, tmp_path, monkeypatch):
    d = tmp_path / "demo"
    d.mkdir()
    (d / "c.csv").write_text("id,name\n1,Alice\n")
    ds = client.post(
        "/api/v1/datasources", json={"name": "demo", "type": "filesystem", "config": {"root": str(d)}}, headers=ADMIN
    ).json()
    body = {"datasource_id": ds["id"], "request": {"path": "c.csv"}}

    # Free tier: the grant is a hard boundary.
    monkeypatch.setattr(license, "FREE_TIER", license.Entitlements("free", 5, 2))
    license.current.cache_clear()
    assert client.post("/api/v1/query", json=body, headers=PRIMARY).status_code == 200
    assert client.post("/api/v1/query", json=body, headers=PRIMARY).status_code == 200
    over = client.post("/api/v1/query", json=body, headers=PRIMARY)
    assert over.status_code == 402 and "free tier" in over.json()["detail"]

    # Licensed: overage is recorded once, and service continues.
    from eiye_db import audit

    signed(tmp_path, max_queries_per_month=2)
    assert client.post("/api/v1/query", json=body, headers=PRIMARY).status_code == 200
    assert client.post("/api/v1/query", json=body, headers=PRIMARY).status_code == 200
    assert len([a for a in audit.recent(100) if a["action"] == "license_overage"]) == 1


def test_quota_covers_mcp_not_just_rest(client, keys, tmp_path, monkeypatch):
    """Licensing lives in service.py for the same reason the other invariants do:
    an agent must not get an unmetered path to the data."""
    import asyncio

    from eiye_db import mcp_server

    d = tmp_path / "demo"
    d.mkdir()
    (d / "c.csv").write_text("id,name\n1,Alice\n")
    ds = client.post(
        "/api/v1/datasources", json={"name": "demo", "type": "filesystem", "config": {"root": str(d)}}, headers=ADMIN
    ).json()
    monkeypatch.setattr(license, "FREE_TIER", license.Entitlements("free", 5, 0))
    license.current.cache_clear()
    with pytest.raises(license.LicenseLimitExceeded):
        asyncio.run(mcp_server.query_datasource(ds["id"], {"path": "c.csv"}))


def test_admin_does_not_bypass_the_license(client, keys, tmp_path, monkeypatch):
    """Admins bypass ABAC because they govern their data. Nobody bypasses the
    licence, because nobody in the deployment is the licensor."""
    d = tmp_path / "demo"
    d.mkdir()
    (d / "c.csv").write_text("id,name\n1,Alice\n")
    ds = client.post(
        "/api/v1/datasources", json={"name": "demo", "type": "filesystem", "config": {"root": str(d)}}, headers=ADMIN
    ).json()
    monkeypatch.setattr(license, "FREE_TIER", license.Entitlements("free", 5, 0))
    license.current.cache_clear()
    res = client.post("/api/v1/query", json={"datasource_id": ds["id"], "request": {"path": "c.csv"}}, headers=ADMIN)
    assert res.status_code == 402


def test_status_discloses_entitlements_and_usage(client, keys):
    body = client.get("/api/v1/status", headers=PRIMARY).json()
    assert body["license"]["tier"] == "free" and body["license"]["licensed"] is False
    assert body["usage"] == {"datasources": 0, "queries_this_month": 0}


# --- the issuing tool ---


def test_issue_license_script_produces_a_verifiable_file(tmp_path, monkeypatch):
    """The licensor-side tool and the runtime verifier must agree; a mismatch
    here means every issued license is dead on arrival."""
    out = tmp_path / "acme.license"
    script = Path(__file__).resolve().parents[2] / "scripts" / "issue_license.py"
    res = subprocess.run(
        [sys.executable, str(script), "--customer", "Acme Corp", "--tier", "business",
         "--datasources", "150", "--queries", "1000000", "--expires", "2030-01-01",
         "--features", "sso,compliance_reports", "--out", str(out)],
        env={**os.environ, "EIYE_LICENSE_SIGNING_KEY": TEST_SIGNING_KEY},
        capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    monkeypatch.setattr(license, "LICENSOR_PUBLIC_KEY", TEST_PUBLIC_KEY)
    ent = license.verify(out.read_text())
    assert ent.tier == "business" and ent.max_datasources == 150
    assert ent.has_feature("compliance_reports") and ent.customer == "Acme Corp"


def test_issue_license_refuses_without_a_signing_key(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts" / "issue_license.py"
    res = subprocess.run(
        [sys.executable, str(script), "--customer", "X", "--tier", "pro", "--datasources", "1",
         "--queries", "1", "--expires", "2030-01-01", "--out", str(tmp_path / "x.license")],
        env={k: v for k, v in os.environ.items() if k != "EIYE_LICENSE_SIGNING_KEY"},
        capture_output=True, text=True,
    )
    assert res.returncode == 2 and "SIGNING_KEY" in res.stderr
