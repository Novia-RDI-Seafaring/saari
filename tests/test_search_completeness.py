"""Search completeness and honesty: paging, totals, caps, retries, snowball
accounting, persistence, and reporting. All HTTP is mocked (no network)."""

from __future__ import annotations

import json
import logging
import sqlite3

import httpx
import pytest

from saari import db, paths, report, study
from saari.sources import SearchTooLarge, openalex, run_search, scopus
from saari.sources.common import SearchHits
from saari.sources.openalex import OpenAlexError


def _work(i: int, refs: list[str] | None = None) -> dict:
    return {
        "id": f"https://openalex.org/W{i}",
        "title": f"Paper {i}",
        "publication_year": 2020,
        "referenced_works": [f"https://openalex.org/{r}" for r in (refs or [])],
    }


class FakeOpenAlex:
    """Serves /works list queries from an in-memory result set.

    - `total`: what meta.count reports (and how many works exist).
    - supports basic `page` paging and `cursor` paging (cursor = offset).
    - `openalex_id:` filter returns the requested ids that exist.
    - `script`: optional list of status codes / exceptions served first.
    """

    def __init__(self, total: int = 0, known_ids: set[str] | None = None, script=None):
        self.total = total
        self.known_ids = known_ids
        self.script = list(script or [])
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            if isinstance(item, tuple):
                code, headers = item
                return httpx.Response(code, headers=headers, json={"error": "canned"})
            return httpx.Response(item, json={"error": "canned"})
        q = request.url.params
        if request.url.path.startswith("/works/"):
            wid = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=_work(int(wid[1:])))
        flt = q.get("filter", "")
        if flt.startswith("openalex_id:"):
            ids = flt.removeprefix("openalex_id:").split("|")
            known = self.known_ids if self.known_ids is not None else set(ids)
            res = [_work(int(i[1:])) for i in ids if i in known]
            return httpx.Response(200, json={"meta": {"count": len(res)}, "results": res})
        per_page = int(q.get("per_page", 25))
        if "cursor" in q:
            if q["cursor"] != "*" and "api_key" not in q:
                return httpx.Response(401, json={"message": "API key required for cursor paging"})
            offset = 0 if q["cursor"] == "*" else int(q["cursor"])
        else:
            offset = (int(q.get("page", 1)) - 1) * per_page
        end = min(offset + per_page, self.total)
        res = [_work(10_000 + i) for i in range(offset, end)]
        meta = {"count": self.total, "per_page": per_page}
        if "cursor" in q:
            meta["next_cursor"] = str(end) if end < self.total else None
        return httpx.Response(200, json={"meta": meta, "results": res})


@pytest.fixture()
def project(tmp_path, monkeypatch):
    root = paths.init_project(tmp_path)
    for var in ("OPENALEX_API_KEY", "OPENALEX_MAILTO", "SAARI_SEARCH_MAX_RECORDS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SAARI_PROJECT_ROOT", str(root))
    return root


@pytest.fixture()
def sleeps(monkeypatch):
    seen: list[float] = []
    monkeypatch.setattr(openalex, "_sleep", seen.append)
    return seen


def _install(monkeypatch, fake: FakeOpenAlex) -> FakeOpenAlex:
    transport = httpx.MockTransport(fake.handler)
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(**kwargs)

    monkeypatch.setattr(openalex.httpx, "Client", fake_client)
    return fake


# --------------------------------------------------------------------------- #
# OpenAlex search: totals, paging, scope, caps
# --------------------------------------------------------------------------- #

def test_search_reports_total_and_truncation(project, monkeypatch):
    fake = _install(monkeypatch, FakeOpenAlex(total=16_616))
    hits = openalex.search("digital twin", limit=25, project_root=project)
    assert isinstance(hits, SearchHits)
    assert len(hits) == 25
    assert hits.n_available == 16_616
    assert hits.truncated is True
    assert len(fake.requests) == 1


def test_search_pages_past_100_without_key(project, monkeypatch):
    fake = _install(monkeypatch, FakeOpenAlex(total=1000))
    hits = openalex.search("q", limit=250, project_root=project)
    assert len(hits) == 250
    assert len({p.id for p, _ in hits}) == 250  # no duplicate pages
    sent = [dict(r.url.params) for r in fake.requests]
    assert [s["per_page"] for s in sent] == ["100", "100", "100"]
    assert [s["page"] for s in sent] == ["1", "2", "3"]
    assert all("cursor" not in s and "api_key" not in s for s in sent)


