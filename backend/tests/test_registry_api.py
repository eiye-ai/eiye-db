"""Datasource registry CRUD API tests (dev mode: no API key set)."""


def _create(client, name="files"):
    return client.post(
        "/api/v1/datasources",
        json={"name": name, "type": "filesystem", "config": {"root": "/tmp"}},
    )


def test_create_and_get(client):
    resp = _create(client)
    assert resp.status_code == 201
    ds = resp.json()
    assert ds["name"] == "files"
    assert ds["status"] == "discovered"
    assert client.get(f"/api/v1/datasources/{ds['id']}").json()["id"] == ds["id"]


def test_duplicate_name_409(client):
    assert _create(client).status_code == 201
    assert _create(client).status_code == 409


def test_list(client):
    _create(client, "a")
    _create(client, "b")
    names = [d["name"] for d in client.get("/api/v1/datasources").json()]
    assert names == ["a", "b"]


def test_update(client):
    ds_id = _create(client).json()["id"]
    resp = client.put(f"/api/v1/datasources/{ds_id}", json={"description": "docs"})
    assert resp.status_code == 200
    assert resp.json()["description"] == "docs"
    assert resp.json()["name"] == "files"


def test_delete(client):
    ds_id = _create(client).json()["id"]
    assert client.delete(f"/api/v1/datasources/{ds_id}").status_code == 204
    assert client.get(f"/api/v1/datasources/{ds_id}").status_code == 404


def test_unknown_404(client):
    assert client.get("/api/v1/datasources/nope").status_code == 404
    assert client.put("/api/v1/datasources/nope", json={}).status_code == 404
    assert client.delete("/api/v1/datasources/nope").status_code == 404


def test_invalid_type_422(client):
    resp = client.post("/api/v1/datasources", json={"name": "x", "type": "fax_machine"})
    assert resp.status_code == 422


def test_unimplemented_types_rejected_at_register(client):
    # These used to be accepted by DataSourceType and then blow up in
    # get_connector at test/query time. Registering must fail up front, so
    # the enum can only ever advertise types that actually run.
    for type in ("mongodb", "google_drive", "github", "csv", "mcp_server", "sqlite"):
        resp = client.post("/api/v1/datasources", json={"name": type, "type": type})
        assert resp.status_code == 422, type
    assert client.get("/api/v1/datasources").json() == []


def test_implemented_type_registers(client):
    # The other half of the contract: a type is in the enum only once its
    # connector runs, so every enum member must be registrable.
    resp = client.post(
        "/api/v1/datasources",
        json={"name": "db", "type": "mysql", "config": {"dsn": "mysql://u:p@h:3306/db"}},
    )
    assert resp.status_code == 201
