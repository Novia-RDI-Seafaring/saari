"""MCP server exposing saari skills to agents (Claude Code, custom agents, etc.).

Run via `saari-mcp` (stdio). The host (Claude Code) typically starts the server with
a project directory as cwd; saari then resolves the project root by walking up for
`.saaristo/`, or honors `SAARI_PROJECT_ROOT`.

Every mutating tool persists to the project DB and writes raw API payloads to
`.saaristo/raw/<source>/<id>.json` — the agent can `Read` those directly when it
wants ground-truth JSON instead of normalized fields.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

from pathlib import Path

from saari import canvas as _canvas_mod, db, paths, study as _study
from saari.embed import (
    embed_papers as _embed_papers,
    query_corpus as _query_corpus,
    similar_to_paper as _similar_to_paper,
)
from saari.export import (
    export_bibtex as _export_bibtex,
    export_paper as _export_paper,
    export_prisma as _export_prisma,
    export_slides as _export_slides,
    export_slr as _export_slr,
)
from saari.guide import MCP_INSTRUCTIONS, guide_data as _guide_data
from saari.models import Paper
from saari.projection import project_corpus as _project_corpus
from saari.snowball import snowball as _snowball
from saari.snowball_batch import snowball_seeds as _snowball_seeds
from saari.triage import rank_papers as _rank_papers

mcp = FastMCP("saari", instructions=MCP_INSTRUCTIONS)


class PaperCard(BaseModel):
    """Compact paper summary — returned from list / search / triage tools so
    multi-paper responses stay small. Call `paper_show` for the full record."""

    id: str
    title: str
    year: int | None = None
    cited_by_count: int | None = None
    venue: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    oa_status: str | None = None
    status: str = "candidate"
    seen_in: int = 0
    abstract_suspect: bool = False
    abstract_excerpt: str | None = None


def _to_card(p: Paper, include_excerpt: bool = False) -> PaperCard:
    excerpt: str | None = None
    if include_excerpt and p.abstract:
        s = " ".join(p.abstract.split())
        excerpt = s if len(s) <= 240 else s[:239].rstrip() + "…"
    return PaperCard(
        id=p.id,
        title=p.title,
        year=p.year,
        cited_by_count=p.cited_by_count,
        venue=p.venue,
        doi=p.doi,
        arxiv_id=p.arxiv_id,
        oa_status=p.oa_status,
        status=p.status,
        seen_in=p.seen_in,
        abstract_suspect=p.abstract_suspect,
        abstract_excerpt=excerpt,
    )


@mcp.tool()
def guide() -> dict[str, Any]:
    """Start here. The workflow map in one call: pipeline stages in order, the
    tools for each, what each tool requires, this project's state per stage,
    and `next` = {stage, tool, why}, the recommended next call.
    """
    return _guide_data()


@mcp.tool()
def project_info() -> dict[str, Any]:
    """Project root, key paths, corpus counts by status, and pipeline counts.

    `pipeline.embedded` < `n_papers` means run `refresh` before `query`,
    `papers_similar`, or relevance triage. For "what next?" call `guide`.
    """
    root = paths.project_root()
    with db.connect(paths.db_path(root)) as con:
        n_papers = con.execute("SELECT COUNT(*) FROM paper").fetchone()[0]
        n_searches = con.execute("SELECT COUNT(*) FROM search").fetchone()[0]
        by_status = db.count_by_status(con)
        n_embedded = con.execute("SELECT COUNT(*) FROM paper_embedding_meta").fetchone()[0]
        n_projected = con.execute("SELECT COUNT(*) FROM paper_projection").fetchone()[0]
    return {
        "root": str(root),
        "db": str(paths.db_path(root)),
        "raw_dir": str(paths.raw_dir(root)),
        "papers_dir": str(paths.papers_dir(root)),
        "n_papers": n_papers,
        "n_searches": n_searches,
        "by_status": by_status,
        "pipeline": {
            "embedded": n_embedded,
            "projected": n_projected,
        },
    }


@mcp.tool()
def review_status() -> dict[str, Any]:
    """Where the review stands: the five SLR stages (protocol, search, screen,
    snowball, report), each with `state` (done | active | todo), `detail`,
    and a methodology `hint`; `next` is the first stage not done. `guide`
    wraps this with tool names and a concrete next call.
    """
    from saari.status import stages_data

    return stages_data(project_root=paths.project_root())


@mcp.tool()
def search(
    query: str,
    limit: int = 25,
    year_from: int | None = None,
    year_to: int | None = None,
    source: str = "openalex",
    scope: str = "all",
    fetch_all: bool = False,
    max_records: int | None = None,
) -> dict[str, Any]:
    """Keyword search a bibliographic source; persist hits as candidates.

    Keyword search is noisy. Run several phrasings, then `refresh` and
    `query` (a 2-3 sentence prose description beats keywords) or `triage`
    to re-rank semantically. Set the protocol (`study_set`) first.

    `source`: "openalex" (default, keyless), "scopus" (needs
    SCOPUS_API_KEY, plus SCOPUS_INST_TOKEN off a registered institutional
    network; records may lack abstracts), or "crossref" (keyless, abstracts
    often missing). Deduplicated across sources by DOI / PMID / arXiv id.

    Completeness: `n_available` is the source's total for the query and
    `truncated` is True when only a sample was kept. A systematic review
    must screen everything a query returns: when truncated, rerun with
    `fetch_all=True` or narrow the query.
    - `limit`: records to fetch (pages automatically).
    - `fetch_all`: every match; refused above the safety cap `max_records`
      (default SAARI_SEARCH_MAX_RECORDS or 2000).
    - `scope`: "all" (OpenAlex also matches full text) or "title_abstract"
      (less noisy; not available for crossref).

    Returns `{search_id, n_fetched, n_available, truncated, n_new,
    n_duplicate, paper_ids}`, no paper
    data; read it with `triage` / `papers_list` / `paper_show`.
    """
    from saari.sources import run_search

    return run_search(
        query,
        source=source,
        limit=limit,
        year_from=year_from,
        year_to=year_to,
        project_root=paths.project_root(),
        scope=scope,
        fetch_all=fetch_all,
        max_records=max_records,
    )


@mcp.tool()
def papers_list(
    limit: int = 20,
    offset: int = 0,
    status: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    min_citations: int | None = None,
    title_grep: str | None = None,
    seen_in_at_least: int | None = None,
    sort: str = "recent",
    include_excerpt: bool = False,
) -> dict[str, Any]:
    """List papers with composable filters as compact PaperCards `{n, papers}`.

    For the screening queue prefer `triage` (relevance-ranked); use this to
    browse a status (e.g. status="included") or filter by year/citations.

    Filters:
    - `status`: candidate | included | excluded | maybe
    - `year_from` / `year_to`: inclusive
    - `min_citations`: minimum cited_by_count
    - `title_grep`: case-insensitive title substring
    - `seen_in_at_least`: paper appeared in at least N distinct searches (useful for relevance)

    `sort`: recent (first_seen_at desc, default) | cited (citations desc) | seen_in (overlap desc).
    `include_excerpt`: include up to 240 chars of abstract in each card. Call `paper_show` for full.
    """
    root = paths.project_root()
    order_by = {
        "recent": "first_seen_at DESC",
        "cited": "COALESCE(cited_by_count, 0) DESC",
        "seen_in": "seen_in DESC, COALESCE(cited_by_count, 0) DESC",
    }.get(sort, "first_seen_at DESC")

    with db.connect(paths.db_path(root)) as con:
        papers = db.list_papers(
            con,
            limit=limit,
            offset=offset,
            status=status,
            year_from=year_from,
            year_to=year_to,
            min_citations=min_citations,
            title_grep=title_grep,
            seen_in_at_least=seen_in_at_least,
            order_by=order_by,
        )
    return {
        "n": len(papers),
        "papers": [_to_card(p, include_excerpt=include_excerpt).model_dump() for p in papers],
    }


@mcp.tool()
def triage(
    limit: int = 20,
    sort: str = "auto",
    query: str | None = None,
    min_citations: int | None = None,
    year_from: int | None = None,
    seen_in_at_least: int | None = None,
) -> dict[str, Any]:
    """The screening queue: top undecided (candidate) papers with abstract
    excerpts. Scan the cards, `paper_show` if unsure, then `screen` each.

    `sort`:
    - `relevance`: cosine similarity to `query` if given, else to the study
      protocol (title + question + criteria). Each card gets `score`.
    - `seen_in`: found by the most searches/snowballs first.
    - `cited`: citation count (surfaces famous, often off-topic papers).
    - `auto` (default): relevance when a protocol/query and embeddings exist,
      else seen_in. `sort` in the reply is the order actually applied;
      `note` explains any fallback (e.g. run `refresh` to embed).
    Pass `query` for "most relevant undecided papers about X".

    Returns `{n_candidates, n_matching, shown, sort, basis, papers}`;
    `n_candidates` is the whole remaining pool, not the slice.
    """
    root = paths.project_root()
    try:
        r = _rank_papers(
            status="candidate",
            sort=sort,
            query=query,
            limit=limit,
            min_citations=min_citations,
            year_from=year_from,
            seen_in_at_least=seen_in_at_least,
            project_root=root,
        )
    except ValueError as e:
        return {"error": str(e), "papers": []}
    with db.connect(paths.db_path(root)) as con:
        counts = db.count_by_status(con)
    cards = []
    for p in r.papers:
        card = _to_card(p, include_excerpt=True).model_dump()
        if r.sort == "relevance":
            card["score"] = r.scores.get(p.id)
        cards.append(card)
    out: dict[str, Any] = {
        "n_candidates": counts.get("candidate", 0),
        "n_matching": r.n_matching,
        "shown": len(cards),
        "sort": r.sort,
        "basis": r.basis,
        "papers": cards,
    }
    if r.note:
        out["note"] = r.note
    return out


@mcp.tool()
def screen(
    paper_id: str,
    decision: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Record a screening decision: included | excluded | maybe | candidate.

    Always pass `note`: the reason, phrased against the protocol criteria
    (notes feed the PRISMA exclusion box). Aliases: include, exclude, reject,
    keep, yes, no, reset. Returns `{paper_id, status, ok}`; ok=False means
    the id was not found. Next: more `triage`, or `snowball_included`.
    """
    root = paths.project_root()
    normalized = db.normalize_status(decision)
    with db.connect(paths.db_path(root)) as con:
        ok = db.set_screening(con, paper_id, normalized, note=note)
    return {"paper_id": paper_id, "status": normalized, "ok": ok}


