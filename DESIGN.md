# saari — design

A toolkit for agent-driven literature reviews. Not an app with a "do literature review" button — a set of small, composable skills an agent (Claude Code, a custom agent, a human at a CLI) calls in whatever order the work requires.

The user is writing a paper. They want their agent to be able to: start a new literature study, find relevant papers, map the current research landscape, surface gaps, and feed the result back into drafting.

## Two services on a shared core

Per the workspace CLAUDE.md:

- `litreview` — finding, screening, organizing papers (study lifecycle, search, resolve, snowball, embed, screen, query, export).
- `visualize` — projecting and analyzing the corpus (cluster, project, gap analysis, render).

Both expose MCP servers and CLIs. Both read/write the same SQLite database. The split is conceptual: `visualize` can be invoked against an existing study without booting the ingest stack.

## Core concept: agent-callable skills

Each "skill" is one MCP tool / one CLI subcommand. Design rules for every skill:

- **One verb per skill.** `search`, `embed`, `snowball`, not `ingest_and_embed_and_cluster`. The agent composes.
- **Idempotent by default.** `embed(only_missing=true)`, `resolve_abstracts(only_missing=true)`, `snowball` dedupes. Agents will retry; we don't want double-charges or doubled state.
- **Side effects persist; returns are summaries.** `search` adds papers to the study DB and returns `{n_added, n_duplicate, search_id}` — not the paper list. The agent calls `paper_list` if it needs the rows. Keeps tool outputs small (context-window-friendly).
- **Cost transparency.** Any tool that hits a paid API returns `cost_estimate` (before) or `cost_actual` (after). Agents can choose to confirm with the user before expensive calls.
- **Long-running tools return a job_id.** Embedding 1000 papers takes minutes; we return `{job_id}` immediately and expose `job_status(job_id)`. Agents poll instead of blocking a tool call.
- **Every mutating call writes a study event** (timestamp, tool, args, summary of result). The event log is what `study/` will use to observe how agents conduct reviews — and what the user uses to retrace a session.

## The workflow we're enabling

A typical session, expressed as the calls an agent would make:

```
study_create(name="reflective-equilibrium-in-ml-ethics", goal="map current debate, find gaps")
  → study_id

# Cast a wide net
search(study_id, query="reflective equilibrium machine learning ethics", source="openalex", limit=200)
search(study_id, query="wide reflective equilibrium AI alignment", source="openalex", limit=200)
  → {n_added: 312, n_duplicate: 88, ...}

# Fill in missing abstracts via Crossref + portal hints
resolve_abstracts(study_id, only_missing=true)
  → {n_resolved: 287, n_failed: 25}

# Embed everything new
embed(study_id, only_missing=true)
  → {job_id: "..."}
job_status(job_id)
  → {status: "done", n_embedded: 287, cost_actual_usd: 0.04}

# Project + cluster
project(study_id, method="umap")
cluster(study_id, method="hdbscan", min_cluster_size=8)
  → {n_clusters: 11, n_noise: 34, run_id: "..."}

# Inspect what we found
cluster_summary(study_id, run_id="...")
  → per-cluster: top keywords, 3 exemplar papers, size

# Query the corpus
query(study_id, "papers that critique narrow reflective equilibrium specifically", k=20)
  → ranked list

# Expand promising threads
snowball(study_id, paper_id="W123...", direction="both", depth=1)

# Screen
screen(study_id, paper_id="W123...", decision="include", reason="central to debate")
screen(study_id, paper_id="W456...", decision="exclude", reason="off-topic, just shares term")

# Find what's missing
gap_report(study_id)
  → {sparse_clusters: [...], suggested_queries: [...], narrative: "..."}

# Hand to the writer
export(study_id, format="bibtex", status_filter="included", out_path="../paper/refs.bib")
render(study_id, format="html", out_path="../paper/figs/landscape.html")
```

The agent decides when to loop, when to widen the search, when to stop. Skills don't enforce a pipeline.

## Skill surface (v1)

### `litreview`

Skills marked **live** are implemented on both CLI (`saari <skill>`) and MCP (`saari-mcp`). Others are roadmap. Since studies are now project-scoped, most skills don't take a `study_id` — the project *is* the study.

