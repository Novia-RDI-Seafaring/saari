from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from saari import paths
from saari.models import Author, Location, Paper
from saari.sources.common import SearchHits, SearchTooLarge, default_max_records

log = logging.getLogger(__name__)

BASE_URL = "https://api.openalex.org"
# OpenAlex caps a page at 100 records (`per_page=200` is deprecated legacy).
PAGE_SIZE = 100
# Basic (`page=`) paging stops at page * per_page = 10,000. Past that,
# cursor paging is required, and cursor paging needs an API key beyond the
# first page (a keyless `next_cursor` request returns 401).
BASIC_PAGING_LIMIT = 10_000
# Search scopes. "all" is the OpenAlex default `search` param: title,
# abstract, full text and keywords. "title_abstract" maps to the
# `search.title_and_abstract` param.
SCOPES = {"all": "search", "title_abstract": "search.title_and_abstract"}

# Retry policy for transient failures (429, 5xx, timeouts, connection
# errors). Exponential backoff from RETRY_BASE_DELAY, honoring Retry-After.
MAX_ATTEMPTS = 5
RETRY_BASE_DELAY = 1.0
RETRY_MAX_DELAY = 60.0
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_sleep = time.sleep  # patched in tests


class OpenAlexError(RuntimeError):
    """OpenAlex request failed after retries, or with a diagnosable cause."""


def _api_key() -> str | None:
    return os.environ.get("OPENALEX_API_KEY") or None


def _resolve_mailto() -> str | None:
    """Optional `mailto` attribution: the request's bound identity email
    (hosted mode, opt-in via SAARI_MAILTO_FROM_IDENTITY) or OPENALEX_MAILTO.

    No default: when neither is set, no mailto is sent. Note that OpenAlex
    replaced the mailto "polite pool" with API keys (OPENALEX_API_KEY) in
    2026 and now ignores mailto; it is kept only as harmless attribution.
    """
    from saari.hosting import request_mailto

    return request_mailto() or os.environ.get("OPENALEX_MAILTO") or None


def _auth_params(mailto: str | None = None) -> dict[str, str]:
    out: dict[str, str] = {}
    if key := _api_key():
        out["api_key"] = key
    if m := (mailto or _resolve_mailto()):
        out["mailto"] = m
    return out


def _redact(params: dict[str, Any]) -> dict[str, Any]:
    return {k: ("***" if k == "api_key" else v) for k, v in params.items()}


def _retry_delay(attempt: int, resp: httpx.Response | None) -> float:
    if resp is not None:
        ra = resp.headers.get("Retry-After")
        if ra:
            try:
                return min(max(float(ra), 0.0), RETRY_MAX_DELAY)
            except ValueError:
                pass
    return min(RETRY_BASE_DELAY * (2 ** (attempt - 1)), RETRY_MAX_DELAY)


