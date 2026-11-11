"""Bibliographic sources and shared search persistence.

`run_search` is the one entry point every surface (CLI, MCP, HTTP, in-app
agent) calls: it dispatches to the named source, dedupes across sources by
DOI, upserts, and records the search event. Keeping it here means a new
source lands on all surfaces by being added to `SOURCES`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from saari import db, paths
from saari.sources.common import SearchHits, SearchTooLarge

__all__ = ["SOURCE_NAMES", "SCOPE_NAMES", "SearchHits", "SearchTooLarge", "run_search"]

SOURCE_NAMES = ("openalex", "scopus")
SCOPE_NAMES = ("all", "title_abstract")


def _search_source(
    source: str,
    query: str,
    *,
    limit: int,
    year_from: int | None,
    year_to: int | None,
    project_root: Path | None,
    scope: str,
    fetch_all: bool,
    max_records: int | None,
) -> SearchHits:
    kw: dict[str, Any] = {
        "limit": limit,
        "year_from": year_from,
        "year_to": year_to,
        "project_root": project_root,
        "scope": scope,
        "fetch_all": fetch_all,
        "max_records": max_records,
    }
    if source == "openalex":
        from saari.sources import openalex

        return openalex.search(query, **kw)
    if source == "scopus":
        from saari.sources import scopus

        return scopus.search(query, **kw)
    raise ValueError(f"unknown source {source!r}; available: {', '.join(SOURCE_NAMES)}")


def run_search(
    query: str,
    *,
    source: str = "openalex",
    limit: int = 25,
    year_from: int | None = None,
    year_to: int | None = None,
    project_root: Path | None = None,
    scope: str = "all",
    fetch_all: bool = False,
    max_records: int | None = None,
) -> dict[str, Any]:
    """Search `source`, persist results, record the search event.

    Completeness: the result reports `n_available` (the source's total for
    the query, None if unknown) and `truncated` (True when fewer records
    were retrieved than available). Both are stored on the search event so
    PRISMA / Limitations reporting can disclose sampling. `fetch_all`
    fetches every record up to the safety cap (`max_records`) and raises
    SearchTooLarge above it. `scope="title_abstract"` restricts matching to
    title and abstract.

    Cross-source dedup: a fetched paper whose DOI already exists in the
    corpus under another id (e.g. Scopus result already found via OpenAlex)
    is not inserted again - the search links to the existing record, which
    counts toward `seen_in` and `n_duplicate`.
    """
    root = project_root or paths.project_root()
    if scope not in SCOPE_NAMES:
        raise ValueError(f"unknown scope {scope!r}; available: {', '.join(SCOPE_NAMES)}")
    fetched = _search_source(
        source,
        query,
        limit=limit,
        year_from=year_from,
        year_to=year_to,
        project_root=root,
        scope=scope,
        fetch_all=fetch_all,
        max_records=max_records,
    )
    n_available = fetched.n_available
    truncated = fetched.truncated

    paper_ids: list[str] = []
    n_new = 0
    n_duplicate = 0
    with db.connect(paths.db_path(root)) as con:
        for paper, raw_path in fetched:
            existing = con.execute(
                "SELECT id FROM paper WHERE id = ?", (paper.id,)
            ).fetchone()
            if existing is None and paper.doi:
                row = con.execute(
                    "SELECT id FROM paper WHERE lower(doi) = ?", (paper.doi.lower(),)
                ).fetchone()
                if row is not None:
                    # Same work, found earlier via another source.
                    n_duplicate += 1
                    paper_ids.append(row["id"])
                    continue
            if existing is None:
                n_new += 1
            else:
                n_duplicate += 1
            db.upsert_paper(con, paper, raw_path=raw_path)
            paper_ids.append(paper.id)
        search_id = db.record_search(
            con,
            source=source,
            query=query,
            params={
                "limit": limit,
                "year_from": year_from,
                "year_to": year_to,
                "scope": scope,
                "fetch_all": fetch_all,
                "notes": fetched.notes,
            },
            paper_ids=paper_ids,
            n_available=n_available,
            truncated=truncated,
        )

    return {
        "search_id": search_id,
        "source": source,
        "query": query,
        "scope": scope,
        "n_fetched": len(fetched),
        "n_available": n_available,
        "truncated": truncated,
        "notes": fetched.notes,
        "n_new": n_new,
        "n_duplicate": n_duplicate,
        "paper_ids": paper_ids,
    }
