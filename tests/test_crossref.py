"""Tests for the Crossref source and identifier parsing. No network."""

from __future__ import annotations

import json

import httpx
import pytest

from saari import db, paths
from saari.models import Paper
from saari.sources import SOURCE_NAMES, crossref, run_search
from saari.sources.ids import IdentifierError, parse_identifier


def _item(doi="10.5555/ABC.1", title="A Crossref Paper", **over):
    w = {
        "DOI": doi,
        "title": [title],
        "author": [
            {"given": "Ada", "family": "Lovelace", "ORCID": "https://orcid.org/0000-0001", "affiliation": [{"name": "Uni"}]},
            {"name": "The Consortium"},
        ],
        "container-title": ["Journal of Tests"],
        "issued": {"date-parts": [[2023, 4]]},
        "is-referenced-by-count": 12,
        "URL": f"https://doi.org/{doi}",
        "type": "journal-article",
        "abstract": "<jats:title>Abstract</jats:title><jats:p>We study <jats:italic>things</jats:italic> &amp; stuff.</jats:p>",
        "link": [{"URL": "https://pub.example/x.pdf", "content-type": "application/pdf"}],
    }
    w.update(over)
    return w


def _search_page(items, total=None):
    return {"status": "ok", "message": {"total-results": total if total is not None else len(items), "items": items}}


def _patch(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(**kwargs)

    monkeypatch.setattr(crossref.httpx, "Client", fake_client)


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.delenv("CROSSREF_MAILTO", raising=False)
    monkeypatch.delenv("OPENALEX_MAILTO", raising=False)
    return paths.init_project(tmp_path)


# ---------- mapping ----------


def test_item_maps_to_paper():
    p = crossref._work_to_paper(_item())
    assert p is not None
    assert p.id == "crossref:10.5555/abc.1"
    assert p.doi == "10.5555/abc.1"
    assert p.year == 2023
    assert p.venue == "Journal of Tests"
    assert p.cited_by_count == 12
    assert p.authors[0].name == "Ada Lovelace"
    assert p.authors[0].orcid == "0000-0001"
    assert p.authors[0].affiliation == "Uni"
    assert p.authors[1].name == "The Consortium"
    assert p.abstract == "We study things & stuff."
    assert p.pdf_url == "https://pub.example/x.pdf"
    assert p.source_provenance["crossref"]["type"] == "journal-article"


def test_title_markup_and_subtitle():
    p = crossref._work_to_paper(_item(title="Abstract <i>algebra</i> methods", subtitle=["A survey"]))
    assert p.title == "Abstract algebra methods: A survey"


def test_item_without_title_is_skipped():
    assert crossref._work_to_paper(_item(title=None) | {"title": []}) is None


def test_arxiv_doi_sets_arxiv_id():
    p = crossref._work_to_paper(_item(doi="10.48550/arXiv.2101.00001"))
    assert p.arxiv_id == "2101.00001"


def test_strip_jats_plain_and_empty():
    assert crossref.strip_jats(None) is None
    assert crossref.strip_jats("<jats:p>  </jats:p>") is None
    assert crossref.strip_jats("<p>One.</p><p>Two.</p>") == "One. Two."


# ---------- search ----------


def test_registered_as_search_source():
    assert "crossref" in SOURCE_NAMES


def test_search_persists_and_dumps_raw(project, monkeypatch):
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json=_search_page([_item()]))

    _patch(monkeypatch, handler)
    result = run_search("ship digital twin", source="crossref", limit=5, year_from=2020, year_to=2024, project_root=project)
    assert result["source"] == "crossref" and result["n_new"] == 1
    assert result["paper_ids"] == ["crossref:10.5555/abc.1"]
    assert seen["params"]["query.bibliographic"] == "ship digital twin"
    assert seen["params"]["filter"] == "from-pub-date:2020,until-pub-date:2024"
    assert "mailto" not in seen["params"]  # no built-in address
    raw_files = list(paths.raw_dir(project, "crossref").glob("*.json"))
    assert len(raw_files) == 1 and "/" not in raw_files[0].name
    assert json.loads(raw_files[0].read_text())["DOI"] == "10.5555/ABC.1"


