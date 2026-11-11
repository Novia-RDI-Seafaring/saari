"""Regressions for bugs that only show once the feature branches meet:
snowball vs. cross-source records, HTTP route order, error mapping."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from saari import db, paths, server
from saari.models import Paper
from saari.sources import openalex


@pytest.fixture()
def project(tmp_path, monkeypatch):
    root = paths.init_project(tmp_path)
    for var in ("OPENALEX_API_KEY", "OPENALEX_MAILTO", "SAARI_SEARCH_MAX_RECORDS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SAARI_PROJECT_ROOT", str(root))
    return root


def _serve(monkeypatch, works: dict[str, dict]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        flt = request.url.params.get("filter", "")
        ids = flt.removeprefix("openalex_id:").split("|")
        res = [works[i] for i in ids if i in works]
        return httpx.Response(200, json={"meta": {"count": len(res)}, "results": res})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(**kwargs)

    monkeypatch.setattr(openalex.httpx, "Client", fake_client)


def test_snowball_links_reference_already_present_under_another_id(project, monkeypatch):
    """A reference whose DOI is already stored as a crossref: record must not
    crash on the DOI unique constraint, nor be inserted twice."""
    from saari.snowball import snowball

    with db.connect(paths.db_path(project)) as con:
        db.upsert_paper(con, Paper(id="crossref:10.1/known", doi="10.1/known", title="Known"))
        db.upsert_paper(
            con, Paper(id="openalex:W1", title="Seed", referenced_works=["W2", "W3"])
        )
    _serve(
        monkeypatch,
        {
            "W2": {"id": "https://openalex.org/W2", "title": "Known", "doi": "https://doi.org/10.1/KNOWN"},
            "W3": {"id": "https://openalex.org/W3", "title": "Fresh"},
        },
    )
    r = snowball("openalex:W1", direction="backward", project_root=project)
    assert r.n_fetched == 2
    assert r.n_new == 1
    assert sorted(r.backward_paper_ids) == ["crossref:10.1/known", "openalex:W3"]
    with db.connect(paths.db_path(project)) as con:
        ids = {row["id"] for row in con.execute("SELECT id FROM paper")}
    assert ids == {"crossref:10.1/known", "openalex:W1", "openalex:W3"}


def test_paper_sub_routes_are_not_swallowed(project):
    with db.connect(paths.db_path(project)) as con:
        db.upsert_paper(con, Paper(id="openalex:W1", title="Seed", doi="10.1/x"))
    client = TestClient(server.create_app())
    assert client.get("/api/papers/openalex:W1").json()["id"] == "openalex:W1"
    links = client.get("/api/papers/openalex:W1/links")
    assert links.status_code == 200, links.text
    assert "not found" not in links.text.lower()


def test_snowball_endpoint_maps_bad_input_to_400(project):
    with db.connect(paths.db_path(project)) as con:
        db.upsert_paper(con, Paper(id="openalex:W1", title="Seed"))
    client = TestClient(server.create_app())
    r = client.post("/api/papers/openalex:W1/snowball", json={"direction": "sideways"})
    assert r.status_code == 400