def test_search_uses_cursor_paging_with_api_key(project, monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "secret-key")
    fake = _install(monkeypatch, FakeOpenAlex(total=230))
    hits = openalex.search("q", fetch_all=True, project_root=project)
    assert len(hits) == 230
    assert hits.truncated is False
    list_reqs = [dict(r.url.params) for r in fake.requests]
    # first a 1-record count probe, then cursor pages
    assert list_reqs[0]["per_page"] == "1"
    assert [r["cursor"] for r in list_reqs[1:]] == ["*", "100", "200"]
    assert all(r["api_key"] == "secret-key" for r in list_reqs)


def test_api_key_is_not_logged(project, monkeypatch, caplog):
    monkeypatch.setenv("OPENALEX_API_KEY", "secret-key")
    _install(monkeypatch, FakeOpenAlex(total=3))
    with caplog.at_level(logging.DEBUG, logger="saari.sources.openalex"):
        openalex.search("q", project_root=project)
    assert "secret-key" not in caplog.text
    assert "openalex GET" in caplog.text


def test_keyless_basic_paging_limit_is_flagged(project, monkeypatch):
    monkeypatch.setattr(openalex, "BASIC_PAGING_LIMIT", 200)
    _install(monkeypatch, FakeOpenAlex(total=500))
    hits = openalex.search("q", fetch_all=True, max_records=1000, project_root=project)
    assert len(hits) == 200
    assert hits.n_available == 500
    assert hits.truncated is True
    assert any("OPENALEX_API_KEY" in n for n in hits.notes)


def test_fetch_all_refuses_above_cap_and_persists_nothing(project, monkeypatch):
    fake = _install(monkeypatch, FakeOpenAlex(total=16_616))
    with pytest.raises(SearchTooLarge, match="16616 records available"):
        run_search("big", fetch_all=True, project_root=project)
    assert len(fake.requests) == 1  # only the count probe
    with db.connect(paths.db_path(project)) as con:
        assert con.execute("SELECT COUNT(*) FROM search").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM paper").fetchone()[0] == 0


def test_cap_is_configurable_by_env(project, monkeypatch):
    monkeypatch.setenv("SAARI_SEARCH_MAX_RECORDS", "50")
    fake = _install(monkeypatch, FakeOpenAlex(total=100))
    with pytest.raises(SearchTooLarge, match="safety cap of 50"):
        openalex.search("q", limit=60, project_root=project)
    assert fake.requests == []  # refused before any request
    hits = openalex.search("q", limit=50, project_root=project)
    assert len(hits) == 50


def test_title_abstract_scope_param(project, monkeypatch):
    fake = _install(monkeypatch, FakeOpenAlex(total=5))
    openalex.search("ship twin", scope="title_abstract", project_root=project)
    params = dict(fake.requests[0].url.params)
    assert params["search.title_and_abstract"] == "ship twin"
    assert "search" not in params
    openalex.search("ship twin", project_root=project)
    assert dict(fake.requests[1].url.params)["search"] == "ship twin"
    with pytest.raises(ValueError):
        openalex.search("x", scope="fulltext", project_root=project)


def test_no_mailto_sent_by_default(project, monkeypatch):
    fake = _install(monkeypatch, FakeOpenAlex(total=1))
    openalex.search("q", project_root=project)
    assert "mailto" not in dict(fake.requests[0].url.params)
    monkeypatch.setenv("OPENALEX_MAILTO", "ops@example.org")
    openalex.search("q", project_root=project)
    assert dict(fake.requests[1].url.params)["mailto"] == "ops@example.org"


# --------------------------------------------------------------------------- #
# Retry / backoff
# --------------------------------------------------------------------------- #

def test_retry_on_429_honors_retry_after(project, monkeypatch, sleeps):
    fake = _install(monkeypatch, FakeOpenAlex(total=3, script=[(429, {"Retry-After": "7"}), 503]))
    hits = openalex.search("q", project_root=project)
    assert len(hits) == 3
    assert len(fake.requests) == 3
    assert sleeps == [7.0, 2.0]  # Retry-After, then exponential (attempt 2)


