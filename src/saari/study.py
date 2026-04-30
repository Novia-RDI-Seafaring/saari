"""Study metadata: research question, inclusion/exclusion criteria, tags.

Stored as a plain JSON file at `.saaristo/study.json` so users (and the agent,
and Claude Code, and `cat`) can all read it the same way. Filesystem-as-API.

Every saaristo project = one study. The study is what tells a returning user
(or a collaborator) *why* this corpus exists. Optional — empty/missing file
is the same as "no study metadata yet".
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from saari import paths


def study_path(project_root: Path | None = None) -> Path:
    return paths.saaristo_dir(project_root) / "study.json"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def get(project_root: Path | None = None) -> dict[str, Any]:
    """Return the study record. Missing fields default to empty.

    Always returns the full shape so callers can render without conditionals.
    """
    p = study_path(project_root)
    base: dict[str, Any] = {
        "question": "",
        "criteria": "",
        "tags": [],
        "started_at": None,
        "updated_at": None,
    }
    if p.exists():
        try:
            stored = json.loads(p.read_text())
            if isinstance(stored, dict):
                base.update(stored)
        except (OSError, json.JSONDecodeError):
            pass
    return base


def update(
    *,
    question: str | None = None,
    criteria: str | None = None,
    tags: list[str] | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Patch the study record. Only fields you pass get changed.

    Pass `""` to clear a text field; pass `[]` to clear tags. Pass `None` to leave
    a field alone. Sets `started_at` on first write, `updated_at` on every write.
    """
    p = study_path(project_root)
    cur = get(project_root)
    if question is not None:
        cur["question"] = question
    if criteria is not None:
        cur["criteria"] = criteria
    if tags is not None:
        cur["tags"] = tags
    if cur["started_at"] is None:
        cur["started_at"] = _now()
    cur["updated_at"] = _now()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cur, indent=2) + "\n")
    return cur


def funnel(project_root: Path | None = None) -> dict[str, Any]:
    """PRISMA-style aggregate: searches → fetched → unique → screened.

    Numbers come from the project DB. `n_fetched` sums the `n_returned` column
    across all searches (with duplication — one paper found by two searches
    counts twice). `n_unique` is the dedup'd corpus size.
    """
    from saari import db

    root = project_root or paths.project_root()
    with db.connect(paths.db_path(root)) as con:
        sr = con.execute(
            "SELECT id, source, query, n_returned, created_at FROM search ORDER BY created_at ASC"
        ).fetchall()
        searches = [dict(r) for r in sr]
        n_fetched = sum(int(r["n_returned"] or 0) for r in sr)
        n_unique = con.execute("SELECT COUNT(*) FROM paper").fetchone()[0]
        by_status_rows = con.execute(
            "SELECT status, COUNT(*) AS n FROM paper GROUP BY status"
        ).fetchall()
        by_status = {r["status"]: int(r["n"]) for r in by_status_rows}
        n_embedded = con.execute("SELECT COUNT(*) FROM paper_embedding_meta").fetchone()[0]
        n_projected = con.execute("SELECT COUNT(*) FROM paper_projection").fetchone()[0]

    total_screened = (
        by_status.get("included", 0)
        + by_status.get("excluded", 0)
        + by_status.get("maybe", 0)
    )
    return {
        "n_searches": len(searches),
        "searches": searches,
        "n_fetched": n_fetched,
        "n_unique": n_unique,
        "n_screened": total_screened,
        "n_unscreened": by_status.get("candidate", 0),
        "by_status": by_status,
        "embedded": n_embedded,
        "projected": n_projected,
    }