def test_mailto_from_env(project, monkeypatch):
    monkeypatch.setenv("CROSSREF_MAILTO", "ops@example.org")
    seen = {}

    def handler(req):
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json=_search_page([]))

    _patch(monkeypatch, handler)
    crossref.search("x", limit=3, project_root=project)
    assert seen["params"]["mailto"] == "ops@example.org"


def test_search_pagination(project, monkeypatch):
    pages = [
        _search_page([_item(doi=f"10.1/a{i}") for i in range(100)], total=120),
        _search_page([_item(doi=f"10.1/b{i}") for i in range(20)], total=120),
    ]
    offsets = []

    def handler(req):
        offsets.append(int(req.url.params["offset"]))
        return httpx.Response(200, json=pages[len(offsets) - 1])

    _patch(monkeypatch, handler)
    out = crossref.search("x", limit=120, project_root=project)
    assert len(out) == 120
    assert offsets == [0, 100]


def test_search_dedups_against_existing_doi(project, monkeypatch):
    with db.connect(paths.db_path(project)) as con:
        db.upsert_paper(con, Paper(id="openalex:W1", title="Same", doi="10.5555/abc.1"))
    _patch(monkeypatch, lambda req: httpx.Response(200, json=_search_page([_item()])))
    result = run_search("x", source="crossref", limit=5, project_root=project)
    assert result["n_new"] == 0 and result["paper_ids"] == ["openalex:W1"]


def test_rate_limit_is_diagnosed(project, monkeypatch):
    _patch(monkeypatch, lambda req: httpx.Response(429))
    with pytest.raises(crossref.CrossrefError, match="429"):
        crossref.search("x", limit=3, project_root=project)


# ---------- DOI lookup ----------


def test_fetch_by_doi(project, monkeypatch):
    def handler(req):
        assert req.url.path == "/works/10.5555/abc.1"
        return httpx.Response(200, json={"status": "ok", "message": _item()})

    _patch(monkeypatch, handler)
    paper, raw = crossref.fetch_by_doi("10.5555/ABC.1", project_root=project)
    assert paper.id == "crossref:10.5555/abc.1"
    assert (project / raw).exists()


def test_fetch_by_doi_404(project, monkeypatch):
    _patch(monkeypatch, lambda req: httpx.Response(404, text="Resource not found."))
    assert crossref.fetch_by_doi("10.5555/missing", project_root=project) is None


# ---------- identifier parsing ----------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("10.1000/XYZ.123", ("doi", "10.1000/xyz.123")),
        ("doi:10.1000/xyz", ("doi", "10.1000/xyz")),
        ("https://doi.org/10.1000/xyz", ("doi", "10.1000/xyz")),
        ("http://dx.doi.org/10.1000/a%2Fb", ("doi", "10.1000/a/b")),
        ("arXiv:2101.00001v2", ("arxiv", "2101.00001")),
        ("2101.00001", ("arxiv", "2101.00001")),
        ("https://arxiv.org/abs/2101.00001v3", ("arxiv", "2101.00001")),
        ("https://arxiv.org/pdf/2101.00001.pdf", ("arxiv", "2101.00001")),
        ("arxiv.org/abs/hep-th/9901001", ("arxiv", "hep-th/9901001")),
        ("pmid:29456894", ("pmid", "29456894")),
        ("PMID: 29456894", ("pmid", "29456894")),
        ("https://pubmed.ncbi.nlm.nih.gov/29456894/", ("pmid", "29456894")),
        ("W2741809807", ("openalex", "W2741809807")),
        ("openalex:W2741809807", ("openalex", "W2741809807")),
        ("https://openalex.org/W2741809807", ("openalex", "W2741809807")),
        ("https://api.openalex.org/works/w2741809807", ("openalex", "W2741809807")),
    ],
)
def test_parse_identifier(raw, expected):
    assert parse_identifier(raw) == expected


@pytest.mark.parametrize("raw", ["", "12345", "not an id", "doi:11.1/x", "pmid:abc", "https://example.org/paper"])
def test_parse_identifier_rejects(raw):
    with pytest.raises(IdentifierError):
        parse_identifier(raw)
