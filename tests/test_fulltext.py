"""Open-access full-text fetch + extract + paging. No network: httpx.MockTransport."""

from __future__ import annotations

import sqlite3

import httpx
import pytest

from saari import db, fulltext, paths, report
from saari.models import Location, Paper


def make_pdf(pages: list[str]) -> bytes:
    """Tiny valid PDF with one Helvetica text line per page (pypdf-extractable)."""
    objs: list[bytes] = []
    n = len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    font_id = 3 + 2 * n
    for i, text in enumerate(pages):
        content_id = 4 + 2 * i
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>".encode()
        )
        safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET".encode()
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


LONG = "Digital twins support maritime safety assessment. " * 12  # > 300 chars/page
PDF = make_pdf([LONG + "Hull stress monitoring.", LONG + "Ballast water results.", LONG])
HTML = b"<!DOCTYPE html><html><head><title>Article</title></head><body>landing</body></html>"


@pytest.fixture()
def root(tmp_path, monkeypatch):
    r = paths.init_project(tmp_path)
    monkeypatch.setenv("SAARI_PROJECT_ROOT", str(r))
    return r


def _add(root, status: str = "included", **kw) -> Paper:
    p = Paper(**{"title": "A paper", **kw})
    with db.connect(paths.db_path(root)) as con:
        db.upsert_paper(con, p)
        db.set_screening(con, p.id, status)
    return p


