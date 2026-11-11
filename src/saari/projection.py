"""2D projection over the embedded corpus.

Uses UMAP by default (captures semantic clusters better). Falls back to a
numpy-only PCA if UMAP is unavailable. One `paper_projection` row per paper;
re-running `project_corpus()` replaces all rows.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from saari import db, paths

log = logging.getLogger(__name__)


def _load_embeddings(con) -> tuple[list[str], np.ndarray]:
    rows = con.execute("SELECT paper_id, embedding FROM paper_vec").fetchall()
    if not rows:
        return [], np.zeros((0, 0), dtype=np.float32)
    ids = [r["paper_id"] for r in rows]
    vecs = np.stack(
        [np.frombuffer(r["embedding"], dtype=np.float32) for r in rows]
    )
    return ids, vecs


@dataclass
class ProjectionResult:
    n_points: int
    method: str
    params: dict
    x_range: tuple[float, float] | None = None
    y_range: tuple[float, float] | None = None
    note: str | None = None


# Below this, umap-learn's spectral initialisation fails outright
# (zero-size reduction at n=2, eigsh k >= N at n=3).
UMAP_MIN_POINTS = 4


def _effective_neighbors(n_neighbors: int, n: int) -> int:
    """UMAP needs 2 <= n_neighbors < n_samples; clamp for tiny corpora."""
    return min(n_neighbors, max(2, n - 1))


def _umap_project(
    vecs: np.ndarray, n_neighbors: int, min_dist: float, random_state: int
) -> np.ndarray:
    import umap  # heavy import; delay until needed

    nn = _effective_neighbors(n_neighbors, len(vecs))
    reducer = umap.UMAP(
        n_neighbors=nn,
        min_dist=min_dist,
        n_components=2,
        metric="cosine",
        random_state=random_state,
        # A fixed seed forces single-threaded UMAP anyway; saying so up front
        # avoids a UserWarning on every run.
        n_jobs=1,
    )
    return reducer.fit_transform(vecs)


def _pca_project(vecs: np.ndarray) -> np.ndarray:
    X = vecs - vecs.mean(axis=0, keepdims=True)
    U, S, _ = np.linalg.svd(X, full_matrices=False)
    return U[:, :2] * S[:2]


def project_corpus(
    method: str = "umap",
    n_neighbors: int = 15,
    min_dist: float = 0.1,
    random_state: int = 42,
    project_root: Path | None = None,
) -> ProjectionResult:
    """Compute a 2D projection of all embedded papers; persist to paper_projection.

    method: 'umap' (default) | 'pca'. PCA is numpy-only (fallback for low-dep setups).
    """
    root = project_root or paths.project_root()
    t0 = time.perf_counter()
    with db.connect(paths.db_path(root)) as con:
        ids, vecs = _load_embeddings(con)
        n = len(ids)
        if n < 2:
            log.info("project: skipped, only %d embedding(s)", n)
            return ProjectionResult(
                n_points=n, method=method, params={},
                note="Not enough embeddings (need 2+). Run `saari embed` first.",
            )

        params: dict
        if method == "umap" and n < UMAP_MIN_POINTS:
            # UMAP's spectral init raises on 2-3 points; skip the attempt.
            log.info("project: %d points is below UMAP's minimum, using PCA", n)
            coords = _pca_project(vecs)
            method = "pca"
            params = {"fallback_reason": f"UMAP needs {UMAP_MIN_POINTS}+ points, got {n}"}
        elif method == "umap":
            try:
                coords = _umap_project(vecs, n_neighbors, min_dist, random_state)
                if not np.isfinite(coords).all():
                    raise ValueError("UMAP returned non-finite coordinates")
                params = {"n_neighbors": _effective_neighbors(n_neighbors, n), "min_dist": min_dist, "metric": "cosine"}
            except Exception as e:
                log.warning("UMAP failed on %d points, falling back to PCA: %s", n, e)
                coords = _pca_project(vecs)
                method = "pca"
                params = {"fallback_reason": str(e)}
        elif method == "pca":
            coords = _pca_project(vecs)
            params = {}
        else:
            raise ValueError(f"Unknown projection method: {method!r}")

        params_json = json.dumps(params)
        con.execute("DELETE FROM paper_projection")
        con.executemany(
            "INSERT INTO paper_projection (paper_id, x, y, method, params_json) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (pid, float(x), float(y), method, params_json)
                for pid, (x, y) in zip(ids, coords)
            ],
        )

    log.info("projected %d points with %s in %.2fs", n, method, time.perf_counter() - t0)
    return ProjectionResult(
        n_points=n,
        method=method,
        params=params,
        x_range=(float(coords[:, 0].min()), float(coords[:, 0].max())),
        y_range=(float(coords[:, 1].min()), float(coords[:, 1].max())),
    )


def get_projection(project_root: Path | None = None) -> list[dict]:
    """Return the current projection: [{paper_id, x, y}, ...]."""
    root = project_root or paths.project_root()
    with db.connect(paths.db_path(root)) as con:
        rows = con.execute(
            "SELECT paper_id, x, y FROM paper_projection"
        ).fetchall()
    return [{"paper_id": r["paper_id"], "x": r["x"], "y": r["y"]} for r in rows]
