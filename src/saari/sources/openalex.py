from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from saari import paths
from saari.models import Author, Location, Paper

BASE_URL = "https://api.openalex.org"
# OpenAlex "polite pool": identify the operator. Override in deployments
# (all users of a hosted instance share one egress IP).
DEFAULT_MAILTO = os.environ.get("OPENALEX_MAILTO", "info.graceful.ai@gmail.com")

def _resolve_mailto() -> str:
    """Polite-pool attribution: the request's bound identity email (hosted
    mode, opt-in via SAARI_MAILTO_FROM_IDENTITY) or the operator default."""
    from saari.hosting import request_mailto

    return request_mailto() or DEFAULT_MAILTO


_ARXIV_URL_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/([^/?#v]+)", re.IGNORECASE)
_PMID_URL_RE = re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", re.IGNORECASE)
_PMCID_URL_RE = re.compile(r"ncbi\.nlm\.nih\.gov/pmc/articles/(PMC\d+)", re.IGNORECASE)
_OPENALEX_ID_RE = re.compile(r"openalex\.org/(W\d+)", re.IGNORECASE)
_MIN_ABSTRACT_LEN = 100


def _reconstruct_abstract(inverted: dict[str, list[int]] | None) -> str | None:
    if not inverted:
        return None
    positions: dict[int, str] = {}
    for word, idxs in inverted.items():
        for i in idxs:
            positions[i] = word
    if not positions:
        return None
    return " ".join(positions[i] for i in sorted(positions))


def _is_abstract_suspect(abstract: str | None) -> bool:
    if abstract is None:
        return False
    return len(abstract.strip()) < _MIN_ABSTRACT_LEN


def _strip_openalex_id(url_or_id: str) -> str:
    m = _OPENALEX_ID_RE.search(url_or_id)
    return m.group(1) if m else url_or_id


def _map_location(loc: dict[str, Any]) -> Location:
    src = loc.get("source") or {}
    return Location(
        source_name=src.get("display_name"),
        landing_page_url=loc.get("landing_page_url"),
        pdf_url=loc.get("pdf_url"),
        version=loc.get("version"),
        license=loc.get("license"),
        is_oa=bool(loc.get("is_oa")),
    )


def _extract_cross_ids(
    ids: dict[str, Any], locations: list[Location]
) -> tuple[str | None, str | None, str | None]:
    """Returns (arxiv_id, pmid, pmcid). Falls back to parsing location URLs."""
    arxiv_id = ids.get("arxiv")
    if arxiv_id:
        arxiv_id = str(arxiv_id).split("/")[-1].removeprefix("arXiv:")

    pmid = ids.get("pmid")
    if pmid:
        pmid = str(pmid).rsplit("/", 1)[-1]

    pmcid = ids.get("pmcid")
    if pmcid:
        pmcid = str(pmcid).rsplit("/", 1)[-1]

    for loc in locations:
        url = loc.landing_page_url or loc.pdf_url or ""
        if not arxiv_id and (m := _ARXIV_URL_RE.search(url)):
            arxiv_id = m.group(1)
        if not pmid and (m := _PMID_URL_RE.search(url)):
            pmid = m.group(1)
        if not pmcid and (m := _PMCID_URL_RE.search(url)):
            pmcid = m.group(1)

    return arxiv_id, pmid, pmcid


def _work_to_paper(work: dict[str, Any]) -> Paper:
    openalex_id = work["id"].rsplit("/", 1)[-1]

    doi: str | None = None
    if work.get("doi"):
        doi = str(work["doi"]).removeprefix("https://doi.org/")

    authors: list[Author] = []
    for authorship in work.get("authorships") or []:
        author_obj = authorship.get("author") or {}
        institutions = authorship.get("institutions") or []
        orcid = (author_obj.get("orcid") or "").removeprefix("https://orcid.org/") or None
        authors.append(
            Author(
                name=author_obj.get("display_name") or "",
                orcid=orcid,
                affiliation=institutions[0].get("display_name") if institutions else None,
            )
        )

    locations = [_map_location(loc) for loc in (work.get("locations") or [])]

    venue: str | None = None
    landing_page_url: str | None = None
    pdf_url: str | None = None
    primary_loc = work.get("primary_location") or {}
    if primary_loc:
        src = primary_loc.get("source") or {}
        venue = src.get("display_name")
        landing_page_url = primary_loc.get("landing_page_url")
        pdf_url = primary_loc.get("pdf_url")

    oa = work.get("open_access") or {}
    oa_status = oa.get("oa_status")
    if not pdf_url:
        best_oa = work.get("best_oa_location") or {}
        pdf_url = best_oa.get("pdf_url") or oa.get("oa_url")

    arxiv_id, pmid, pmcid = _extract_cross_ids(work.get("ids") or {}, locations)

    abstract = _reconstruct_abstract(work.get("abstract_inverted_index"))

    referenced_works = [_strip_openalex_id(w) for w in work.get("referenced_works") or []]
    related_works = [_strip_openalex_id(w) for w in work.get("related_works") or []]

    return Paper(
        id=f"openalex:{openalex_id}",
        doi=doi,
        arxiv_id=arxiv_id,
        pmid=pmid,
        pmcid=pmcid,
        title=work.get("title") or work.get("display_name") or "",
        abstract=abstract,
        abstract_suspect=_is_abstract_suspect(abstract),
        year=work.get("publication_year"),
        venue=venue,
        authors=authors,
        cited_by_count=work.get("cited_by_count"),
        oa_status=oa_status,
        landing_page_url=landing_page_url,
        pdf_url=pdf_url,
        locations=locations,
        referenced_works=referenced_works,
        related_works=related_works,
        source_provenance={
            "openalex": {
                "fetched_at": datetime.now(UTC).isoformat(),
                "work_id": openalex_id,
            }
        },
    )


def _dump_raw(work: dict[str, Any], project_root: Path | None = None) -> str:
    """Persist raw OpenAlex response to .saaristo/raw/openalex/<work_id>.json.

    Returns the path relative to project_root."""
    work_id = work["id"].rsplit("/", 1)[-1]
    dest = paths.raw_dir(project_root, "openalex") / f"{work_id}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(work, indent=2, ensure_ascii=False))
    return str(dest.relative_to(project_root or paths.project_root()))


def search(
    query: str,
    limit: int = 25,
    year_from: int | None = None,
    year_to: int | None = None,
    mailto: str | None = None,
    project_root: Path | None = None,
) -> list[tuple[Paper, str]]:
    """Search OpenAlex /works. Returns [(paper, raw_path), ...] up to `limit`, ranked by relevance.

    Persists each raw response under `.saaristo/raw/openalex/<id>.json`.
    """
    params: dict[str, Any] = {
        "search": query,
        "per-page": min(limit, 200),
        "mailto": mailto or _resolve_mailto(),
    }
    filters: list[str] = []
    if year_from is not None:
        filters.append(f"from_publication_date:{year_from}-01-01")
    if year_to is not None:
        filters.append(f"to_publication_date:{year_to}-12-31")
    if filters:
        params["filter"] = ",".join(filters)

    with httpx.Client(timeout=30.0) as client:
        resp = client.get(f"{BASE_URL}/works", params=params)
        resp.raise_for_status()
        data = resp.json()

    results = (data.get("results") or [])[:limit]
    return [(_work_to_paper(w), _dump_raw(w, project_root)) for w in results]


def fetch_by_id(
    work_id: str,
    mailto: str | None = None,
    project_root: Path | None = None,
) -> tuple[Paper, str] | None:
    """Fetch a single OpenAlex work by ID. Returns (paper, raw_path) or None."""
    bare = work_id.removeprefix("openalex:")
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(f"{BASE_URL}/works/{bare}", params={"mailto": mailto or _resolve_mailto()})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        work = resp.json()
    return _work_to_paper(work), _dump_raw(work, project_root)


def fetch_works_by_ids(
    ids: list[str],
    mailto: str | None = None,
    project_root: Path | None = None,
    chunk_size: int = 50,
) -> list[tuple[Paper, str]]:
    """Batch-fetch OpenAlex works by ID. Dumps raw for each. Chunks if len(ids) > chunk_size."""
    if not ids:
        return []
    bare_ids = [i.removeprefix("openalex:") for i in ids]
    results: list[tuple[Paper, str]] = []
    with httpx.Client(timeout=30.0) as client:
        for start in range(0, len(bare_ids), chunk_size):
            chunk = bare_ids[start : start + chunk_size]
            params = {
                "filter": f"openalex_id:{'|'.join(chunk)}",
                "per-page": chunk_size,
                "mailto": mailto or _resolve_mailto(),
            }
            resp = client.get(f"{BASE_URL}/works", params=params)
            resp.raise_for_status()
            data = resp.json()
            for work in data.get("results") or []:
                results.append((_work_to_paper(work), _dump_raw(work, project_root)))
    return results


def fetch_citing_works(
    work_id: str,
    limit: int = 25,
    mailto: str | None = None,
    project_root: Path | None = None,
) -> list[tuple[Paper, str]]:
    """Fetch works that cite `work_id` (forward snowball via OpenAlex `cites:` filter)."""
    bare = work_id.removeprefix("openalex:")
    params: dict[str, Any] = {
        "filter": f"cites:{bare}",
        "per-page": min(limit, 200),
        "mailto": mailto or _resolve_mailto(),
    }
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(f"{BASE_URL}/works", params=params)
        resp.raise_for_status()
        data = resp.json()
    results = (data.get("results") or [])[:limit]
    return [(_work_to_paper(w), _dump_raw(w, project_root)) for w in results]
