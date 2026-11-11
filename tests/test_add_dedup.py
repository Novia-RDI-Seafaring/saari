"""Add-by-identifier, duplicate candidates, merge, and their PRISMA accounting.

All HTTP is mocked (OpenAlex + Crossref through one MockTransport).
"""

from __future__ import annotations

import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

# Import the surface modules before any test patches httpx.Client: their
# dependencies evaluate `httpx.Client | None` style annotations at import.
from saari import cli, db, dedup, mcp_server, paths, report, server
from saari.models import Author, Paper
from saari.sources import add_papers, find_existing, run_search


def _oa_work(wid="W1", doi="10.1/pub", title="Digital twins for ship hull monitoring", year=2022, **over):
    w = {
        "id": f"https://openalex.org/{wid}",
        "doi": f"https://doi.org/{doi}" if doi else None,
        "title": title,
        "publication_year": year,
        "authorships": [{"author": {"display_name": "Jane Doe"}, "institutions": []}],
        "primary_location": {"source": {"display_name": "Ocean Eng"}},
        "ids": {},
    }
    w.update(over)
    return w


def _cr_item(doi="10.9999/only.cr", title="A paper only Crossref knows about"):
    return {"DOI": doi, "title": [title], "issued": {"date-parts": [[2025]]}, "author": [{"given": "A", "family": "Smith"}]}


class Router:
    """Serve canned OpenAlex / Crossref responses keyed by URL path."""

    def __init__(self):
        self.openalex: dict[str, dict] = {}
        self.crossref: dict[str, dict] = {}
        self.calls: list[str] = []
        self.offline = False

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if self.offline:
            raise httpx.ConnectError("offline", request=req)
        path = req.url.raw_path.decode().split("?", 1)[0]
        self.calls.append(f"{req.url.host}{path}")
        table = self.openalex if req.url.host == "api.openalex.org" else self.crossref
        body = table.get(path)
        if body is None:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json=body)


@pytest.fixture()
def router(monkeypatch):
    r = Router()
    transport = httpx.MockTransport(r)
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "Client", fake_client)
    return r


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENALEX_MAILTO", "ops@example.org")
    return paths.init_project(tmp_path)


def _con(root):
    return db.connect(paths.db_path(root))


# --------------------------------------------------------------------------- #
# paper add
# --------------------------------------------------------------------------- #


def test_add_by_doi_via_openalex(project, router):
    router.openalex["/works/doi:10.1/pub"] = _oa_work()
    res = add_papers(["https://doi.org/10.1/PUB"], project_root=project)
    assert res["n_new"] == 1 and res["n_failed"] == 0, res
    assert res["results"][0]["status"] == "added"
    assert res["results"][0]["resolved_via"] == "openalex"
    assert res["paper_ids"] == ["openalex:W1"]
    with _con(project) as con:
        s = con.execute("SELECT * FROM search WHERE id = ?", (res["search_id"],)).fetchone()
        assert s["source"] == "manual" and s["n_returned"] == 1


def test_add_falls_back_to_crossref(project, router):
    router.crossref["/works/10.9999/only.cr"] = {"message": _cr_item()}
    res = add_papers(["doi:10.9999/only.cr"], project_root=project)
    r = res["results"][0]
    assert r["status"] == "added" and r["resolved_via"] == "crossref"
    assert r["paper_id"] == "crossref:10.9999/only.cr"
    assert "api.openalex.org/works/doi:10.9999/only.cr" in router.calls  # OpenAlex first


def test_add_pmid_arxiv_openalex_forms(project, router):
    router.openalex["/works/pmid:123"] = _oa_work("W2", doi="10.1/b", title="Paper B title here")
    router.openalex["/works/doi:10.48550/arxiv.2101.00001"] = _oa_work("W3", doi="10.48550/arxiv.2101.00001", title="Paper C title here")
    router.openalex["/works/W4"] = _oa_work("W4", doi="10.1/d", title="Paper D title here")
    res = add_papers(
        ["pmid:123", "arXiv:2101.00001v2", "https://openalex.org/W4", "12345", "doi:10.1/missing"],
        project_root=project,
    )
    statuses = {r["input"]: r["status"] for r in res["results"]}
    assert statuses == {
        "pmid:123": "added",
        "arXiv:2101.00001v2": "added",
        "https://openalex.org/W4": "added",
        "12345": "invalid",
        "doi:10.1/missing": "not_found",
    }
    assert res["n_new"] == 3 and res["n_failed"] == 2


