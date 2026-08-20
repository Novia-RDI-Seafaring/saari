"""SLR stage model: derive where the review stands from the project DB.

The systematic-review workflow has five stages -- protocol, search, screen,
snowball, report. Nothing here is tracked by hand: every stage state is
computed from what the database and the export bundle already contain, so
the stepper in the UI, `saari status` in the shell, and the `review_status`
MCP tool all read the same truth.

`build_stages` is pure (inject the facts, get the stages); `stages_data`
does the DB/file I/O.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from saari import paths

# States: "done" (green), "active" (in progress), "todo" (not started).
# `next` in the payload is the first stage that is not done -- the
# recommended place to work.


def build_stages(
    *,
    study: dict[str, Any],
    funnel: dict[str, Any],
    n_snowball_events: int,
    last_snowball_new: int | None,
    bundle_exists: bool,
    write_slots: int | None,
) -> dict[str, Any]:
    """Derive the five SLR stage states from injected facts.

    `last_snowball_new`: papers first seen in the most recent snowball event
    (None when there is no snowball event yet). Zero means the last round
    found nothing new -- the saturation signal.
    `write_slots`: `<!-- WRITE: -->` slots left in paper.md (None = no file).
    """
    searches = funnel.get("searches", [])
    db_searches = [s for s in searches if (s.get("source") or "") != "snowball"]
    n_records = sum(int(s.get("n_returned") or 0) for s in searches)
    by_status = funnel.get("by_status", {})
    n_total = int(funnel.get("n_unique", 0))
    n_candidate = int(by_status.get("candidate", 0))
    n_maybe = int(by_status.get("maybe", 0))
    n_decided = int(by_status.get("included", 0)) + int(by_status.get("excluded", 0)) + n_maybe
    n_included = int(by_status.get("included", 0))

    question = (study.get("question") or "").strip()
    criteria = (study.get("criteria") or "").strip()

    # 1 -- protocol
    if question and criteria:
        protocol_state, protocol_detail = "done", "research question + criteria set"
    elif question or criteria:
        protocol_state = "active"
        protocol_detail = "criteria missing" if question else "research question missing"
    else:
        protocol_state, protocol_detail = "todo", "no research question, no criteria"

    # 2 -- search
    if not db_searches:
        search_state, search_detail = "todo", "no searches yet"
    elif len(db_searches) < 3:
        search_state = "active"
        search_detail = f"{len(db_searches)} search(es) -- try several phrasings"
    else:
        search_state = "done"
        search_detail = f"{len(db_searches)} searches - {n_records} records fetched"

    # 3 -- screen
    if n_total == 0 or n_decided == 0:
        screen_state, screen_detail = "todo", "nothing screened yet"
    elif n_candidate == 0 and n_maybe == 0:
        screen_state = "done"
        screen_detail = f"all {n_total} decided - {n_included} included"
    else:
        screen_state = "active"
        parts = [f"{n_decided}/{n_total} decided"]
        if n_candidate:
            parts.append(f"{n_candidate} candidates")
        if n_maybe:
            parts.append(f"{n_maybe} maybe")
        screen_detail = " - ".join(parts)

    # 4 -- snowball (saturation)
    if n_snowball_events == 0:
        snowball_state, snowball_detail = "todo", "no snowball rounds yet"
    elif last_snowball_new == 0:
        snowball_state = "done"
        snowball_detail = f"{n_snowball_events} round(s) - last round found nothing new"
    else:
        snowball_state = "active"
        snowball_detail = (
            f"{n_snowball_events} round(s) - last added {last_snowball_new} new record(s)"
        )

    # 5 -- report
    if not bundle_exists:
        report_state, report_detail = "todo", "no bundle generated yet"
    elif write_slots:
        report_state = "active"
        report_detail = f"bundle generated - {write_slots} to-write slot(s) remain"
    else:
        report_state, report_detail = "done", "bundle generated - all slots authored"

    stages = [
        {
            "key": "protocol",
            "label": "Protocol",
            "state": protocol_state,
            "detail": protocol_detail,
            "hint": (
                "Set the research question and inclusion/exclusion criteria before "
                "searching. Screening decisions without recorded criteria are not "
                "systematic."
            ),
            "view": "/home",
        },
        {
            "key": "search",
            "label": "Search",
            "state": search_state,
            "detail": search_detail,
            "hint": (
                "Run several differently-phrased OpenAlex searches. One query "
                "phrasing always misses relevant work."
            ),
            "view": "/searches",
        },
        {
            "key": "screen",
            "label": "Screen",
            "state": screen_state,
            "detail": screen_detail,
            "hint": (
                "Work through the candidate queue: click a paper, read the "
                "abstract, and decide include/exclude/maybe with a reason "
                "against the criteria. Done = zero candidates and zero maybes. "
                "Reasons become the PRISMA exclusion box."
            ),
            "view": "/papers?status=candidate",
        },
        {
            "key": "snowball",
            "label": "Snowball",
            "state": snowball_state,
            "detail": snowball_detail,
            "hint": (
                "Open an included paper and press snowball (in its detail "
                "panel) to pull in its references and citers. Screen what "
                "arrives; repeat until a round finds nothing new (saturation)."
            ),
            "view": "/papers?status=included",
        },
        {
            "key": "report",
            "label": "Report",
            "state": report_state,
            "detail": report_detail,
            "hint": (
                "Export the PRISMA diagram and manuscript scaffold; the numbers "
                "trace to this database. Then author the WRITE slots -- saari "
                "never invents findings."
            ),
            "view": "/review",
        },
    ]

    next_key = next((s["key"] for s in stages if s["state"] != "done"), None)
    return {"stages": stages, "next": next_key}


_WRITE_SLOT_RE = re.compile(r"<!--\s*WRITE:", re.IGNORECASE)


def stages_data(project_root: Path | None = None) -> dict[str, Any]:
    """DB/file wrapper around `build_stages` for the active project."""
    from saari import db, study as study_mod

    root = project_root or paths.project_root()
    study = study_mod.get(project_root=root)
    funnel = study_mod.funnel(project_root=root)

    with db.connect(paths.db_path(root)) as con:
        row = con.execute(
            "SELECT id FROM search WHERE source = 'snowball' ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
        n_snowball_events = con.execute(
            "SELECT COUNT(*) FROM search WHERE source = 'snowball'"
        ).fetchone()[0]
        last_snowball_new: int | None = None
        if row is not None:
            # Papers whose first appearance (lowest search id) is this event.
            last_snowball_new = con.execute(
                """
                SELECT COUNT(*) FROM search_result sr
                WHERE sr.search_id = ?
                  AND NOT EXISTS (
                    SELECT 1 FROM search_result older
                    WHERE older.paper_id = sr.paper_id AND older.search_id < sr.search_id
                  )
                """,
                (row["id"],),
            ).fetchone()[0]

    paper_md = paths.papers_dir(root) / "review" / "paper.md"
    bundle_exists = paper_md.exists()
    write_slots: int | None = None
    if bundle_exists:
        write_slots = len(_WRITE_SLOT_RE.findall(paper_md.read_text()))

    return build_stages(
        study=study,
        funnel=funnel,
        n_snowball_events=int(n_snowball_events),
        last_snowball_new=last_snowball_new,
        bundle_exists=bundle_exists,
        write_slots=write_slots,
    )
