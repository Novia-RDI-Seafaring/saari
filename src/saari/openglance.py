"""Compile the saari corpus into openglance-format markdown files.

Like `tsx` for TypeScript: pure conversion. Reads from the saari DB,
writes one `<slug>.md` per paper into the target directory. Nothing else —
no vault scaffolding, no README, no `config.json`, no build / serve / port.
The destination directory is the user's; saari just fills it.

What we emit per paper:

    <out>/<slug>.md
      ---
      title: ...
      type: source
      tags: [paper, <status>, <domain>/<field>/<topic>, ...]
      created: YYYY-MM-DD
      updated: YYYY-MM-DD
      url: <best landing URL>
      ---
      # <Title>
      ## TLDR    (used as openglance's card teaser)
      ## Metadata
      ## Status
      ## Related   (only [[wiki-links]] to other papers in this corpus)

Tags come from OpenAlex `topics` read on-the-fly from
`.saaristo/raw/openalex/<id>.json`. Citation edges are restricted to corpus
members so the rendered force-directed graph has real structure.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from saari import db, paths
from saari.export import _citation_key
from saari.models import Paper

_NONWORD = re.compile(r"[^a-z0-9]+")
_TLDR_MAX = 320


def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.lower().replace(" ", "-")
    s = _NONWORD.sub("-", s).strip("-")
    return s or "untitled"


def _abstract_excerpt(text: str | None, n: int = _TLDR_MAX) -> str:
    if not text:
        return ""
    s = " ".join(text.split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _read_topics(paper: Paper, project_root: Path) -> list[dict]:
    with db.connect(paths.db_path(project_root)) as con:
        raw_rel = db.get_raw_path(con, paper.id)
    if not raw_rel:
        return []
    full = project_root / raw_rel
    if not full.exists():
        return []
    try:
        return json.loads(full.read_text()).get("topics") or []
    except (OSError, json.JSONDecodeError):
        return []


def _topics_to_tags(topics: list[dict]) -> list[str]:
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
    """paper.id → unique slug (citation key, with `-2`/`-3` suffix on collision)."""
    used: dict[str, str] = {}
    counts: dict[str, int] = {}
    for p in papers:
        base = _citation_key(p)
        n = counts.get(base, 0) + 1
        counts[base] = n
        used[p.id] = base if n == 1 else f"{base}-{n}"
    return used


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


def _render_paper_page(
    paper: Paper,
    *,
    slug_by_id: dict[str, str],
    project_root: Path,
) -> str:
    topics = _read_topics(paper, project_root)
    fm: dict[str, Any] = {
        "title": paper.title,
        "type": "source",
        "tags": ["paper", paper.status] + _topics_to_tags(topics),
        "created": (paper.first_seen_at.date().isoformat() if paper.first_seen_at else date.today().isoformat()),
        "updated": date.today().isoformat(),
    }
    if paper.landing_page_url or paper.pdf_url:
        fm["url"] = paper.landing_page_url or paper.pdf_url

    body: list[str] = [f"# {paper.title}", ""]

    if paper.abstract and not paper.abstract_suspect:
        body += ["## TLDR", "", _abstract_excerpt(paper.abstract), ""]

    body += ["## Metadata", ""]
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

    body += ["## Status", "", f"**{paper.status}**"]
    if paper.screening_note:
        body += ["", f"> {paper.screening_note}"]
    body.append("")

    related = [
        slug_by_id[f"openalex:{rid}"]
        for rid in paper.referenced_works
        if f"openalex:{rid}" in slug_by_id
    ]
    if related:
        body += ["## Related", ""]
        body += [f"- [[{s}]]" for s in related]
        body.append("")

    return _render_frontmatter(fm) + "\n" + "\n".join(body)


@dataclass
class ExportResult:
    out_dir: str
    n_papers: int
    n_pages_written: int
    n_pages_skipped: int


def export_corpus(
    out_dir: Path,
    status: str | None = None,
    only_missing: bool = False,
    project_root: Path | None = None,
) -> ExportResult:
    """Compile the saari corpus into openglance-format `.md` files in `out_dir`.

    `out_dir` is created if it doesn't exist. Saari doesn't manage its contents
    beyond writing/overwriting paper pages — README/config.json/topic pages are
    the user's responsibility (or another tool's).

    `status` filters which papers to compile. `only_missing` skips slugs that
    already exist on disk.
    """
    root = project_root or paths.project_root()
    out_dir = out_dir.expanduser()
    if not out_dir.is_absolute():
        out_dir = root / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

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
        out = out_dir / f"{slug}.md"
        if only_missing and out.exists():
            skipped += 1
            continue
        out.write_text(_render_paper_page(p, slug_by_id=slug_by_id, project_root=root))
        written += 1

    return ExportResult(
        out_dir=str(out_dir),
        n_papers=len(papers),
        n_pages_written=written,
        n_pages_skipped=skipped,
    )