def test_add_existing_counts_as_duplicate_without_network(project, router):
    with _con(project) as con:
        db.upsert_paper(con, Paper(id="openalex:W1", title="T", doi="10.1/pub"))
        db.record_search(con, "openalex", "q", {}, ["openalex:W1"])
    res = add_papers(["10.1/PUB"], project_root=project)
    assert res["results"][0]["status"] == "duplicate"
    assert res["results"][0]["resolved_via"] == "corpus"
    assert router.calls == []
    d = report.prisma_data(project)
    assert d["identification"]["n_manual"] == 1
    assert d["identification"]["n_database"] == 1
    assert d["n_duplicates_removed"] == 1 and d["n_unique"] == 1


def test_add_nothing_resolved_records_no_event(project, router):
    res = add_papers(["not-an-id"], project_root=project)
    assert res["search_id"] is None and res["paper_ids"] == []
    with _con(project) as con:
        assert con.execute("SELECT COUNT(*) FROM search").fetchone()[0] == 0


def test_add_network_error_is_per_identifier(project, router):
    router.offline = True
    res = add_papers(["10.1/x"], project_root=project)
    assert res["results"][0]["status"] == "error"
    assert "offline" in res["results"][0]["error"]
    assert res["search_id"] is None


def test_manual_is_not_a_database_in_prisma_or_stages(project, router):
    router.openalex["/works/doi:10.1/pub"] = _oa_work()
    add_papers(["10.1/pub"], project_root=project)
    d = report.prisma_data(project)
    idn = d["identification"]
    assert idn["n_manual"] == 1 and idn["n_database"] == 0
    assert idn["n_total"] == 1 and d["n_duplicates_removed"] == 0
    assert "added by identifier: 1" in report.render_prisma_mermaid(d)
    assert "manual" not in report.limitations_block(idn["by_source"]).split("databases")[0]
    from saari.status import stages_data

    st = {s["key"]: s for s in stages_data(project)["stages"]}
    assert st["search"]["state"] == "todo"


# --------------------------------------------------------------------------- #
# ingest-time exact-id dedup
# --------------------------------------------------------------------------- #


def test_find_existing_exact_ids_only(project):
    with _con(project) as con:
        db.upsert_paper(con, Paper(id="openalex:W1", title="X", pmid="999", arxiv_id="2101.00001"))
        db.upsert_paper(con, Paper(id="openalex:W9", title="Y", arxiv_id="hep-th"))  # malformed id
        assert find_existing(con, Paper(id="scopus:1", title="other", pmid="999")) == "openalex:W1"
        assert find_existing(con, Paper(id="crossref:x", title="o", arxiv_id="2101.00001")) == "openalex:W1"
        # Malformed arXiv ids never match, same title never auto-matches.
        assert find_existing(con, Paper(id="scopus:2", title="Y", arxiv_id="hep-th")) is None
        assert find_existing(con, Paper(id="scopus:3", title="X")) is None


# --------------------------------------------------------------------------- #
# candidates
# --------------------------------------------------------------------------- #


def _seed(con, *papers):
    for p in papers:
        db.upsert_paper(con, p)


def _p(pid, title, year, author="Jane Doe", **kw):
    return Paper(id=pid, title=title, year=year, authors=[Author(name=author)] if author else [], **kw)


TITLE = "Digital twins for ship hull monitoring"


def test_preprint_and_published_are_candidates(project):
    with _con(project) as con:
        _seed(
            con,
            _p("openalex:W1", TITLE, 2022, doi="10.1/pub", venue="Ocean Eng"),
            _p("openalex:W2", "Digital Twins for Ship-Hull Monitoring.", 2021, author="Doe, J.",
               doi="10.48550/arxiv.2101.00001", venue="arXiv"),
        )
        cands = dedup.find_candidates(con)
    assert len(cands) == 1
    c = cands[0]
    assert c["confidence"] == "medium"
    assert set(c["signals"]) == {"same_title", "year_within_1", "same_first_author"}
    assert c["suggested_keep"] == "openalex:W1"  # published over preprint


@pytest.mark.parametrize(
    "other",
    [
        _p("openalex:W2", TITLE, 2019),  # years too far apart
        _p("openalex:W2", TITLE, 2022, author="John Smith"),  # different first author
        _p("openalex:W2", TITLE + " part II", 2022),  # different title
    ],
)
def test_non_candidates(project, other):
    with _con(project) as con:
        _seed(con, _p("openalex:W1", TITLE, 2022), other)
        assert dedup.find_candidates(con) == []


def test_generic_titles_never_match(project):
    with _con(project) as con:
        _seed(con, _p("openalex:W1", "Editorial", 2022), _p("openalex:W2", "Editorial", 2022))
        assert dedup.find_candidates(con) == []


def test_missing_author_is_low_confidence(project):
    with _con(project) as con:
        _seed(con, _p("openalex:W1", TITLE, 2022), _p("scopus:1", TITLE, 2022, author=None))
        (c,) = dedup.find_candidates(con)
    assert c["confidence"] == "low" and "first_author_unknown" in c["signals"]


