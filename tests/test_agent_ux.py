"""Agent-UX ops: triage sort orders, the workflow guide, snowball_included.

No network and no embedding model: vectors are written straight into
paper_vec and `embed_text` is monkeypatched to a fixed unit vector.
"""

from __future__ import annotations

import pytest
import sqlite_vec

from saari import db, paths, study
from saari import snowball as snowball_mod
from saari.embed import EMBED_DIM
from saari.models import Paper


def _vec(*weights: float) -> list[float]:
    v = [0.0] * EMBED_DIM
    for i, w in enumerate(weights):
        v[i] = w
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v]


AXIS0 = _vec(1.0)


@pytest.fixture()
def seen_texts():
    return []


@pytest.fixture()
def root(tmp_path, monkeypatch, seen_texts):
    root = paths.init_project(tmp_path)
    with db.connect(paths.db_path(root)) as con:
        # famous: highly cited, off-topic (axis 1)
        # young: barely cited, on-topic (axis 0), found by two searches
        # mid: in between on both
        # raw: not embedded
        papers = [
            Paper(id="openalex:Wfamous", title="Famous", year=2010, cited_by_count=5000),
            Paper(id="openalex:Wyoung", title="Young", year=2025, cited_by_count=1),
            Paper(id="openalex:Wmid", title="Mid", year=2020, cited_by_count=100),
            Paper(id="openalex:Wraw", title="Raw", year=2024, cited_by_count=50),
            Paper(id="openalex:Winc", title="Included", year=2022, cited_by_count=10),
        ]
        for p in papers:
            db.upsert_paper(con, p)
        db.record_search(con, "openalex", "q1", {}, [p.id for p in papers])
        db.record_search(con, "openalex", "q2", {}, ["openalex:Wyoung", "openalex:Wmid"])
        db.record_search(con, "openalex", "q3", {}, ["openalex:Wmid"])
        for pid, vec in [
            ("openalex:Wfamous", _vec(0.0, 1.0)),
            ("openalex:Wyoung", _vec(1.0, 0.1)),
            ("openalex:Wmid", _vec(1.0, 1.0)),
            ("openalex:Winc", _vec(0.5, 0.5)),
        ]:
            db.upsert_embedding(con, pid, sqlite_vec.serialize_float32(vec), "test", EMBED_DIM)
        db.set_screening(con, "openalex:Winc", "included", "on-topic")

    def fake_embed_text(text, model_name=None):
        seen_texts.append(text)
        return AXIS0

    monkeypatch.setattr("saari.triage.embed_text", fake_embed_text)
    monkeypatch.setenv("SAARI_PROJECT_ROOT", str(root))
    return root


def _ids(ranked):
    return [p.id.split(":W")[1] for p in ranked.papers]


# ---------- triage ranking ----------


def test_sort_cited_puts_famous_first(root):
    from saari.triage import rank_papers

    r = rank_papers(sort="cited", project_root=root)
    assert _ids(r) == ["famous", "mid", "raw", "young"]
    assert r.sort == "cited" and r.scores == {}


def test_sort_seen_in_uses_overlap_then_citations(root):
    from saari.triage import rank_papers

    r = rank_papers(sort="seen_in", project_root=root)
    assert _ids(r) == ["mid", "young", "famous", "raw"]


def test_relevance_by_query_scores_and_puts_unembedded_last(root, seen_texts):
    from saari.triage import rank_papers

    r = rank_papers(sort="relevance", query="on-topic prose", project_root=root)
    assert r.sort == "relevance" and r.basis == "query"
    assert _ids(r) == ["young", "mid", "famous", "raw"]
    assert r.scores["openalex:Wyoung"] > r.scores["openalex:Wmid"] > r.scores["openalex:Wfamous"]
    assert r.scores["openalex:Wraw"] is None
    assert r.n_unscored == 1 and "refresh" in r.note
    assert seen_texts == ["on-topic prose"]


def test_relevance_uses_protocol_when_no_query(root, seen_texts):
    from saari.triage import rank_papers

    study.update(project_root=root, title="T", question="RQ about X?", criteria="include X")
    r = rank_papers(sort="auto", project_root=root)
    assert r.sort == "relevance" and r.basis == "protocol"
    assert _ids(r)[0] == "young"
    assert "RQ about X?" in seen_texts[0] and "include X" in seen_texts[0]


