"""Snowballing: chase citations forward and/or backward from a seed paper.

- backward: papers the seed cites (from `referenced_works`)
- forward:  papers that cite the seed (via OpenAlex `cites:` filter)
- both:     both, up to `max_per_direction` each
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from saari import db, paths
from saari.sources import openalex as oa


@dataclass
class SnowballResult:
    seed_paper_id: str
    direction: str
    n_fetched: int = 0
    n_new: int = 0
    backward_paper_ids: list[str] = field(default_factory=list)
    forward_paper_ids: list[str] = field(default_factory=list)
    skipped_reason: str | None = None  # e.g. "no referenced_works" on preprints

    @property
    def paper_ids(self) -> list[str]:
        return self.backward_paper_ids + self.forward_paper_ids


def snowball(
    paper_id: str,
    direction: str = "both",
    max_per_direction: int = 25,
    project_root: Path | None = None,
) -> SnowballResult:
    if direction not in ("backward", "forward", "both"):
        raise ValueError(f"direction must be backward|forward|both, got {direction!r}")

    root = project_root or paths.project_root()
    db_file = paths.db_path(root)

    with db.connect(db_file) as con:
        seed = db.get_paper(con, paper_id)

    if seed is None:
        res = oa.fetch_by_id(paper_id, project_root=root)
        if res is None:
            raise ValueError(f"Paper not found in OpenAlex: {paper_id}")
        seed, raw_path = res
        with db.connect(db_file) as con:
            db.upsert_paper(con, seed, raw_path=raw_path)

    result = SnowballResult(seed_paper_id=seed.id, direction=direction)

    backward_fetched: list[tuple] = []
    if direction in ("backward", "both"):
        refs = seed.referenced_works[:max_per_direction]
        if refs:
            backward_fetched = oa.fetch_works_by_ids(refs, project_root=root)
        elif direction == "backward":
            result.skipped_reason = "seed has no referenced_works (often true for preprints)"

    forward_fetched: list[tuple] = []
    if direction in ("forward", "both"):
        forward_fetched = oa.fetch_citing_works(
            seed.id, limit=max_per_direction, project_root=root
        )

    all_fetched = backward_fetched + forward_fetched
    if not all_fetched and result.skipped_reason is None:
        return result

    with db.connect(db_file) as con:
        fetched_ids = [p.id for p, _ in all_fetched]
        placeholders = ",".join(["?"] * len(fetched_ids)) if fetched_ids else "NULL"
        existing_rows = con.execute(
            f"SELECT id FROM paper WHERE id IN ({placeholders})", fetched_ids
        ).fetchall() if fetched_ids else []
        existing_ids = {row["id"] for row in existing_rows}

        for paper, raw_path in all_fetched:
            db.upsert_paper(con, paper, raw_path=raw_path)

        db.record_search(
            con,
            source="snowball",
            query=f"{direction}:{seed.id}",
            params={"direction": direction, "max_per_direction": max_per_direction},
            paper_ids=fetched_ids,
        )

    result.backward_paper_ids = [p.id for p, _ in backward_fetched]
    result.forward_paper_ids = [p.id for p, _ in forward_fetched]
    result.n_fetched = len(all_fetched)
    result.n_new = sum(1 for pid in fetched_ids if pid not in existing_ids)
    return result
