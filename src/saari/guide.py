"""Workflow guide: the pipeline map plus a next step derived from project state.

One call answers "what can I do here, in what order, and what does each
step need?". The static map (`PIPELINE`, `REQUIRES`) is plain data so new
tools are added by appending an entry. The state part reuses
`status.stages_data` (the same truth as `review_status`, `saari status`,
and the web UI stepper) plus the embedding counts from `study.funnel`.

Tool names are the MCP names; the CLI equivalents are listed per stage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from saari import paths

# Stage keys match status.build_stages so state can be joined in. "map" is a
# cross-cutting step (no SLR stage of its own) and carries no state.
PIPELINE: list[dict[str, Any]] = [
    {
        "key": "protocol",
        "do": "Record title, research question, inclusion/exclusion criteria.",
        "tools": ["study_set", "study_get"],
        "cli": "saari study set --title ... --question ... --criteria ...",
    },
    {
        "key": "search",
        "do": "Run 3-6 differently-phrased keyword searches. Keyword hits are noisy.",
        "tools": ["search", "searches_list"],
        "cli": "saari search QUERY",
    },
    {
        "key": "map",
        "do": (
            "Embed + project new papers, then semantic re-rank with `query` "
            "(2-3 sentence prose beats keywords). Repeat after every search/snowball."
        ),
        "tools": ["refresh", "query", "papers_similar", "canvas"],
        "cli": "saari refresh; saari query TEXT",
    },
    {
        "key": "screen",
        "do": (
            "Work the candidate queue most-relevant first; read the abstract, "
            "decide included/excluded/maybe with a note against the criteria."
        ),
        "tools": ["triage", "paper_show", "screen", "papers_list"],
        "cli": "saari triage; saari screen ID include --note ...",
    },
    {
        "key": "snowball",
        "do": (
            "Pull references/citers of included papers; screen arrivals; repeat "
            "until a round adds nothing new (saturation)."
        ),
        "tools": ["snowball_included", "snowball"],
        "cli": "saari snowball-included; saari snowball ID",
    },
    {
        "key": "report",
        "do": "Export the PRISMA-traced bundle, then author the WRITE slots.",
        "tools": ["export_slr", "export_bibtex", "export_prisma", "funnel"],
        "cli": "saari export slr",
    },
]

# Tool -> what must be true before it is useful.
REQUIRES: dict[str, str] = {
    "search": "protocol set (study_set) so screening has criteria",
    "query": "papers embedded (refresh or embed)",
    "papers_similar": "seed paper embedded (refresh or embed)",
    "triage": "papers in corpus; relevance sort needs protocol or `query` + embeddings",
    "canvas": "embed + project (refresh does both)",
    "project": "embed",
    "snowball_included": "at least one included paper",
    "export_slr": "protocol set and papers screened",
}


def _next_step(stages: dict[str, Any], n_papers: int, n_embedded: int) -> dict[str, Any]:
    by_key = {s["key"]: s for s in stages["stages"]}
    nxt = stages["next"]
    unembedded = n_papers - n_embedded

    def step(stage: str | None, tool: str, why: str) -> dict[str, Any]:
        return {"stage": stage, "tool": tool, "why": why}

    if nxt == "protocol":
        return step("protocol", "study_set", by_key["protocol"]["detail"])
    if n_papers and unembedded > 0:
        return step(
            "map",
            "refresh",
            f"{unembedded} paper(s) not embedded; relevance ranking and query skip them",
        )
    if nxt == "search":
        return step("search", "search", by_key["search"]["detail"])
    if nxt == "screen" or by_key["screen"]["state"] != "done":
        return step("screen", "triage", by_key["screen"]["detail"])
    if nxt == "snowball":
        return step("snowball", "snowball_included", by_key["snowball"]["detail"])
    if nxt == "report":
        return step("report", "export_slr", by_key["report"]["detail"])
    return step(None, "funnel", "all stages done; summarize with funnel")


def build_guide(
    stages: dict[str, Any] | None,
    *,
    n_papers: int = 0,
    n_embedded: int = 0,
) -> dict[str, Any]:
    """Pure: combine the static map with injected state. `stages=None` = no project."""
    state_by_key = {s["key"]: s for s in (stages or {}).get("stages", [])}
    pipeline = []
    for entry in PIPELINE:
        row = dict(entry)
        st = state_by_key.get(entry["key"])
        if st is not None:
            row["state"] = st["state"]
            row["detail"] = st["detail"]
        pipeline.append(row)

    out: dict[str, Any] = {"pipeline": pipeline, "requires": REQUIRES}
    if stages is None:
        out["next"] = {
            "stage": None,
            "tool": "saari init",
            "why": "no saaristo project here; run `saari init` in the paper folder",
        }
    else:
        out["corpus"] = {"n_papers": n_papers, "n_embedded": n_embedded}
        out["next"] = _next_step(stages, n_papers, n_embedded)
    return out


def guide_data(project_root: Path | None = None) -> dict[str, Any]:
    """DB wrapper around `build_guide` for the active project (or none)."""
    from saari import study as study_mod
    from saari.status import stages_data

    try:
        root = project_root or paths.project_root()
    except paths.ProjectNotFoundError:
        return build_guide(None)
    funnel = study_mod.funnel(project_root=root)
    return build_guide(
        stages_data(project_root=root),
        n_papers=int(funnel.get("n_unique", 0)),
        n_embedded=int(funnel.get("embedded", 0)),
    )


# Sent to MCP hosts at connect time. Keep it short: it is paid every session.
MCP_INSTRUCTIONS = (
    "saari: local systematic-literature-review toolkit. Call `guide` first: it "
    "returns the pipeline, prerequisites, and the next step for this project. "
    "Order: study_set (protocol) -> search (several phrasings) -> refresh -> "
    "query/triage (semantic, protocol-relevance first) -> screen with a note -> "
    "snowball_included -> refresh + screen again until saturation -> export_slr. "
    "Never fabricate papers or findings; the user is the reviewer of record."
)
