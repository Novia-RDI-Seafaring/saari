# saari

Agent-driven literature review toolkit. Part of the *saaristo* project at
Novia University of Applied Sciences.

saari is not an app with a "do literature review" button. It is a set of
small, composable operations — search, snowball, screen, embed, map, export —
that an agent (Claude Code, any MCP host, or you at a shell) calls in
whatever order the work requires. All state lives in a `.saaristo/` directory
inside your own project folder, in SQLite. Local-first, no accounts, and the
core loop needs no API keys: search uses OpenAlex, embeddings run locally.

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

Then ask your agent to start a literature review. It will set the protocol,
search, snowball, screen, and export — and the outputs land as plain files
in your folder, next to your draft.

## Quickstart (CLI only)

```bash
cd my-paper/
saari init
saari study set --title "..." --question "..." --criteria "..."
saari search "digital twin maritime safety" --limit 50
saari snowball openalex:W1234567890
saari refresh                      # embed new papers + rebuild the landscape
saari triage                       # screen candidates
saari export slr                   # PRISMA + manuscript scaffold + slides
```

`saari --help` lists everything. For the web UI, install the serve extra
(`uv tool install 'saari[serve]'`) and run `saari serve`.

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
├── .saaristo/       tool-owned state (SQLite DB, raw API responses)
└── papers/          user-visible outputs (landscape.html, refs.bib, review/)
```

Delete `.saaristo/` and the project is gone; zip the folder and the whole
review travels with it.

## Development

```bash
git clone https://github.com/Novia-RDI-Seafaring/saari
cd saari
uv sync --all-extras
uv run pytest -q
```

The web UI is a React SPA in `ui/` (pnpm + Vite): `cd ui && pnpm install &&
pnpm dev` proxies `/api` to a running `saari serve`.

## License

Apache-2.0. See `LICENSE`.
