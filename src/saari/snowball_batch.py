"""Snowball from many seeds in one call: all included papers, or the top N.

A thin wrapper over the public `saari.snowball.snowball()`: it picks seeds,
calls that function once per seed, and aggregates the counts. It does not
reach into snowball internals, and it passes through any extra scalar
fields the per-seed result carries (so new result fields show up here
without a code change).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from saari import db, paths
from saari import snowball as _snowball_mod
from saari.triage import rank_papers


def _result_row(r: Any) -> dict[str, Any]:
    """Per-seed summary: every scalar field of the result, id lists dropped."""
    if dataclasses.is_dataclass(r):
        d = dataclasses.asdict(r)
    else:
        d = dict(vars(r))
    row = {k: v for k, v in d.items() if not isinstance(v, (list, dict, set, tuple))}
    if "seed_paper_id" in row:
        row["seed"] = row.pop("seed_paper_id")
    return row


def _already_snowballed(con, seed_id: str, direction: str) -> bool:
    """True when a prior snowball event covers this seed in this direction.

    Reads the `<direction>:<seed id>` query that snowball records. If that
    format ever changes this returns False, so the seed is simply re-run.
    """
    rows = con.execute(
        "SELECT query FROM search WHERE source = 'snowball' AND query LIKE ?",
        (f"%:{seed_id}",),
    ).fetchall()
    for r in rows:
        done_dir = (r["query"] or "").rsplit(":" + seed_id, 1)[0]
        if done_dir == direction or done_dir == "both":
            return True
    return False


def snowball_seeds(
    *,
    status: str = "included",
    top_n: int | None = None,
    sort: str = "auto",
    direction: str = "both",
    max_per_direction: int = 25,
    skip_done: bool = True,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Snowball every paper with `status` (default: included), or the top `top_n`.

    Seeds are ordered by `sort` (auto | relevance | seen_in | cited, same as
    triage). `skip_done` skips seeds already snowballed in this direction (or
    `both`), which makes repeat calls cheap saturation rounds. A failing seed
    is recorded under `errors` and does not stop the run.
    """
    if direction not in ("backward", "forward", "both"):
        raise ValueError(f"direction must be backward|forward|both, got {direction!r}")
    root = project_root or paths.project_root()
    ranked = rank_papers(
        status=status,
        sort=sort,
        limit=top_n if top_n is not None else 1_000_000,
        project_root=root,
    )

    seeds: list[str] = []
    skipped: list[str] = []
    with db.connect(paths.db_path(root)) as con:
        for p in ranked.papers:
            if skip_done and _already_snowballed(con, p.id, direction):
                skipped.append(p.id)
            else:
                seeds.append(p.id)

    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    n_fetched = n_new = 0
    for seed in seeds:
        try:
            r = _snowball_mod.snowball(
                seed,
                direction=direction,
                max_per_direction=max_per_direction,
                project_root=root,
            )
        except Exception as e:  # one bad seed must not sink the batch
            errors.append({"seed": seed, "error": f"{type(e).__name__}: {e}"})
            continue
        row = _result_row(r)
        rows.append(row)
        n_fetched += int(row.get("n_fetched") or 0)
        n_new += int(row.get("n_new") or 0)

    out: dict[str, Any] = {
        "status": status,
        "direction": direction,
        "seed_sort": ranked.sort,
        "n_pool": ranked.n_matching,
        "n_seeds": len(seeds),
        "n_skipped_done": len(skipped),
        "n_errors": len(errors),
        "n_fetched": n_fetched,
        "n_new": n_new,
        "seeds": rows,
        "skipped_done": skipped,
        "errors": errors,
        "saturated": bool(seeds) and n_new == 0 and not errors,
    }
    if ranked.note:
        out["note"] = ranked.note
    if not ranked.papers:
        out["note"] = f"No papers with status={status!r} to snowball from."
    return out