@mcp.tool()
def paper_show(paper_id: str) -> dict[str, Any] | None:
    """Full Paper record: authors, full abstract, referenced_works, locations,
    screening status + note. Use before a borderline `screen` decision.

    Accepts a paper id (`openalex:Wxxx`), DOI, arXiv id, or PMID. Returns
    null if unknown.
    """
    root = paths.project_root()
    with db.connect(paths.db_path(root)) as con:
        p = db.get_paper(con, paper_id)
    return p.model_dump(mode="json") if p else None


@mcp.tool()
def paper_links(paper_id: str) -> dict[str, Any] | None:
    """Where a paper is hosted: DOI, landing page, pdf_url, and all
    `locations` (publisher, arXiv, PMC, repositories). For a readable copy
    look for `locations[].pdf_url` with `is_oa` true. Null if unknown.
    """
    root = paths.project_root()
    with db.connect(paths.db_path(root)) as con:
        p = db.get_paper(con, paper_id)
    if not p:
        return None
    return {
        "id": p.id,
        "title": p.title,
        "doi": p.doi,
        "arxiv_id": p.arxiv_id,
        "pmid": p.pmid,
        "pmcid": p.pmcid,
        "oa_status": p.oa_status,
        "landing_page_url": p.landing_page_url,
        "pdf_url": p.pdf_url,
        "locations": [loc.model_dump() for loc in p.locations],
    }