def test_retry_on_timeout_then_success(project, monkeypatch, sleeps):
    fake = _install(monkeypatch, FakeOpenAlex(total=2, script=[httpx.ReadTimeout("slow")]))
    assert len(openalex.search("q", project_root=project)) == 2
    assert len(fake.requests) == 2
    assert sleeps == [1.0]


def test_retry_gives_up_with_clear_error(project, monkeypatch, sleeps):
    _install(monkeypatch, FakeOpenAlex(total=2, script=[429] * openalex.MAX_ATTEMPTS))
    with pytest.raises(OpenAlexError, match="OPENALEX_API_KEY"):
        openalex.search("q", project_root=project)
    assert len(sleeps) == openalex.MAX_ATTEMPTS - 1


def test_no_retry_on_client_error(project, monkeypatch, sleeps):
    fake = _install(monkeypatch, FakeOpenAlex(total=2, script=[400]))
    with pytest.raises(httpx.HTTPStatusError):
        openalex.search("q", project_root=project)
    assert len(fake.requests) == 1
    assert sleeps == []


def test_keyless_cursor_401_is_explained(project, monkeypatch):
    fake = FakeOpenAlex(total=300)
    _install(monkeypatch, fake)
    client = httpx.Client()
    with pytest.raises(OpenAlexError, match="401"):
        openalex._check(openalex._get(client, f"{openalex.BASE_URL}/works", {"cursor": "100"}))


# --------------------------------------------------------------------------- #
# Persistence + surfaces
# --------------------------------------------------------------------------- #

def test_run_search_persists_completeness(project, monkeypatch):
    _install(monkeypatch, FakeOpenAlex(total=16_616))
    res = run_search("q", limit=25, scope="title_abstract", project_root=project)
    assert res["n_fetched"] == 25
    assert res["n_available"] == 16_616
    assert res["truncated"] is True
    assert res["scope"] == "title_abstract"
    with db.connect(paths.db_path(project)) as con:
        row = db.list_searches(con)[0]
    assert row["n_returned"] == 25
    assert row["n_available"] == 16_616
    assert row["truncated"] == 1
    assert json.loads(row["params_json"])["scope"] == "title_abstract"


def test_complete_search_not_truncated(project, monkeypatch):
    _install(monkeypatch, FakeOpenAlex(total=7))
    res = run_search("q", limit=25, project_root=project)
    assert (res["n_fetched"], res["n_available"], res["truncated"]) == (7, 7, False)


def test_old_search_table_migrates(tmp_path):
    root = paths.init_project(tmp_path)
    dbp = paths.db_path(root)
    dbp.unlink(missing_ok=True)
    con = sqlite3.connect(dbp)
    con.executescript(
        "CREATE TABLE search (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, "
        "query TEXT NOT NULL, params_json TEXT NOT NULL DEFAULT '{}', n_returned INTEGER, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')));"
        "INSERT INTO search (source, query, n_returned) VALUES ('openalex', 'old', 25);"
    )
    con.commit()
    con.close()
    with db.connect(dbp) as con:
        rows = db.list_searches(con)
    assert rows[0]["n_available"] is None and rows[0]["truncated"] is None
    f = study.funnel(root)
    assert f["searches"][0]["n_available"] is None


def test_http_and_mcp_surfaces_expose_completeness(project, monkeypatch):
    from fastapi.testclient import TestClient

    from saari import mcp_server
    from saari.server import create_app

    _install(monkeypatch, FakeOpenAlex(total=40))
    client = TestClient(create_app())
    body = client.post("/api/searches", json={"query": "q", "limit": 10}).json()
    assert body["n_available"] == 40 and body["truncated"] is True
    r = client.post("/api/searches", json={"query": "q", "fetch_all": True, "max_records": 5})
    assert r.status_code == 400 and "safety cap" in r.json()["detail"]

    tool = getattr(mcp_server.search, "fn", mcp_server.search)
    out = tool("q", limit=40, scope="title_abstract")
    assert out["n_available"] == 40 and out["truncated"] is False


# --------------------------------------------------------------------------- #
# Snowball
# --------------------------------------------------------------------------- #

