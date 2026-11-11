"""Crossref search source and DOI lookup.

Uses the Crossref REST API (https://api.crossref.org). Keyless. Crossref
asks automated clients to identify themselves for the "polite" pool; saari
sends a `mailto` only when one is configured in the environment, never a
built-in address:

- CROSSREF_MAILTO - preferred.
- OPENALEX_MAILTO - used as a fallback when CROSSREF_MAILTO is unset.

Two roles:

- `fetch_by_doi` is the fallback for `paper add` when OpenAlex does not know
  a DOI (very recent, or registered by a small publisher).
- `search` makes `search --source crossref` work like any other database.

Crossref metadata is publisher-deposited. Abstracts are present only when
the publisher deposited one, as JATS XML; tags are stripped here. There are
no reference lists in the OpenAlex sense, so Crossref-only records cannot be
backward-snowballed until OpenAlex indexes them.

Paper ids for Crossref records are `crossref:<doi>` (lowercased DOI).
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from saari import paths
from saari.sources.common import SearchHits, SearchTooLarge, default_max_records
from saari.models import Author, Location, Paper
from saari.sources.ids import arxiv_from_doi, normalize_doi

log = logging.getLogger(__name__)

BASE_URL = "https://api.crossref.org"
# Crossref allows up to 1000 rows per page; smaller pages keep responses snappy.
PAGE_SIZE = 100
# Plain offset paging is capped by Crossref at 10000.
MAX_OFFSET = 10_000

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


class CrossrefError(RuntimeError):
    """Crossref request failed with a diagnosable cause."""


def _mailto() -> str | None:
    return os.environ.get("CROSSREF_MAILTO") or os.environ.get("OPENALEX_MAILTO") or None


def _params(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    params: dict[str, Any] = dict(extra or {})
    mailto = _mailto()
    if mailto:
        params["mailto"] = mailto
    return params


def _headers() -> dict[str, str]:
    return {"User-Agent": "saari (literature-review toolkit; https://api.crossref.org polite use)"}


def strip_jats(text: str | None) -> str | None:
    """JATS/XML abstract -> plain text. Drops a leading 'Abstract' heading."""
    if not text:
        return None
    # Paragraph and title boundaries become spaces so words do not fuse.
    s = re.sub(r"</?(?:jats:)?(?:p|title|sec)\b[^>]*>", " ", text)
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    s = _WS_RE.sub(" ", s).strip()
    if s.lower().startswith("abstract "):
        s = s[len("abstract "):].lstrip()
    return s or None


def _clean_inline(text: str) -> str:
    """Strip inline markup (e.g. <i>, <sub>) from a title."""
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub("", text))).strip()


def _first(values: Any) -> str | None:
    if isinstance(values, list):
        for v in values:
            if v:
                return str(v).strip() or None
        return None
    return str(values).strip() if values else None


def _year(work: dict[str, Any]) -> int | None:
    for key in ("issued", "published", "published-print", "published-online", "created"):
        parts = (work.get(key) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            try:
                return int(parts[0][0])
            except (TypeError, ValueError):
                continue
    return None


def _author_name(a: dict[str, Any]) -> str:
    if a.get("name"):  # organisational author
        return str(a["name"])
    return " ".join(x for x in (a.get("given"), a.get("family")) if x)


def _work_to_paper(work: dict[str, Any]) -> Paper | None:
    doi_raw = work.get("DOI")
    title = _first(work.get("title"))
    if not doi_raw or not title:
        return None
    doi = normalize_doi(doi_raw)
    subtitle = _first(work.get("subtitle"))
    if subtitle and subtitle.lower() not in title.lower():
        title = f"{title}: {subtitle}"

    authors: list[Author] = []
    for a in work.get("author") or []:
        name = _author_name(a)
        if not name:
            continue
        orcid = (a.get("ORCID") or "").rsplit("/", 1)[-1] or None
        affs = a.get("affiliation") or []
        authors.append(
            Author(
                name=name,
                orcid=orcid,
                affiliation=(affs[0].get("name") if affs and isinstance(affs[0], dict) else None),
            )
        )

    landing = work.get("URL") or f"https://doi.org/{doi}"
    pdf_url = None
    for link in work.get("link") or []:
        if (link.get("content-type") or "").lower() == "application/pdf" and link.get("URL"):
            pdf_url = link["URL"]
            break
    licenses = work.get("license") or []
    lic = licenses[0].get("URL") if licenses else None
    venue = _first(work.get("container-title"))
    locations = [
        Location(
            source_name=venue or _first(work.get("publisher")),
            landing_page_url=landing,
            pdf_url=pdf_url,
            license=lic,
        )
    ]
    cites = work.get("is-referenced-by-count")
    relation = work.get("relation") or {}

    return Paper(
        id=f"crossref:{doi}",
        doi=doi,
        arxiv_id=arxiv_from_doi(doi),
        title=_clean_inline(title) or title,
        abstract=strip_jats(work.get("abstract")),
        year=_year(work),
        venue=venue,
        authors=authors,
        cited_by_count=int(cites) if cites is not None else None,
        landing_page_url=landing,
        pdf_url=pdf_url,
        locations=locations,
        source_provenance={
            "crossref": {
                "fetched_at": datetime.now(UTC).isoformat(),
                "doi": doi,
                "type": work.get("type"),
                "publisher": work.get("publisher"),
                # Publisher-asserted preprint/version links: a strong
                # duplicate signal, kept for the reviewer.
                "relation": {
                    k: v for k, v in relation.items()
                    if k in ("is-preprint-of", "has-preprint", "is-version-of", "has-version")
                } or None,
            }
        },
    )


def _raw_name(doi: str) -> str:
    return quote(doi, safe="") + ".json"


def _dump_raw(work: dict[str, Any], doi: str, project_root: Path | None) -> str:
    dest = paths.raw_dir(project_root, "crossref") / _raw_name(doi)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(work, indent=2, ensure_ascii=False))
    return str(dest.relative_to(project_root or paths.project_root()))


def _raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code == 429:
        raise CrossrefError(
            "Crossref rate limit hit (429). Wait a moment and retry; setting "
            "CROSSREF_MAILTO puts requests in the polite pool."
        )
    if resp.status_code >= 500:
        raise CrossrefError(f"Crossref server error ({resp.status_code}); try again later.")
    resp.raise_for_status()


def fetch_by_doi(doi: str, project_root: Path | None = None) -> tuple[Paper, str] | None:
    """Look up one DOI. Returns (paper, raw_path) or None if Crossref does not know it."""
    doi = normalize_doi(doi)
    with httpx.Client(timeout=30.0, headers=_headers()) as client:
        resp = client.get(f"{BASE_URL}/works/{quote(doi, safe='/')}", params=_params())
        if resp.status_code == 404:
            log.info("crossref: DOI %s not found", doi)
            return None
        _raise_for_status(resp)
        work = (resp.json() or {}).get("message") or {}
    paper = _work_to_paper(work)
    if paper is None:
        log.warning("crossref: DOI %s has no title; skipping", doi)
        return None
    return paper, _dump_raw(work, paper.doi or doi, project_root)


def search(
    query: str,
    limit: int = 25,
    year_from: int | None = None,
    year_to: int | None = None,
    project_root: Path | None = None,
    scope: str = "all",
    fetch_all: bool = False,
    max_records: int | None = None,
) -> SearchHits:
    """Search Crossref `query.bibliographic`. Returns `SearchHits`
    ([(paper, raw_path), ...] plus `n_available` from `total-results`).

    `fetch_all` fetches every match, refusing with SearchTooLarge above the
    safety cap (`max_records`, default SAARI_SEARCH_MAX_RECORDS or 2000).
    Crossref has no title-and-abstract query, so only `scope="all"` is
    accepted. Persists each raw item under
    `.saaristo/raw/crossref/<quoted-doi>.json`. Items without a title
    (components, some datasets) are skipped, not counted as truncation.
    """
    if scope != "all":
        raise ValueError(
            f"crossref does not support scope={scope!r}; use scope='all' "
            "or search openalex / scopus for title_abstract"
        )
    cap = max_records or default_max_records()
    if not fetch_all and limit > cap:
        raise SearchTooLarge(
            f"limit {limit} exceeds the safety cap of {cap} records. Raise "
            "max_records / SAARI_SEARCH_MAX_RECORDS, or narrow the query."
        )
    filters: list[str] = []
    if year_from is not None:
        filters.append(f"from-pub-date:{year_from}")
    if year_to is not None:
        filters.append(f"until-pub-date:{year_to}")
    base: dict[str, Any] = {"query.bibliographic": query}
    if filters:
        base["filter"] = ",".join(filters)

    out = SearchHits()
    want = cap if fetch_all else limit
    offset = 0
    walked = 0
    with httpx.Client(timeout=30.0, headers=_headers()) as client:
        while walked < want and offset < MAX_OFFSET:
            rows = min(want - walked, PAGE_SIZE)
            resp = client.get(
                f"{BASE_URL}/works", params=_params({**base, "rows": rows, "offset": offset})
            )
            _raise_for_status(resp)
            message = (resp.json() or {}).get("message") or {}
            total = message.get("total-results")
            if total is not None:
                out.n_available = int(total)
                if fetch_all and out.n_available > cap:
                    raise SearchTooLarge(
                        f"crossref has {out.n_available} records for this query, above "
                        f"the safety cap of {cap}. Narrow the query or raise "
                        "max_records / SAARI_SEARCH_MAX_RECORDS. Nothing was saved."
                    )
            items = message.get("items") or []
            if not items:
                break
            offset += len(items)
            for item in items:
                walked += 1
                paper = _work_to_paper(item)
                if paper is None:
                    continue
                out.append((paper, _dump_raw(item, paper.doi or "", project_root)))
            if out.n_available is not None and offset >= out.n_available:
                break
    out.n_retrieved = walked
    if offset >= MAX_OFFSET and out.truncated:
        out.notes.append(f"Crossref offset paging stops at {MAX_OFFSET} records.")
    return out