@mcp.tool()
def paper_raw(paper_id: str) -> dict[str, Any] | None:
    """The raw source API JSON captured at fetch time, as `{path, content}`.
    Use only when normalized fields look wrong or miss something; it is
    large. Null if no raw payload was stored.
    """
    root = paths.project_root()
    with db.connect(paths.db_path(root)) as con:
        raw_path = db.get_raw_path(con, paper_id)
    if not raw_path:
        return None
    full = root / raw_path
    if not full.exists():
        return None
    return {"path": str(full), "content": full.read_text()}


@mcp.tool()
def paper_add(identifiers: list[str], note: str | None = None) -> dict[str, Any]:
    """Add papers you already know by identifier (not via keyword search).

    Each identifier may be a DOI, a doi.org URL, an arXiv id or URL, a PMID
    (`pmid:12345` or PubMed URL), or an OpenAlex id/URL. Resolved via
    OpenAlex, with Crossref as the fallback for DOIs OpenAlex lacks. All are
    recorded as one `manual` identification event, reported in PRISMA under
    "other methods". Papers already in the corpus count as duplicates.

    Returns per-identifier `results` (`status`: added | duplicate |
    not_found | invalid | error) plus counts and `paper_ids`.
    """
    from saari.sources import add_papers

    return add_papers(identifiers, note=note, project_root=paths.project_root())