def _seed(project, n_refs: int) -> str:
    from saari.models import Paper

    seed = Paper(
        id="openalex:W1",
        title="Seed",
        referenced_works=[f"W{100 + i}" for i in range(n_refs)],
    )
    with db.connect(paths.db_path(project)) as con:
        db.upsert_paper(con, seed)
    return seed.id


def test_snowball_backward_takes_all_references_by_default(project, monkeypatch):
    from saari.snowball import snowball

    seed = _seed(project, 60)
    fake = _install(monkeypatch, FakeOpenAlex())
    r = snowball(seed, direction="backward", project_root=project)
    assert len(r.backward_paper_ids) == 60
    assert r.backward.n_available == 60
    assert r.backward.truncated is False
    assert r.truncated is False
    assert len(fake.requests) == 2  # 50-id chunks


def test_snowball_backward_limit_is_flagged(project, monkeypatch):
    from saari.snowball import snowball

    seed = _seed(project, 60)
    _install(monkeypatch, FakeOpenAlex())
    r = snowball(seed, direction="backward", max_backward=10, project_root=project)
    assert r.backward.n_retrieved == 10
    assert r.backward.truncated is True
    assert r.completeness()["backward"]["n_available"] == 60


def test_snowball_backward_missing_ids_noted_not_truncated(project, monkeypatch):
    from saari.snowball import snowball

    seed = _seed(project, 5)
    _install(monkeypatch, FakeOpenAlex(known_ids={"W100", "W101", "W102"}))
    r = snowball(seed, direction="backward", project_root=project)
    assert r.backward.n_retrieved == 3
    assert r.backward.truncated is False
    assert "2 referenced work(s) not returned" in r.backward.note


def test_snowball_forward_reports_total_and_persists(project, monkeypatch):
    from saari.snowball import snowball

    seed = _seed(project, 2)
    _install(monkeypatch, FakeOpenAlex(total=1439))
    r = snowball(seed, direction="both", project_root=project)
    assert r.forward.n_available == 1439
    assert r.forward.n_retrieved == 25
    assert r.forward.truncated is True
    assert r.n_available == 2 + 1439
    assert r.truncated is True
    with db.connect(paths.db_path(project)) as con:
        row = db.list_searches(con)[0]
    assert row["source"] == "snowball"
    assert row["n_available"] == 1441 and row["truncated"] == 1
    params = json.loads(row["params_json"])
    assert params["forward"]["n_available"] == 1439
    assert params["backward"]["truncated"] is False


def test_snowball_forward_all_over_cap_is_flagged(project, monkeypatch):
    from saari.snowball import snowball

    seed = _seed(project, 0)
    _install(monkeypatch, FakeOpenAlex(total=300))
    r = snowball(seed, direction="forward", max_forward=None, max_records=120, project_root=project)
    assert r.forward.n_retrieved == 120
    assert r.forward.truncated is True
    assert "safety cap" in r.forward.note


def test_snowball_forward_all_under_cap_pages(project, monkeypatch):
    from saari.snowball import snowball

    seed = _seed(project, 0)
    _install(monkeypatch, FakeOpenAlex(total=230))
    r = snowball(seed, direction="forward", max_forward=None, project_root=project)
    assert r.forward.n_retrieved == 230
    assert r.forward.truncated is False


def test_snowball_legacy_max_per_direction(project, monkeypatch):
    from saari.snowball import snowball

    seed = _seed(project, 40)
    _install(monkeypatch, FakeOpenAlex(total=100))
    r = snowball(seed, direction="both", max_per_direction=5, project_root=project)
    assert (r.backward.n_retrieved, r.forward.n_retrieved) == (5, 5)


# --------------------------------------------------------------------------- #
# Scopus totals
# --------------------------------------------------------------------------- #

def _scopus_page(n, total, start=0):
    entries = [
        {"dc:identifier": f"SCOPUS_ID:{start + i}", "dc:title": f"S{start + i}"}
        for i in range(n)
    ]
    return {"search-results": {"opensearch:totalResults": str(total), "entry": entries}}


def _install_scopus(monkeypatch, total):
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        start = int(request.url.params["start"])
        count = int(request.url.params["count"])
        return httpx.Response(200, json=_scopus_page(min(count, total - start), total, start))

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr(
        scopus.httpx, "Client", lambda *a, **kw: real_client(**{**kw, "transport": transport})
    )
    monkeypatch.setenv("SCOPUS_API_KEY", "k")
    return calls