| Skill | Purpose | Returns |
|---|---|---|
| `init(path?)` | **live** — scaffold a new saaristo project (`.saaristo/` + `papers/`) | path |
| `project_info()` / `where` | **live** — active project root, paths, counts by status | summary |
| `search(query, limit, year_from?, year_to?)` | **live** — query OpenAlex, persist hits + raw JSON | `{search_id, n_fetched, n_new, n_duplicate, paper_ids}` |
| `paper_show(id)` | **live** — full Paper record | Paper |
| `paper_links(id)` | **live** — all hosting locations (publisher / arXiv / PMC / repos) | Location[] |
| `paper_raw(id)` | **live** — raw source API response as captured at fetch time | `{path, content}` |
| `papers_list(status?, year_from?, year_to?, min_citations?, title_grep?, seen_in_at_least?, sort?)` | **live** — filtered, sorted list as compact cards | `PaperCard[]` with `seen_in` and `status` |
| `triage(limit, min_citations?, year_from?)` | **live** — top undecided candidates as cards w/ abstract excerpts, citation-sorted | `{n_candidates, papers}` |
| `screen(paper_id, decision, note?)` | **live** — mark include / exclude / maybe; accepts aliases | `{status, ok}` |
| `snowball(paper_id, direction, max_per_direction)` | **live** — backward (referenced_works) / forward (cites:) / both | `{n_fetched, n_new}` |
| `searches_list(limit)` | **live** — search + snowball history | rows |
| `embed(only_missing=true)` | **live** — local embeddings via fastembed + `sentence-transformers/all-MiniLM-L6-v2` (384-dim) stored in sqlite-vec; no API cost | `{n_embedded, n_skipped, model, dim, elapsed_sec}` |
| `query(text, k=20, status?)` | **live** — semantic cosine search over the embedded corpus; returns cards with scores | `{query, n, papers}` with `score` per card |
| `papers_similar(paper_id, k=10, status?)` | **live** — rank corpus by cosine to a seed paper's embedding; seed excluded | `{seed, n, papers}` with `score` per card |
| `export_bibtex(status="included", out?)` | **live** — BibTeX export with `<author><year><word>` keys; strips abstract pollution and caps length at 1500 chars | `{path, n_entries, format}` |
| `refresh()` | **live** — one-shot: embed (only_missing) → project → canvas(both) | per-stage summary |
| `resolve_abstracts(only_missing=true)` | roadmap — fill bad/missing abstracts via Crossref + portal hints | `{n_resolved, n_failed}` |
| `suggest_screening(criteria)` | roadmap — LLM-assisted triage suggestions (non-committing) | suggestions |
| `export(format, status_filter?, out_path)` | roadmap — bibtex / json / csv | path |
| `job_status(job_id)` | roadmap — long-running op status | `{status, progress, result?}` |

### `visualize`

| Skill | Purpose | Returns |
|---|---|---|
| `project(method="umap", n_neighbors?, min_dist?)` | **live** — 2D coords from embeddings (UMAP / PCA fallback); persists to `paper_projection` | `{n_points, method, x_range, y_range}` |
| `canvas(format=obsidian\|html\|both, out?, status?)` | **live** — writes Obsidian `.canvas` and/or a standalone interactive HTML scatter (SVG + pan/zoom/filters) | `{path, n_nodes, size_kb}` |
| `cluster(method, k? \| min_cluster_size?)` | roadmap — cluster assignments | `{cluster_run_id, n_clusters, n_noise}` |
| `cluster_summary(cluster_run_id?, cluster_id?)` | roadmap — top keywords + exemplars per cluster | summary |
| `gap_report(...)` | roadmap — sparse regions, under-cited areas, suggested queries | narrative + structured |

## Data model

Papers are global; per-study state is a join table. Re-finding a paper in another study reuses metadata and embedding — big efficiency win and matches how a researcher actually works (the same paper can matter to multiple projects).

```
Study(id, name, goal, status, created_at, updated_at)
Search(id, study_id, source, query, params_json, n_returned, status, created_at)
Paper(id, doi, title, abstract, authors_json, year, venue, source_provenance_json, ...)
PaperEmbedding(paper_id, model, vec)            # sqlite-vec
PaperStudy(paper_id, study_id, status, screening_note, source, added_at)
                                                # status: candidate|included|excluded|maybe
                                                # source: search:<id> | snowball:<paper_id> | manual
Citation(citing_paper_id, cited_paper_id, source)   # graph; populated on snowball
ProjectionRun(id, study_id, method, params_json, created_at)
ProjectionPoint(run_id, paper_id, x, y)
ClusterRun(id, study_id, method, params_json, created_at)
ClusterAssignment(run_id, paper_id, cluster_id)
ClusterLabel(run_id, cluster_id, top_keywords_json, exemplar_paper_ids_json, size)
StudyEvent(id, study_id, ts, tool, args_json, result_summary_json)
```