@mcp.tool()
def duplicates_list() -> dict[str, Any]:
    """List candidate duplicate pairs for the reviewer to resolve.

    Exact-DOI duplicates are already merged at ingest. These are weaker
    matches: shared PMID / arXiv id, or same normalized title + year (+-1)
    + first-author surname (preprint vs published version). Nothing is
    merged automatically. Resolve each with `papers_merge` or
    `duplicates_dismiss`; `suggested_keep` is only a hint.
    """
    from saari import dedup

    with db.connect(paths.db_path(paths.project_root())) as con:
        return {"candidates": dedup.find_candidates(con)}


@mcp.tool()
def papers_merge(keep: str, remove: str, reason: str | None = None) -> dict[str, Any]:
    """Merge confirmed duplicate `remove` into `keep`.

    Fills missing ids/fields on `keep`, preserves `remove`'s provenance and
    search links, keeps `remove` resolvable as an alias, and counts one more
    "duplicate removed" in the PRISMA funnel. Refused when the two records
    have conflicting screening decisions.
    """
    from saari import dedup

    with db.connect(paths.db_path(paths.project_root())) as con:
        return dedup.merge(con, keep, remove, reason=reason)


@mcp.tool()
def duplicates_dismiss(a: str, b: str, note: str | None = None) -> dict[str, Any]:
    """Record that candidate pair `a` / `b` are distinct works (stops suggesting it)."""
    from saari import dedup

    with db.connect(paths.db_path(paths.project_root())) as con:
        return dedup.dismiss(con, a, b, note=note)


@mcp.tool()
def paper_text(
    paper_id: str,
    offset: int = 0,
    max_chars: int = 4000,
    grep: str | None = None,
    fetch: bool = True,
    refresh: bool = False,
) -> dict[str, Any]:
    """Read a paper's open-access full text, a bounded slice at a time.

    First call downloads an OA copy (OA locations, PMC, arXiv; never
    paywalled) and caches PDF + text under `.saaristo/fulltext/`; later
    calls are offline. Returns metadata (`status`, `reason`, `n_chars`,
    `n_pages`, `quality`, `source_url`, `source_version`, `text_path`) and:

    - without `grep`: `text` = chars `[offset, offset+max_chars)` (cap
      50000), `next_offset` (None at end), `page_start`/`page_end`;
    - with `grep` (case-insensitive regex): `hits[]` of `{offset, page,
      snippet}`; page to a hit with `offset`.

    `status` is available | unavailable (no OA copy) | failed (with reason).
    `quality`: good | low_text | garbled. `refresh` retries a recorded
    failure. `fetch=false` reads only what is cached. Use after title/
    abstract screening, on included/maybe papers.
    """
    from saari import fulltext as _ft

    try:
        return _ft.read_text(
            paper_id, offset=offset, max_chars=max_chars, grep=grep,
            fetch_if_missing=fetch, refresh=refresh, project_root=paths.project_root(),
        )
    except (KeyError, _ft.FulltextDependencyError) as e:
        return {"paper_id": paper_id, "error": str(e).strip("'\"")}