def test_scopus_reports_total(project, monkeypatch):
    _install_scopus(monkeypatch, total=900)
    hits = scopus.search("q", limit=30, project_root=project)
    assert len(hits) == 30 and hits.n_available == 900 and hits.truncated is True


def test_scopus_fetch_all_and_cap(project, monkeypatch):
    calls = _install_scopus(monkeypatch, total=60)
    hits = scopus.search("q", fetch_all=True, project_root=project)
    assert len(hits) == 60 and hits.truncated is False
    assert "TITLE-ABS-KEY(q)" in calls[0].url.params["query"]
    with pytest.raises(SearchTooLarge):
        scopus.search("q", fetch_all=True, max_records=10, project_root=project)
    scopus.search("q", scope="title_abstract", project_root=project)
    assert calls[-1].url.params["query"] == "TITLE-ABS(q)"


# --------------------------------------------------------------------------- #
# Reporting honesty
# --------------------------------------------------------------------------- #

FUNNEL_TRUNC = {
    "searches": [
        {"source": "openalex", "query": "big", "n_returned": 25, "n_available": 16616,
         "truncated": 1, "created_at": "2026-11-01T10:00:00"},
        {"source": "openalex", "query": "small", "n_returned": 7, "n_available": 7,
         "truncated": 0, "created_at": "2026-11-01T10:01:00"},
        {"source": "snowball", "query": "both:openalex:W1", "n_returned": 50,
         "n_available": 1441, "truncated": 1, "created_at": "2026-11-01T10:02:00"},
    ],
    "n_fetched": 82,
    "n_unique": 80,
    "by_status": {"included": 3, "excluded": 2, "candidate": 75},
}


def test_prisma_discloses_truncation():
    d = report.build_prisma(FUNNEL_TRUNC, [])
    c = d["identification"]["completeness"]
    assert c["n_truncated"] == 2
    assert c["n_retrieved_known"] == 82
    assert c["n_available_known"] == 16616 + 7 + 1441
    for rendered in (report.render_prisma_mermaid(d), report.render_prisma_svg(d)):
        assert "retrieved 82 of 18064 available" in rendered
        assert "2 search(es) truncated" in rendered


def test_limitations_list_truncated_searches():
    d = report.build_prisma(FUNNEL_TRUNC, [])
    block = report.limitations_block(
        d["identification"]["by_source"], d["identification"]["completeness"]
    )
    assert "Truncated retrieval" in block
    assert "`big`: retrieved 25 of 16616 available" in block
    assert "Complete retrieval" not in block
    md = report.build_paper_md({}, d, FUNNEL_TRUNC["searches"], {}, [])
    assert "Truncated retrieval" in md
    assert "| 25 | 16616 (truncated) |" in md
    assert "| 7 | 7 |" in md
    slides = report.build_slides_md({}, d, {})
    assert "Retrieval completeness: retrieved 82 of 18064" in slides


def test_old_records_disclosed_as_not_recorded():
    old = {"searches": [{"source": "openalex", "query": "q", "n_returned": 25}],
           "n_fetched": 25, "n_unique": 25, "by_status": {}}
    d = report.build_prisma(old, [])
    block = report.limitations_block(None, d["identification"]["completeness"])
    assert "Completeness not recorded" in block
    assert "1 with total not recorded" in report.render_prisma_mermaid(d)
    assert "not recorded" in report.search_strategy_table(old["searches"])


def test_complete_retrieval_stated_and_no_prisma_noise():
    full = {"searches": [{"source": "openalex", "query": "q", "n_returned": 7,
                          "n_available": 7, "truncated": 0}],
            "n_fetched": 7, "n_unique": 7, "by_status": {}}
    d = report.build_prisma(full, [])
    assert "Complete retrieval" in report.limitations_block(
        None, d["identification"]["completeness"]
    )
    assert "available" not in report.render_prisma_mermaid(d)
    # No searches at all: claim nothing either way.
    assert report.completeness_bullets(report.search_completeness([])) == ""


def test_prisma_end_to_end_from_db(project, monkeypatch):
    _install(monkeypatch, FakeOpenAlex(total=500))
    run_search("q", limit=20, project_root=project)
    d = report.prisma_data(project)
    assert d["identification"]["completeness"]["truncated"][0]["n_available"] == 500
