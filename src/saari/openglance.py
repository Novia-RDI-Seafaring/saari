"""Export the saari corpus as an openglance-compatible vault.

Saari's responsibility ends at writing the vault to disk in a layout openglance
understands. Building the renderer's data files (`openglance build`) and
serving the dev server are intentionally out of scope — those are openglance's
job; the user (or another tool) runs them.

The vault layout we produce:

    <vault>/
      README.md             — corpus scope + tag conventions (seeded once, user-editable)
      config.json           — {"renderer": "document"}
      wiki/
        <slug>.md           — one renderable page per paper (auto) or agent-authored

Per-paper pages are derived from the saari DB; tags come from OpenAlex `topics`
read on the fly from `.saaristo/raw/openalex/<id>.json`. Citation edges between
papers in the corpus become `[[wiki-links]]` for the graph view. Agent-authored
pages (topic / synthesis / comparison / question) are written via `write_page`.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from saari import config, db, paths
from saari.export import _citation_key
from saari.models import Paper

VALID_PAGE_TYPES = {"source", "entity", "concept", "synthesis", "comparison", "question"}

_NONWORD = re.compile(r"[^a-z0-9]+")
_DEFAULT_VAULT_REL = "papers/openglance"
_TLDR_MAX = 320  # chars; openglance teaser is 300


# ---------- paths ----------


def vault_path(project_root: Path | None = None) -> Path:
    """Resolve the openglance vault path. Honors `[openglance].vault` config; defaults to papers/openglance."""
    root = project_root or paths.project_root()
    rel_or_abs = config.get("openglance.vault", _DEFAULT_VAULT_REL, project_root=root)
    p = Path(rel_or_abs).expanduser()
    if not p.is_absolute():
        p = root / p
    return p


def _wiki_dir(project_root: Path | None = None) -> Path:
    return vault_path(project_root) / "wiki"


def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.lower().replace(" ", "-")
    s = _NONWORD.sub("-", s).strip("-")
    return s or "untitled"


# ---------- vault scaffolding ----------


_DEFAULT_README = """# saaristo prior-art corpus — openglance vault

Generated from the saari index at `../../.saaristo/saari.db`.
Each paper in the corpus appears as a page in `wiki/`. Citation edges
between papers in the corpus become `[[wiki-links]]` so openglance's
graph view has real structure to draw.

## Tag hierarchy

Tags follow openglance's SKOS-like convention: `<theme>/<topic>/<concept>`.
We derive these from OpenAlex's per-paper `topics`:
`<domain>/<field>/<topic>`, slugified.

Flat tags carry page-type metadata:
- `paper` — auto-generated from a paper record
- `included` / `excluded` / `maybe` / `candidate` — screening status from saari

Agent-authored pages (topic / concept / synthesis / comparison / question)
use whatever tags fit.

## Render

```bash
saari openglance build      # rebuild the renderer's data files
saari openglance serve      # start the dev server (Next.js)
```

