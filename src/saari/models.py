from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

ScreenStatus = Literal["candidate", "included", "excluded", "maybe"]


class Author(BaseModel):
    name: str
    orcid: str | None = None
    affiliation: str | None = None


class Location(BaseModel):
    """One hosting location for a paper: publisher page, arXiv, PMC, repo, etc."""

    source_name: str | None = None
    landing_page_url: str | None = None
    pdf_url: str | None = None
    version: str | None = None  # submittedVersion | acceptedVersion | publishedVersion
    license: str | None = None
    is_oa: bool = False


class Paper(BaseModel):
    """A paper record. Source-agnostic; `source_provenance` records who provided what."""

    id: str
    doi: str | None = None
    arxiv_id: str | None = None
    pmid: str | None = None
    pmcid: str | None = None

    title: str
    abstract: str | None = None
    abstract_suspect: bool = False

    year: int | None = None
    venue: str | None = None
    authors: list[Author] = Field(default_factory=list)
    cited_by_count: int | None = None

    oa_status: str | None = None  # gold | hybrid | bronze | green | closed | diamond
    landing_page_url: str | None = None
    pdf_url: str | None = None
    locations: list[Location] = Field(default_factory=list)

    referenced_works: list[str] = Field(default_factory=list)  # bare OpenAlex IDs
    related_works: list[str] = Field(default_factory=list)

    status: ScreenStatus = "candidate"
    screening_note: str | None = None
    seen_in: int = 0  # count of distinct searches that returned this paper; computed, not stored

    source_provenance: dict[str, Any] = Field(default_factory=dict)
    first_seen_at: datetime | None = None
    last_updated_at: datetime | None = None
