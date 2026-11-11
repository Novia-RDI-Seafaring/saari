---
name: litreview
description: >
  Conduct a systematic literature review with the saari toolkit: set a review
  protocol, search OpenAlex, snowball references, screen with recorded reasons,
  map the corpus landscape, and export a PRISMA-traced SLR draft. Use when the
  user wants to do a literature review, find prior work, screen papers, build a
  reference list, or generate a PRISMA diagram or review manuscript scaffold.
---

# Systematic literature review with saari

saari is a local-first literature-review toolkit. All state lives in a
`.saaristo/` directory inside the current project folder (usually the folder
where the user is writing their paper). It exposes the same operations as a
CLI (`saari`), an MCP server (`saari-mcp`), and an HTTP server with a web UI
(`saari serve`). Prefer the MCP tools when they are available; fall back to
the CLI otherwise.

## Step 0: bootstrap

1. Check whether the saari MCP tools (`guide`, `search`, `screen`, ...)
   are available in this session. If yes, call `guide`: it returns the
   pipeline, each tool's prerequisites, and `next` (the recommended next
   call for this project's state). Then skip to Step 1.
2. Check the CLI: `uvx saari where`. If saari is not installed, install it
   with `uv tool install saari` (or use `uvx saari ...` per call).
3. If the folder is not yet a saari project, run `saari init`. This creates
   `.saaristo/` (tool state) and `papers/` (user-visible outputs).
4. Offer to register the MCP server for future sessions by merging this into
   the project's `.mcp.json` (never overwrite existing entries):

   ```json
   { "mcpServers": { "saari": { "command": "uvx", "args": ["saari-mcp"] } } }
   ```

## Step 1: protocol before searching

Never search before the protocol is set. Ask the user for, then record:

- the research question,
- inclusion and exclusion criteria,
- a working title.

Record them with `saari study set --title ... --question ... --criteria ...`
(CLI) or the `study_set` MCP tool. The question and criteria also drive the
relevance ranking in `triage`, so write them as concrete prose. Screening decisions made without recorded
criteria are not systematic and will weaken the exported review.

## Step 2: search wide

- Run several differently-phrased searches (`search`), not one. OpenAlex is
  keyword-sensitive; 3-6 query variants with year bounds beat one big query.
- If `SCOPUS_API_KEY` is configured, repeat the key queries with
  `source="scopus"` — a second database strengthens PRISMA coverage, and
  results are deduplicated by DOI automatically. If Scopus errors mention
  IP ranges or InstToken, relay the message to the user (it is an
  institutional-access problem, not a bug) and continue with OpenAlex.
- Duplicates are deduplicated automatically; a paper seen in multiple
  searches is a relevance signal (`seen_in` on paper cards).
- Keyword search is noisy. After searching, run `refresh` once to embed new
  papers and re-project the landscape. Then use `query` (semantic search
  over the corpus) to check coverage. Write the query as 2-3 sentences
  describing what a relevant paper says; that beats keywords by a wide
  margin. If an obviously relevant phrasing returns thin results, search
  again with new terms.

## Step 3: snowball

Once some papers are included, run `snowball_included` (CLI:
`saari snowball-included`). It snowballs every included paper (references
and citers via OpenAlex) in one call and skips seeds already snowballed, so
each later round only expands new includes. Use `top_n` to cap a large
round, or `snowball` for a single seed. After each round: `refresh`, screen
the arrivals, and run `snowball_included` again. The round reports
`saturated: true` when it found nothing new; that is the stopping signal.

## Step 4: screen with reasons

- Screen every candidate to `included`, `excluded`, or `maybe` using
  `screen`, and always record a short note with the reason, phrased against
  the protocol's criteria.
- Use `triage` to work through candidates in batches. By default it ranks
  by semantic relevance to the protocol (each card has a `score`) once
  papers are embedded, else by `seen_in`; the reply's `sort` and `note` say
  which. `triage(query="...")` gives the most relevant undecided papers for
  a specific sub-topic. Avoid `sort="cited"` for screening: it surfaces
  famous off-topic papers first. Read abstracts (`paper_show`) before
  deciding. Do not screen on titles alone
  unless the title alone establishes an exclusion criterion.
- Revisit `maybe` papers at the end; a review should ship with zero maybes.
- The user is the reviewer of record: for borderline calls, present the
  abstract and your reasoning and let the user decide.

## Step 5: map the landscape

- `saari serve` starts the local web UI (corpus, screening, graph views) if
  the user wants to look at the corpus visually; it is optional.
- `canvas` regenerates `papers/landscape.html` and an Obsidian canvas —
  a UMAP map of the corpus useful for spotting clusters and gaps.
- `papers_similar` finds nearest neighbors of a given paper.

## Step 6: export

- `export bibtex` writes `papers/refs.bib` for citing from the draft.
- `export slr` writes the full bundle to `papers/review/`: a PRISMA 2020
  flow diagram from the actual search/screen funnel, a Markdown SLR
  manuscript scaffold, and a slide deck.
- Everything mechanical in the export (counts, tables, references, PRISMA
  numbers) traces to the database. Interpretive prose is emitted as
  `<!-- WRITE: ... -->` slots.

## Hard rules

- **Never fabricate.** Do not invent papers, findings, counts, or citations.
  Only fill a `WRITE:` slot with claims you can trace to included papers,
  and cite them.
- Do not mark a paper `included`/`excluded` without a recorded reason.
- Do not delete or hand-edit files under `.saaristo/` — it is tool-owned.
- Do not weaken the exported Limitations section; it exists so drafts
  cannot overclaim.

## Verification

The review is in a sound state when: the protocol fields are set; zero
candidates and zero maybes remain; every included/excluded paper has a note;
`refresh` reports all papers embedded and projected; and `export slr` runs
without warnings and its PRISMA counts match `project_info`.
