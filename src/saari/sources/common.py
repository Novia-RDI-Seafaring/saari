"""Shared types for search sources: result lists that carry completeness
metadata, and the safety cap for exhaustive fetches."""

from __future__ import annotations

import os

from saari.models import Paper

# Upper bound on records one search / one snowball direction may fetch.
# "Fetch all" above this refuses (search) or flags truncation (snowball)
# instead of silently cutting. Override per call (max_records) or with the
# SAARI_SEARCH_MAX_RECORDS env var.
DEFAULT_MAX_RECORDS = 2000


def default_max_records() -> int:
    env = os.environ.get("SAARI_SEARCH_MAX_RECORDS")
    if env:
        try:
            n = int(env)
            if n > 0:
                return n
        except ValueError:
            pass
    return DEFAULT_MAX_RECORDS


class SearchTooLarge(ValueError):
    """A requested fetch exceeds the safety cap. Narrow the query or raise
    the cap; saari refuses rather than keeping a silent sample."""


class SearchHits(list[tuple[Paper, str]]):
    """`[(paper, raw_path), ...]` plus completeness metadata.

    - `n_available`: total records the source reports for the query
      (OpenAlex `meta.count`, Scopus `opensearch:totalResults`), or None
      when the source did not say.
    - `notes`: human-readable caveats (paging limits, caps hit).
    - `n_retrieved`: source records walked, if different from len().
    """

    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        self.n_available: int | None = None
        self.notes: list[str] = []
        # Source records walked, when that differs from len(self) (e.g. a
        # Scopus entry without id/title is skipped, not truncated).
        self.n_retrieved: int | None = None

    @property
    def truncated(self) -> bool | None:
        """True when fewer records were retrieved than the source has.
        None when the source gave no total."""
        if self.n_available is None:
            return None
        got = self.n_retrieved if self.n_retrieved is not None else len(self)
        return got < self.n_available
