"""Export the project corpus to manuscript-ready formats.

v1: BibTeX (what `paper/` will cite). Keys follow the Better BibTeX convention:
`<firstauthorlastname><year><firsttitleword>`, normalized to ASCII lowercase.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from saari import db, paths
from saari.models import Paper

_NONWORD = re.compile(r"[^a-z0-9]+")
_BIBTEX_ESCAPES = str.maketrans({
    "{": r"\{",
    "}": r"\}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
})

_ABSTRACT_MAX = 1500  # typical journal abstract upper bound; anything bigger is front-matter pollution
_ABSTRACT_NOISE_MARKERS = (
    "CrossrefMedlineGoogle",
    "Search for more papers by this author",
    "Download Citations",
    "Track Citations",
    "Disclosure Information",
)


def _clean_abstract(s: str | None) -> str | None:
    if not s:
        return None
    if any(marker in s for marker in _ABSTRACT_NOISE_MARKERS):
        return None
    s = s.strip()
    if len(s) > _ABSTRACT_MAX:
        # Take the first full sentence boundary before the cap, else hard truncate.
        head = s[:_ABSTRACT_MAX]
        last_period = head.rfind(". ")
        if last_period > _ABSTRACT_MAX * 0.5:
            head = head[: last_period + 1]
        s = head.rstrip() + " …"
    return s


def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = _NONWORD.sub("", s)
    return s


def _citation_key(paper: Paper) -> str:
    parts: list[str] = []
    if paper.authors:
        surname = paper.authors[0].name.strip().split()[-1] if paper.authors[0].name else ""
        if surname:
            parts.append(_slug(surname))
    if paper.year is not None:
        parts.append(str(paper.year))
    if paper.title:
        first_word = paper.title.strip().split()[0]
        parts.append(_slug(first_word)[:14])
    key = "".join(parts).strip() or _slug(paper.id) or "unknown"
    return key


def _escape(s: str) -> str:
    return s.translate(_BIBTEX_ESCAPES) if s else ""


def _format_authors(paper: Paper) -> str:
    names = [a.name.strip() for a in paper.authors if a.name]
    if not names:
        return ""
    return " and ".join(_escape(n) for n in names)


def _entry_type(paper: Paper) -> str:
    if paper.venue:
        return "article"
    return "misc"


def _to_bibtex(paper: Paper) -> str:
    entry_type = _entry_type(paper)
    key = _citation_key(paper)
    fields: list[tuple[str, str]] = []
    if paper.title:
        fields.append(("title", _escape(paper.title)))
    authors = _format_authors(paper)
    if authors:
        fields.append(("author", authors))
    if paper.year is not None:
        fields.append(("year", str(paper.year)))
    if paper.venue:
        fields.append(("journal", _escape(paper.venue)))
    if paper.doi:
        fields.append(("doi", paper.doi))
    url = paper.landing_page_url or paper.pdf_url or (
        f"https://doi.org/{paper.doi}" if paper.doi else None
    )
    if url:
        fields.append(("url", url))
    clean_abs = _clean_abstract(paper.abstract) if not paper.abstract_suspect else None
    if clean_abs:
        fields.append(("abstract", _escape(clean_abs)))

    body = ",\n".join(f"  {name} = {{{value}}}" for name, value in fields)
    return f"@{entry_type}{{{key},\n{body}\n}}"


@dataclass
class ExportResult:
    path: str
    n_entries: int
    format: str


def export_bibtex(
    out: Path,
    status_filter: str | None = "included",
    project_root: Path | None = None,
) -> ExportResult:
    """Export papers to a BibTeX file. Default: included papers only."""
    root = project_root or paths.project_root()
    with db.connect(paths.db_path(root)) as con:
        papers = db.list_papers(
            con,
            limit=10_000,
            status=status_filter,
            order_by="year ASC NULLS LAST, first_seen_at ASC",
        )
    entries = [_to_bibtex(p) for p in papers]
    out.parent.mkdir(parents=True, exist_ok=True)
    header_bits = [
        f"% BibTeX export from saaristo project @ {root}",
    ]
    if status_filter:
        header_bits.append(f"% status filter: {status_filter}")
    header_bits.append(f"% {len(entries)} entries")
    out.write_text("\n".join(header_bits) + "\n\n" + "\n\n".join(entries) + "\n")
    return ExportResult(path=str(out), n_entries=len(entries), format="bibtex")
