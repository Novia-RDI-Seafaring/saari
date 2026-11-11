"""Snowballing: chase citations forward and/or backward from a seed paper.

- backward: papers the seed cites (from `referenced_works`)
- forward:  papers that cite the seed (via OpenAlex `cites:` filter)
- both:     both

Completeness is reported per direction (`n_available`, `n_retrieved`,
`truncated`) and stored on the snowball event, so reports can say when a
direction was sampled rather than exhausted.

Defaults: backward takes ALL references (a reference list is bounded,
usually well under a few hundred, and fetched 50 ids per request).
Forward takes the 25 most relevant citing works by default, since a
classic paper can have thousands; pass `max_forward=None` to fetch all
citers up to the safety cap (SAARI_SEARCH_MAX_RECORDS, default 2000).
Above the cap the first `cap` are fetched and the direction is flagged
truncated, never silently cut.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from saari import db, paths
from saari.sources import openalex as oa
from saari.sources.common import default_max_records

DEFAULT_MAX_FORWARD = 25


@dataclass
class DirectionStats:
    n_available: int | None = None  # references on the seed / total citers
    n_requested: int = 0  # how many we asked for after applying limits
    n_retrieved: int = 0  # how many OpenAlex returned
    truncated: bool | None = None  # retrieved < available (None = unknown)
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_available": self.n_available,
            "n_requested": self.n_requested,
            "n_retrieved": self.n_retrieved,
            "truncated": self.truncated,
            "note": self.note,
        }


@dataclass
class SnowballResult:
    seed_paper_id: str
    direction: str
    n_fetched: int = 0
    n_new: int = 0
    backward_paper_ids: list[str] = field(default_factory=list)
    forward_paper_ids: list[str] = field(default_factory=list)
    skipped_reason: str | None = None  # e.g. "no referenced_works" on preprints
    backward: DirectionStats | None = None
    forward: DirectionStats | None = None

    @property
    def paper_ids(self) -> list[str]:
        return self.backward_paper_ids + self.forward_paper_ids

    @property
    def n_available(self) -> int | None:
        """Sum over the directions run; None if any direction's total is unknown."""
        stats = [s for s in (self.backward, self.forward) if s is not None]
        if not stats or any(s.n_available is None for s in stats):
            return None
        return sum(int(s.n_available or 0) for s in stats)

    @property
    def truncated(self) -> bool | None:
        stats = [s for s in (self.backward, self.forward) if s is not None]
        if any(s.truncated for s in stats):
            return True
        if not stats or any(s.truncated is None for s in stats):
            return None
        return False

    def completeness(self) -> dict[str, Any]:
        """Surface-ready completeness fields (shared by CLI / MCP / HTTP)."""
        return {
            "n_available": self.n_available,
            "truncated": self.truncated,
            "backward": self.backward.as_dict() if self.backward else None,
            "forward": self.forward.as_dict() if self.forward else None,
        }


def _resolve_limits(
    max_per_direction: int | None,
    max_backward: int | None,
    max_forward: int | None,
) -> tuple[int | None, int | None]:
    """Legacy `max_per_direction`, when given, sets both directions."""
    if max_per_direction is not None:
        return max_per_direction, max_per_direction
    return max_backward, max_forward


def snowball(
    paper_id: str,
    direction: str = "both",
    max_per_direction: int | None = None,
    project_root: Path | None = None,
    *,
    max_backward: int | None = None,
    max_forward: int | None = DEFAULT_MAX_FORWARD,
    max_records: int | None = None,
) -> SnowballResult:
    """Snowball from `paper_id`.

    - `max_backward`: references to fetch; None (default) = all.
    - `max_forward`: citing works to fetch; None = all up to the safety cap.
    - `max_per_direction`: legacy; when set, overrides both limits.
    - `max_records`: safety cap for "all" (default SAARI_SEARCH_MAX_RECORDS or 2000).
    """
    if direction not in ("backward", "forward", "both"):
        raise ValueError(f"direction must be backward|forward|both, got {direction!r}")
    lim_back, lim_fwd = _resolve_limits(max_per_direction, max_backward, max_forward)
    cap = max_records or default_max_records()

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
        all_refs = seed.referenced_works
        st = DirectionStats(n_available=len(all_refs))
        n_take = len(all_refs) if lim_back is None else min(lim_back, len(all_refs))
        if n_take > cap:
            st.note = (
                f"seed has {len(all_refs)} references; fetched the first {cap} "
                "(safety cap; raise max_records to fetch more)"
            )
            n_take = cap
        refs = all_refs[:n_take]
        st.n_requested = len(refs)
        if refs:
            backward_fetched = oa.fetch_works_by_ids(refs, project_root=root)
            missing = len(refs) - len(backward_fetched)
            if missing > 0 and st.note is None:
                st.note = f"{missing} referenced work(s) not returned by OpenAlex (deleted or merged)"
        elif direction == "backward":
            result.skipped_reason = "seed has no referenced_works (often true for preprints)"
        st.n_retrieved = len(backward_fetched)
        # Truncation is about what we chose not to request, not about ids
        # OpenAlex no longer serves (those are reported in `note`).
        st.truncated = st.n_requested < len(all_refs)
        result.backward = st

    forward_fetched: list[tuple] = []
    if direction in ("forward", "both"):
        hits = oa.fetch_citing_works(
            seed.id, limit=lim_fwd, project_root=root, max_records=cap
        )
        forward_fetched = list(hits)
        st = DirectionStats(
            n_available=hits.n_available,
            n_requested=len(hits) if lim_fwd is None else lim_fwd,
            n_retrieved=len(hits),
            truncated=hits.truncated,
            note="; ".join(hits.notes) or None,
        )
        result.forward = st

    all_fetched = backward_fetched + forward_fetched

    with db.connect(db_file) as con:
        fetched_ids = [p.id for p, _ in all_fetched]
        existing_ids: set[str] = set()
        if fetched_ids:
            placeholders = ",".join(["?"] * len(fetched_ids))
            existing_rows = con.execute(
                f"SELECT id FROM paper WHERE id IN ({placeholders})", fetched_ids
            ).fetchall()
            existing_ids = {row["id"] for row in existing_rows}

        for paper, raw_path in all_fetched:
            db.upsert_paper(con, paper, raw_path=raw_path)

        # Recorded even when nothing came back: "0 of 0 available" is a
        # fact the report should be able to show.
        db.record_search(
            con,
            source="snowball",
            query=f"{direction}:{seed.id}",
            params={
                "direction": direction,
                "max_backward": lim_back,
                "max_forward": lim_fwd,
                **result.completeness(),
            },
            paper_ids=fetched_ids,
            n_available=result.n_available,
            truncated=result.truncated,
        )

    result.backward_paper_ids = [p.id for p, _ in backward_fetched]
    result.forward_paper_ids = [p.id for p, _ in forward_fetched]
    result.n_fetched = len(all_fetched)
    result.n_new = sum(1 for pid in fetched_ids if pid not in existing_ids)
    return result
