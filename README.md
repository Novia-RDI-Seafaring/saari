# saari

Agent-driven literature review toolkit. Part of the *saaristo* project at
Novia University of Applied Sciences.

saari is not an app with a "do literature review" button. It is a set of
small, composable operations — search, snowball, screen, embed, map, export —
that an agent (Claude Code, any MCP host, or you at a shell) calls in
whatever order the work requires. All state lives in a `.saaristo/` directory
inside your own project folder, in SQLite. Local-first, no accounts, and the
core loop needs no API keys: search uses OpenAlex, embeddings run locally.

**Sources.** OpenAlex is the default (keyless). Scopus is available as a
second database (`saari search "..." --source scopus`): set `SCOPUS_API_KEY`
(create one at dev.elsevier.com) and note that a key alone only works from a
network Elsevier has registered for your institution's subscription — off
that network you also need `SCOPUS_INST_TOKEN`, an institutional token your
library requests from Elsevier. Results are deduplicated across sources by
DOI, PRISMA reporting splits identification per database, and Scopus search
records usually arrive without abstracts (Elsevier entitlement) — a matching
OpenAlex hit on the same DOI supplies the abstract.

Crossref is a keyless third database (`--source crossref`). Set
`CROSSREF_MAILTO` to join its polite pool; saari sends no address otherwise.
Crossref abstracts are often missing.

**Known papers.** Add papers you already know by identifier:
`saari papers add 10.1000/xyz arXiv:2101.00001 pmid:29456894 W2741809807`
(doi.org, arxiv.org, PubMed and OpenAlex URLs work too). Lookups go to
OpenAlex first, then Crossref for DOIs OpenAlex lacks. Each call is one
`manual` identification event. PRISMA reports it under other methods, not
as a database search.

**Duplicates.** Exact DOI, PMID or arXiv-id matches are deduplicated at
ingest. Weaker matches (same normalized title, year within 1, same first
author, such as a preprint and its published version) are never merged
automatically. List them with `saari papers duplicates`, then resolve each
with `saari papers merge KEEP REMOVE` or `saari papers not-duplicate A B`.
A merge keeps the removed record's ids, provenance and search links, and
counts as a duplicate removed in the PRISMA flow.

One toolkit, three surfaces with the same operations:

- **MCP server** (`saari-mcp`) — for agents. The primary interface.
- **CLI** (`saari`) — for humans at a shell and for scripts.
- **HTTP server + web UI** (`saari serve`) — corpus browser, screening view,
  interactive UMAP landscape. Optional.

## Quickstart (with an agent harness)

The intended setup: you are in a folder where you are writing a paper, and
your agent harness (e.g. Claude Code) works there with you.

```bash
uv tool install saari        # or: pipx install saari
cd my-paper/
saari init
```

Register the MCP server in the folder's `.mcp.json`:

```json
{ "mcpServers": { "saari": { "command": "uvx", "args": ["saari-mcp"] } } }
```

Optionally install the literature-review skill, which teaches the agent the
systematic-review methodology (protocol first, screen with reasons, PRISMA
at the end):

```bash
npx skills add Novia-RDI-Seafaring/saari --skill litreview
```

Then ask your agent to start a literature review. On connect the MCP server
tells the agent to call `guide`, which returns the pipeline, what each step
needs, and the next step for this project. The agent will set the protocol,
search, snowball, screen, and export — and the outputs land as plain files
in your folder, next to your draft.

## Quickstart (CLI only)

```bash
cd my-paper/
saari init
saari study set --title "..." --question "..." --criteria "..."
saari search "digital twin maritime safety" --limit 50
saari refresh                      # embed new papers + rebuild the landscape
saari triage                       # candidates, most relevant to the protocol first
saari screen openalex:W123 include --note "meets criterion 2"
saari snowball-included            # references + citers of every included paper
saari refresh && saari triage      # screen what arrived; repeat to saturation
saari export slr                   # PRISMA + manuscript scaffold + slides
```

`saari guide` prints the workflow map and the suggested next step.
`saari triage --sort` takes `auto` (default: relevance to the protocol when
papers are embedded, else `seen_in`), `relevance`, `seen_in` (found by the
most searches), or `cited`. `--query "..."` ranks by relevance to that text
instead of the protocol.

`saari --help` lists everything. For the web UI, install the serve extra
(`uv tool install 'saari[serve]'`) and run `saari serve`.

## Search completeness

