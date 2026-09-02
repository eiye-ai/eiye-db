"""API-key authentication tests."""

import hashlib

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


# --- named keys (EIYE_API_KEYS) ---

SUPPORT_SECRET = "support-agent-secret"
OPS_SECRET = "ops-agent-secret"
SUPPORT = {"X-API-Key": SUPPORT_SECRET}
OPS = {"X-API-Key": OPS_SECRET}


def _entry(secret: str, **kwargs):
    from eiye_db.config import NamedKey

    return NamedKey(sha256=hashlib.sha256(secret.encode()).hexdigest(), **kwargs)


@pytest.fixture
def named(monkeypatch):
    """Only the map is configured: neither single-key setting is set."""
    monkeypatch.setattr(settings, "api_key", None)
    monkeypatch.setattr(settings, "admin_api_key", None)
    monkeypatch.setattr(
        settings,
        "api_keys",
        {"support-agent": _entry(SUPPORT_SECRET), "ops": _entry(OPS_SECRET, is_admin=True)},
    )


def _register(client, tmp_path, headers, name="demo"):
    d = tmp_path / name
    d.mkdir()
    (d / "customers.csv").write_text("id,name\n1,Alice\n")
    return client.post(
        "/api/v1/datasources",
        json={"name": name, "type": "filesystem", "config": {"root": str(d)}},
        headers=headers,
    ).json()


def test_named_map_alone_is_not_dev_mode(client, named):
    """The trap EIYE_API_KEY already had, re-armed by a third setting: an
    operator who secures the service with only the map has secured it. Reading
    that as 'no keys set' would serve every caller as admin."""
    assert client.get("/api/v1/surface/sources").status_code == 401


def test_named_key_opens_the_agent_surface(client, named):
    assert client.get("/api/v1/surface/sources", headers=SUPPORT).status_code == 200


def test_named_non_admin_denied_the_admin_surface(client, named):
    assert client.get("/api/v1/datasources", headers=SUPPORT).status_code == 403


def test_named_admin_key_opens_the_admin_surface(client, named):
    assert client.get("/api/v1/datasources", headers=OPS).status_code == 200


def test_wrong_key_401_against_the_map(client, named):
    assert client.get("/api/v1/surface/sources", headers={"X-API-Key": "wrong"}).status_code == 401


def test_named_key_is_its_own_audit_principal(client, named, tmp_path):
    """The point of the map. With EIYE_API_KEY alone every HTTP caller lands in
    the trail as 'primary' and one agent cannot be told from another."""
    from eiye_db import audit

    ds = _register(client, tmp_path, OPS)
    client.post(
        "/api/v1/query",
        json={"datasource_id": ds["id"], "request": {"path": "customers.csv"}},
        headers=SUPPORT,
    )
    queries = [a for a in audit.recent(10) if a["action"] == "query"]
    assert queries and queries[0]["api_key_id"] == "support-agent"


def test_named_key_is_its_own_abac_subject(client, named, tmp_path, monkeypatch):
    """Per-agent authorization, which subject matching could not express while
    every HTTP caller resolved to 'primary'."""
    from eiye_db import policy

    ds = _register(client, tmp_path, OPS)
    policy.create("block-support", "", "deny", ds["id"], ["read"], ["support-agent"])
    body = {"datasource_id": ds["id"], "request": {"path": "customers.csv"}}
    assert client.post("/api/v1/query", json=body, headers=SUPPORT).status_code == 403
    # A second named key, denied nothing, is unaffected by the first one's deny.
    monkeypatch.setitem(settings.api_keys, "billing-agent", _entry("billing-agent-secret"))
    resp = client.post("/api/v1/query", json=body, headers={"X-API-Key": "billing-agent-secret"})
    assert resp.status_code == 200


def test_expired_named_key_401_with_a_distinct_message(client, monkeypatch):
    """An expired credential is a rotation that did not happen. Reporting it as
    an invalid key sends the operator hunting for a typo that is not there."""
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(settings, "api_key", None)
    monkeypatch.setattr(settings, "admin_api_key", None)
    monkeypatch.setattr(
        settings,
        "api_keys",
        {
            "stale": _entry(SUPPORT_SECRET, expires_at=datetime.now(timezone.utc) - timedelta(days=1)),
            "fresh": _entry(OPS_SECRET, is_admin=True, expires_at=datetime.now(timezone.utc) + timedelta(days=1)),
        },
    )
    resp = client.get("/api/v1/surface/sources", headers=SUPPORT)
    assert resp.status_code == 401 and "expired" in resp.json()["detail"]
    assert client.get("/api/v1/surface/sources", headers=OPS).status_code == 200


