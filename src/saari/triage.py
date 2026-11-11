"""Ranking papers for triage: by protocol relevance, search overlap, or fame.

One core shared by the CLI (`saari triage`), MCP (`triage`), and HTTP
(`GET /api/triage`) so every surface orders the queue the same way.

Sort orders:

- `relevance`: cosine similarity of each paper's embedding to a reference
  text -- an explicit `query`, else the study protocol (title + question +
  criteria). Scores every matching paper exactly (no top-k cut-off), so
  "most relevant undecided papers" never comes back short.
- `seen_in`: number of distinct searches/snowballs that surfaced the paper,
  citations as tie-break. Cheap, needs no embeddings.
- `cited`: global citation count. Biased toward famous papers, kept for
  completeness.
- `auto` (default): `relevance` when there is reference text and at least
  one matching paper is embedded, else `seen_in`.

Relevance degrades gracefully: when it cannot run, the result falls back to
`seen_in` and says why in `note`. Unembedded papers are listed after the
scored ones (score None) and counted in `n_unscored`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from saari import db, paths
from saari import study as _study
from saari.embed import embed_text
from saari.models import Paper

SORTS = ("auto", "relevance", "seen_in", "cited")

_ORDER_BY = {
    "seen_in": "seen_in DESC, COALESCE(cited_by_count, 0) DESC",
    "cited": "COALESCE(cited_by_count, 0) DESC",
}


def protocol_text(study: dict) -> str:
    """Reference text for relevance ranking, built from the study protocol."""
    parts = [(study.get(k) or "").strip() for k in ("title", "question", "criteria")]
    return "\n\n".join(p for p in parts if p)


@dataclass
class Ranked:
    papers: list[Paper]
    scores: dict[str, float | None]
    sort: str  # the order actually applied
    requested_sort: str
    basis: str | None = None  # "query" | "protocol" when sort == relevance
    n_unscored: int = 0
    n_matching: int = 0  # papers matching the filters, before `limit`
    notes: list[str] = field(default_factory=list)

    @property
    def note(self) -> str | None:
        return " ".join(self.notes) if self.notes else None


def _load_vectors(con, ids: list[str]) -> dict[str, np.ndarray]:
    out: dict[str, np.ndarray] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        ph = ",".join(["?"] * len(chunk))
        rows = con.execute(
            f"SELECT paper_id, embedding FROM paper_vec WHERE paper_id IN ({ph})", chunk
        ).fetchall()
        for r in rows:
            out[r["paper_id"]] = np.frombuffer(r["embedding"], dtype=np.float32)
    return out


def rank_papers(
    *,
    status: str | None = "candidate",
    sort: str = "auto",
    query: str | None = None,
    limit: int = 20,
    min_citations: int | None = None,
    year_from: int | None = None,
    seen_in_at_least: int | None = None,
    project_root: Path | None = None,
) -> Ranked:
    """Rank papers with `status` by `sort`. See module docstring for orders."""
    if sort not in SORTS:
        raise ValueError(f"sort must be one of {'|'.join(SORTS)}, got {sort!r}")
    root = project_root or paths.project_root()
    query = (query or "").strip() or None
    notes: list[str] = []

    filters = dict(
        status=status,
        min_citations=min_citations,
        year_from=year_from,
        seen_in_at_least=seen_in_at_least,
    )

    with db.connect(paths.db_path(root)) as con:
        # Base order seen_in: it is the fallback and the tie-break for relevance.
        pool = db.list_papers(con, limit=-1, order_by=_ORDER_BY["seen_in"], **filters)
        vecs = _load_vectors(con, [p.id for p in pool]) if sort in ("auto", "relevance") else {}

    applied = sort
    basis: str | None = None
    ref_text: str | None = None
    if sort in ("auto", "relevance"):
        if query:
            basis, ref_text = "query", query
        else:
            ref_text = protocol_text(_study.get(project_root=root)) or None
            basis = "protocol" if ref_text else None
        if ref_text is None:
            applied = "seen_in"
            if sort == "relevance":
                notes.append(
                    "relevance needs a `query` or a study protocol (study_set "
                    "question/criteria); fell back to seen_in."
                )
        elif not vecs:
            applied = "seen_in"
            if pool:
                notes.append(
                    "No matching papers are embedded yet; run `refresh` (or `embed`) "
                    "to enable relevance ranking. Fell back to seen_in."
                )
        else:
            applied = "relevance"

    scores: dict[str, float | None] = {}
    n_unscored = 0
    if applied == "relevance":
        q = np.asarray(embed_text(ref_text), dtype=np.float32)
        q = q / (np.linalg.norm(q) or 1.0)
        for p in pool:
            v = vecs.get(p.id)
            if v is None:
                scores[p.id] = None
                n_unscored += 1
            else:
                n = float(np.linalg.norm(v)) or 1.0
                scores[p.id] = round(float(np.dot(q, v) / n), 4)
        # Stable sort keeps the seen_in order as tie-break; unscored go last.
        pool = sorted(
            pool, key=lambda p: (scores[p.id] is None, -(scores[p.id] or 0.0))
        )
        if n_unscored:
            notes.append(
                f"{n_unscored} paper(s) not embedded are listed last without a "
                "score; run `refresh` to score them."
            )
    elif applied == "cited":
        pool = sorted(pool, key=lambda p: -(p.cited_by_count or 0))

    return Ranked(
        papers=pool[: max(limit, 0)],
        scores=scores,
        sort=applied,
        requested_sort=sort,
        basis=basis if applied == "relevance" else None,
        n_unscored=n_unscored,
        n_matching=len(pool),
        notes=notes,
    )