def test_auto_without_protocol_or_query_falls_back_to_seen_in_silently(root, seen_texts):
    from saari.triage import rank_papers

    r = rank_papers(sort="auto", project_root=root)
    assert r.sort == "seen_in" and r.note is None
    assert seen_texts == []


def test_explicit_relevance_without_reference_text_explains_fallback(root):
    from saari.triage import rank_papers

    r = rank_papers(sort="relevance", project_root=root)
    assert r.sort == "seen_in" and r.requested_sort == "relevance"
    assert "study_set" in r.note


def test_relevance_without_embeddings_explains_fallback(root, seen_texts):
    from saari.triage import rank_papers

    with db.connect(paths.db_path(root)) as con:
        con.execute("DELETE FROM paper_vec")
        con.execute("DELETE FROM paper_embedding_meta")
    r = rank_papers(sort="relevance", query="x", project_root=root)
    assert r.sort == "seen_in" and "refresh" in r.note
    assert seen_texts == []


def test_filters_and_limit_apply_before_ranking(root):
    from saari.triage import rank_papers

    r = rank_papers(sort="relevance", query="x", limit=1, year_from=2015, project_root=root)
    assert _ids(r) == ["young"] and r.n_matching == 3


def test_invalid_sort_raises(root):
    from saari.triage import rank_papers

    with pytest.raises(ValueError):
        rank_papers(sort="score", project_root=root)


def test_mcp_and_http_triage_agree(root):
    from fastapi.testclient import TestClient

    from saari import mcp_server
    from saari.server import create_app

    m = mcp_server.triage(sort="relevance", query="x")
    h = TestClient(create_app()).get("/api/triage", params={"sort": "relevance", "query": "x"}).json()
    for out in (m, h):
        assert out["sort"] == "relevance" and out["basis"] == "query"
        assert out["n_candidates"] == 4
        assert [p["id"] for p in out["papers"]][0] == "openalex:Wyoung"
        assert "score" in out["papers"][0]
    assert [p["id"] for p in m["papers"]] == [p["id"] for p in h["papers"]]

    cited = mcp_server.triage(sort="cited")
    assert "score" not in cited["papers"][0]
    assert "error" in mcp_server.triage(sort="bogus")
    assert TestClient(create_app()).get("/api/triage", params={"sort": "bogus"}).status_code == 400


# ---------- guide ----------


def test_guide_map_is_complete_and_tools_exist():
    from saari import mcp_server
    from saari.guide import PIPELINE, REQUIRES

    import asyncio

    tool_names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    for st in PIPELINE:
        for t in st["tools"]:
            assert t in tool_names, t
    for t in REQUIRES:
        assert t in tool_names, t
    assert "guide" in tool_names and "snowball_included" in tool_names
    assert "guide" in (mcp_server.mcp.instructions or "")


