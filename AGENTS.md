# saaristo / code — agent guide

This is the agent source of truth for the **`code/`** repo of the saaristo
project (the `saari` Python library). For the workspace overview (the
three-repo archipelago, the paper, the study, the wiki) see `../CLAUDE.md`.

## What this repo is

- The `saari` literature-review + visualization toolkit. Python 3.12+,
  uv-managed (v0.0.1, early stage).
- Two services on a shared core: `litreview` and `visualize`, each exposed
  as a **CLI** (`saari`), an **MCP server** (`saari-mcp`), and an
  **HTTP / FastAPI** service (`server.py`).
- Backed by SQLite + sqlite-vec, fastembed embeddings, OpenAlex + Crossref
  search, UMAP projection. Modules live in `src/saari/`.

## Working here

- Use **uv**, never pip: `uv sync`, `uv run <cmd>`.
- Tests: `uv run pytest -q`. If an issue touches code that has no tests
  yet, **adding the test scaffold is in scope**, not optional.
- ruff is installed (`uv run ruff check`). A lint CI step is not yet
  wired in `.github/workflows/ci.yml` because the existing code is
  unlinted; enable it once the code is cleaned up.
- This is one of three sibling repos under `saari2/`. Do NOT create a
  top-level repo, and do not auto-edit `../paper/` or `../study/` from
  code work. Surface manuscript-worthy findings in chat instead.
- Generated text (docs, commit messages, PR bodies): ASCII only, no
  em-dashes, short sentences. No "Co-Authored-By" / AI-meta / authorship
  trailers in commits, PRs, or public docs. No personal attributions or
  pasted private-chat content in public artifacts.
- **Never force-push** a shared branch without explicit maintainer OK. To
  catch a PR branch up, use merge or `gh pr update-branch`, not
  rebase + force.

## Adapter parity

`saari` reaches users through CLI, MCP, and HTTP. Every new operation
should land on all the surfaces it belongs to **in the same PR**, so an
agent (MCP), a shell user (CLI), and a UI / HTTP client get the same
capability. Mirror an op across `cli.py`, `mcp_server.py`, and `server.py`
rather than shipping one in isolation.

## GitHub issue work loop

This repo is worked by an autonomous issue loop (the maintainer's harness
plus any teammate agents). Discipline:

1. **Author-trust gate.** Only act on issues whose `authorAssociation` is
   OWNER / MEMBER / COLLABORATOR. The sole maintainer's own issues are
   OWNER. Ignore drive-by issues from untrusted accounts.
2. **Triage-first / clarify gate.** Never implement an ambiguous issue. An
   issue is "ready to implement" only when its body carries a clear spec +
   explicit acceptance criteria + verification steps (added during
   triage). If a workable issue lacks that, post the proposed
   spec/acceptance as a comment and ask the maintainer to confirm. Do not
   write code yet.
3. **Claim before working** (so concurrent harnesses do not collide):
   assign yourself and post a short claim comment. Before starting,
   re-check the issue was not claimed in a race; reclaim only if a prior
   claim is stale (>2h with no progress). Release (unassign + note) if you
   stop without finishing.
4. **For a ready issue:** branch from the default branch
   (`feat|fix|docs/...`), implement professionally **with tests** (plus
   adapter parity in the same PR), run `uv run pytest -q` (and lint once
   configured), open a PR with `Closes #<n>`, drive CI green. **Hold
   merge/release for the maintainer** unless told otherwise.
5. **After each pass**, update the issues so the next agent (you, a
   teammate, a subagent) knows exactly what to do, how to verify, and when
   it is "done" / acceptable. Surface open questions to the maintainer
   rather than guessing.

## Cross-repo coordination

saaristo is one of several interdependent repos the maintainer runs
(Anchor, OIP, graph-data-extractor, ...). Rules so the repos stay
coordinated without colliding:

- **A need you discover here that belongs to another repo: file a ticket
  in THAT repo**, cross-linked (`needed-by: saari#<n>` there, and link
  back `blocked-by: <repo>#<n>` here). Do NOT cross-edit another repo as a
  default. Each repo is its own source of truth with its own tests / CI /
  review. Narrow exception: a trivial, fully-understood fix, and even then
  via a PR, not a direct push.
- **Tickets filed here by another repo's agent** (for example an Anchor
  agent that needs a saari feature for the lit-review integration) are
  trusted (same maintainer / org). Triage and claim them like any other.
- **Runtime vs dev channels differ.** A tool consuming saari at runtime
  calls the **MCP server** (an Anchor agent pulling papers). That is
  separate from dev coordination, which goes through **GitHub issues**.
- **Shared contracts (OIP) are RFC-first.** If saari ever produces or
  consumes an OIP artifact, land contract changes in the OIP repo (RFC +
  schema + example) and test against the published schema. Never extend
  the contract from inside saari.