def test_shared_identifier_is_high_confidence(project):
    with _con(project) as con:
        _seed(con, _p("openalex:W1", "Title one is here", 2022, pmid="42"),
              _p("scopus:1", "Totally different title", 2022, pmid="42"))
        (c,) = dedup.find_candidates(con)
    assert c["confidence"] == "high" and c["signals"] == ["same_pmid"]


def test_dismiss_hides_pair(project):
    with _con(project) as con:
        _seed(con, _p("openalex:W1", TITLE, 2022), _p("openalex:W2", TITLE, 2022))
        dedup.dismiss(con, "openalex:W2", "openalex:W1", note="conference vs journal")
        assert dedup.find_candidates(con) == []


# --------------------------------------------------------------------------- #
# merge
# --------------------------------------------------------------------------- #


def _two_searches(root):
    with _con(root) as con:
        _seed(
            con,
            _p("openalex:W1", TITLE, 2022, venue="Ocean Eng", abstract=None,
               source_provenance={"openalex": {"work_id": "W1"}}),
            _p("scopus:1", TITLE, 2022, doi="10.1/pub", pmid="77", abstract="x" * 200,
               source_provenance={"scopus": {"eid": "e"}}),
        )
        db.record_search(con, "openalex", "q1", {}, ["openalex:W1"])
        db.record_search(con, "scopus", "q2", {}, ["scopus:1"])
        db.set_screening(con, "scopus:1", "included", "on topic")


def test_merge_semantics(project):
    _two_searches(project)
    before = report.prisma_data(project)
    assert before["n_duplicates_removed"] == 0 and before["n_unique"] == 2
    with _con(project) as con:
        r = dedup.merge(con, "openalex:W1", "scopus:1", reason="same paper")
        assert r["kept_id"] == "openalex:W1" and r["status"] == "included"
        kept = db.get_paper(con, "openalex:W1")
        assert kept.doi == "10.1/pub" and kept.pmid == "77"
        assert kept.abstract == "x" * 200
        assert kept.status == "included" and kept.screening_note == "on topic"
        assert kept.seen_in == 2
        assert set(kept.source_provenance) >= {"openalex", "scopus", "merged_from"}
        assert kept.source_provenance["merged_from"][0]["id"] == "scopus:1"
        # Alias: the removed id still resolves.
        assert db.get_paper(con, "scopus:1").id == "openalex:W1"
        assert dedup.find_candidates(con) == []
        assert dedup.list_merges(con)[0]["removed_id"] == "scopus:1"
    after = report.prisma_data(project)
    assert after["n_unique"] == 1
    assert after["n_duplicates_removed"] == 1
    assert after["n_duplicates_merged"] == 1
    assert after["identification"]["by_source"] == {"openalex": 1, "scopus": 1}
    assert "1 by reviewer merge" in report.render_prisma_svg(after)


def test_merge_with_unlinked_record_still_counts(project):
    with _con(project) as con:
        _seed(con, _p("openalex:W1", TITLE, 2022), _p("openalex:W2", TITLE, 2021))
        db.record_search(con, "openalex", "q", {}, ["openalex:W1"])  # W2 is unlinked (e.g. seed)
    assert report.prisma_data(project)["n_duplicates_removed"] == 0
    with _con(project) as con:
        dedup.merge(con, "openalex:W1", "openalex:W2")
    d = report.prisma_data(project)
    assert d["identification"]["n_total"] == 2
    assert d["n_unique"] == 1 and d["n_duplicates_removed"] == 1


def test_merge_refuses_conflicting_decisions(project):
    with _con(project) as con:
        _seed(con, _p("openalex:W1", TITLE, 2022), _p("openalex:W2", TITLE, 2022))
        db.set_screening(con, "openalex:W1", "included")
        db.set_screening(con, "openalex:W2", "excluded")
        with pytest.raises(ValueError, match="Screening conflict"):
            dedup.merge(con, "openalex:W1", "openalex:W2")
        with pytest.raises(ValueError, match="same record"):
            dedup.merge(con, "openalex:W1", "openalex:W1")


def test_merged_id_is_not_resurrected_by_later_search(project, router):
    _two_searches(project)
    with _con(project) as con:
        dedup.merge(con, "openalex:W1", "scopus:1")
        # A later snowball/search returning the removed id: upsert is a no-op,
        # and the search links to the kept record (no FK error).
        db.upsert_paper(con, _p("scopus:1", TITLE, 2022))
        db.record_search(con, "snowball", "backward:x", {}, ["scopus:1"])
        assert con.execute("SELECT COUNT(*) FROM paper").fetchone()[0] == 1
        assert db.get_paper(con, "openalex:W1").seen_in == 3
    # Same through run_search's find_existing path.
    with _con(project) as con:
        assert find_existing(con, Paper(id="scopus:1", title="t")) == "openalex:W1"