def test_guide_without_project_says_init(tmp_path, monkeypatch):
    from saari.guide import guide_data

    monkeypatch.delenv("SAARI_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    g = guide_data()
    assert g["next"]["tool"] == "saari init"
    assert "state" not in g["pipeline"][0]


def test_guide_next_step_follows_state(root):
    from saari.guide import guide_data

    g = guide_data(project_root=root)
    assert g["next"]["tool"] == "study_set"
    assert g["pipeline"][0]["state"] == "todo"

    study.update(project_root=root, question="RQ?", criteria="c")
    g = guide_data(project_root=root)
    # Wraw is not embedded: refresh before ranking.
    assert g["next"] == {
        "stage": "map",
        "tool": "refresh",
        "why": "1 paper(s) not embedded; relevance ranking and query skip them",
    }

    with db.connect(paths.db_path(root)) as con:
        db.upsert_embedding(
            con, "openalex:Wraw", sqlite_vec.serialize_float32(_vec(0.0, 0.0, 1.0)), "test", EMBED_DIM
        )
    g = guide_data(project_root=root)
    assert g["next"]["tool"] == "triage"
    assert g["corpus"] == {"n_papers": 5, "n_embedded": 5}


def test_build_guide_routes_to_snowball_and_report():
    from saari.guide import build_guide

    def stages(**states):
        keys = ["protocol", "search", "screen", "snowball", "report"]
        st = [{"key": k, "state": states.get(k, "done"), "detail": k} for k in keys]
        return {"stages": st, "next": next((s["key"] for s in st if s["state"] != "done"), None)}

    assert build_guide(stages(snowball="todo"))["next"]["tool"] == "snowball_included"
    assert build_guide(stages(report="todo"))["next"]["tool"] == "export_slr"
    assert build_guide(stages())["next"]["tool"] == "funnel"
    # Screening left over after snowballing still routes back to triage.
    assert build_guide(stages(screen="active", report="todo"))["next"]["tool"] == "triage"


def test_guide_http_and_cli(root):
    from fastapi.testclient import TestClient
    from typer.testing import CliRunner

    from saari.cli import app
    from saari.server import create_app

    assert TestClient(create_app()).get("/api/guide").json()["next"]["tool"] == "study_set"
    out = CliRunner().invoke(app, ["guide"])
    assert out.exit_code == 0 and "next:" in out.output and "study_set" in out.output


# ---------- snowball_included ----------


class _FakeResult:
    def __init__(self, seed, n_fetched, n_new):
        self.seed_paper_id = seed
        self.direction = "both"
        self.n_fetched = n_fetched
        self.n_new = n_new
        self.backward_paper_ids = ["x"] * n_fetched
        self.forward_paper_ids = []
        self.skipped_reason = None
        self.truncated = True  # a field snowball() may grow later


def test_snowball_included_aggregates_and_skips_done(root, monkeypatch):
    from saari.snowball_batch import snowball_seeds

    with db.connect(paths.db_path(root)) as con:
        db.set_screening(con, "openalex:Wyoung", "included", "on-topic")
        db.set_screening(con, "openalex:Wraw", "included", "on-topic")
        # Wraw was already snowballed in both directions.
        db.record_search(con, "snowball", "both:openalex:Wraw", {}, [])

    calls: list[str] = []

    def fake_snowball(paper_id, direction="both", project_root=None, **_kw):
        calls.append(paper_id)
        if paper_id == "openalex:Wyoung":
            raise ValueError("Paper not found in OpenAlex")
        return _FakeResult(paper_id, 4, 3)

    monkeypatch.setattr(snowball_mod, "snowball", fake_snowball)
    r = snowball_seeds(sort="seen_in", project_root=root)
    assert sorted(calls) == ["openalex:Winc", "openalex:Wyoung"]
    assert r["n_seeds"] == 2 and r["n_skipped_done"] == 1
    assert r["skipped_done"] == ["openalex:Wraw"]
    assert r["n_fetched"] == 4 and r["n_new"] == 3
    assert r["n_errors"] == 1 and r["errors"][0]["seed"] == "openalex:Wyoung"
    row = r["seeds"][0]
    assert row["seed"] == "openalex:Winc" and row["truncated"] is True
    assert "backward_paper_ids" not in row
    assert r["saturated"] is False

    calls.clear()
    r = snowball_seeds(sort="seen_in", top_n=1, skip_done=False, project_root=root)
    assert len(calls) == 1


def test_snowball_included_saturation_and_empty(root, monkeypatch):
    from saari import mcp_server

    monkeypatch.setattr(
        snowball_mod, "snowball", lambda pid, **kw: _FakeResult(pid, 5, 0)
    )
    r = mcp_server.snowball_included(sort="cited")
    assert r["n_seeds"] == 1 and r["saturated"] is True
    assert "error" in mcp_server.snowball_included(direction="sideways")

    with db.connect(paths.db_path(root)) as con:
        db.set_screening(con, "openalex:Winc", "excluded", "off-topic")
    r = mcp_server.snowball_included()
    assert r["n_seeds"] == 0 and "No papers" in r["note"] and r["saturated"] is False


def test_snowball_included_http_and_cli(root, monkeypatch):
    from fastapi.testclient import TestClient
    from typer.testing import CliRunner

    from saari.cli import app
    from saari.server import create_app

    monkeypatch.setattr(snowball_mod, "snowball", lambda pid, **kw: _FakeResult(pid, 2, 2))
    body = TestClient(create_app()).post("/api/snowball-included", json={"sort": "cited"}).json()
    assert body["n_seeds"] == 1 and body["n_new"] == 2
    out = CliRunner().invoke(app, ["snowball-included", "--sort", "cited", "--redo"])
    assert out.exit_code == 0, out.output
    assert "new=2" in out.output


def test_cli_triage_shows_scores(root):
    from typer.testing import CliRunner

    from saari.cli import app

    out = CliRunner().invoke(app, ["triage", "--sort", "relevance", "--query", "x"])
    assert out.exit_code == 0, out.output
    assert "sort=relevance vs query" in out.output and "score=+" in out.output
    assert out.output.index("Young") < out.output.index("Famous")