Or directly: `npx openglance build wiki && npx openglance serve`.
"""


def init_vault(out: Path | None = None, project_root: Path | None = None) -> dict[str, Any]:
    """Scaffold (or refresh) an openglance vault. Idempotent.

    - Creates `<vault>/wiki/` (where renderable pages live).
    - Seeds `README.md` if missing (user-editable).
    - Always writes `config.json` (renderer = "document").

    Returns `{path, created: bool}`.
    """
    root = project_root or paths.project_root()
    target = out or vault_path(root)
    existed = target.exists()
    target.mkdir(parents=True, exist_ok=True)
    (target / "wiki").mkdir(exist_ok=True)
    readme = target / "README.md"
    if not readme.exists():
        readme.write_text(_DEFAULT_README)
    (target / "config.json").write_text(json.dumps({"renderer": "document"}, indent=2))
    return {"path": str(target), "created": not existed}


# ---------- per-paper export ----------


def _abstract_excerpt(text: str | None, n: int = _TLDR_MAX) -> str:
    if not text:
        return ""
    s = " ".join(text.split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _read_topics(paper: Paper, project_root: Path) -> list[dict]:
    """Read OpenAlex topics from the raw JSON file we kept at fetch time."""
    if not paper.source_provenance and not _has_raw_path(paper):
        return []
    raw_rel = _has_raw_path(paper)
    if not raw_rel:
        return []
    full = project_root / raw_rel
    if not full.exists():
        return []
    try:
        data = json.loads(full.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return data.get("topics") or []


def _has_raw_path(paper: Paper) -> str | None:
    """Pull the raw_path from the DB row if available."""
    # Paper model doesn't carry raw_path; look it up on demand.
    with db.connect() as con:
        return db.get_raw_path(con, paper.id)


def _topics_to_tags(topics: list[dict]) -> list[str]:
    """OpenAlex topics → hierarchical tags `domain/field/topic`."""
    tags: set[str] = set()
    for t in topics:
        domain = (t.get("domain") or {}).get("display_name")
        field = (t.get("field") or {}).get("display_name")
        name = t.get("display_name")
        parts = [p for p in (domain, field, name) if p]
        if len(parts) >= 2:
            tags.add("/".join(_slug(p) for p in parts))
    return sorted(tags)


def _slug_map(papers: list[Paper]) -> dict[str, str]:
    """Build paper.id → unique slug map, disambiguating collisions on citation key."""
    used: dict[str, str] = {}  # paper_id -> slug
    counts: dict[str, int] = {}
    for p in papers:
        base = _citation_key(p)
        n = counts.get(base, 0) + 1
        counts[base] = n
        slug = base if n == 1 else f"{base}-{n}"
        used[p.id] = slug
    return used


def _render_frontmatter(fm: dict[str, Any]) -> str:
    lines = ["---"]
    for k, v in fm.items():
        if isinstance(v, list):
            lines.append(f"{k}:")
            for item in v:
                lines.append(f"  - {item}")
            continue
        lines.append(f"{k}: {_yaml_scalar(v)}")
    lines.append("---")
    return "\n".join(lines)


def _yaml_scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if s == "":
        return '""'
    if any(c in s for c in [":", "#", "[", "]", "{", "}", "&", "*", "!", "|", ">", "'", '"', "%", "@", "`", "\n"]):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def _render_paper_page(
    paper: Paper,
    *,
    slug: str,
    slug_by_id: dict[str, str],
    project_root: Path,
) -> str:
    topics = _read_topics(paper, project_root)
    hierarchical = _topics_to_tags(topics)
    flat = ["paper", paper.status]
    fm: dict[str, Any] = {
        "title": paper.title,
        "type": "source",
        "tags": flat + hierarchical,
        "created": (paper.first_seen_at.date().isoformat() if paper.first_seen_at else date.today().isoformat()),
        "updated": date.today().isoformat(),
    }
    if paper.landing_page_url or paper.pdf_url:
        fm["url"] = paper.landing_page_url or paper.pdf_url

    body: list[str] = [f"# {paper.title}", ""]

    # TLDR (first — openglance teaser prefers TLDR section)
    if paper.abstract and not paper.abstract_suspect:
        body.append("## TLDR")
        body.append("")
        body.append(_abstract_excerpt(paper.abstract))
        body.append("")

    # Metadata
    body.append("## Metadata")
    body.append("")
    if paper.year is not None:
        body.append(f"- **Year:** {paper.year}")
    if paper.cited_by_count is not None:
        body.append(f"- **Citations:** {paper.cited_by_count:,}")
    if paper.venue:
        body.append(f"- **Venue:** {paper.venue}")
    if paper.authors:
        names = ", ".join(a.name for a in paper.authors[:6])
        if len(paper.authors) > 6:
            names += f", +{len(paper.authors) - 6} more"
        body.append(f"- **Authors:** {names}")
    if paper.doi:
        body.append(f"- **DOI:** [{paper.doi}](https://doi.org/{paper.doi})")
    if paper.arxiv_id:
        body.append(f"- **arXiv:** [{paper.arxiv_id}](https://arxiv.org/abs/{paper.arxiv_id})")
    if paper.pmcid:
        body.append(f"- **PMC:** [{paper.pmcid}](https://www.ncbi.nlm.nih.gov/pmc/articles/{paper.pmcid}/)")
    if paper.oa_status and paper.oa_status != "closed":
        body.append(f"- **Open access:** {paper.oa_status}")
    body.append("")

    # Status
    body.append("## Status")
    body.append("")
    body.append(f"**{paper.status}**")
    if paper.screening_note:
        body.append("")
        body.append(f"> {paper.screening_note}")
    body.append("")

    # Related (citation edges within corpus only)
    related_target_ids = [
        f"openalex:{rid}" for rid in paper.referenced_works
    ]
    related_in_corpus = [
        slug_by_id[tid] for tid in related_target_ids if tid in slug_by_id
    ]
    if related_in_corpus:
        body.append("## Related")
        body.append("")
        for s in related_in_corpus:
            body.append(f"- [[{s}]]")
        body.append("")

    return _render_frontmatter(fm) + "\n" + "\n".join(body)


@dataclass
class ExportResult:
    vault: str
    n_papers: int
    n_pages_written: int
    n_pages_skipped: int


def export_corpus_papers(
    status: str | None = None,
    only_missing: bool = False,
    project_root: Path | None = None,
) -> ExportResult:
    """Auto-write one wiki page per paper in the corpus.

    `status` filters which papers to export (None = all).
    `only_missing` skips papers whose wiki page already exists.
    """
    root = project_root or paths.project_root()
    init_vault(project_root=root)
    wiki = _wiki_dir(root)

    with db.connect(paths.db_path(root)) as con:
        papers = db.list_papers(
            con,
            limit=100_000,
            status=status,
            order_by="first_seen_at ASC",
        )

    slug_by_id = _slug_map(papers)
    written = 0
    skipped = 0
    for p in papers:
        slug = slug_by_id[p.id]
        out = wiki / f"{slug}.md"
        if only_missing and out.exists():
            skipped += 1
            continue
        out.write_text(_render_paper_page(
            p, slug=slug, slug_by_id=slug_by_id, project_root=root,
        ))
        written += 1

    return ExportResult(
        vault=str(vault_path(root)),
        n_papers=len(papers),
        n_pages_written=written,
        n_pages_skipped=skipped,
    )


# ---------- generic page CRUD (agent-authored topics, syntheses, etc.) ----------


def write_page(
    slug: str,
    title: str,
    body: str,
    *,
    type: str = "concept",
    tags: list[str] | None = None,
    sources: list[str] | None = None,
    url: str | None = None,
    overwrite: bool = False,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Write an agent-authored wiki page to the openglance vault.

    `body` is the markdown body (do not include frontmatter; we add it).
    Convention: include a `## TLDR` section at the top for openglance's teaser.

    Raises FileExistsError if the slug exists and `overwrite=False`.
    """
    if type not in VALID_PAGE_TYPES:
        raise ValueError(
            f"Invalid type {type!r}. Must be one of: {', '.join(sorted(VALID_PAGE_TYPES))}"
        )
    init_vault(project_root=project_root)
    wiki = _wiki_dir(project_root)
    safe_slug = _slug(slug)
    out = wiki / f"{safe_slug}.md"
    if out.exists() and not overwrite:
        raise FileExistsError(f"Page already exists: {out} (pass overwrite=True to replace)")

    fm: dict[str, Any] = {
        "title": title,
        "type": type,
        "tags": tags or [],
        "created": date.today().isoformat(),
        "updated": date.today().isoformat(),
    }
    if sources:
        fm["sources"] = sources
    if url:
        fm["url"] = url

    content = _render_frontmatter(fm) + "\n\n" + body.strip() + "\n"
    out.write_text(content)
    return {"path": str(out), "slug": safe_slug, "type": type, "overwrote": out.exists() and overwrite}