def test_merge_chain_repoints_aliases(project):
    with _con(project) as con:
        _seed(con, _p("a:1", TITLE, 2022), _p("a:2", TITLE, 2022), _p("a:3", TITLE, 2022))
        dedup.merge(con, "a:2", "a:3")
        dedup.merge(con, "a:1", "a:2")
        assert db.canonical_id(con, "a:3") == "a:1"
        assert db.get_paper(con, "a:3").id == "a:1"


# --------------------------------------------------------------------------- #
# schema on pre-existing DBs
# --------------------------------------------------------------------------- #


def test_old_db_gains_merge_tables(tmp_path):
    root = paths.init_project(tmp_path)
    dbf = paths.db_path(root)
    raw = sqlite3.connect(dbf)
    raw.executescript(
        "CREATE TABLE paper (id TEXT PRIMARY KEY, doi TEXT UNIQUE, title TEXT NOT NULL, abstract TEXT,"
        " year INTEGER, venue TEXT, authors_json TEXT NOT NULL DEFAULT '[]', cited_by_count INTEGER,"
        " source_provenance_json TEXT NOT NULL DEFAULT '{}',"
        " first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),"
        " last_updated_at TEXT NOT NULL DEFAULT (datetime('now')));"
        "INSERT INTO paper (id, title, year, authors_json) VALUES"
        " ('openalex:W1', 'Digital twins for ship hull monitoring', 2022, '[{\"name\": \"Jane Doe\"}]'),"
        " ('openalex:W2', 'Digital twins for ship hull monitoring', 2022, '[{\"name\": \"Jane Doe\"}]');"
    )
    raw.commit()
    raw.close()
    with db.connect(dbf) as con:
        assert len(dedup.find_candidates(con)) == 1
        dedup.merge(con, "openalex:W1", "openalex:W2")
    d = report.prisma_data(root)
    assert d["n_unique"] == 1 and d["n_duplicates_removed"] == 1


# --------------------------------------------------------------------------- #
# surfaces
# --------------------------------------------------------------------------- #


def test_http_endpoints(project, router, monkeypatch):
    create_app = server.create_app
    router.openalex["/works/doi:10.1/pub"] = _oa_work()
    with _con(project) as con:
        _seed(con, _p("scopus:1", TITLE, 2022))
    monkeypatch.setenv("SAARI_PROJECT_ROOT", str(project))
    client = TestClient(create_app())
    r = client.post("/api/papers", json={"identifiers": ["10.1/pub"]})
    assert r.status_code == 200 and r.json()["n_new"] == 1
    cands = client.get("/api/duplicates").json()["candidates"]
    assert len(cands) == 1
    r = client.post("/api/duplicates/merge", json={"keep": "openalex:W1", "remove": "scopus:1"})
    assert r.status_code == 200 and r.json()["removed_id"] == "scopus:1"
    r = client.post("/api/duplicates/dismiss", json={"a": "openalex:W1", "b": "openalex:W1"})
    assert r.status_code == 400


def test_cli_commands(project, router, monkeypatch):
    from typer.testing import CliRunner

    app = cli.app

    router.openalex["/works/doi:10.1/pub"] = _oa_work()
    with _con(project) as con:
        _seed(con, _p("scopus:1", TITLE, 2022))
    monkeypatch.setenv("SAARI_PROJECT_ROOT", str(project))
    runner = CliRunner()
    r = runner.invoke(app, ["papers", "add", "10.1/pub", "--note", "expert"])
    assert r.exit_code == 0, r.output
    assert "added" in r.output
    r = runner.invoke(app, ["papers", "duplicates"])
    assert r.exit_code == 0 and "scopus:1" in r.output
    r = runner.invoke(app, ["papers", "not-duplicate", "openalex:W1", "scopus:1"])
    assert r.exit_code == 0
    r = runner.invoke(app, ["papers", "duplicates"])
    assert "No candidate duplicates" in r.output
    r = runner.invoke(app, ["papers", "merge", "openalex:W1", "openalex:W1"])
    assert r.exit_code == 2


def test_mcp_tools_registered():
    names = {t.name for t in mcp_server.mcp._tool_manager.list_tools()}
    assert {"paper_add", "duplicates_list", "papers_merge", "duplicates_dismiss"} <= names


def test_run_search_still_links_doi_duplicates(project, router):
    with _con(project) as con:
        db.upsert_paper(con, Paper(id="scopus:1", title="T", doi="10.1/pub"))
    router.openalex["/works"] = {"results": [_oa_work()]}
    res = run_search("q", source="openalex", limit=5, project_root=project)
    assert res["paper_ids"] == ["scopus:1"] and res["n_duplicate"] == 1
