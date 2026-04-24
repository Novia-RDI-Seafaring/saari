"""Embeddings: encode papers + semantic search.

Uses `sentence-transformers/all-MiniLM-L6-v2` (384-dim) via `fastembed`. Same
model as graceful.ai's course-kb; same model ships as WASM-friendly ONNX for
`@xenova/transformers` in a browser, so a future saaristo web UI can produce
the same vectors client-side and filter in the browser.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import sqlite_vec
from fastembed import TextEmbedding

from saari import db, paths
from saari.models import Paper

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBED_DIM = 384

_MODEL_CACHE: dict[str, TextEmbedding] = {}


def _get_model(name: str = DEFAULT_MODEL) -> TextEmbedding:
    """Load (and cache) a fastembed TextEmbedding model."""
    if name not in _MODEL_CACHE:
        _MODEL_CACHE[name] = TextEmbedding(model_name=name)
    return _MODEL_CACHE[name]


def _paper_text(paper: Paper) -> str:
    """Build the text representation used for embedding: title + abstract."""
    parts: list[str] = []
    if paper.title:
        parts.append(paper.title.strip())
    if paper.abstract and not paper.abstract_suspect:
        parts.append(paper.abstract.strip())
    return "\n\n".join(parts) if parts else ""


@dataclass
class EmbedResult:
    n_embedded: int
    n_skipped: int
    model: str
    dim: int
    elapsed_sec: float


def embed_papers(
    only_missing: bool = True,
    model_name: str = DEFAULT_MODEL,
    project_root: Path | None = None,
    batch_size: int = 32,
) -> EmbedResult:
    """Embed papers in the project DB and store vectors in paper_vec.

    `only_missing=True` skips papers already embedded. Set False to re-embed
    (useful after switching models).

    Returns counts + model metadata + elapsed time.
    """
    root = project_root or paths.project_root()
    model = _get_model(model_name)

    start = time.perf_counter()

    with db.connect(paths.db_path(root)) as con:
        if only_missing:
            target_ids = db.paper_ids_without_embedding(con)
        else:
            rows = con.execute("SELECT id FROM paper").fetchall()
            target_ids = [r["id"] for r in rows]

        if not target_ids:
            return EmbedResult(0, 0, model_name, EMBED_DIM, 0.0)

        # Pull papers into memory (48 papers * ~3KB = trivial; revisit at 10k+)
        placeholders = ",".join(["?"] * len(target_ids))
        rows = con.execute(
            f"SELECT * FROM paper WHERE id IN ({placeholders})", target_ids
        ).fetchall()
        papers = [db._row_to_paper(r) for r in rows]

        texts: list[str] = []
        ids: list[str] = []
        skipped = 0
        for p in papers:
            text = _paper_text(p)
            if not text:
                skipped += 1
                continue
            texts.append(text)
            ids.append(p.id)

        n_embedded = 0
        if texts:
            for batch_start in range(0, len(texts), batch_size):
                batch_ids = ids[batch_start : batch_start + batch_size]
                batch_texts = texts[batch_start : batch_start + batch_size]
                vectors = list(model.embed(batch_texts))
                for pid, vec in zip(batch_ids, vectors):
                    blob = sqlite_vec.serialize_float32(vec.tolist())
                    db.upsert_embedding(con, pid, blob, model_name, EMBED_DIM)
                    n_embedded += 1

    elapsed = time.perf_counter() - start
    return EmbedResult(n_embedded, skipped, model_name, EMBED_DIM, elapsed)


@dataclass
class QueryHit:
    paper_id: str
    score: float  # cosine similarity (1 = identical, 0 = orthogonal)
    distance: float  # sqlite-vec raw distance


def embed_text(text: str, model_name: str = DEFAULT_MODEL) -> list[float]:
    """Encode a single text to an embedding vector."""
    model = _get_model(model_name)
    vec = next(iter(model.embed([text])))
    return vec.tolist()


def query_corpus(
    text: str,
    k: int = 20,
    model_name: str = DEFAULT_MODEL,
    project_root: Path | None = None,
) -> list[QueryHit]:
    """Semantic search over the embedded corpus. Returns top-k hits."""
    root = project_root or paths.project_root()
    vec = embed_text(text, model_name=model_name)
    blob = sqlite_vec.serialize_float32(vec)

    with db.connect(paths.db_path(root)) as con:
        rows = db.vector_search(con, blob, k=k)

    # sqlite-vec returns L2 distance on normalized vectors; convert to cosine similarity.
    # For unit-normalized vectors: cos_sim = 1 - (L2^2) / 2
    hits: list[QueryHit] = []
    for paper_id, distance in rows:
        cos_sim = max(-1.0, min(1.0, 1.0 - (distance * distance) / 2.0))
        hits.append(QueryHit(paper_id=paper_id, score=cos_sim, distance=distance))
    return hits
