"""Tests for hosted (multi-user) mode: identity -> per-user project roots."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from saari import hosting, paths


@pytest.fixture()
def hosted_client(tmp_path, monkeypatch):
    monkeypatch.setenv("SAARI_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("SAARI_PROJECT_ROOT", raising=False)
    from saari.server import create_app

    return TestClient(create_app()), tmp_path


def test_request_scoped_root_overrides_walkup(tmp_path):
    root = paths.init_project(tmp_path / "proj")
    token = paths.set_request_root(root)
    try:
        assert paths.project_root() == root
    finally:
        paths.reset_request_root(token)


def test_resolve_creates_user_project_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SAARI_DATA_ROOT", str(tmp_path))
    root = hosting.resolve_hosted_root({"x-saari-user": "user-1"})
    assert root == tmp_path / "user-1" / "default"
    assert (root / paths.MARKER).is_dir()


def test_resolve_rejects_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv("SAARI_DATA_ROOT", str(tmp_path))
    with pytest.raises(ValueError):
        hosting.resolve_hosted_root({"x-saari-user": "../evil"})
    with pytest.raises(ValueError):
        hosting.resolve_hosted_root(
            {"x-saari-user": "u", "x-saari-project": "a/b"}
        )


def test_resolve_none_without_hosted_mode(monkeypatch):
    monkeypatch.delenv("SAARI_DATA_ROOT", raising=False)
    assert hosting.resolve_hosted_root({"x-saari-user": "u"}) is None


def test_http_requires_identity(hosted_client):
    client, _ = hosted_client
    r = client.get("/api/project")
    assert r.status_code == 401


def test_health_probe_exempt(hosted_client):
    client, _ = hosted_client
    assert client.get("/api/health").status_code == 200


def test_two_users_get_isolated_projects(hosted_client):
    client, data = hosted_client
    a = client.get("/api/project", headers={"x-saari-user": "alice"}).json()
    b = client.get("/api/project", headers={"x-saari-user": "bob"}).json()
    assert a["root"] == str(data / "alice" / "default")
    assert b["root"] == str(data / "bob" / "default")
    assert a["n_papers"] == 0 and b["n_papers"] == 0


def test_project_header_switches_project(hosted_client):
    client, data = hosted_client
    r = client.get(
        "/api/project",
        headers={"x-saari-user": "alice", "x-saari-project": "review-two"},
    ).json()
    assert r["root"] == str(data / "alice" / "review-two")


def test_easy_auth_header_accepted(hosted_client):
    client, data = hosted_client
    oid = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
    r = client.get(
        "/api/project", headers={"x-ms-client-principal-id": oid}
    ).json()
    assert r["root"] == str(data / oid / "default")


def test_bad_project_name_is_400(hosted_client):
    client, _ = hosted_client
    r = client.get(
        "/api/project",
        headers={"x-saari-user": "alice", "x-saari-project": "../oops"},
    )
    assert r.status_code == 400


def test_projects_list_and_create(hosted_client):
    client, _ = hosted_client
    h = {"x-saari-user": "alice"}
    client.get("/api/project", headers=h)  # touch default project
    r = client.post("/api/projects", headers=h, json={"name": "maritime"})
    assert r.status_code == 200
    listed = client.get("/api/projects", headers=h).json()
    assert listed["hosted"] is True
    assert listed["projects"] == ["default", "maritime"]


def test_local_mode_untouched(tmp_path, monkeypatch):
    monkeypatch.delenv("SAARI_DATA_ROOT", raising=False)
    root = paths.init_project(tmp_path / "local")
    monkeypatch.setenv("SAARI_PROJECT_ROOT", str(root))
    from saari.server import create_app

    client = TestClient(create_app())
    r = client.get("/api/project")  # no identity headers anywhere
    assert r.status_code == 200
    assert r.json()["root"] == str(root)
    listed = client.get("/api/projects").json()
    assert listed == {"hosted": False, "projects": ["local"], "active": "local"}


def test_mailto_from_identity_opt_in(tmp_path, monkeypatch):
    """With the flag on, the middleware binds the caller's email for OpenAlex
    polite-pool attribution; with it off (default), it never does."""
    from saari.hosting import RequestRootMiddleware
    from saari.sources.openalex import DEFAULT_MAILTO, _resolve_mailto

    monkeypatch.setenv("SAARI_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("SAARI_MAILTO_FROM_IDENTITY", "1")

    seen: list[str] = []

    async def inner(scope, receive, send):
        seen.append(_resolve_mailto())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    client = TestClient(RequestRootMiddleware(inner))
    client.get(
        "/x",
        headers={"x-saari-user": "alice", "x-ms-client-principal-name": "alice@novia.fi"},
    )
    assert seen == ["alice@novia.fi"]

    # malformed principal name falls back to the operator default
    client.get(
        "/x",
        headers={"x-saari-user": "alice", "x-ms-client-principal-name": "not-an-email"},
    )
    assert seen[-1] == DEFAULT_MAILTO

    # flag off: identity email is ignored even when present
    monkeypatch.delenv("SAARI_MAILTO_FROM_IDENTITY")
    client.get(
        "/x",
        headers={"x-saari-user": "alice", "x-ms-client-principal-name": "alice@novia.fi"},
    )
    assert seen[-1] == DEFAULT_MAILTO


def test_mailto_unbound_outside_requests():
    from saari import hosting
    from saari.sources.openalex import DEFAULT_MAILTO, _resolve_mailto

    assert hosting.request_mailto() is None
    assert _resolve_mailto() == DEFAULT_MAILTO