def list_pages(
    type: str | None = None,
    tag_substring: str | None = None,
    project_root: Path | None = None,
) -> list[dict[str, Any]]:
    """List wiki pages with their frontmatter summaries."""
    wiki = _wiki_dir(project_root)
    if not wiki.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(wiki.glob("*.md")):
        fm = _parse_frontmatter(path.read_text())
        page_type = fm.get("type")
        if type is not None and page_type != type:
            continue
        if tag_substring is not None:
            tags = fm.get("tags") or []
            if not any(tag_substring.lower() in str(t).lower() for t in tags):
                continue
        out.append({
            "slug": path.stem,
            "title": fm.get("title") or path.stem,
            "type": page_type,
            "tags": fm.get("tags") or [],
            "url": fm.get("url"),
            "updated": fm.get("updated"),
        })
    return out


def read_page(slug: str, project_root: Path | None = None) -> dict[str, Any] | None:
    wiki = _wiki_dir(project_root)
    path = wiki / f"{_slug(slug)}.md"
    if not path.exists():
        return None
    text = path.read_text()
    fm = _parse_frontmatter(text)
    body = _strip_frontmatter(text)
    return {"slug": path.stem, "path": str(path), "frontmatter": fm, "body": body}


def delete_page(slug: str, project_root: Path | None = None) -> bool:
    wiki = _wiki_dir(project_root)
    path = wiki / f"{_slug(slug)}.md"
    if not path.exists():
        return False
    path.unlink()
    return True


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Tiny YAML-ish frontmatter parser — handles the keys we emit (title/type/tags/etc.)."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block = text[3:end].lstrip("\n")
    fm: dict[str, Any] = {}
    current_key: str | None = None
    current_list: list[str] | None = None
    for line in block.split("\n"):
        if not line.strip():
            continue
        if line.startswith("  - "):
            if current_list is not None:
                current_list.append(_yaml_unquote(line[4:].strip()))
            continue
        # New key
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if value:
                fm[key] = _yaml_unquote(value)
                current_key = None
                current_list = None
            else:
                current_key = key
                current_list = []
                fm[key] = current_list
    return fm


def _yaml_unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return s


def _strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    return text[end + 4 :].lstrip("\n")


def wiki_dir(project_root: Path | None = None) -> Path:
    """The directory the user/openglance points at for `openglance build`/`serve`."""
    return _wiki_dir(project_root)
