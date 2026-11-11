"""Pipeline core: embed -> project -> canvas, over a real 11-paper seed corpus.

The fastembed model is replaced by a deterministic hashed bag-of-words
embedder (`FakeEmbedding`), so tests need no model download or network.
Papers sharing words get similar vectors, which is enough to check that
search ranks the right paper first. UMAP and sqlite-vec run for real.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import numpy as np
import pytest

from saari import canvas, db, embed, paths, projection
from saari.models import Author, Paper

SEED_DIR = Path(__file__).parent / "fixtures" / "seed_papers"
NO_ABSTRACT_ID = "doi:10.1016/j.heliyon.2023.e14534"


class FakeEmbedding:
    """Stand-in for fastembed.TextEmbedding: 384-d unit vectors, no download."""

    instances: list[FakeEmbedding] = []

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self.batches: list[int] = []
        FakeEmbedding.instances.append(self)

    @staticmethod
    def vector(text: str) -> np.ndarray:
        v = np.zeros(embed.EMBED_DIM, dtype=np.float32)
        for tok in re.findall(r"[a-z0-9]+", text.lower()):
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            v[h % embed.EMBED_DIM] += 1.0
        n = np.linalg.norm(v)
        if n == 0:  # never return a zero vector: cosine is undefined on it
            v[0] = 1.0
            n = 1.0
        return v / n

    def embed(self, texts):
        self.batches.append(len(texts))
        for t in texts:
            yield self.vector(t)


@pytest.fixture(autouse=True)
def fake_model(monkeypatch):
    FakeEmbedding.instances.clear()
    monkeypatch.setattr(embed, "TextEmbedding", FakeEmbedding)
    monkeypatch.setattr(embed, "_MODEL_CACHE", {})
    return FakeEmbedding


def load_seed() -> list[Paper]:
    papers = []
    for f in sorted(SEED_DIR.glob("*.json")):
        d = json.loads(f.read_text())
        doi = f.stem.replace("_", "/", 1)
        papers.append(
            Paper(
                id=f"doi:{doi}",
                doi=doi,
                title=d["title"],
                abstract=d["abstract"] or None,
                year=int(d["year"]),
                venue=d["publication"],
                authors=[Author(name=d["authors"])],
            )
        )
    return papers


def insert(root: Path, papers: list[Paper]) -> None:
    with db.connect(paths.db_path(root)) as con:
        for p in papers:
            db.upsert_paper(con, p)


@pytest.fixture()
def empty_root(tmp_path):
    return paths.init_project(tmp_path / "proj")


@pytest.fixture()
def seed_root(empty_root):
    insert(empty_root, load_seed())
    return empty_root


def count(root: Path, table: str) -> int:
    with db.connect(paths.db_path(root)) as con:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ---------------------------------------------------------------- fixtures


def test_seed_corpus_maps_onto_paper_model():
    papers = load_seed()
    assert len(papers) == 11
    assert all(p.title and p.doi and p.year for p in papers)
    no_abs = [p for p in papers if p.abstract is None]
    assert [p.id for p in no_abs] == [NO_ABSTRACT_ID]


# ---------------------------------------------------------------- embed


def test_paper_text_uses_title_and_abstract_but_not_suspect_abstract():
    p = Paper(id="x", title="  Title  ", abstract="  Body  ")
    assert embed._paper_text(p) == "Title\n\nBody"
    p.abstract_suspect = True
    assert embed._paper_text(p) == "Title"
    assert embed._paper_text(Paper(id="y", title="", abstract=None)) == ""


def test_embed_seed_corpus_embeds_every_paper_including_title_only(seed_root):
    r = embed.embed_papers(project_root=seed_root, batch_size=4)
    assert (r.n_embedded, r.n_skipped) == (11, 0)
    assert r.dim == embed.EMBED_DIM
    assert count(seed_root, "paper_vec") == 11
    assert count(seed_root, "paper_embedding_meta") == 11
    # batching respected: 11 texts in batches of 4
    assert FakeEmbedding.instances[0].batches == [4, 4, 3]


def test_embed_skips_papers_with_no_text(seed_root):
    insert(seed_root, [Paper(id="blank", title="", abstract=None)])
    r = embed.embed_papers(project_root=seed_root)
    assert (r.n_embedded, r.n_skipped) == (11, 1)
    with db.connect(paths.db_path(seed_root)) as con:
        assert "blank" in db.paper_ids_without_embedding(con)


def test_embed_rerun_is_idempotent_and_does_not_load_model(seed_root):
    embed.embed_papers(project_root=seed_root)
    FakeEmbedding.instances.clear()
    embed._MODEL_CACHE.clear()

    r = embed.embed_papers(project_root=seed_root)
    assert (r.n_embedded, r.n_skipped) == (0, 0)
    assert count(seed_root, "paper_vec") == 11
    # Nothing new to embed: the (possibly downloading) model is never loaded.
    assert FakeEmbedding.instances == []


def test_embed_only_missing_false_reembeds_without_duplicating(seed_root):
    embed.embed_papers(project_root=seed_root)
    r = embed.embed_papers(only_missing=False, project_root=seed_root)
    assert r.n_embedded == 11
    assert count(seed_root, "paper_vec") == 11


def test_embed_empty_project(empty_root):
    r = embed.embed_papers(project_root=empty_root)
    assert (r.n_embedded, r.n_skipped) == (0, 0)
    assert FakeEmbedding.instances == []


def test_embed_picks_up_papers_added_later(seed_root):
    embed.embed_papers(project_root=seed_root)
    insert(seed_root, [Paper(id="new", title="Hull fouling detection", abstract="x")])
    r = embed.embed_papers(project_root=seed_root)
    assert r.n_embedded == 1
    assert count(seed_root, "paper_vec") == 12


def test_query_corpus_ranks_matching_paper_first(seed_root):
    embed.embed_papers(project_root=seed_root)
    target = "doi:10.1680/jmaen.2022.027"  # structural health monitoring paper
    hits = embed.query_corpus(
        "structural health monitoring of ship hulls", k=5, project_root=seed_root
    )
    assert len(hits) == 5
    assert hits[0].paper_id == target
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)
    assert all(-1.0 <= s <= 1.0 for s in scores)


def test_query_corpus_score_is_cosine(seed_root):
    """score must equal the true cosine of the stored unit vectors."""
    papers = {p.id: p for p in load_seed()}
    embed.embed_papers(project_root=seed_root)
    q = "digital twin ship power propulsion"
    hit = embed.query_corpus(q, k=1, project_root=seed_root)[0]
    expected = float(
        FakeEmbedding.vector(q) @ FakeEmbedding.vector(embed._paper_text(papers[hit.paper_id]))
    )
    assert hit.score == pytest.approx(expected, abs=1e-5)


def test_query_corpus_on_empty_project_returns_nothing(empty_root):
    assert embed.query_corpus("anything", project_root=empty_root) == []


def test_similar_to_paper_excludes_seed_and_resolves_doi(seed_root):
    embed.embed_papers(project_root=seed_root)
    seed = "doi:10.1016/j.oceaneng.2022.113479"
    by_id = embed.similar_to_paper(seed, k=3, project_root=seed_root)
    by_doi = embed.similar_to_paper("10.1016/j.oceaneng.2022.113479", k=3, project_root=seed_root)
    assert len(by_id) == 3
    assert seed not in {h.paper_id for h in by_id}
    assert [h.paper_id for h in by_id] == [h.paper_id for h in by_doi]


def test_similar_to_paper_errors(seed_root):
    with pytest.raises(ValueError, match="not found"):
        embed.similar_to_paper("doi:nope", project_root=seed_root)
    with pytest.raises(ValueError, match="not embedded"):
        embed.similar_to_paper(NO_ABSTRACT_ID, project_root=seed_root)


# ---------------------------------------------------------------- projection


def test_project_empty_project_returns_note(empty_root):
    r = projection.project_corpus(project_root=empty_root)
    assert r.n_points == 0
    assert r.note and "saari embed" in r.note
    assert projection.get_projection(project_root=empty_root) == []


def test_project_single_paper_returns_note(empty_root):
    insert(empty_root, load_seed()[:1])
    embed.embed_papers(project_root=empty_root)
    r = projection.project_corpus(project_root=empty_root)
    assert r.n_points == 1 and r.note
    assert count(empty_root, "paper_projection") == 0


def test_project_umap_with_fewer_papers_than_neighbors(seed_root):
    """11 papers, default n_neighbors=15: UMAP must clamp, not fail or fall back."""
    embed.embed_papers(project_root=seed_root)
    r = projection.project_corpus(project_root=seed_root)
    assert r.note is None
    assert r.method == "umap"
    assert r.params["n_neighbors"] == 10
    assert r.n_points == 11
    pts = projection.get_projection(project_root=seed_root)
    assert len(pts) == 11
    assert all(np.isfinite([p["x"], p["y"]]).all() for p in pts)
    assert r.x_range[0] <= r.x_range[1] and r.y_range[0] <= r.y_range[1]


@pytest.mark.parametrize("n", [2, 3])
def test_project_below_umap_minimum_uses_pca(empty_root, n):
    """umap-learn raises on 2-3 points; the corpus still gets a projection."""
    insert(empty_root, load_seed()[:n])
    embed.embed_papers(project_root=empty_root)
    r = projection.project_corpus(project_root=empty_root)
    assert (r.n_points, r.method) == (n, "pca")
    assert "fallback_reason" in r.params
    pts = projection.get_projection(project_root=empty_root)
    assert len(pts) == n
    assert all(np.isfinite([p["x"], p["y"]]).all() for p in pts)


def test_project_umap_minimum_corpus(empty_root):
    n = projection.UMAP_MIN_POINTS
    insert(empty_root, load_seed()[:n])
    embed.embed_papers(project_root=empty_root)
    r = projection.project_corpus(project_root=empty_root)
    assert (r.n_points, r.method) == (n, "umap")
    # params must report the n_neighbors UMAP actually ran with
    assert r.params["n_neighbors"] == projection._effective_neighbors(15, n) == n - 1
    pts = projection.get_projection(project_root=empty_root)
    assert all(np.isfinite([p["x"], p["y"]]).all() for p in pts)


def test_project_falls_back_when_umap_returns_nan(seed_root, monkeypatch):
    embed.embed_papers(project_root=seed_root)
    monkeypatch.setattr(
        projection, "_umap_project", lambda vecs, *a: np.full((len(vecs), 2), np.nan)
    )
    r = projection.project_corpus(project_root=seed_root)
    assert r.method == "pca"
    assert "non-finite" in r.params["fallback_reason"]


def test_project_rerun_replaces_rows_and_is_deterministic(seed_root):
    embed.embed_papers(project_root=seed_root)
    projection.project_corpus(project_root=seed_root)
    first = sorted((p["paper_id"], p["x"], p["y"]) for p in projection.get_projection(project_root=seed_root))
    projection.project_corpus(project_root=seed_root)
    second = sorted((p["paper_id"], p["x"], p["y"]) for p in projection.get_projection(project_root=seed_root))
    assert len(second) == 11
    assert [a[0] for a in first] == [b[0] for b in second]
    np.testing.assert_allclose([a[1:] for a in first], [b[1:] for b in second], atol=1e-4)


def test_project_pca_and_unknown_method(seed_root):
    embed.embed_papers(project_root=seed_root)
    r = projection.project_corpus(method="pca", project_root=seed_root)
    assert (r.method, r.n_points) == ("pca", 11)
    with db.connect(paths.db_path(seed_root)) as con:
        methods = {row[0] for row in con.execute("SELECT method FROM paper_projection")}
    assert methods == {"pca"}
    with pytest.raises(ValueError, match="Unknown projection method"):
        projection.project_corpus(method="tsne", project_root=seed_root)


def test_project_falls_back_to_pca_when_umap_fails(seed_root, monkeypatch):
    embed.embed_papers(project_root=seed_root)

    def boom(*a, **k):
        raise RuntimeError("umap exploded")

    monkeypatch.setattr(projection, "_umap_project", boom)
    r = projection.project_corpus(project_root=seed_root)
    assert r.method == "pca"
    assert r.params == {"fallback_reason": "umap exploded"}
    assert count(seed_root, "paper_projection") == 11


# ---------------------------------------------------------------- canvas


@pytest.fixture()
def projected_root(seed_root):
    embed.embed_papers(project_root=seed_root)
    projection.project_corpus(method="pca", project_root=seed_root)
    with db.connect(paths.db_path(seed_root)) as con:
        db.set_screening(con, "doi:10.1680/jmaen.2022.027", "included")
        db.set_screening(con, "doi:10.1109/MNET.011.2000020", "excluded")
    return seed_root


def test_obsidian_canvas_shape(projected_root, tmp_path):
    out = tmp_path / "out" / "landscape.canvas"
    r = canvas.write_obsidian_canvas(out, project_root=projected_root)
    assert r["n_nodes"] == 11
    data = json.loads(out.read_text())
    assert data["edges"] == []
    nodes = {n["id"]: n for n in data["nodes"]}
    assert len(nodes) == 11
    assert all(":" not in nid for nid in nodes)  # Obsidian node ids
    assert nodes["doi-10.1680/jmaen.2022.027"]["color"] == "4"
    assert nodes["doi-10.1109/MNET.011.2000020"]["color"] == "1"
    # candidates get the default color (no key)
    assert "color" not in nodes["doi-10.1016/j.ssci.2020.104799"]
    # coordinates normalized into the canvas box
    assert all(-400 <= n["x"] <= 4800 and -200 <= n["y"] <= 3400 for n in nodes.values())
    # the title-only paper renders without an abstract quote
    assert "> " not in nodes["doi-10.1016/j.heliyon.2023.e14534"]["text"]


def test_canvas_status_filter(projected_root, tmp_path):
    r = canvas.write_obsidian_canvas(tmp_path / "inc.canvas", status="included", project_root=projected_root)
    assert r["n_nodes"] == 1


def test_canvas_empty_project_writes_nothing(empty_root, tmp_path):
    for fn, name in [(canvas.write_obsidian_canvas, "a.canvas"), (canvas.write_html_viewer, "a.html")]:
        r = fn(tmp_path / name, project_root=empty_root)
        assert r["n_nodes"] == 0 and "note" in r
        assert not (tmp_path / name).exists()


def test_html_viewer_embeds_all_points(projected_root, tmp_path):
    out = tmp_path / "landscape.html"
    r = canvas.write_html_viewer(out, project_root=projected_root)
    assert r["n_nodes"] == 11
    text = out.read_text()
    assert "__PAYLOAD__" not in text and "__PALETTE__" not in text
    m = re.search(r"const DATA = (\[.*?\]);\n", text, re.S)
    data = json.loads(m.group(1))
    assert {d["id"] for d in data} == {p.id for p in load_seed()}


def test_html_viewer_title_cannot_close_script_tag(empty_root, tmp_path):
    """A hostile title from a source API must not break out of the inline script."""
    evil = 'Evil </script><script>alert(1)</script> paper'
    insert(empty_root, [Paper(id="a", title=evil, abstract="one"), Paper(id="b", title="ok", abstract="two")])
    embed.embed_papers(project_root=empty_root)
    projection.project_corpus(method="pca", project_root=empty_root)
    out = tmp_path / "v.html"
    canvas.write_html_viewer(out, project_root=empty_root)
    text = out.read_text()
    assert text.count("</script>") == 1  # only the template's own closing tag
    m = re.search(r"const DATA = (\[.*?\]);\n", text, re.S)
    assert {d["title"] for d in json.loads(m.group(1))} == {evil, "ok"}


# ---------------------------------------------------------------- refresh (CLI)


def test_cli_refresh_end_to_end_and_rerun(seed_root, monkeypatch):
    from typer.testing import CliRunner

    from saari.cli import app

    monkeypatch.setenv("SAARI_PROJECT_ROOT", str(seed_root))
    runner = CliRunner()
    first = runner.invoke(app, ["refresh"])
    assert first.exit_code == 0, first.output
    assert "embedded 11" in first.output
    canvas_path = paths.papers_dir(seed_root) / "landscape.canvas"
    html_path = paths.papers_dir(seed_root) / "landscape.html"
    assert canvas_path.exists() and html_path.exists()

    second = runner.invoke(app, ["refresh"])
    assert second.exit_code == 0, second.output
    assert "embedded 0" in second.output
    assert count(seed_root, "paper_vec") == 11
    assert count(seed_root, "paper_projection") == 11
    assert len(json.loads(canvas_path.read_text())["nodes"]) == 11


def test_cli_refresh_empty_project(empty_root, monkeypatch):
    from typer.testing import CliRunner

    from saari.cli import app

    monkeypatch.setenv("SAARI_PROJECT_ROOT", str(empty_root))
    r = CliRunner().invoke(app, ["refresh"])
    assert r.exit_code == 0, r.output
    assert "Not enough embeddings" in r.output
    assert not (paths.papers_dir(empty_root) / "landscape.canvas").exists()
    assert FakeEmbedding.instances == []
