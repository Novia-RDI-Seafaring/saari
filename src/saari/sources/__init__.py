"""Bibliographic sources and shared search persistence.

`run_search` is the one entry point every surface (CLI, MCP, HTTP, in-app
agent) calls: it dispatches to the named source, dedupes across sources by
exact identifier (DOI, then PMID / arXiv id), upserts, and records the search
event. Keeping it here means a new source lands on all surfaces by being
added to `SOURCE_NAMES`.

`add_papers` is the "known paper" path: resolve user-supplied identifiers
(OpenAlex first, Crossref as DOI fallback) and record them as one `manual`
identification event, which PRISMA reports under "other methods".
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

import httpx

from saari import db, paths
from saari.models import Paper
from saari.sources.ids import IdentifierError, is_valid_arxiv_id, parse_identifier

log = logging.getLogger(__name__)

SOURCE_NAMES = ("openalex", "scopus", "crossref")
# Search-event `source` for papers added by identifier (PRISMA "other methods").
MANUAL_SOURCE = "manual"


def _search_source(
    source: str,
    query: str,
    *,
    limit: int,
    year_from: int | None,
    year_to: int | None,
    project_root: Path | None,
) -> list[tuple[Paper, str]]:
    if source == "openalex":
        from saari.sources import openalex

        return openalex.search(
            query, limit=limit, year_from=year_from, year_to=year_to, project_root=project_root
        )
    if source == "scopus":
        from saari.sources import scopus

        return scopus.search(
            query, limit=limit, year_from=year_from, year_to=year_to, project_root=project_root
        )
    if source == "crossref":
        from saari.sources import crossref

        return crossref.search(
            query, limit=limit, year_from=year_from, year_to=year_to, project_root=project_root
        )
    raise ValueError(f"unknown source {source!r}; available: {', '.join(SOURCE_NAMES)}")


def find_existing(con: sqlite3.Connection, paper: Paper) -> str | None:
    """Id of the corpus record that is unambiguously the same work, or None.

    Only exact persistent identifiers count: the paper's own id (or a merge
    alias of it), DOI (case-insensitive), PMID, or a well-formed arXiv id.
    Title/author similarity never merges automatically; see `saari.dedup`.
    """
    row = con.execute("SELECT id FROM paper WHERE id = ?", (paper.id,)).fetchone()
    if row is not None:
        return row["id"]
    kept = db.canonical_id(con, paper.id)
    if kept != paper.id:
        return kept
    if paper.doi:
        row = con.execute(
            "SELECT id FROM paper WHERE lower(doi) = ?", (paper.doi.lower(),)
        ).fetchone()
        if row is not None:
            return row["id"]
    if paper.pmid and str(paper.pmid).isdigit():
        row = con.execute("SELECT id FROM paper WHERE pmid = ?", (paper.pmid,)).fetchone()
        if row is not None:
            return row["id"]
    if is_valid_arxiv_id(paper.arxiv_id):
        row = con.execute(
            "SELECT id FROM paper WHERE arxiv_id = ?", (paper.arxiv_id,)
        ).fetchone()
        if row is not None:
            return row["id"]
    return None


def run_search(
    query: str,
    *,
    source: str = "openalex",
    limit: int = 25,
    year_from: int | None = None,
    year_to: int | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Search `source`, persist results, record the search event.

    Cross-source dedup: a fetched paper whose DOI (or PMID / arXiv id)
    already exists in the corpus under another id (e.g. Scopus result
    already found via OpenAlex) is not inserted again - the search links to
    the existing record, which counts toward `seen_in` and `n_duplicate`.
    """
    root = project_root or paths.project_root()
    fetched = _search_source(
        source, query, limit=limit, year_from=year_from, year_to=year_to, project_root=root
    )

    paper_ids: list[str] = []
    n_new = 0
    n_duplicate = 0
    with db.connect(paths.db_path(root)) as con:
        for paper, raw_path in fetched:
            existing = find_existing(con, paper)
            if existing is None:
                n_new += 1
                db.upsert_paper(con, paper, raw_path=raw_path)
                paper_ids.append(paper.id)
                continue
            n_duplicate += 1
            if existing == paper.id:
                db.upsert_paper(con, paper, raw_path=raw_path)  # refresh own record
            # else: same work, found earlier via another source / id.
            paper_ids.append(existing)
        search_id = db.record_search(
            con,
            source=source,
            query=query,
            params={"limit": limit, "year_from": year_from, "year_to": year_to},
            paper_ids=paper_ids,
        )

    return {
        "search_id": search_id,
        "source": source,
        "query": query,
        "n_fetched": len(fetched),
        "n_new": n_new,
        "n_duplicate": n_duplicate,
        "paper_ids": paper_ids,
    }


def _lookup_corpus(con: sqlite3.Connection, kind: str, value: str) -> str | None:
    if kind == "openalex":
        pid = db.canonical_id(con, f"openalex:{value}")
        row = con.execute("SELECT id FROM paper WHERE id = ?", (pid,)).fetchone()
    elif kind == "doi":
        row = con.execute("SELECT id FROM paper WHERE lower(doi) = ?", (value,)).fetchone()
    elif kind == "pmid":
        row = con.execute("SELECT id FROM paper WHERE pmid = ?", (value,)).fetchone()
    else:
        row = con.execute("SELECT id FROM paper WHERE arxiv_id = ?", (value,)).fetchone()
    return row["id"] if row else None


