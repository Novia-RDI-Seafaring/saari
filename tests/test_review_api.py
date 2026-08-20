"""Integration tests for the review-bundle HTTP endpoints (serve / edit / download).

Builds a tiny project, generates the bundle, then drives the FastAPI app via
TestClient, pointing it at the temp project through SAARI_PROJECT_ROOT.
"""

import pytest
from fastapi.testclient import TestClient

from saari import db, export, paths, study
from saari.models import Author, Paper


@pytest.fixture()
def client(tmp_path, monkeypatch):
    root = paths.init_project(tmp_path)
    with db.connect(paths.db_path(root)) as con:
        p = Paper(id="openalex:W1", title="A RAG Paper", year=2024, venue="V",
                  cited_by_count=10, doi="10.1/w1", authors=[Author(name="A B")])
        db.upsert_paper(con, p)
        con.execute(
            "INSERT INTO paper_projection (paper_id, x, y, method, params_json) VALUES (?,?,?,?,?)",
            (p.id, 1.0, 2.0, "test", "{}"),
        )
        db.record_search(con, "openalex", "rag", {}, ["openalex:W1"])
        db.set_screening(con, "openalex:W1", "included", "on-topic")
    study.update(project_root=root, title="T", question="RQ?", criteria="c")
    export.export_slr(project_root=root)

    monkeypatch.setenv("SAARI_PROJECT_ROOT", str(root))
    from saari.server import create_app

    return TestClient(create_app())


def test_review_list_reports_bundle_files(client):
    r = client.get("/api/review")
    assert r.status_code == 200
    body = r.json()
    assert body["exists"] is True
    names = {f["name"] for f in body["files"]}
    assert {"paper.md", "slides.md", "prisma.svg", "refs.bib"} <= names
    editable = {f["name"] for f in body["files"] if f["editable"]}
    assert editable == {"paper.md", "slides.md"}


def test_get_file_returns_content_and_type(client):
    r = client.get("/api/review/file/slides.md")
    assert r.status_code == 200
    assert "marp: true" in r.text
    assert r.headers["content-type"].startswith("text/markdown")
    # no attachment header unless requested
    assert "content-disposition" not in {k.lower() for k in r.headers}


def test_download_sets_attachment_header(client):
    r = client.get("/api/review/file/paper.md?download=1")
    assert r.status_code == 200
    assert r.headers["content-disposition"] == 'attachment; filename="paper.md"'


def test_put_round_trips_an_edit(client):
    new = "---\nmarp: true\n---\n\n# Edited deck\n"
    r = client.put("/api/review/file/slides.md", json={"content": new})
    assert r.status_code == 200 and r.json()["ok"] is True
    again = client.get("/api/review/file/slides.md")
    assert again.text == new


def test_non_editable_file_rejected_on_put(client):
    r = client.put("/api/review/file/prisma.svg", json={"content": "<svg/>"})
    assert r.status_code == 400


def test_unknown_and_traversal_names_rejected(client):
    # a bare name not on the allow-list is rejected by the route handler
    assert client.get("/api/review/file/secrets.env").status_code == 404
    # a slashed/traversal name can't match the {name} route (no `:path`); it must
    # NOT leak an arbitrary file — worst case it falls through to the SPA index.
    r = client.get("/api/review/file/..%2F..%2F..%2Fetc%2Fpasswd")
    assert "root:" not in r.text  # no /etc/passwd contents leaked