@mcp.tool()
def fulltext_fetch(
    status: str | None = "included",
    refresh: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """Batch-fetch open-access full text for papers with screening `status`.

    Default `included`; pass "maybe" for borderline papers, or null for all.
    Already-recorded papers are skipped unless `refresh=True`. Returns
    counts (`n_available`, `n_unavailable`, `n_failed`, `n_from_cache`)
    plus `missing[]` with a reason per paper without full text. Synchronous
    and polite (1 s per host), so batch a few dozen at a time with `limit`.
    """
    from saari import fulltext as _ft

    try:
        return _ft.fetch_many(
            status=status, refresh=refresh, limit=limit, project_root=paths.project_root(),
        )
    except _ft.FulltextDependencyError as e:
        return {"error": str(e)}


@mcp.tool()
def searches_list(limit: int = 20) -> dict[str, Any]:
    """Past searches and snowball events (source, query, n_returned, created_at).

    Use to avoid repeating a phrasing and to report the search strategy.
    """
    root = paths.project_root()
    with db.connect(paths.db_path(root)) as con:
        rows = db.list_searches(con, limit=limit)
    return {"searches": rows}


@mcp.tool()
def snowball(
    paper_id: str,
    direction: str = "both",
    max_per_direction: int | None = None,
    max_backward: int | None = None,
    max_forward: int | None = 25,
    max_records: int | None = None,
) -> dict[str, Any]:
    """Pull one seed paper's citation neighbors in as candidates.

    `direction`: backward (papers it cites; all references by default,
    `max_backward`) | forward (papers citing it; 25 by default,
    `max_forward=None` = all up to `max_records`) | both.
    `max_per_direction` is legacy and overrides both limits when set.

    Returns counts + new ids, plus completeness per direction (`backward` /
    `forward`: `n_available`, `n_retrieved`, `truncated`). A survey seed
    yields its whole bibliography in one call. For all included papers use
    `snowball_included`. Next: `refresh`, `triage`.
    """
    root = paths.project_root()
    r = _snowball(
        paper_id,
        direction=direction,
        max_per_direction=max_per_direction,
        project_root=root,
        max_backward=max_backward,
        max_forward=max_forward,
        max_records=max_records,
    )
    return {
        "seed": r.seed_paper_id,
        "direction": r.direction,
        "n_fetched": r.n_fetched,
        "n_new": r.n_new,
        "backward_paper_ids": r.backward_paper_ids,
        "forward_paper_ids": r.forward_paper_ids,
        "skipped_reason": r.skipped_reason,
        **r.completeness(),
    }


@mcp.tool()
def snowball_included(
    top_n: int | None = None,
    sort: str = "auto",
    direction: str = "both",
    max_backward: int | None = None,
    max_forward: int | None = 25,
    skip_done: bool = True,
) -> dict[str, Any]:
    """Snowball every included paper in one call (or the top `top_n` by
    `sort`: auto | relevance | seen_in | cited, as in `triage`).

    `skip_done` (default) skips seeds already snowballed in this direction,
    so re-running after each screening round snowballs only new includes.
    Per seed: all references backward, `max_forward` citers forward (None =
    all up to the safety cap); `n_truncated` counts seeds cut short.
    Returns totals `{n_seeds, n_skipped_done, n_fetched, n_new, saturated}`,
    per-seed rows, and per-seed `errors` (a bad seed does not stop the run).
    `saturated`=true: this round found nothing new. Next: `refresh`, `triage`.
    """
    try:
        return _snowball_seeds(
            status="included",
            top_n=top_n,
            sort=sort,
            direction=direction,
            max_backward=max_backward,
            max_forward=max_forward,
            skip_done=skip_done,
            project_root=paths.project_root(),
        )
    except ValueError as e:
        return {"error": str(e)}


@mcp.tool()
def embed(only_missing: bool = True) -> dict[str, Any]:
    """Embed papers locally (all-MiniLM-L6-v2, 384-dim; ~80MB model download
    on first use). Idempotent with only_missing=True. Usually call `refresh`
    instead, which also projects and redraws the canvas.

    Returns `{n_embedded, n_skipped, model, dim, elapsed_sec}`.
    """
    r = _embed_papers(only_missing=only_missing, project_root=paths.project_root())
    return {
        "n_embedded": r.n_embedded,
        "n_skipped": r.n_skipped,
        "model": r.model,
        "dim": r.dim,
        "elapsed_sec": round(r.elapsed_sec, 3),
    }


@mcp.tool()
def query(
    text: str,
    k: int = 20,
    status: str | None = None,
    include_excerpt: bool = True,
) -> dict[str, Any]:
    """Semantic search over the corpus: cards ranked by cosine `score`.

    Write `text` as 2-3 sentences describing what a relevant paper says;
    that works far better than keywords. Requires `refresh` (or `embed`)
    after new papers arrive. `status` filters after a 3x over-fetch, so it
    can return fewer than k; for "most relevant undecided" use
    `triage(query=...)`, which scores every candidate.

    Returns `{query, n, papers}`; thin results on a relevant phrasing mean
    `search` again with new terms.
    """
    root = paths.project_root()
    hits = _query_corpus(text, k=max(k * 3, k), project_root=root)
    if not hits:
        return {"query": text, "n": 0, "papers": [], "note": "No embedded papers. Run `embed` first."}

    score_by_id = {h.paper_id: h.score for h in hits}
    with db.connect(paths.db_path(root)) as con:
        placeholders = ",".join(["?"] * len(hits))
        sql = (
            "SELECT p.*, "
            "(SELECT COUNT(DISTINCT search_id) FROM search_result sr WHERE sr.paper_id = p.id) AS seen_in "
            f"FROM paper p WHERE p.id IN ({placeholders})"
        )
        args: list[Any] = [h.paper_id for h in hits]
        if status is not None:
            sql += " AND p.status = ?"
            args.append(status)
        rows = con.execute(sql, args).fetchall()
        papers = sorted(
            (db._row_to_paper(r) for r in rows),
            key=lambda p: score_by_id.get(p.id, 0.0),
            reverse=True,
        )[:k]

    return {
        "query": text,
        "n": len(papers),
        "papers": [
            {
                **_to_card(p, include_excerpt=include_excerpt).model_dump(),
                "score": round(score_by_id[p.id], 4),
            }
            for p in papers
        ],
    }


@mcp.tool()
def papers_similar(
    paper_id: str,
    k: int = 10,
    status: str | None = None,
    include_excerpt: bool = True,
) -> dict[str, Any]:
    """Find "more like this": papers nearest a seed paper's embedding, seed
    excluded, each card with `score`. Seed must be embedded (`refresh`).
    Good after a strong include; `status="candidate"` limits to undecided.
    """
    root = paths.project_root()
    try:
        hits = _similar_to_paper(paper_id, k=max(k * 3, k), project_root=root)
    except ValueError as e:
        return {"error": str(e), "papers": []}

    score_by_id = {h.paper_id: h.score for h in hits}
    with db.connect(paths.db_path(root)) as con:
        placeholders = ",".join(["?"] * len(hits)) or "NULL"
        sql = (
            "SELECT p.*, "
            "(SELECT COUNT(DISTINCT search_id) FROM search_result sr WHERE sr.paper_id = p.id) AS seen_in "
            f"FROM paper p WHERE p.id IN ({placeholders})"
        )
        args: list[Any] = [h.paper_id for h in hits]
        if status is not None:
            sql += " AND p.status = ?"
            args.append(status)
        rows = con.execute(sql, args).fetchall()
        papers = sorted(
            (db._row_to_paper(r) for r in rows),
            key=lambda p: score_by_id.get(p.id, 0.0),
            reverse=True,
        )[:k]

    return {
        "seed": paper_id,
        "n": len(papers),
        "papers": [
            {
                **_to_card(p, include_excerpt=include_excerpt).model_dump(),
                "score": round(score_by_id[p.id], 4),
            }
            for p in papers
        ],
    }


@mcp.tool()
def project(
    method: str = "umap",
    n_neighbors: int = 15,
    min_dist: float = 0.1,
) -> dict[str, Any]:
    """Project embedded papers to 2D (UMAP; PCA fallback) for the canvas.

    Prerequisite: `embed`. Usually call `refresh` instead.
    Returns `{n_points, method, params, x_range, y_range, note}`.
    """
    r = _project_corpus(
        method=method,
        n_neighbors=n_neighbors,
        min_dist=min_dist,
        project_root=paths.project_root(),
    )
    return {
        "n_points": r.n_points,
        "method": r.method,
        "params": r.params,
        "x_range": r.x_range,
        "y_range": r.y_range,
        "note": r.note,
    }


@mcp.tool()
def canvas(
    format: str = "both",
    status: str | None = None,
    out: str | None = None,
) -> dict[str, Any]:
    """Write the corpus map: Obsidian .canvas and/or self-contained HTML
    (`format`: obsidian | html | both) to `papers/landscape.*`, for a human
    to spot clusters and gaps. Prerequisite: embed + project (`refresh`
    does all three). Returns the written paths and node counts.
    """
    root = paths.project_root()
    default_dir = paths.papers_dir(root)
    results: dict[str, Any] = {}

    if format in ("obsidian", "both"):
        target = Path(out) if out and format == "obsidian" else default_dir / "landscape.canvas"
        results["obsidian"] = _canvas_mod.write_obsidian_canvas(
            target, status=status, project_root=root
        )
    if format in ("html", "both"):
        target = Path(out) if out and format == "html" else default_dir / "landscape.html"
        results["html"] = _canvas_mod.write_html_viewer(
            target, status=status, project_root=root
        )
    return results


@mcp.tool()
def export_bibtex(
    status: str | None = "included",
    out: str | None = None,
) -> dict[str, Any]:
    """Write BibTeX for citing from the draft (default: included papers to
    `papers/refs.bib`; keys `<lastname><year><firstword>`). Noisy or very
    long abstracts are dropped. Returns `{path, n_entries, format}`.
    """
    root = paths.project_root()
    target = Path(out) if out else paths.papers_dir(root) / "refs.bib"
    r = _export_bibtex(target, status_filter=status, project_root=root)
    return {"path": r.path, "n_entries": r.n_entries, "format": r.format}


@mcp.tool()
def export_prisma(fmt: str = "svg", out: str | None = None) -> dict[str, Any]:
    """Write the PRISMA 2020 flow diagram (`fmt`: svg | mermaid; default
    `papers/prisma.svg`). Counts trace to the DB (`funnel`). Already
    included in `export_slr`. Returns `{path, n_entries, format}`.
    """
    root = paths.project_root()
    ext = "mmd" if fmt == "mermaid" else "svg"
    target = Path(out) if out else paths.papers_dir(root) / f"prisma.{ext}"
    r = _export_prisma(target, fmt=fmt, project_root=root)
    return {"path": r.path, "n_entries": r.n_entries, "format": r.format}


@mcp.tool()
def export_paper(out: str | None = None) -> dict[str, Any]:
    """Write the Markdown SLR manuscript scaffold (default `papers/paper.md`)
    plus prisma.svg, landscape.svg, refs.bib. Counts, tables, and references
    trace to the DB; synthesis is left as `<!-- WRITE: -->` slots. Prefer
    `export_slr` for the full bundle. Returns `{path, n_entries, format}`.
    """
    root = paths.project_root()
    target = Path(out) if out else paths.papers_dir(root) / "paper.md"
    r = _export_paper(target, project_root=root)
    return {"path": r.path, "n_entries": r.n_entries, "format": r.format}


@mcp.tool()
def export_slides(out: str | None = None) -> dict[str, Any]:
    """Write the Marp slide deck (default `papers/slides.md`). Already
    included in `export_slr`. Returns `{path, n_entries, format}`.
    """
    root = paths.project_root()
    target = Path(out) if out else paths.papers_dir(root) / "slides.md"
    r = _export_slides(target, project_root=root)
    return {"path": r.path, "n_entries": r.n_entries, "format": r.format}


@mcp.tool()
def export_slr(out_dir: str | None = None) -> dict[str, Any]:
    """Final step: write the full review bundle to `papers/review/` (paper.md,
    slides.md, prisma.svg/.mmd, landscape.svg, refs.bib). Needs the protocol
    set and papers screened. Then fill the `<!-- WRITE: -->` slots in
    paper.md from included papers only. Returns `{path, n_entries, format}`.
    """
    root = paths.project_root()
    r = _export_slr(Path(out_dir) if out_dir else None, project_root=root)
    return {"path": r.path, "n_entries": r.n_entries, "format": r.format}


@mcp.tool()
def refresh() -> dict[str, Any]:
    """Embed new papers, re-project, and redraw the canvas in one call.

    Call after every `search` / `snowball` round, before `query`, relevance
    `triage`, or `papers_similar`. Returns per-stage counts + artifact paths.
    """
    root = paths.project_root()
    e = _embed_papers(only_missing=True, project_root=root)
    p = _project_corpus(project_root=root)
    if p.note:
        return {"embed": e.__dict__, "project": {"note": p.note}, "canvas": None}
    papers_dir = paths.papers_dir(root)
    oc = _canvas_mod.write_obsidian_canvas(
        papers_dir / "landscape.canvas", project_root=root
    )
    hv = _canvas_mod.write_html_viewer(
        papers_dir / "landscape.html", project_root=root
    )
    return {
        "embed": {
            "n_embedded": e.n_embedded,
            "n_skipped": e.n_skipped,
            "elapsed_sec": round(e.elapsed_sec, 3),
        },
        "project": {
            "n_points": p.n_points,
            "method": p.method,
            "x_range": p.x_range,
            "y_range": p.y_range,
        },
        "canvas": {"obsidian": oc, "html": hv},
    }


@mcp.tool()
def study_get() -> dict[str, Any]:
    """The review protocol: title, authors, research question, criteria, tags.

    Read it before screening so decisions cite the criteria. Empty strings
    mean unset; set with `study_set`.
    """
    return _study.get(project_root=paths.project_root())


@mcp.tool()
def study_set(
    title: str | None = None,
    authors: str | None = None,
    question: str | None = None,
    criteria: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Set / patch the review protocol. Do this before the first `search`.
    Only fields you pass change. `question` + `criteria` also drive the
    relevance sort in `triage`.

    - `title`: manuscript title (used by `export_paper`).
    - `authors`: author line (free text).
    - `question`: the research question (markdown OK).
    - `criteria`: inclusion/exclusion criteria (markdown OK).
    - `tags`: free-form tags. Pass `[]` to clear, `None` to leave alone.

    Returns the full updated study record.
    """
    return _study.update(
        title=title,
        authors=authors,
        question=question,
        criteria=criteria,
        tags=tags,
        project_root=paths.project_root(),
    )


@mcp.tool()
def funnel() -> dict[str, Any]:
    """PRISMA-style counts: searches, records fetched, unique, screened, by
    status, embedded/projected, plus per-search rows. Use for progress
    summaries and to check `export_slr` numbers.
    """
    return _study.funnel(project_root=paths.project_root())


def main() -> None:
    """Run the MCP server.

    Default: stdio, for local harnesses (Claude Code, Claude Desktop) that
    spawn the process per project directory.

    `saari-mcp --http` serves the same tools over streamable HTTP for remote
    hosts (Copilot Studio, hosted deployments). With SAARI_DATA_ROOT set,
    each request is scoped to the caller's project directory via the shared
    hosting middleware; without it, the server serves the project it was
    started in, like stdio mode.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="saari-mcp")
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3045)
    args = parser.parse_args()

    from saari import log

    # stderr/file only: in stdio mode stdout carries the JSON-RPC stream.
    log.setup(propagate=False)

    if not args.http:
        mcp.run()
        return

    import uvicorn

    from saari.hosting import RequestRootMiddleware

    # Stateless: no server-side session pinning, so replicas stay
    # interchangeable and simple HTTP clients work.
    mcp.settings.stateless_http = True
    app = RequestRootMiddleware(mcp.streamable_http_app())
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
