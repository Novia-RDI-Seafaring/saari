"""Duplicate detection and reviewer-confirmed merging.

Policy (systematic-review safe):

- **Automatic** dedup happens only at ingest time and only on exact
  persistent identifiers (DOI, PMID, well-formed arXiv id); see
  `saari.sources.find_existing`. Nothing here merges on its own.
- **Candidates** are surfaced by `find_candidates` for a reviewer to judge:
  * two records sharing an identifier (case-differing DOI, PMID, arXiv id
    or PMCID) - strong evidence, labelled `confidence="high"`;
  * same normalized title, publication years at most 1 apart (preprint vs
    journal version), and the same first-author surname when both records
    list authors - labelled `confidence="medium"`, or `"low"` when a record
    has no authors or no year to compare.
  Records whose first-author surnames differ are never candidates. Very
  short or generic titles ("Editorial", "Introduction") are never matched
  on title alone.
- **merge** keeps one record and folds the other into it: identifiers and
  missing fields are filled in, provenance is preserved under
  `source_provenance["merged_from"]`, search links move to the kept record,
  and the removed id stays resolvable as an alias. A merge counts as one
  more "duplicate removed" in the PRISMA funnel.
- **dismiss** records that a candidate pair is NOT a duplicate so it stops
  being suggested.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import unicodedata
from datetime import UTC, datetime
from typing import Any

from saari import db
from saari.models import Paper
from saari.sources.ids import is_valid_arxiv_id

log = logging.getLogger(__name__)

# Titles shorter than this (after normalization) are too generic to match on.
MIN_TITLE_CHARS = 20
MIN_TITLE_WORDS = 3
MAX_YEAR_GAP = 1
_GENERIC_TITLES = {
    "editorial", "introduction", "preface", "foreword", "erratum", "corrigendum",
    "correction", "retraction", "book review", "index", "contents", "table of contents",
    "front matter", "back matter", "reply", "response", "commentary", "discussion",
}
_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_PREPRINT_DOI_PREFIXES = ("10.48550/", "10.1101/", "10.20944/", "10.2139/ssrn", "10.31234/", "10.21203/")


def _fold(text: str) -> str:
    s = unicodedata.normalize("NFKD", text)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower()


def normalize_title(title: str | None) -> str:
    """Lowercase, accent-folded, punctuation collapsed to single spaces."""
    if not title:
        return ""
    s = re.sub(r"<[^>]+>", " ", title)  # stray inline markup
    return _NON_ALNUM.sub(" ", _fold(s)).strip()


def title_key(title: str | None) -> str | None:
    """Normalized title if it is specific enough to match on, else None."""
    t = normalize_title(title)
    if len(t) < MIN_TITLE_CHARS or len(t.split()) < MIN_TITLE_WORDS:
        return None
    if t in _GENERIC_TITLES:
        return None
    return t


def first_author_surname(paper: Paper) -> str | None:
    """Folded surname of the first listed author ("Doe, Jane" or "Jane Doe")."""
    if not paper.authors:
        return None
    name = (paper.authors[0].name or "").strip()
    if not name:
        return None
    surname = name.split(",", 1)[0] if "," in name else name.split()[-1]
    s = _NON_ALNUM.sub("", _fold(surname))
    return s or None


def _pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def _is_preprint(p: Paper) -> bool:
    doi = (p.doi or "").lower()
    if doi.startswith(_PREPRINT_DOI_PREFIXES):
        return True
    venue = (p.venue or "").lower()
    return any(v in venue for v in ("arxiv", "biorxiv", "medrxiv", "ssrn", "preprint"))


def suggest_keep(a: Paper, b: Paper) -> str:
    """Which record to keep: published over preprint, then richer metadata.

    A suggestion only; the reviewer chooses.
    """
    def score(p: Paper) -> tuple:
        return (
            not _is_preprint(p),
            p.status != "candidate",  # keep the screened record
            bool(p.doi),
            bool(p.abstract) and not p.abstract_suspect,
            p.id.startswith("openalex:"),  # snowball-able
            p.cited_by_count or 0,
        )

    return a.id if score(a) >= score(b) else b.id


def _title_signals(a: Paper, b: Paper) -> tuple[list[str], str] | None:
    """Signals + confidence if a/b match on title rules, else None."""
    if a.year is not None and b.year is not None:
        if abs(a.year - b.year) > MAX_YEAR_GAP:
            return None
        year_sig = "same_year" if a.year == b.year else "year_within_1"
    else:
        year_sig = "year_unknown"
    sa, sb = first_author_surname(a), first_author_surname(b)
    if sa and sb:
        if sa != sb:
            return None
        author_sig = "same_first_author"
    else:
        author_sig = "first_author_unknown"
    signals = ["same_title", year_sig, author_sig]
    confidence = "low" if "unknown" in year_sig or "unknown" in author_sig else "medium"
    return signals, confidence


def _dismissed(con: sqlite3.Connection) -> set[tuple[str, str]]:
    return {(r["a"], r["b"]) for r in con.execute("SELECT a, b FROM paper_not_duplicate")}


def find_candidates(con: sqlite3.Connection) -> list[dict[str, Any]]:
    """All candidate duplicate pairs in the corpus, strongest first."""
    papers = db.list_papers(con, limit=10_000_000, order_by="first_seen_at ASC")
    by_id = {p.id: p for p in papers}
    dismissed = _dismissed(con)
    found: dict[tuple[str, str], dict[str, Any]] = {}

    def add(a: Paper, b: Paper, signals: list[str], confidence: str) -> None:
        key = _pair(a.id, b.id)
        if key in dismissed or a.id == b.id:
            return
        cur = found.get(key)
        if cur is None:
            found[key] = {"signals": list(signals), "confidence": confidence}
            return
        cur["signals"] = sorted(set(cur["signals"]) | set(signals))
        rank = {"high": 2, "medium": 1, "low": 0}
        if rank[confidence] > rank[cur["confidence"]]:
            cur["confidence"] = confidence

    # Identifier collisions between distinct rows.
    def ident_groups(fn, label: str) -> None:
        groups: dict[str, list[Paper]] = {}
        for p in papers:
            v = fn(p)
            if v:
                groups.setdefault(v, []).append(p)
        for group in groups.values():
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    add(group[i], group[j], [label], "high")

    ident_groups(lambda p: (p.doi or "").lower() or None, "same_doi")
    ident_groups(lambda p: p.pmid if p.pmid and str(p.pmid).isdigit() else None, "same_pmid")
    ident_groups(lambda p: p.pmcid or None, "same_pmcid")
    ident_groups(lambda p: p.arxiv_id if is_valid_arxiv_id(p.arxiv_id) else None, "same_arxiv_id")

    # Title-based candidates.
    by_title: dict[str, list[Paper]] = {}
    for p in papers:
        k = title_key(p.title)
        if k:
            by_title.setdefault(k, []).append(p)
    for group in by_title.values():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                res = _title_signals(group[i], group[j])
                if res:
                    add(group[i], group[j], *res)

    rank = {"high": 0, "medium": 1, "low": 2}
    out = []
    for (ida, idb), info in found.items():
        a, b = by_id[ida], by_id[idb]
        out.append(
            {
                "a": ida,
                "b": idb,
                "confidence": info["confidence"],
                "signals": info["signals"],
                "suggested_keep": suggest_keep(a, b),
                "papers": [_summary(a), _summary(b)],
            }
        )
    out.sort(key=lambda c: (rank[c["confidence"]], c["a"], c["b"]))
    return out


def _summary(p: Paper) -> dict[str, Any]:
    return {
        "id": p.id,
        "doi": p.doi,
        "title": p.title,
        "year": p.year,
        "venue": p.venue,
        "first_author": p.authors[0].name if p.authors else None,
        "status": p.status,
        "seen_in": p.seen_in,
    }


def _resolve(con: sqlite3.Connection, ref: str) -> Paper:
    p = db.get_paper(con, ref)
    if p is None:
        raise ValueError(f"Paper not found: {ref}")
    return p


def _union(a: list, b: list) -> list:
    out = list(a)
    for x in b:
        if x not in out:
            out.append(x)
    return out


def merge(
    con: sqlite3.Connection,
    keep: str,
    remove: str,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    """Fold record `remove` into record `keep` (reviewer-confirmed duplicate).

    - Missing identifiers / fields on the kept record are filled from the
      removed one; locations and reference lists are unioned.
    - Screening: if only one record has a decision, the kept record carries
      it. Conflicting decisions (e.g. included vs excluded) are refused;
      re-screen one record first.
    - The removed record's search links move to the kept record (so
      `seen_in` and per-source PRISMA counts are unchanged), its full row is
      snapshotted in `paper_merge`, and its id becomes an alias.
    - Its embedding / projection are dropped (re-run `refresh`).
    """
    kp, rp = _resolve(con, keep), _resolve(con, remove)
    if kp.id == rp.id:
        raise ValueError(f"{keep!r} and {remove!r} are the same record ({kp.id})")
    decided = {"included", "excluded", "maybe"}
    if kp.status in decided and rp.status in decided and kp.status != rp.status:
        raise ValueError(
            f"Screening conflict: {kp.id} is {kp.status!r}, {rp.id} is {rp.status!r}. "
            "Re-screen one of them so the decisions agree, then merge."
        )
    status, note = kp.status, kp.screening_note
    if kp.status == "candidate" and rp.status != "candidate":
        status, note = rp.status, rp.screening_note
    elif kp.status == rp.status and rp.screening_note and not kp.screening_note:
        note = rp.screening_note

    def links(pid: str) -> int:
        return con.execute(
            "SELECT COUNT(*) FROM search_result WHERE paper_id = ?", (pid,)
        ).fetchone()[0]

    absorbed_unlinked = int(links(kp.id) == 0 or links(rp.id) == 0)
    raw_row = dict(con.execute("SELECT * FROM paper WHERE id = ?", (rp.id,)).fetchone())

    # Move search links, then drop the removed record and its derived data.
    con.execute(
        "INSERT OR IGNORE INTO search_result (search_id, paper_id, rank) "
        "SELECT search_id, ?, rank FROM search_result WHERE paper_id = ?",
        (kp.id, rp.id),
    )
    con.execute("DELETE FROM search_result WHERE paper_id = ?", (rp.id,))
    con.execute("DELETE FROM paper_vec WHERE paper_id = ?", (rp.id,))
    con.execute("DELETE FROM paper_embedding_meta WHERE paper_id = ?", (rp.id,))
    con.execute("DELETE FROM paper_projection WHERE paper_id = ?", (rp.id,))
    con.execute("DELETE FROM paper WHERE id = ?", (rp.id,))

    prov = dict(kp.source_provenance)
    for k, v in rp.source_provenance.items():
        if k != "merged_from":
            prov.setdefault(k, v)
    merged_from = list(prov.get("merged_from") or [])
    merged_from.append(
        {
            "id": rp.id,
            "doi": rp.doi,
            "title": rp.title,
            "year": rp.year,
            "merged_at": datetime.now(UTC).isoformat(),
            "reason": reason,
            "source_provenance": {
                k: v for k, v in rp.source_provenance.items() if k != "merged_from"
            },
        }
    )
    merged_from.extend(rp.source_provenance.get("merged_from") or [])
    prov["merged_from"] = merged_from

    abstract, suspect = kp.abstract, kp.abstract_suspect
    if rp.abstract and (not kp.abstract or (kp.abstract_suspect and not rp.abstract_suspect)):
        abstract, suspect = rp.abstract, rp.abstract_suspect

    con.execute(
        """
        UPDATE paper SET
            doi = ?, arxiv_id = ?, pmid = ?, pmcid = ?,
            abstract = ?, abstract_suspect = ?,
            year = ?, venue = ?, oa_status = ?, landing_page_url = ?, pdf_url = ?,
            locations_json = ?, referenced_works_json = ?, related_works_json = ?,
            status = ?, screening_note = ?, source_provenance_json = ?,
            last_updated_at = datetime('now')
        WHERE id = ?
        """,
        (
            kp.doi or rp.doi, kp.arxiv_id or rp.arxiv_id, kp.pmid or rp.pmid,
            kp.pmcid or rp.pmcid, abstract, int(suspect),
            kp.year if kp.year is not None else rp.year, kp.venue or rp.venue,
            kp.oa_status or rp.oa_status, kp.landing_page_url or rp.landing_page_url,
            kp.pdf_url or rp.pdf_url,
            json.dumps([loc.model_dump() for loc in _union(kp.locations, rp.locations)]),
            json.dumps(_union(kp.referenced_works, rp.referenced_works)),
            json.dumps(_union(kp.related_works, rp.related_works)),
            status, note, json.dumps(prov), kp.id,
        ),
    )

    # Aliases that pointed at the removed record now point at the kept one.
    con.execute("UPDATE paper_merge SET kept_id = ? WHERE kept_id = ?", (kp.id, rp.id))
    signals = find_pair_signals(kp, rp)
    con.execute(
        "INSERT INTO paper_merge (removed_id, kept_id, reason, signals_json, absorbed_unlinked, removed_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (rp.id, kp.id, reason, json.dumps(signals), absorbed_unlinked, json.dumps(raw_row, default=str)),
    )
    log.info("merged %s into %s (%s)", rp.id, kp.id, ", ".join(signals) or "no signals")
    return {
        "kept_id": kp.id,
        "removed_id": rp.id,
        "status": status,
        "signals": signals,
        "hint": "run refresh to update the landscape projection",
    }


def find_pair_signals(a: Paper, b: Paper) -> list[str]:
    """Which duplicate signals this specific pair shows (for the merge log)."""
    sig: list[str] = []
    if a.doi and b.doi and a.doi.lower() == b.doi.lower():
        sig.append("same_doi")
    if a.pmid and a.pmid == b.pmid:
        sig.append("same_pmid")
    if a.pmcid and a.pmcid == b.pmcid:
        sig.append("same_pmcid")
    if is_valid_arxiv_id(a.arxiv_id) and a.arxiv_id == b.arxiv_id:
        sig.append("same_arxiv_id")
    ka = title_key(a.title)
    if ka and ka == title_key(b.title):
        res = _title_signals(a, b)
        sig.extend(res[0] if res else ["same_title"])
    return sig


def dismiss(con: sqlite3.Connection, a: str, b: str, note: str | None = None) -> dict[str, Any]:
    """Record that `a` and `b` are distinct works; they stop being suggested."""
    pa, pb = _resolve(con, a), _resolve(con, b)
    if pa.id == pb.id:
        raise ValueError(f"{a!r} and {b!r} are the same record ({pa.id})")
    x, y = _pair(pa.id, pb.id)
    con.execute(
        "INSERT INTO paper_not_duplicate (a, b, note) VALUES (?, ?, ?) "
        "ON CONFLICT(a, b) DO UPDATE SET note = excluded.note",
        (x, y, note),
    )
    return {"a": x, "b": y, "dismissed": True}


def list_merges(con: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = con.execute(
        "SELECT removed_id, kept_id, reason, signals_json, merged_at FROM paper_merge ORDER BY merged_at"
    ).fetchall()
    return [
        {**{k: r[k] for k in ("removed_id", "kept_id", "reason", "merged_at")},
         "signals": json.loads(r["signals_json"] or "[]")}
        for r in rows
    ]