def _resolve_remote(kind: str, value: str, root: Path) -> tuple[Paper, str, str] | None:
    """Fetch one identifier: OpenAlex first, Crossref for DOIs OpenAlex lacks."""
    from saari.sources import crossref, openalex
    from saari.sources.ids import arxiv_doi

    res: tuple[Paper, str] | None
    if kind == "openalex":
        res = openalex.fetch_by_id(value, project_root=root)
    elif kind == "pmid":
        res = openalex.fetch_by_external_id("pmid", value, project_root=root)
    elif kind == "arxiv":
        # OpenAlex has no arXiv-id lookup; arXiv's DataCite DOI is the handle.
        res = openalex.fetch_by_external_id("doi", arxiv_doi(value), project_root=root)
    else:
        res = openalex.fetch_by_external_id("doi", value, project_root=root)
    if res is not None:
        return res[0], res[1], "openalex"
    if kind == "doi":
        log.info("OpenAlex does not know DOI %s; trying Crossref", value)
        res = crossref.fetch_by_doi(value, project_root=root)
        if res is not None:
            return res[0], res[1], "crossref"
    return None


def add_papers(
    identifiers: list[str],
    *,
    note: str | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Add known papers by DOI / doi.org URL / arXiv / PMID / OpenAlex id.

    Each identifier is resolved against the corpus first, then OpenAlex,
    then (DOIs only) Crossref. All resolved records are linked to ONE
    search event with `source="manual"`, so PRISMA counts them as
    identified via other methods (not as a database search) and a paper
    already in the corpus counts as a duplicate, not a new record.

    Returns `{"search_id", "n_new", "n_duplicate", "n_failed", "results",
    "paper_ids"}`; each result has `input`, `status` (added | duplicate |
    not_found | invalid | error), and when resolved `paper_id` and
    `resolved_via` (corpus | openalex | crossref).
    """
    root = project_root or paths.project_root()
    results: list[dict[str, Any]] = []
    parsed: list[tuple[str, str, str]] = []
    for raw in identifiers:
        try:
            kind, value = parse_identifier(raw)
        except IdentifierError as e:
            results.append({"input": raw, "status": "invalid", "error": str(e)})
            continue
        parsed.append((raw, kind, value))

    with db.connect(paths.db_path(root)) as con:
        known = {(k, v): _lookup_corpus(con, k, v) for _, k, v in parsed}

    fetched: dict[tuple[str, str], tuple[Paper, str, str] | None | Exception] = {}
    for _, kind, value in parsed:
        if known[(kind, value)] is None and (kind, value) not in fetched:
            try:
                fetched[(kind, value)] = _resolve_remote(kind, value, root)
            except (httpx.HTTPError, RuntimeError) as e:
                log.warning("add: could not resolve %s:%s: %s", kind, value, e)
                fetched[(kind, value)] = e

    paper_ids: list[str] = []
    n_new = n_duplicate = 0
    with db.connect(paths.db_path(root)) as con:
        for raw, kind, value in parsed:
            entry: dict[str, Any] = {"input": raw, "kind": kind, "value": value}
            pid = known[(kind, value)] or _lookup_corpus(con, kind, value)
            if pid is not None:
                entry.update(status="duplicate", paper_id=pid, resolved_via="corpus")
                n_duplicate += 1
            else:
                got = fetched.get((kind, value))
                if isinstance(got, Exception):
                    entry.update(status="error", error=str(got))
                    results.append(entry)
                    continue
                if got is None:
                    entry.update(status="not_found")
                    results.append(entry)
                    continue
                paper, raw_path, via = got
                existing = find_existing(con, paper)
                if existing is not None:
                    entry.update(status="duplicate", paper_id=existing, resolved_via=via)
                    n_duplicate += 1
                    if existing == paper.id:
                        db.upsert_paper(con, paper, raw_path=raw_path)
                    pid = existing
                else:
                    db.upsert_paper(con, paper, raw_path=raw_path)
                    entry.update(status="added", paper_id=paper.id, resolved_via=via)
                    n_new += 1
                    pid = paper.id
            paper_ids.append(pid)
            results.append(entry)

        search_id: int | None = None
        if paper_ids:
            params: dict[str, Any] = {"identifiers": [r for r, _, _ in parsed]}
            if note:
                params["note"] = note
            search_id = db.record_search(
                con,
                source=MANUAL_SOURCE,
                query=", ".join(r for r, _, _ in parsed),
                params=params,
                paper_ids=paper_ids,
            )

    return {
        "search_id": search_id,
        "source": MANUAL_SOURCE,
        "n_new": n_new,
        "n_duplicate": n_duplicate,
        "n_failed": sum(1 for r in results if r["status"] in ("invalid", "not_found", "error")),
        "results": results,
        "paper_ids": paper_ids,
    }