def test_single_key_settings_still_work_beside_the_map(client, monkeypatch):
    """Backward compatibility is the whole reason the two settings survive:
    existing policies name the subjects 'primary' and 'admin'."""
    monkeypatch.setattr(settings, "api_key", "secret")
    monkeypatch.setattr(settings, "admin_api_key", "root-secret")
    monkeypatch.setattr(settings, "api_keys", {"support-agent": _entry(SUPPORT_SECRET)})
    assert client.get("/api/v1/datasources", headers={"X-API-Key": "root-secret"}).status_code == 200
    assert client.get("/api/v1/surface/sources", headers={"X-API-Key": "secret"}).status_code == 200
    assert client.get("/api/v1/surface/sources", headers=SUPPORT).status_code == 200


def test_boot_refuses_a_map_with_no_admin(monkeypatch):
    """Every entry non-admin and no EIYE_ADMIN_API_KEY locks the operator out of
    registrations, policies, the audit log and every approval."""
    from fastapi.testclient import TestClient

    from eiye_db.main import app

    monkeypatch.setattr(settings, "api_key", None)
    monkeypatch.setattr(settings, "admin_api_key", None)
    monkeypatch.setattr(settings, "api_keys", {"support-agent": _entry(SUPPORT_SECRET)})
    with pytest.raises(RuntimeError, match="no admin principal"):
        with TestClient(app):
            pass


def test_boot_refuses_a_reserved_key_id(monkeypatch):
    """Two credentials under one subject erase the distinction the map exists
    to draw — in the ABAC subject and in the audit trail alike."""
    from fastapi.testclient import TestClient

    from eiye_db.main import app

    monkeypatch.setattr(settings, "api_key", "secret")
    monkeypatch.setattr(settings, "admin_api_key", "root-secret")
    monkeypatch.setattr(settings, "api_keys", {"primary": _entry(SUPPORT_SECRET)})
    with pytest.raises(RuntimeError, match="reserved key id"):
        with TestClient(app):
            pass


def test_boot_accepts_a_map_with_an_admin(monkeypatch, tmp_path):
    """The refusals above must not over-fire: map-only is a complete config."""
    from fastapi.testclient import TestClient

    from eiye_db.main import app

    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path}/boot.db")
    monkeypatch.setattr(settings, "api_key", None)
    monkeypatch.setattr(settings, "admin_api_key", None)
    monkeypatch.setattr(settings, "api_keys", {"ops": _entry(OPS_SECRET, is_admin=True)})
    with TestClient(app) as c:
        assert c.get("/api/v1/datasources", headers=OPS).status_code == 200


# --- the NamedKey shape itself ---


def test_named_key_rejects_a_bad_digest():
    """A truncated or mistyped hash would otherwise authenticate nobody, and
    look like a broken key rather than a broken config."""
    import pydantic

    from eiye_db.config import NamedKey

    with pytest.raises(pydantic.ValidationError):
        NamedKey(sha256="not-a-digest")
    with pytest.raises(pydantic.ValidationError):
        NamedKey(sha256="ab" * 31)  # 62 chars


def test_named_key_rejects_unknown_fields():
    """`"admin": true` instead of `"is_admin": true` must not be accepted in
    silence as the non-admin key nobody intended."""
    import pydantic

    from eiye_db.config import NamedKey

    with pytest.raises(pydantic.ValidationError):
        NamedKey(sha256="a" * 64, admin=True)


def test_naive_expiry_is_read_as_utc():
    """`"expires_at": "2027-01-01"` parses naive; comparing naive to aware
    raises TypeError, which would surface as a 500 on every request."""
    from datetime import timezone

    from eiye_db.config import NamedKey

    assert NamedKey(sha256="a" * 64, expires_at="2027-01-01").expires_at.tzinfo == timezone.utc


def test_map_parses_from_the_environment(monkeypatch):
    """Every other test here monkeypatches the settings object, so nothing else
    exercises the documented interface: EIYE_API_KEYS as a JSON env var."""
    import json

    from eiye_db.config import Settings

    monkeypatch.setenv(
        "EIYE_API_KEYS",
        json.dumps({"ops": {"sha256": "b" * 64, "is_admin": True, "expires_at": "2027-01-01"}}),
    )
    parsed = Settings(_env_file=None)
    assert parsed.api_keys["ops"].is_admin is True
    assert parsed.api_keys["ops"].expires_at.year == 2027