def _client(routes: dict[str, tuple[int, str, bytes]], calls: list[str] | None = None):
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if calls is not None:
            calls.append(url)
        if url not in routes:
            return httpx.Response(404, text="nope")
        code, ctype, body = routes[url]
        return httpx.Response(code, headers={"content-type": ctype}, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_candidates_order_and_oa_only():
    p = Paper(
        id="openalex:W1", title="t", arxiv_id="2401.00001", pmcid="PMC123",
        oa_status="green", pdf_url="https://repo.example/a.pdf",
        locations=[
            Location(pdf_url="https://pub.example/closed.pdf", is_oa=False),
            Location(pdf_url="https://repo.example/a.pdf", is_oa=True, version="acceptedVersion"),
            Location(pdf_url="https://pub.example/vor.pdf", is_oa=True, version="publishedVersion"),
        ],
    )
    urls = [c.url for c in fulltext.candidates(p)]
    assert urls == [
        "https://pub.example/vor.pdf",
        "https://repo.example/a.pdf",
        "https://europepmc.org/articles/PMC123?pdf=render",
        "https://arxiv.org/pdf/2401.00001",
    ]
    assert "https://pub.example/closed.pdf" not in urls


def test_closed_record_pdf_url_not_tried():
    p = Paper(id="x", title="t", oa_status="closed", pdf_url="https://pub.example/p.pdf")
    assert fulltext.candidates(p) == []


def test_fetch_html_landing_then_arxiv_pdf_and_cache(root):
    _add(root, id="openalex:W1", arxiv_id="2401.00001", oa_status="green",
         locations=[Location(pdf_url="https://pub.example/landing", is_oa=True)])
    calls: list[str] = []
    client = _client({
        "https://pub.example/landing": (200, "text/html", HTML),
        "https://arxiv.org/pdf/2401.00001": (200, "application/pdf", PDF),
    }, calls)

    r = fulltext.fetch("openalex:W1", project_root=root, client=client)
    assert r["status"] == "available"
    assert r["source_kind"] == "arxiv" and r["source_url"].startswith("https://arxiv.org/pdf/")
    assert r["n_pages"] == 3 and r["quality"] == "good"
    assert r["attempts"][0]["ok"] is False and "HTML" in r["attempts"][0]["error"]
    assert (root / ".saaristo" / "fulltext" / "openalex_W1.pdf").read_bytes() == PDF
    txt = (root / ".saaristo" / "fulltext" / "openalex_W1.txt").read_text()
    assert "Hull stress monitoring" in txt and "<html" not in txt.lower()

    # repeat call: no network
    n = len(calls)
    again = fulltext.fetch("openalex:W1", project_root=root, client=client)
    assert again["cached"] is True and again["status"] == "available"
    assert len(calls) == n


def test_unavailable_when_no_oa_location(root):
    _add(root, id="openalex:W2", oa_status="closed")
    r = fulltext.fetch("openalex:W2", project_root=root, client=_client({}))
    assert r["status"] == "unavailable"
    assert "oa_status=closed" in r["reason"]


def test_failed_records_reason_and_refresh_retries(root):
    _add(root, id="openalex:W3", pdf_url="https://pub.example/x.pdf", oa_status="gold")
    r = fulltext.fetch("openalex:W3", project_root=root, client=_client({
        "https://pub.example/x.pdf": (403, "text/html", HTML),
    }))
    assert r["status"] == "failed" and "HTTP 403" in r["reason"]

    ok = _client({"https://pub.example/x.pdf": (200, "application/pdf", PDF)})
    assert fulltext.fetch("openalex:W3", project_root=root, client=ok)["status"] == "failed"
    assert fulltext.fetch("openalex:W3", refresh=True, project_root=root, client=ok)["status"] == "available"


def test_html_with_pdf_content_type_is_not_stored(root):
    _add(root, id="openalex:W4", pdf_url="https://pub.example/y.pdf", oa_status="gold")
    r = fulltext.fetch("openalex:W4", project_root=root, client=_client({
        "https://pub.example/y.pdf": (200, "application/pdf", HTML),
    }))
    assert r["status"] == "failed"
    assert r["text_path"] is None and r["pdf_path"] is None
    assert not (root / ".saaristo" / "fulltext" / "openalex_W4.txt").exists()


def test_size_cap(root):
    _add(root, id="openalex:W5", pdf_url="https://pub.example/big.pdf", oa_status="gold")
    r = fulltext.fetch("openalex:W5", project_root=root, max_bytes=100, client=_client({
        "https://pub.example/big.pdf": (200, "application/pdf", PDF),
    }))
    assert r["status"] == "failed" and "too large" in r["reason"]


def test_scanned_pdf_without_text_fails_honestly(root):
    _add(root, id="openalex:W6", pdf_url="https://pub.example/scan.pdf", oa_status="gold")
    r = fulltext.fetch("openalex:W6", project_root=root, client=_client({
        "https://pub.example/scan.pdf": (200, "application/pdf", make_pdf(["", ""])),
    }))
    assert r["status"] == "failed" and "OCR" in r["reason"]


def test_read_text_paging_and_grep(root):
    _add(root, id="openalex:W7", pdf_url="https://pub.example/p.pdf", oa_status="gold")
    client = _client({"https://pub.example/p.pdf": (200, "application/pdf", PDF)})

    first = fulltext.read_text("openalex:W7", max_chars=100, project_root=root, client=client)
    assert first["status"] == "available" and first["n_returned"] == 100
    assert first["next_offset"] == 100 and first["page_start"] == 1
    assert "attempts" not in first

    total = first["n_chars"]
    last = fulltext.read_text("openalex:W7", offset=total - 10, max_chars=100, project_root=root)
    assert last["n_returned"] == 10 and last["next_offset"] is None and last["page_end"] == 3

    g = fulltext.read_text("openalex:W7", grep="ballast WATER", project_root=root)
    assert g["n_hits"] == 1 and g["hits"][0]["page"] == 2
    assert "Ballast water" in g["hits"][0]["snippet"]
    # invalid regex is matched literally rather than raising
    assert fulltext.read_text("openalex:W7", grep="(unclosed", project_root=root)["n_hits"] == 0


def test_read_text_no_fetch_reports_not_fetched(root):
    _add(root, id="openalex:W8", arxiv_id="2401.1")
    r = fulltext.read_text("openalex:W8", fetch_if_missing=False, project_root=root)
    assert r["status"] is None and "not fetched" in r["reason"]


def test_unknown_paper_raises(root):
    with pytest.raises(KeyError):
        fulltext.read_text("nope", project_root=root)


def test_fetch_many_summary_and_coverage(root):
    _add(root, id="openalex:A", pdf_url="https://pub.example/a.pdf", oa_status="gold")
    _add(root, id="openalex:B", oa_status="closed")
    _add(root, id="openalex:C", pdf_url="https://pub.example/c.pdf", oa_status="gold")
    with db.connect(paths.db_path(root)) as con:
        db.upsert_paper(con, Paper(id="openalex:D", title="cand", arxiv_id="1"))  # candidate
    client = _client({
        "https://pub.example/a.pdf": (200, "application/pdf", PDF),
        "https://pub.example/c.pdf": (500, "text/plain", b"boom"),
    })
    s = fulltext.fetch_many(project_root=root, client=client, min_interval=0)
    assert (s["n_papers"], s["n_available"], s["n_unavailable"], s["n_failed"]) == (3, 1, 1, 1)
    assert {m["paper_id"] for m in s["missing"]} == {"openalex:B", "openalex:C"}

    again = fulltext.fetch_many(project_root=root, client=client, min_interval=0)
    assert again["n_from_cache"] == 3

    cov = fulltext.coverage(root)
    assert cov == {"n_papers": 3, "n_available": 1, "n_unavailable": 1, "n_failed": 1}


def test_limitations_states_fulltext_coverage_without_overclaiming():
    none = report.limitations_block(None, fulltext={"n_papers": 5, "n_available": 0})
    assert "Title/abstract screening only" in none
    some = report.limitations_block(None, fulltext={"n_papers": 5, "n_available": 2})
    assert "retrieved for 2 of 5 included papers" in some
    assert "other 3 had no retrieved full text" in some
    assert "Retrieval is not assessment" in some


def test_schema_added_to_existing_db(tmp_path):
    """An older project DB (no paper_fulltext table) gains it on connect."""
    path = tmp_path / ".saaristo" / "saari.db"
    path.parent.mkdir(parents=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE paper (id TEXT PRIMARY KEY, doi TEXT, title TEXT NOT NULL, "
                "abstract TEXT, year INTEGER, venue TEXT, authors_json TEXT NOT NULL DEFAULT '[]', "
                "cited_by_count INTEGER, source_provenance_json TEXT NOT NULL DEFAULT '{}', "
                "first_seen_at TEXT NOT NULL DEFAULT (datetime('now')), "
                "last_updated_at TEXT NOT NULL DEFAULT (datetime('now')))")
    con.commit()
    con.close()
    with db.connect(path) as c2:
        cols = {r["name"] for r in c2.execute("PRAGMA table_info(paper_fulltext)")}
    assert {"status", "reason", "text_path", "n_chars"} <= cols


def test_missing_extra_gives_actionable_error(root, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "pypdf":
            raise ImportError("no pypdf")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    _add(root, id="openalex:W9", arxiv_id="2401.2")
    calls: list[str] = []
    with pytest.raises(fulltext.FulltextDependencyError, match="saari\\[fulltext\\]"):
        fulltext.fetch("openalex:W9", project_root=root, client=_client({}, calls))
    assert calls == []  # failed before any network
    with db.connect(paths.db_path(root)) as con:
        assert con.execute("SELECT COUNT(*) FROM paper_fulltext").fetchone()[0] == 0


def test_adapters_expose_fulltext(root, monkeypatch):
    """MCP tool + HTTP route + CLI command exist and agree on the cached result."""
    _add(root, id="openalex:W10", pdf_url="https://pub.example/p.pdf", oa_status="gold")
    fulltext.fetch("openalex:W10", project_root=root, client=_client({
        "https://pub.example/p.pdf": (200, "application/pdf", PDF),
    }))

    from saari import mcp_server

    m = mcp_server.paper_text("openalex:W10", max_chars=50, fetch=False)
    assert m["status"] == "available" and m["n_returned"] == 50
    assert "error" in mcp_server.paper_text("missing-id")

    from fastapi.testclient import TestClient

    from saari.server import create_app

    c = TestClient(create_app())
    h = c.get("/api/fulltext/openalex:W10", params={"grep": "hull", "fetch": False})
    assert h.status_code == 200 and h.json()["n_hits"] == 1
    assert c.get("/api/fulltext/missing-id").status_code == 404

    from typer.testing import CliRunner

    from saari.cli import app

    out = CliRunner().invoke(app, ["papers", "text", "openalex:W10", "--no-fetch", "-n", "40"])
    assert out.exit_code == 0, out.output
    assert "Digital twins" in out.output
