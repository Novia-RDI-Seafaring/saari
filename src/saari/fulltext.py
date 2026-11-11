"""Open-access full text: find, download, extract, cache, and page through it.

saari's search sources give titles and abstracts. This module adds the
optional next step: for a paper already in the project, find an open-access
copy using what the record already knows (OA locations, `pdf_url`, arXiv id,
PMCID), download it, extract plain text with pypdf, and cache both files under
`.saaristo/fulltext/` so repeat reads are free and offline.

Rules:

- **Open access only.** Candidates come from OA locations, arXiv, and PubMed
  Central. Closed records get `unavailable`; saari never tries to get around
  a paywall.
- **Honest status.** Every attempt writes one `paper_fulltext` row:
  `available` (text extracted), `unavailable` (no OA copy known), or
  `failed` (an OA copy was tried but nothing usable came back), with a reason
  and the per-candidate attempt log.
- **Agent-shaped reads.** `read_text` returns metadata plus a bounded slice
  (`offset` / `max_chars`) or grep hits, never a 100k-char dump.

Extraction needs the optional `fulltext` extra (pypdf). Without it, calls
raise `FulltextDependencyError` with install instructions; nothing is recorded
against the paper because that is an environment problem, not a paper one.

Pages are separated by a form feed (`\\f`) in the cached `.txt`, so a
character offset maps back to a page number.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from saari import __version__, db, paths
from saari.models import Paper

logger = logging.getLogger(__name__)

USER_AGENT = (
    f"saari/{__version__} (open-access full-text fetch; "
    "+https://github.com/Novia-RDI-Seafaring/saari)"
)
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_CHARS = 4000
HARD_MAX_CHARS = 50_000
PAGE_SEP = "\f"

STATUS_AVAILABLE = "available"
STATUS_UNAVAILABLE = "unavailable"
STATUS_FAILED = "failed"

_INSTALL_HINT = (
    "Full-text extraction needs the optional 'fulltext' extra (pypdf). "
    "Install it with `uv sync --extra fulltext` in a checkout, "
    "`uv add 'saari[fulltext]'` in a project, or run the MCP server as "
    "`uvx --from 'saari[fulltext]' saari-mcp`."
)

# Version of record first: it is what a review cites.
_VERSION_RANK = {"publishedVersion": 0, "acceptedVersion": 1, "submittedVersion": 2}


class FulltextDependencyError(RuntimeError):
    """The optional `fulltext` extra (pypdf) is not installed."""


class _NotPdf(Exception):
    """The server answered, but not with a PDF (HTML landing page, etc.)."""


def _require_pypdf():
    try:
        import pypdf
    except ImportError:
        raise FulltextDependencyError(_INSTALL_HINT) from None
    return pypdf


def fulltext_dir(root: Path | None = None) -> Path:
    return paths.saaristo_dir(root) / "fulltext"


def _slug(paper_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", paper_id).strip("_") or "paper"


# --------------------------------------------------------------------------- #
# Candidate discovery
# --------------------------------------------------------------------------- #

@dataclass
class Candidate:
    url: str
    kind: str  # location | pdf_url | pmc | arxiv
    version: str | None = None
    source_name: str | None = None


def candidates(paper: Paper) -> list[Candidate]:
    """Ordered, de-duplicated OA download candidates for a paper.

    Order: OA location PDFs (version of record first), the record's own
    `pdf_url` (unless the record is marked closed), PubMed Central, arXiv.
    Later entries are fallbacks; the first one that yields a PDF wins.
    """
    out: list[Candidate] = []
    oa_locs = [loc for loc in paper.locations if loc.is_oa and loc.pdf_url]
    oa_locs.sort(key=lambda loc: _VERSION_RANK.get(loc.version or "", 3))
    for loc in oa_locs:
        out.append(Candidate(loc.pdf_url or "", "location", loc.version, loc.source_name))
    if paper.pdf_url and (paper.oa_status or "").lower() != "closed":
        out.append(Candidate(paper.pdf_url, "pdf_url"))
    if paper.pmcid:
        pmcid = paper.pmcid if paper.pmcid.upper().startswith("PMC") else f"PMC{paper.pmcid}"
        out.append(Candidate(
            f"https://europepmc.org/articles/{pmcid}?pdf=render", "pmc", source_name="Europe PMC",
        ))
    if paper.arxiv_id:
        out.append(Candidate(
            f"https://arxiv.org/pdf/{paper.arxiv_id}", "arxiv", "submittedVersion", "arXiv",
        ))

    seen: set[str] = set()
    unique: list[Candidate] = []
    for c in out:
        key = c.url.strip().rstrip("/")
        if key and key not in seen:
            seen.add(key)
            unique.append(c)
    return unique


# --------------------------------------------------------------------------- #
# Download + extraction
# --------------------------------------------------------------------------- #

class _Throttle:
    """Minimum interval between requests to the same host (batch politeness)."""

    def __init__(self, min_interval: float = 1.0) -> None:
        self.min_interval = min_interval
        self._last: dict[str, float] = {}

    def wait(self, url: str) -> None:
        if self.min_interval <= 0:
            return
        host = urlparse(url).netloc
        last = self._last.get(host)
        if last is not None:
            delay = self.min_interval - (time.monotonic() - last)
            if delay > 0:
                time.sleep(delay)
        self._last[host] = time.monotonic()


def _download_pdf(client: httpx.Client, url: str, max_bytes: int) -> tuple[bytes, str]:
    """GET `url` and return (pdf_bytes, final_url). Raises _NotPdf / httpx errors."""
    with client.stream("GET", url) as resp:
        if resp.status_code >= 400:
            raise _NotPdf(f"HTTP {resp.status_code}")
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        declared = resp.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise _NotPdf(f"too large ({int(declared)} bytes > cap {max_bytes})")
        buf = bytearray()
        for chunk in resp.iter_bytes():
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise _NotPdf(f"too large (> cap {max_bytes} bytes)")
            # Bail early on an obvious HTML page instead of reading all of it.
            if len(buf) >= 1024 and b"%PDF-" not in bytes(buf[:1024]):
                break
        data = bytes(buf)
        final_url = str(resp.url)
    if b"%PDF-" not in data[:1024]:
        head = data[:512].lstrip().lower()
        if "html" in ctype or head.startswith((b"<!doctype", b"<html", b"<")):
            raise _NotPdf("HTML page served instead of a PDF (landing page or bot check)")
        raise _NotPdf(f"not a PDF (content-type {ctype or 'unknown'})")
    return data, final_url


def _extract(pdf_bytes: bytes) -> tuple[str, int]:
    """Extract text page by page. Returns (text with \\f page breaks, n_pages)."""
    pypdf = _require_pypdf()
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as e:  # noqa: BLE001 - pypdf raises several types
            raise ValueError(f"encrypted PDF ({e})") from None
    pages: list[str] = []
    for page in reader.pages:
        try:
            t = page.extract_text() or ""
        except Exception as e:  # noqa: BLE001 - one bad page should not sink the paper
            logger.warning("page extraction failed: %s", e)
            t = ""
        pages.append(t.replace("\x00", "").replace(PAGE_SEP, " ").strip())
    return PAGE_SEP.join(pages), len(pages)


def quality_hint(text: str, n_pages: int) -> str:
    """Rough extraction quality: good | low_text | garbled | empty.

    `empty` usually means a scanned PDF (OCR is not supported). `low_text`
    means very little text per page (figures-only, a cover sheet, or partial
    extraction). `garbled` means mostly non-word characters (font encoding
    problems).
    """
    body = text.replace(PAGE_SEP, "")
    n = len(body.strip())
    if n == 0:
        return "empty"
    sample = body[:20_000]
    wordish = sum(ch.isalnum() or ch.isspace() for ch in sample) / max(len(sample), 1)
    if wordish < 0.75:
        return "garbled"
    if n / max(n_pages, 1) < 300:
        return "low_text"
    return "good"


# --------------------------------------------------------------------------- #
# DB rows
# --------------------------------------------------------------------------- #

def _get_row(con: sqlite3.Connection, paper_id: str) -> dict[str, Any] | None:
    row = con.execute("SELECT * FROM paper_fulltext WHERE paper_id = ?", (paper_id,)).fetchone()
    if not row:
        return None
    rec = dict(row)
    rec["attempts"] = json.loads(rec.pop("attempts_json") or "[]")
    return rec


def _put_row(con: sqlite3.Connection, paper_id: str, rec: dict[str, Any]) -> None:
    con.execute(
        """
        INSERT INTO paper_fulltext (
            paper_id, status, reason, source_url, source_kind, source_version,
            pdf_path, text_path, n_chars, n_pages, quality, sha256, attempts_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(paper_id) DO UPDATE SET
            status = excluded.status, reason = excluded.reason,
            source_url = excluded.source_url, source_kind = excluded.source_kind,
            source_version = excluded.source_version, pdf_path = excluded.pdf_path,
            text_path = excluded.text_path, n_chars = excluded.n_chars,
            n_pages = excluded.n_pages, quality = excluded.quality,
            sha256 = excluded.sha256, attempts_json = excluded.attempts_json,
            fetched_at = datetime('now')
        """,
        (
            paper_id, rec["status"], rec.get("reason"), rec.get("source_url"),
            rec.get("source_kind"), rec.get("source_version"), rec.get("pdf_path"),
            rec.get("text_path"), rec.get("n_chars"), rec.get("n_pages"),
            rec.get("quality"), rec.get("sha256"), json.dumps(rec.get("attempts") or []),
        ),
    )


def _public(rec: dict[str, Any], root: Path) -> dict[str, Any]:
    """Record shape returned to callers: absolute cache paths, no internals."""
    out = {k: rec.get(k) for k in (
        "status", "reason", "source_url", "source_kind", "source_version",
        "n_chars", "n_pages", "quality", "fetched_at",
    )}
    out["pdf_path"] = str(root / rec["pdf_path"]) if rec.get("pdf_path") else None
    out["text_path"] = str(root / rec["text_path"]) if rec.get("text_path") else None
    out["attempts"] = rec.get("attempts") or []
    return out


# --------------------------------------------------------------------------- #
# Public operations
# --------------------------------------------------------------------------- #

def _fetch_one(
    con: sqlite3.Connection,
    paper: Paper,
    root: Path,
    *,
    refresh: bool,
    client: httpx.Client,
    max_bytes: int,
    throttle: _Throttle | None,
) -> tuple[dict[str, Any], bool]:
    """Fetch (or reuse) full text for one paper. Returns (record, from_cache)."""
    rel_dir = fulltext_dir(root).relative_to(root)
    pdf_rel = rel_dir / f"{_slug(paper.id)}.pdf"
    txt_rel = rel_dir / f"{_slug(paper.id)}.txt"

    cached = _get_row(con, paper.id)
    if cached and not refresh:
        if cached["status"] != STATUS_AVAILABLE:
            return cached, True
        if (root / txt_rel).exists():
            return cached, True

    _require_pypdf()  # fail before any network if the extra is missing
    fulltext_dir(root).mkdir(parents=True, exist_ok=True)

    # Cached PDF but no text (e.g. text file deleted): re-extract offline.
    pdf_bytes: bytes | None = None
    source: dict[str, Any] = {}
    attempts: list[dict[str, Any]] = []
    if cached and cached.get("pdf_path") and (root / cached["pdf_path"]).exists() and not refresh:
        pdf_bytes = (root / cached["pdf_path"]).read_bytes()
        source = {k: cached.get(k) for k in ("source_url", "source_kind", "source_version")}
        attempts = cached.get("attempts") or []

    if pdf_bytes is None:
        cands = candidates(paper)
        if not cands:
            oa = paper.oa_status or "unknown"
            rec = {
                "status": STATUS_UNAVAILABLE,
                "reason": f"no open-access PDF location recorded (oa_status={oa})",
                "attempts": [],
            }
            _put_row(con, paper.id, rec)
            return _get_row(con, paper.id) or rec, False

        for c in cands:
            if throttle:
                throttle.wait(c.url)
            try:
                data, final_url = _download_pdf(client, c.url, max_bytes)
            except (_NotPdf, httpx.HTTPError) as e:
                msg = str(e) or type(e).__name__
                logger.info("full text candidate failed for %s: %s (%s)", paper.id, c.url, msg)
                attempts.append({"url": c.url, "kind": c.kind, "ok": False, "error": msg})
                continue
            attempts.append({"url": c.url, "kind": c.kind, "ok": True})
            pdf_bytes = data
            source = {"source_url": final_url, "source_kind": c.kind, "source_version": c.version}
            break

        if pdf_bytes is None:
            rec = {
                "status": STATUS_FAILED,
                "reason": f"no usable PDF from {len(cands)} open-access candidate(s); "
                          f"last error: {attempts[-1]['error']}",
                "attempts": attempts,
            }
            _put_row(con, paper.id, rec)
            return _get_row(con, paper.id) or rec, False
        (root / pdf_rel).write_bytes(pdf_bytes)

    sha = hashlib.sha256(pdf_bytes).hexdigest()
    try:
        text, n_pages = _extract(pdf_bytes)
    except FulltextDependencyError:
        raise
    except Exception as e:  # noqa: BLE001 - malformed PDFs raise many types
        logger.warning("extraction failed for %s: %s", paper.id, e)
        rec = {
            "status": STATUS_FAILED, "reason": f"PDF downloaded but text extraction failed: {e}",
            "pdf_path": str(pdf_rel), "sha256": sha, "attempts": attempts, **source,
        }
        _put_row(con, paper.id, rec)
        return _get_row(con, paper.id) or rec, False

    quality = quality_hint(text, n_pages)
    n_chars = len(text)
    if quality == "empty":
        rec = {
            "status": STATUS_FAILED,
            "reason": "PDF has no extractable text (likely scanned; OCR is not supported)",
            "pdf_path": str(pdf_rel), "n_pages": n_pages, "n_chars": 0, "quality": quality,
            "sha256": sha, "attempts": attempts, **source,
        }
        _put_row(con, paper.id, rec)
        return _get_row(con, paper.id) or rec, False

    (root / txt_rel).write_text(text, encoding="utf-8")
    rec = {
        "status": STATUS_AVAILABLE, "reason": None,
        "pdf_path": str(pdf_rel), "text_path": str(txt_rel),
        "n_chars": n_chars, "n_pages": n_pages, "quality": quality, "sha256": sha,
        "attempts": attempts, **source,
    }
    _put_row(con, paper.id, rec)
    return _get_row(con, paper.id) or rec, False


def _client(timeout: float) -> httpx.Client:
    return httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept": "application/pdf,*/*;q=0.5"},
    )


def fetch(
    paper_id: str,
    *,
    refresh: bool = False,
    project_root: Path | None = None,
    client: httpx.Client | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    """Fetch and cache open-access full text for one paper.

    Idempotent: a recorded result (any status) is returned from the DB
    without network unless `refresh=True`. Raises KeyError if the paper is
    not in the project, FulltextDependencyError if pypdf is missing.
    """
    root = project_root or paths.project_root()
    own = client is None
    http = client or _client(timeout)
    try:
        with db.connect(paths.db_path(root)) as con:
            paper = db.get_paper(con, paper_id)
            if paper is None:
                raise KeyError(f"Paper not found: {paper_id}")
            rec, from_cache = _fetch_one(
                con, paper, root, refresh=refresh, client=http,
                max_bytes=max_bytes, throttle=None,
            )
    finally:
        if own:
            http.close()
    return {"paper_id": paper.id, "title": paper.title, "cached": from_cache, **_public(rec, root)}


def fetch_many(
    *,
    status: str | None = "included",
    refresh: bool = False,
    limit: int | None = None,
    project_root: Path | None = None,
    client: httpx.Client | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    min_interval: float = 1.0,
) -> dict[str, Any]:
    """Fetch full text for every paper with screening `status` (default included).

    Returns a compact summary: counts per outcome plus one line per paper
    that did not get full text, with the reason. `status=None` means all
    papers. Already-recorded papers are skipped unless `refresh=True`.
    """
    root = project_root or paths.project_root()
    _require_pypdf()
    own = client is None
    http = client or _client(timeout)
    throttle = _Throttle(min_interval)
    counts = {STATUS_AVAILABLE: 0, STATUS_UNAVAILABLE: 0, STATUS_FAILED: 0}
    n_cached = 0
    missing: list[dict[str, Any]] = []
    try:
        with db.connect(paths.db_path(root)) as con:
            papers = db.list_papers(
                con, limit=limit if limit is not None else 1_000_000,
                status=status, order_by="first_seen_at ASC",
            )
            for p in papers:
                rec, from_cache = _fetch_one(
                    con, p, root, refresh=refresh, client=http,
                    max_bytes=max_bytes, throttle=throttle,
                )
                con.commit()  # keep progress if a long batch is interrupted
                counts[rec["status"]] = counts.get(rec["status"], 0) + 1
                n_cached += int(from_cache)
                if rec["status"] != STATUS_AVAILABLE:
                    missing.append({
                        "paper_id": p.id, "title": p.title[:100],
                        "status": rec["status"], "reason": rec.get("reason"),
                    })
    finally:
        if own:
            http.close()
    return {
        "status_filter": status,
        "n_papers": len(papers),
        "n_available": counts[STATUS_AVAILABLE],
        "n_unavailable": counts[STATUS_UNAVAILABLE],
        "n_failed": counts[STATUS_FAILED],
        "n_from_cache": n_cached,
        "missing": missing,
    }


def _page_at(text: str, offset: int) -> int:
    return text.count(PAGE_SEP, 0, max(offset, 0)) + 1


def read_text(
    paper_id: str,
    *,
    offset: int = 0,
    max_chars: int = DEFAULT_MAX_CHARS,
    grep: str | None = None,
    context: int = 200,
    max_hits: int = 20,
    fetch_if_missing: bool = True,
    refresh: bool = False,
    project_root: Path | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Read a bounded slice of a paper's full text, or grep it.

    Without `grep`: returns `text[offset:offset+max_chars]` plus
    `next_offset` (None at the end). With `grep` (case-insensitive regex;
    an invalid pattern is matched literally): returns up to `max_hits`
    snippets with `context` chars either side, each with its char offset
    and page number, so the agent can page to it.

    Fetches the PDF first when nothing is recorded yet and
    `fetch_if_missing` is true. The response always carries the status
    record, so an unavailable paper explains itself.
    """
    root = project_root or paths.project_root()
    with db.connect(paths.db_path(root)) as con:
        paper = db.get_paper(con, paper_id)
        if paper is None:
            raise KeyError(f"Paper not found: {paper_id}")
        row = _get_row(con, paper.id)

    if refresh or (row is None and fetch_if_missing) or (
        row is not None and row["status"] == STATUS_AVAILABLE
        and not (root / (row.get("text_path") or "")).is_file()
    ):
        meta = fetch(paper.id, refresh=refresh, project_root=root, client=client)
    elif row is None:
        return {
            "paper_id": paper.id, "title": paper.title, "status": None,
            "reason": "full text not fetched yet (call with fetch enabled)",
        }
    else:
        meta = {"paper_id": paper.id, "title": paper.title, "cached": True, **_public(row, root)}

    if meta["status"] != STATUS_AVAILABLE or not meta.get("text_path"):
        return meta

    text = Path(meta["text_path"]).read_text(encoding="utf-8")
    meta = {**meta}
    meta.pop("attempts", None)  # noise once text is available; still in the DB

    if grep:
        try:
            pat = re.compile(grep, re.IGNORECASE)
        except re.error:
            pat = re.compile(re.escape(grep), re.IGNORECASE)
        hits: list[dict[str, Any]] = []
        n_hits = 0
        for m in pat.finditer(text):
            if m.end() == m.start():
                continue
            n_hits += 1
            if len(hits) < max_hits:
                a, b = max(m.start() - context, 0), min(m.end() + context, len(text))
                hits.append({
                    "offset": m.start(),
                    "page": _page_at(text, m.start()),
                    "snippet": " ".join(text[a:b].split()),
                })
        return {**meta, "grep": grep, "n_hits": n_hits, "hits": hits,
                "truncated": n_hits > len(hits)}

    max_chars = max(1, min(int(max_chars), HARD_MAX_CHARS))
    offset = max(0, int(offset))
    chunk = text[offset:offset + max_chars]
    end = offset + len(chunk)
    return {
        **meta,
        "offset": offset,
        "n_returned": len(chunk),
        "next_offset": end if end < len(text) else None,
        "page_start": _page_at(text, offset),
        "page_end": _page_at(text, max(end - 1, offset)),
        "text": chunk,
    }


def coverage(
    project_root: Path | None = None, status: str = "included"
) -> dict[str, int]:
    """Full-text retrieval counts for papers with screening `status`.

    Used by the report's Limitations block. `n_available` counts retrieved
    and extracted text only; it says nothing about whether it was read.
    """
    root = project_root or paths.project_root()
    with db.connect(paths.db_path(root)) as con:
        n_papers = con.execute(
            "SELECT COUNT(*) FROM paper WHERE status = ?", (status,)
        ).fetchone()[0]
        rows = con.execute(
            "SELECT f.status, COUNT(*) AS n FROM paper_fulltext f "
            "JOIN paper p ON p.id = f.paper_id WHERE p.status = ? GROUP BY f.status",
            (status,),
        ).fetchall()
    by = {r["status"]: r["n"] for r in rows}
    return {
        "n_papers": int(n_papers),
        "n_available": int(by.get(STATUS_AVAILABLE, 0)),
        "n_unavailable": int(by.get(STATUS_UNAVAILABLE, 0)),
        "n_failed": int(by.get(STATUS_FAILED, 0)),
    }
