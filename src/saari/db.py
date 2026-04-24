from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from saari import paths
from saari.models import Author, Location, Paper

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper (
    id TEXT PRIMARY KEY,
    doi TEXT UNIQUE,
    arxiv_id TEXT,
    pmid TEXT,
    pmcid TEXT,
    title TEXT NOT NULL,
    abstract TEXT,
    abstract_suspect INTEGER NOT NULL DEFAULT 0,
    year INTEGER,
    venue TEXT,
    authors_json TEXT NOT NULL DEFAULT '[]',
    cited_by_count INTEGER,
    oa_status TEXT,
    landing_page_url TEXT,
    pdf_url TEXT,
    locations_json TEXT NOT NULL DEFAULT '[]',
    referenced_works_json TEXT NOT NULL DEFAULT '[]',
    related_works_json TEXT NOT NULL DEFAULT '[]',
    raw_path TEXT,
    status TEXT NOT NULL DEFAULT 'candidate',
    screening_note TEXT,
    source_provenance_json TEXT NOT NULL DEFAULT '{}',
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS search (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    query TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    n_returned INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS search_result (
    search_id INTEGER NOT NULL REFERENCES search(id) ON DELETE CASCADE,
    paper_id TEXT NOT NULL REFERENCES paper(id),
    rank INTEGER NOT NULL,
    PRIMARY KEY (search_id, paper_id)
);

CREATE INDEX IF NOT EXISTS idx_search_result_paper ON search_result(paper_id);
"""

_PAPER_COLUMNS = [
    ("arxiv_id", "TEXT"),
    ("pmid", "TEXT"),
    ("pmcid", "TEXT"),
    ("abstract_suspect", "INTEGER NOT NULL DEFAULT 0"),
    ("oa_status", "TEXT"),
    ("landing_page_url", "TEXT"),
    ("pdf_url", "TEXT"),
    ("locations_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("referenced_works_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("related_works_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("raw_path", "TEXT"),
    ("status", "TEXT NOT NULL DEFAULT 'candidate'"),
    ("screening_note", "TEXT"),
]

_VALID_STATUSES = {"candidate", "included", "excluded", "maybe"}
_STATUS_ALIASES = {
    "include": "included",
    "exclude": "excluded",
    "reject": "excluded",
    "keep": "included",
    "reset": "candidate",
    "yes": "included",
    "no": "excluded",
}


def normalize_status(s: str) -> str:
    s = s.strip().lower()
    return _STATUS_ALIASES.get(s, s)


def _migrate(con: sqlite3.Connection) -> None:
    """Bring older DBs up to the canonical schema."""
    existing = {row["name"] for row in con.execute("PRAGMA table_info(paper)")}
    for name, defn in _PAPER_COLUMNS:
        if name not in existing:
            con.execute(f"ALTER TABLE paper ADD COLUMN {name} {defn}")


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = db_path or paths.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        con.executescript(SCHEMA)
        _migrate(con)
        yield con
        con.commit()
    finally:
        con.close()


def upsert_paper(
    con: sqlite3.Connection, paper: Paper, raw_path: str | None = None
) -> None:
    authors_json = json.dumps([a.model_dump() for a in paper.authors])
    locations_json = json.dumps([loc.model_dump() for loc in paper.locations])
    refs_json = json.dumps(paper.referenced_works)
    rels_json = json.dumps(paper.related_works)
    prov_json = json.dumps(paper.source_provenance)
    con.execute(
        """
        INSERT INTO paper (
            id, doi, arxiv_id, pmid, pmcid, title, abstract, abstract_suspect,
            year, venue, authors_json, cited_by_count, oa_status,
            landing_page_url, pdf_url, locations_json,
            referenced_works_json, related_works_json, raw_path,
            source_provenance_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            doi = COALESCE(excluded.doi, paper.doi),
            arxiv_id = COALESCE(excluded.arxiv_id, paper.arxiv_id),
            pmid = COALESCE(excluded.pmid, paper.pmid),
            pmcid = COALESCE(excluded.pmcid, paper.pmcid),
            title = excluded.title,
            abstract = COALESCE(excluded.abstract, paper.abstract),
            abstract_suspect = excluded.abstract_suspect,
            year = COALESCE(excluded.year, paper.year),
            venue = COALESCE(excluded.venue, paper.venue),
            authors_json = excluded.authors_json,
            cited_by_count = excluded.cited_by_count,
            oa_status = COALESCE(excluded.oa_status, paper.oa_status),
            landing_page_url = COALESCE(excluded.landing_page_url, paper.landing_page_url),
            pdf_url = COALESCE(excluded.pdf_url, paper.pdf_url),
            locations_json = excluded.locations_json,
            referenced_works_json = excluded.referenced_works_json,
            related_works_json = excluded.related_works_json,
            raw_path = COALESCE(excluded.raw_path, paper.raw_path),
            source_provenance_json = excluded.source_provenance_json,
            last_updated_at = datetime('now')
        """,
        (
            paper.id, paper.doi, paper.arxiv_id, paper.pmid, paper.pmcid,
            paper.title, paper.abstract, int(paper.abstract_suspect),
            paper.year, paper.venue, authors_json, paper.cited_by_count,
            paper.oa_status, paper.landing_page_url, paper.pdf_url,
            locations_json, refs_json, rels_json, raw_path, prov_json,
        ),
    )


def get_raw_path(con: sqlite3.Connection, paper_id: str) -> str | None:
    row = con.execute(
        "SELECT raw_path FROM paper WHERE id = ? OR doi = ? OR arxiv_id = ? OR pmid = ?",
        (paper_id, paper_id, paper_id, paper_id),
    ).fetchone()
    return row["raw_path"] if row else None


def record_search(
    con: sqlite3.Connection,
    source: str,
    query: str,
    params: dict,
    paper_ids: list[str],
) -> int:
    cur = con.execute(
        "INSERT INTO search (source, query, params_json, n_returned) VALUES (?, ?, ?, ?)",
        (source, query, json.dumps(params), len(paper_ids)),
    )
    search_id = int(cur.lastrowid or 0)
    con.executemany(
        "INSERT OR IGNORE INTO search_result (search_id, paper_id, rank) VALUES (?, ?, ?)",
        [(search_id, pid, rank) for rank, pid in enumerate(paper_ids)],
    )
    return search_id


def _row_to_paper(row: sqlite3.Row) -> Paper:
    def _col(name: str, default: Any = None) -> Any:
        try:
            return row[name]
        except (IndexError, KeyError):
            return default

    return Paper(
        id=row["id"],
        doi=row["doi"],
        arxiv_id=_col("arxiv_id"),
        pmid=_col("pmid"),
        pmcid=_col("pmcid"),
        title=row["title"],
        abstract=row["abstract"],
        abstract_suspect=bool(_col("abstract_suspect", 0)),
        year=row["year"],
        venue=row["venue"],
        authors=[Author(**a) for a in json.loads(row["authors_json"])],
        cited_by_count=row["cited_by_count"],
        oa_status=_col("oa_status"),
        landing_page_url=_col("landing_page_url"),
        pdf_url=_col("pdf_url"),
        locations=[Location(**loc) for loc in json.loads(_col("locations_json") or "[]")],
        referenced_works=json.loads(_col("referenced_works_json") or "[]"),
        related_works=json.loads(_col("related_works_json") or "[]"),
        status=_col("status", "candidate") or "candidate",
        screening_note=_col("screening_note"),
        seen_in=int(_col("seen_in", 0) or 0),
        source_provenance=json.loads(row["source_provenance_json"]),
        first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
        last_updated_at=datetime.fromisoformat(row["last_updated_at"]),
    )


_LIST_SELECT = (
    "SELECT p.*, "
    "  (SELECT COUNT(DISTINCT search_id) FROM search_result sr WHERE sr.paper_id = p.id) AS seen_in "
    "FROM paper p"
)


def list_papers(
    con: sqlite3.Connection,
    limit: int = 20,
    offset: int = 0,
    *,
    status: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    min_citations: int | None = None,
    title_grep: str | None = None,
    seen_in_at_least: int | None = None,
    order_by: str = "first_seen_at DESC",
) -> list[Paper]:
    where: list[str] = []
    args: list[Any] = []
    if status is not None:
        where.append("p.status = ?")
        args.append(status)
    if year_from is not None:
        where.append("p.year >= ?")
        args.append(year_from)
    if year_to is not None:
        where.append("p.year <= ?")
        args.append(year_to)
    if min_citations is not None:
        where.append("COALESCE(p.cited_by_count, 0) >= ?")
        args.append(min_citations)
    if title_grep:
        where.append("LOWER(p.title) LIKE ?")
        args.append(f"%{title_grep.lower()}%")

    inner = _LIST_SELECT
    if where:
        inner += " WHERE " + " AND ".join(where)

    if seen_in_at_least is not None:
        sql = f"SELECT * FROM ({inner}) WHERE seen_in >= ?"
        args.append(seen_in_at_least)
    else:
        sql = inner

    sql += f" ORDER BY {order_by} LIMIT ? OFFSET ?"
    args.extend([limit, offset])
    rows = con.execute(sql, args).fetchall()
    return [_row_to_paper(r) for r in rows]


def get_paper(con: sqlite3.Connection, paper_id: str) -> Paper | None:
    row = con.execute(
        _LIST_SELECT + " WHERE p.id = ? OR p.doi = ? OR p.arxiv_id = ? OR p.pmid = ?",
        (paper_id, paper_id, paper_id, paper_id),
    ).fetchone()
    return _row_to_paper(row) if row else None


def set_screening(
    con: sqlite3.Connection,
    paper_id: str,
    status: str,
    note: str | None = None,
) -> bool:
    status = normalize_status(status)
    if status not in _VALID_STATUSES:
        raise ValueError(
            f"Invalid status {status!r}. Must be one of: {', '.join(sorted(_VALID_STATUSES))} "
            f"(or aliases: {', '.join(sorted(_STATUS_ALIASES))})"
        )
    cur = con.execute(
        "UPDATE paper SET status = ?, screening_note = ?, last_updated_at = datetime('now') "
        "WHERE id = ? OR doi = ? OR arxiv_id = ? OR pmid = ?",
        (status, note, paper_id, paper_id, paper_id, paper_id),
    )
    return cur.rowcount > 0


def count_by_status(con: sqlite3.Connection) -> dict[str, int]:
    rows = con.execute("SELECT status, COUNT(*) AS n FROM paper GROUP BY status").fetchall()
    return {row["status"]: row["n"] for row in rows}


def list_searches(con: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = con.execute(
        "SELECT * FROM search ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
