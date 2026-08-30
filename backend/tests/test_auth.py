"""API-key authentication tests."""

import pytest

from eiye_db.config import settings


@pytest.fixture
def locked(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "secret")
    monkeypatch.setattr(settings, "admin_api_key", "root-secret")


def test_dev_mode_open(client):
    assert client.get("/api/v1/datasources").status_code == 200


def test_admin_key_only_is_not_dev_mode(client, monkeypatch):
    """Dev mode means *no keys at all*. If an absent EIYE_API_KEY alone opened
    the service, an operator who set only the admin key would get a fully open,
    fully admin deployment — and every admin gate in the API would be vacuous."""
    monkeypatch.setattr(settings, "api_key", None)
    monkeypatch.setattr(settings, "admin_api_key", "root-secret")
    assert client.get("/api/v1/surface/sources").status_code == 401
    assert client.get("/api/v1/surface/sources", headers={"X-API-Key": "root-secret"}).status_code == 200


def test_partially_configured_auth_refuses_to_boot(monkeypatch):
    """Half-configured auth is the dangerous state — someone tried to secure the
    service and stopped halfway. Fail at boot rather than serve while they
    believe it is locked down."""
    from fastapi.testclient import TestClient

    from eiye_db.main import app

    monkeypatch.setattr(settings, "api_key", None)
    monkeypatch.setattr(settings, "admin_api_key", "root-secret")
    with pytest.raises(RuntimeError, match="partially configured"):
        with TestClient(app):
            pass


def test_no_keys_still_boots_in_dev_mode(monkeypatch, tmp_path):
    """Zero-config stays working: a fresh clone is obviously a dev box."""
    from fastapi.testclient import TestClient

    from eiye_db.main import app

    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path}/boot.db")
    monkeypatch.setattr(settings, "api_key", None)
    monkeypatch.setattr(settings, "admin_api_key", None)
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200


def test_missing_key_401(client, locked):
    assert client.get("/api/v1/datasources").status_code == 401


def test_wrong_key_401(client, locked):
    assert client.get("/api/v1/datasources", headers={"X-API-Key": "wrong"}).status_code == 401


def test_valid_key_ok(client, locked):
    # /datasources is admin-only; the agent-facing surface is what a valid
    # non-admin key opens.
    assert client.get("/api/v1/surface/sources", headers={"X-API-Key": "secret"}).status_code == 200


def test_admin_key_ok(client, locked):
    assert client.get("/api/v1/datasources", headers={"X-API-Key": "root-secret"}).status_code == 200


def test_health_needs_no_key(client, locked):
    assert client.get("/health").status_code == 200


def test_status_needs_a_key(client, locked):
    """/health is the unauthenticated liveness probe; /api/v1/status sits inside
    the authenticated prefix and reports the build and the debug flag."""
    assert client.get("/api/v1/status").status_code == 401
    assert client.get("/api/v1/status", headers={"X-API-Key": "secret"}).status_code == 200


def test_audit_denied_for_primary_key(client, locked):
    assert client.get("/api/v1/audit", headers={"X-API-Key": "secret"}).status_code == 403


def test_audit_allowed_for_admin_key(client, locked):
    assert client.get("/api/v1/audit", headers={"X-API-Key": "root-secret"}).status_code == 200


def test_include_pii_denied_for_primary_key(client, locked, tmp_path):
    (tmp_path / "d.csv").write_text("a\n1\n")
    ds = client.post(
        "/api/v1/datasources",
        json={"name": "d", "type": "filesystem", "config": {"root": str(tmp_path)}},
        headers={"X-API-Key": "root-secret"},
    ).json()
    resp = client.post(
        "/api/v1/query",
        json={"datasource_id": ds["id"], "request": {"path": "d.csv"}, "include_pii": True},
        headers={"X-API-Key": "secret"},
    )
    assert resp.status_code == 403