A systematic review has to screen everything a query returns, so saari
never keeps a silent sample. Every search reports `n_available` (the
source's total) next to what it retrieved, and flags `truncated` when it
kept only part:

```bash
saari search "digital twin ship" --limit 25          # "Retrieved 25 of 850 available"
saari search "digital twin ship" --all               # fetch every record
saari search "digital twin ship" --scope title_abstract   # skip full-text matches
saari snowball openalex:W123 --all-forward           # all citers, not just 25
```

- `--limit` pages past OpenAlex's 100-per-page maximum automatically.
- `--all` fetches every match up to a safety cap (2000 records; set
  `--max-records` or `SAARI_SEARCH_MAX_RECORDS`). Above the cap the search
  is refused, so you narrow the query instead of screening a sample.
- `--scope title_abstract` matches title and abstract only. The default
  OpenAlex search also matches full text and keywords, which is noisy.
- Snowball takes **all** references backward by default, and the 25 most
  relevant citing works forward (`--max-forward N`, or `--all-forward`).
  Both directions report how many exist.
- OpenAlex works without a key, but keyless use shares a small per-IP daily
  budget and basic paging stops at 10,000 records. Set `OPENALEX_API_KEY`
  (free at openalex.org/settings/api) for a larger budget and cursor paging.
  `OPENALEX_MAILTO` is optional; OpenAlex now ignores it.
- Transient OpenAlex failures (429, 5xx, timeouts) are retried with
  backoff, honoring `Retry-After`.

The PRISMA diagram, search log table, and Limitations block in the export
state how many records were retrieved of how many were available, list
every truncated search, and say when older searches lack a recorded total.

## Full text (optional)

Screening starts from titles and abstracts. For included papers you can
fetch open-access full text (OA locations, PubMed Central, arXiv; never
paywalled copies). It needs the `fulltext` extra (pypdf):

```bash
uv tool install 'saari[fulltext]'
saari fulltext fetch                   # all included papers; summary of got/missing
saari papers text openalex:W123 -n 4000 --offset 0
saari papers text openalex:W123 --grep "ballast water"
```

PDFs and extracted text are cached in `.saaristo/fulltext/`. Each paper gets
an honest status (available / unavailable / failed, with a reason), and the
exported Limitations section states how many included papers had full text
retrieved. MCP: `paper_text`, `fulltext_fetch`. For the MCP server use
`uvx --from 'saari[fulltext]' saari-mcp`. Scanned PDFs (no text layer) are
reported as failed; there is no OCR.

## What the export gives you

`saari export slr` writes `papers/review/`:

- a **PRISMA 2020 flow diagram** computed from your actual search and
  screening funnel,
- a **Markdown SLR manuscript scaffold** — counts, tables, references and
  figures auto-filled and traceable to the database; interpretive prose
  left as explicit `<!-- WRITE: ... -->` slots,
- a slide deck (Marp).

The governing rule is **never fabricate**: saari fills in only what it can
trace to the corpus, and always writes an honest Limitations section.

## Layout on disk

```
my-paper/
├── .saaristo/       tool-owned state (SQLite DB, raw API responses, fulltext/)
└── papers/          user-visible outputs (landscape.html, refs.bib, review/)
```

Delete `.saaristo/` and the project is gone; zip the folder and the whole
review travels with it.

## Hosted mode (experimental)

saari is local-first, but every surface can also run multi-user behind an
authenticating proxy (e.g. Azure Container Apps with Entra ID "Easy Auth"):

```bash
SAARI_DATA_ROOT=/data saari serve          # HTTP API + UI, per-user projects
SAARI_DATA_ROOT=/data saari-mcp --http     # MCP over streamable HTTP (for
                                           # Copilot Studio and remote hosts)
```

Each request is scoped to `$SAARI_DATA_ROOT/<user>/<project>/` from the
`x-ms-client-principal-id` (or `x-saari-user`) and `x-saari-project`
headers. The auth layer in front must inject these; saari trusts them.
Requests without an identity get 401. Without `SAARI_DATA_ROOT`, nothing
changes: single project, resolved from the working directory.

A `Dockerfile` ships in the repo (SPA build + `saari[serve]`, default CMD
`saari serve`); `docs/hosting.md` walks through an Azure Container Apps +
Entra ID deployment.

## Development

```bash
git clone https://github.com/Novia-RDI-Seafaring/saari
cd saari
uv sync --all-extras
uv run pytest -q
uv run ruff check .
```

Logs go to stderr (never stdout, which `saari-mcp` uses for JSON-RPC).
`saari -v <command>` shows INFO, `-vv` DEBUG. For the MCP server and other
non-CLI runs, set `SAARI_LOG_LEVEL=INFO` (or `DEBUG`); `SAARI_LOG_FILE=path`
also writes to a file.

The web UI is a React SPA in `ui/` (pnpm + Vite): `cd ui && pnpm install &&
pnpm dev` proxies `/api` to a running `saari serve`.

## License

Apache-2.0. See `LICENSE`.