def _get(client: httpx.Client, url: str, params: dict[str, Any]) -> httpx.Response:
    """GET with retry + backoff on transient failures. Returns the final
    response (caller handles 404 / raise_for_status for non-transient codes).
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        t0 = time.monotonic()
        resp: httpx.Response | None = None
        try:
            resp = client.get(url, params=params)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            ms = (time.monotonic() - t0) * 1000
            if attempt == MAX_ATTEMPTS:
                raise OpenAlexError(
                    f"OpenAlex request failed after {attempt} attempts: {e!r}"
                ) from e
            delay = _retry_delay(attempt, None)
            log.warning(
                "openalex %s failed (%r) after %.0f ms; retry %d/%d in %.1fs",
                url, e, ms, attempt, MAX_ATTEMPTS - 1, delay,
            )
            _sleep(delay)
            continue
        ms = (time.monotonic() - t0) * 1000
        log.debug(
            "openalex GET %s %s -> %d in %.0f ms", url, _redact(params), resp.status_code, ms
        )
        if resp.status_code not in _RETRY_STATUSES:
            return resp
        if attempt == MAX_ATTEMPTS:
            break
        delay = _retry_delay(attempt, resp)
        log.warning(
            "openalex %s returned %d; retry %d/%d in %.1fs",
            url, resp.status_code, attempt, MAX_ATTEMPTS - 1, delay,
        )
        _sleep(delay)
    assert resp is not None
    hint = ""
    if resp.status_code == 429:
        hint = (
            " Rate limit or daily budget exceeded. Keyless use shares a small "
            "per-IP daily budget; set OPENALEX_API_KEY (free at "
            "https://openalex.org/settings/api) for a larger one."
        )
    raise OpenAlexError(
        f"OpenAlex returned {resp.status_code} after {MAX_ATTEMPTS} attempts.{hint}"
    )


def _check(resp: httpx.Response) -> dict[str, Any]:
    if resp.status_code == 401:
        try:
            msg = resp.json().get("message") or resp.text[:200]
        except Exception:  # noqa: BLE001 - body may be empty/HTML
            msg = resp.text[:200]
        raise OpenAlexError(f"OpenAlex refused the request (401): {msg}")
    resp.raise_for_status()
    return resp.json()


def _iter_works(
    client: httpx.Client,
    base_params: dict[str, Any],
    limit: int,
    meta_out: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    """Yield up to `limit` works for a /works list query, paging as needed.

    With OPENALEX_API_KEY set, uses cursor paging (no depth limit).
    Without a key, uses basic page paging, which stops at 10,000 records;
    `meta_out["paging_limit"]` is set when that bound cut the run short.
    `meta_out["count"]` receives OpenAlex `meta.count` (the source total).
    """
    use_cursor = _api_key() is not None
    params = dict(base_params)
    yielded = 0
    page = 1
    cursor: str | None = "*"
    # per_page stays fixed for the whole run: basic paging computes the
    # offset as (page - 1) * per_page, so shrinking the last page would
    # re-serve earlier records. The final page is sliced instead.
    per_page = max(min(limit, PAGE_SIZE), 1)
    params["per_page"] = per_page
    while yielded < limit:
        if use_cursor:
            params["cursor"] = cursor
        else:
            if (page - 1) * per_page >= BASIC_PAGING_LIMIT:
                meta_out["paging_limit"] = BASIC_PAGING_LIMIT
                return
            params["page"] = page
        data = _check(_get(client, f"{BASE_URL}/works", params))
        meta = data.get("meta") or {}
        if "count" not in meta_out and meta.get("count") is not None:
            meta_out["count"] = int(meta["count"])
        results = data.get("results") or []
        for w in results[: limit - yielded]:
            yield w
            yielded += 1
        if not results or len(results) < per_page:
            return
        if use_cursor:
            cursor = meta.get("next_cursor")
            if not cursor:
                return
        else:
            page += 1


def _plan_limit(
    limit: int, fetch_all: bool, n_available: int | None, cap: int, what: str
) -> int:
    """Resolve how many records to fetch. Refuses (SearchTooLarge) instead
    of silently cutting when the request exceeds the safety cap."""
    if not fetch_all:
        if limit > cap:
            raise SearchTooLarge(
                f"limit={limit} exceeds the safety cap of {cap} records for {what}. "
                "Raise it with max_records / SAARI_SEARCH_MAX_RECORDS, or narrow the query."
            )
        return limit
    if n_available is None:
        return cap
    if n_available > cap:
        raise SearchTooLarge(
            f"{what} has {n_available} records available, above the safety cap of "
            f"{cap}. Narrow the query (terms, year bounds, scope='title_abstract') "
            "or raise the cap with max_records / SAARI_SEARCH_MAX_RECORDS."
        )
    return n_available


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


def _count(client: httpx.Client, base_params: dict[str, Any]) -> int | None:
    """Total matching records (`meta.count`) via a one-record request."""
    params = {**base_params, "per_page": 1}
    data = _check(_get(client, f"{BASE_URL}/works", params))
    c = (data.get("meta") or {}).get("count")
    return int(c) if c is not None else None


def _list_query(
    base_params: dict[str, Any],
    *,
    limit: int,
    fetch_all: bool,
    cap: int,
    what: str,
    project_root: Path | None,
    refuse_over_cap: bool = True,
) -> SearchHits:
    hits = SearchHits()
    meta: dict[str, Any] = {}
    with httpx.Client(timeout=30.0) as client:
        if fetch_all:
            total = _count(client, base_params)
            hits.n_available = total
            if refuse_over_cap:
                n = _plan_limit(limit, True, total, cap, what)
            else:
                n = min(total if total is not None else cap, cap)
                if total is not None and total > cap:
                    hits.notes.append(
                        f"{what}: {total} available, fetched the first {cap} "
                        f"(safety cap; raise max_records to fetch more)"
                    )
        else:
            n = _plan_limit(limit, False, None, cap, what)
        for w in _iter_works(client, base_params, n, meta):
            hits.append((_work_to_paper(w), _dump_raw(w, project_root)))
    if hits.n_available is None:
        hits.n_available = meta.get("count")
    if meta.get("paging_limit"):
        hits.notes.append(
            f"{what}: stopped at {meta['paging_limit']} records (OpenAlex basic paging "
            "limit). Set OPENALEX_API_KEY to enable cursor paging past it."
        )
    log.info(
        "openalex %s: retrieved %d of %s available", what, len(hits), hits.n_available
    )
    return hits


def search(
    query: str,
    limit: int = 25,
    year_from: int | None = None,
    year_to: int | None = None,
    mailto: str | None = None,
    project_root: Path | None = None,
    *,
    scope: str = "all",
    fetch_all: bool = False,
    max_records: int | None = None,
) -> SearchHits:
    """Search OpenAlex /works. Returns [(paper, raw_path), ...] ranked by relevance.

    The returned list carries `n_available` (OpenAlex `meta.count`, the
    total matching records) so callers can tell a sample from the full set.

    - `limit`: records to fetch; pages transparently past 100.
    - `fetch_all`: fetch every matching record (ignores `limit`). Refuses
      with SearchTooLarge when the total exceeds `max_records` (default
      SAARI_SEARCH_MAX_RECORDS or 2000) rather than silently cutting.
    - `scope`: "all" (title, abstract, full text, keywords; the OpenAlex
      default) or "title_abstract" (title and abstract only, less noisy).

    Persists each raw response under `.saaristo/raw/openalex/<id>.json`.
    """
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {', '.join(SCOPES)}, got {scope!r}")
    params: dict[str, Any] = {SCOPES[scope]: query, **_auth_params(mailto)}
    filters: list[str] = []
    if year_from is not None:
        filters.append(f"from_publication_date:{year_from}-01-01")
    if year_to is not None:
        filters.append(f"to_publication_date:{year_to}-12-31")
    if filters:
        params["filter"] = ",".join(filters)
    return _list_query(
        params,
        limit=limit,
        fetch_all=fetch_all,
        cap=max_records or default_max_records(),
        what=f"search {query!r}",
        project_root=project_root,
    )


def fetch_by_id(
    work_id: str,
    mailto: str | None = None,
    project_root: Path | None = None,
) -> tuple[Paper, str] | None:
    """Fetch a single OpenAlex work by ID. Returns (paper, raw_path) or None."""
    bare = work_id.removeprefix("openalex:")
    with httpx.Client(timeout=30.0) as client:
        resp = _get(client, f"{BASE_URL}/works/{bare}", _auth_params(mailto))
        if resp.status_code == 404:
            return None
        work = _check(resp)
    return _work_to_paper(work), _dump_raw(work, project_root)


def fetch_works_by_ids(
    ids: list[str],
    mailto: str | None = None,
    project_root: Path | None = None,
    chunk_size: int = 50,
) -> list[tuple[Paper, str]]:
    """Batch-fetch OpenAlex works by ID. Dumps raw for each. Chunks if len(ids) > chunk_size.

    IDs OpenAlex does not return (deleted or merged works) are simply absent.
    """
    if not ids:
        return []
    bare_ids = [i.removeprefix("openalex:") for i in ids]
    results: list[tuple[Paper, str]] = []
    with httpx.Client(timeout=30.0) as client:
        for start in range(0, len(bare_ids), chunk_size):
            chunk = bare_ids[start : start + chunk_size]
            params = {
                "filter": f"openalex_id:{'|'.join(chunk)}",
                "per_page": chunk_size,
                **_auth_params(mailto),
            }
            data = _check(_get(client, f"{BASE_URL}/works", params))
            for work in data.get("results") or []:
                results.append((_work_to_paper(work), _dump_raw(work, project_root)))
    return results


def fetch_citing_works(
    work_id: str,
    limit: int | None = 25,
    mailto: str | None = None,
    project_root: Path | None = None,
    *,
    max_records: int | None = None,
) -> SearchHits:
    """Fetch works that cite `work_id` (forward snowball via OpenAlex `cites:` filter).

    `limit=None` fetches all citing works up to the safety cap; above the
    cap the first `max_records` are fetched and a note says so. The result
    carries `n_available` (total citing works).
    """
    bare = work_id.removeprefix("openalex:")
    params: dict[str, Any] = {"filter": f"cites:{bare}", **_auth_params(mailto)}
    cap = max_records or default_max_records()
    return _list_query(
        params,
        limit=limit if limit is not None else cap,
        fetch_all=limit is None,
        cap=cap,
        what=f"works citing {bare}",
        project_root=project_root,
        refuse_over_cap=False,
    )