Notes:
- DOI is the natural identifier but not always present. `Paper.id` is internal (e.g. OpenAlex Work ID or a hash); DOI is unique-when-present.
- `source_provenance_json` records every source that contributed to a `Paper` and its raw payload — so we can re-derive fields if a parser changes.
- Multiple `ProjectionRun` / `ClusterRun` per study lets the agent try different params without losing prior runs.
- Eight AI-label fields from old saari → gone. Labels are derived per-cluster (`ClusterLabel`), not per-paper.

## What we drop from old saari (and why)

- **FastHTML web app, multi-user auth, settings tables.** The agent is the UI. Single-user local CLI + MCP. No web server in v1.
- **`ahoi` / `ioha` / `island` / `dnalsi` ORM stack.** Too much abstraction for a single-user single-DB tool. Direct SQL via `sqlite-utils` or a thin Pydantic layer.
- **Selenium Scopus scraper.** Crossref + OpenAlex cover the metadata + abstracts in most cases. Portal hints become an optional enrichment plugin, not the default path.
- **Eight per-paper AI-label fields.** Replaced by per-cluster labels (`ClusterLabel`).
- **One-shot notebooks as the workflow surface.** The skill set replaces them.

## What we mine for ideas

- **`Paper.from_crossref` / `Paper.from_elsevier` factory pattern** — keep the dispatch idea but Pydantic-based.
- **Portal hints** (`saari/utils/retriever/hints.py`) — port as an optional enrichment provider, used only when Crossref/OpenAlex don't have an abstract.
- **`DomainThrottler`** — keep, generalize. Critical when hitting publisher portals.
- **Cache layer** (`saari/utils/cache.py`) — keep the abstraction; back it with the same SQLite DB by default.
- **Per-publisher abstract parsers from `abstract-retriever/abstract_parsers/`** — port the structure (a registry of parsers keyed by domain) into the portal-hints plugin.
- **`citations_finder` from `abstract-retriever`** — the precursor to snowballing; the actual implementation will use OpenAlex's `referenced_works` and citing-works query, but the user-facing concept is the same.
- **OpenAI embeddings + `text-embedding-3-large` + sqlite-vec + UMAP pipeline** — same shape, cleaner code.

## Stack

- Python 3.12+, `uv` for env/dep management.
- **Storage:** SQLite + `sqlite-vec` extension. One DB file per workspace (default: `~/.saari/saari.db`).
- **Models:** Pydantic v2.
- **Sources:**
  - **OpenAlex** — primary search, citation graph, fast/free, no key required.
  - **Crossref** — metadata fallback + DOI lookup.
  - Portal-hints scraping behind a feature flag for stubborn publishers.
- **Embeddings:** OpenAI `text-embedding-3-large` (configurable; provider-agnostic interface).
- **Projection:** `umap-learn`.
- **Clustering:** `hdbscan` primary, `scikit-learn` KMeans secondary.
- **MCP:** official `mcp` Python SDK.
- **CLI:** Typer.
- **Optional LLM steps** (screening assist, gap-report narrative, cluster naming): use Anthropic SDK; gated behind explicit skills, never implicit.

## Repo layout (proposal)

```
code/
  pyproject.toml
  src/saari/
    core/
      models.py          # Pydantic: Paper, Study, Search, Citation, Projection, Cluster
      db.py              # SQLite + sqlite-vec; schema; migrations
      events.py          # study event log
      jobs.py            # async job queue for long-running ops
      cost.py            # cost tracking
    sources/
      base.py            # ABC: Source.search(), .get_by_doi(), .citations_of()
      openalex.py
      crossref.py
    enrich/
      portal_hints.py    # ported from old saari (optional)
      parsers/           # per-publisher abstract parsers (from abstract-retriever)
      llm_extract.py     # optional LLM cleanup
    embed/
      base.py
      openai.py
    analysis/
      cluster.py
      project.py
      gaps.py
    services/
      litreview/
        tools.py         # MCP tool definitions (one per skill)
        cli.py           # Typer CLI (mirrors tools.py)
        server.py        # MCP server entry point
      visualize/
        tools.py
        cli.py
        server.py
  tests/
    fixtures/            # the 11 backup paper JSONs from ~/dev/ai/novia/saari-backups/papers/
```

`tools.py` and `cli.py` are thin — both call into the same underlying functions in `core/`/`sources/`/`analysis/`. The MCP tool spec and the CLI subcommand spec are generated from the same function signatures where possible.

## Project layout on disk

Saari is **project-scoped, like `git`**. A project is any directory containing a `.saaristo/` folder; `saari init` creates one. Commands walk up from cwd to find the nearest root (or honor `SAARI_PROJECT_ROOT`).

```
my-paper-project/
├── .saaristo/                          # tool-owned state (analogous to .git/)
│   ├── saari.db                        # SQLite index (papers, searches, later: embeddings)
│   ├── raw/
│   │   └── openalex/
│   │       └── W4401667275.json        # immutable raw API responses
│   └── README.md                       # explains the folder; safe to gitignore or commit
├── papers/                             # user-visible full-text PDFs
│   └── …
├── manuscript.md                       # your paper (coexists with saari)
└── refs.bib                            # exported citations (coexists with saari)
```

Every paper project gets its own corpus — you don't want one project's literature bleeding into another's. The root is intended to be a place where other tools (git, Obsidian, LaTeX, reference managers) also operate: `.saaristo/` is the hidden tool state, `papers/` and anything else at root is open territory.

### Two-tier storage (raw + index)

| Tier | Purpose | Format |
|---|---|---|
| **Raw** (`.saaristo/raw/<source>/<id>.json`) | Ground truth, immutable audit trail, re-derivable if the extractor changes | One JSON file per API response |
| **Index** (`.saaristo/saari.db`) | Fast ops: search, embed, cluster, joins, cross-search overlap | SQLite + sqlite-vec |

The `paper.raw_path` column points from index → raw. An agent with just `Read` can `cat .saaristo/raw/openalex/Wxxx.json` without learning the CLI; an agent that wants fast queries uses the CLI. When OpenAlex mis-merges a record (observed: GPT-4 TR got a Dagstuhl DOI; a deep-learning review got a `SHILAP Revista de lepidopterología` mirror), the raw file preserves the ground truth.

### How this changes over time

When the wiki service comes back, it adds a third tier — **promoted** papers become markdown companions somewhere under `papers/` (or in an external vault) following the vault schema at `/Volumes/Nenya/vaults/CLAUDE.md`. Raw→index is free and automatic; index→wiki is user-directed and curated.

## Open questions to resolve before building

1. **Citation source for snowballing.** OpenAlex has `referenced_works` and a citing-works query and is free. Probably the only source we need for v1. Confirm. (Observation from dogfooding: `referenced_works` is often empty on arXiv-only records; fall back to `related_works`.)
2. **Gap analysis methodology.** Numeric (sparse clusters, low-density UMAP regions, weakly-cited central papers) + optional LLM narrative? Or LLM-only? Lean numeric-first with optional narrative.
3. **LLM-assisted screening.** v1 or v2? Argument for v1: it's the load-bearing reason to prefer this tool over Zotero+manual. Argument for v2: scope creep. Lean v1 but as an explicit, optional skill (`suggest_screening(study_id, criteria)` returns suggestions; the agent or user calls `screen` to commit).
4. **Studies as named collections or implicit?** Project-scoping makes "one study per project" the default, which may make the `Study` table unnecessary for v1 — searches and papers just live in the project. Revisit when we want multiple parallel reviews in one workspace.
5. **How `paper/` and `study/` consume the corpus.** Read the project DB directly? Export via a skill? The replay story (walk the event log) is probably the right fit for `study/`.
6. **Should the tool write to the vault directly, or just emit `wiki_capture`-style payloads?** Deferred with the wiki service itself.

## Anti-goals (v1)

- Web UI.
- Multi-user, sharing, sync.
- Reference manager replacement (Zotero etc.). We export to BibTeX; we don't manage your library.
- PDF full-text ingestion. (Possible v2 if needed for screening.)
- Real-time monitoring of new papers.
