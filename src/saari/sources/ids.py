"""Parse user-supplied paper identifiers (DOI, arXiv, PMID, OpenAlex).

`parse_identifier` turns whatever a user pastes (a bare DOI, a doi.org URL,
`arXiv:2101.00001v2`, a PubMed URL, an OpenAlex URL, ...) into a
`(kind, value)` pair with a canonical value:

- doi:      lowercased, no resolver prefix, e.g. `10.1000/xyz`
- arxiv:    no `arXiv:` prefix, no version suffix, e.g. `2101.00001`
- pmid:     digits only
- openalex: bare work id, e.g. `W2741809807`

Bare digits are rejected on purpose: they could be a PMID, a Scopus id or a
typo. Ask for `pmid:<n>` instead.
"""

from __future__ import annotations

import re
from urllib.parse import unquote

KINDS = ("doi", "arxiv", "pmid", "openalex")

# Registrant codes are normally 4+ digits; accept shorter and subdivided
# prefixes (10.1000.10/...) rather than reject a real DOI.
_DOI_RE = re.compile(r"^10\.\d+(?:\.\d+)*/\S+$")
_DOI_URL_RE = re.compile(r"^(?:https?://)?(?:dx\.)?doi\.org/(10\.\S+)$", re.IGNORECASE)
# Modern (YYMM.NNNNN) and legacy (archive[.SUB]/YYMMNNN) arXiv ids.
_ARXIV_NEW = r"\d{4}\.\d{4,5}"
_ARXIV_OLD = r"[a-z][a-z\-]*(?:\.[A-Z]{2})?/\d{7}"
_ARXIV_ID_RE = re.compile(rf"^(?:{_ARXIV_NEW}|{_ARXIV_OLD})$")
_ARXIV_BODY_RE = re.compile(rf"^({_ARXIV_NEW}|{_ARXIV_OLD})(?:v\d+)?$")
_ARXIV_URL_RE = re.compile(
    rf"^(?:https?://)?(?:www\.|export\.)?arxiv\.org/(?:abs|pdf)/"
    rf"({_ARXIV_NEW}|{_ARXIV_OLD})(?:v\d+)?(?:\.pdf)?/?$",
    re.IGNORECASE,
)
_ARXIV_DOI_RE = re.compile(r"^10\.48550/arxiv\.(.+)$", re.IGNORECASE)
_PMID_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:pubmed\.ncbi\.nlm\.nih\.gov|ncbi\.nlm\.nih\.gov/pubmed)/(\d+)/?$",
    re.IGNORECASE,
)
_OPENALEX_RE = re.compile(r"^W\d+$", re.IGNORECASE)
_OPENALEX_URL_RE = re.compile(
    r"^(?:https?://)?(?:api\.)?openalex\.org/(?:works/)?(W\d+)/?$", re.IGNORECASE
)


class IdentifierError(ValueError):
    """The string is not a recognizable paper identifier."""


def normalize_doi(doi: str) -> str:
    return unquote(doi.strip()).rstrip(".").lower()


def is_valid_arxiv_id(value: str | None) -> bool:
    """True for a well-formed arXiv id (used to guard id-based dedup)."""
    return bool(value) and bool(_ARXIV_ID_RE.match(value or ""))


def parse_identifier(raw: str) -> tuple[str, str]:
    """Return `(kind, canonical_value)` or raise `IdentifierError`."""
    s = (raw or "").strip()
    if not s:
        raise IdentifierError("empty identifier")
    low = s.lower()

    # Explicit prefixes first.
    for prefix, kind in (("doi:", "doi"), ("arxiv:", "arxiv"), ("pmid:", "pmid"), ("openalex:", "openalex")):
        if low.startswith(prefix):
            body = s[len(prefix):].strip()
            return _parse_kind(kind, body, raw)

    if m := _DOI_URL_RE.match(s):
        return _parse_kind("doi", m.group(1), raw)
    if m := _ARXIV_URL_RE.match(s):
        return "arxiv", m.group(1)
    if m := _PMID_URL_RE.match(s):
        return "pmid", m.group(1)
    if m := _OPENALEX_URL_RE.match(s):
        return "openalex", m.group(1).upper()
    if _OPENALEX_RE.match(s):
        return "openalex", s.upper()
    if _DOI_RE.match(s):
        return _parse_kind("doi", s, raw)
    if m := _ARXIV_BODY_RE.match(s):
        return "arxiv", m.group(1)
    if s.isdigit():
        raise IdentifierError(
            f"{raw!r} is a bare number; write pmid:{s} if it is a PubMed id"
        )
    raise IdentifierError(
        f"{raw!r} is not a recognized DOI, doi.org URL, arXiv id/URL, "
        "pmid:<n> / PubMed URL, or OpenAlex id/URL"
    )


def _parse_kind(kind: str, body: str, raw: str) -> tuple[str, str]:
    if kind == "doi":
        if m := _DOI_URL_RE.match(body):
            body = m.group(1)
        doi = normalize_doi(body)
        if not _DOI_RE.match(doi):
            raise IdentifierError(f"{raw!r} is not a valid DOI (expected 10.<prefix>/<suffix>)")
        # arXiv DOIs are DataCite DOIs; keep them as DOIs (OpenAlex resolves them).
        return "doi", doi
    if kind == "arxiv":
        m = _ARXIV_BODY_RE.match(body) or _ARXIV_URL_RE.match(body)
        if not m:
            raise IdentifierError(f"{raw!r} is not a valid arXiv id")
        return "arxiv", m.group(1)
    if kind == "pmid":
        if not body.isdigit():
            raise IdentifierError(f"{raw!r} is not a valid PMID (digits only)")
        return "pmid", body
    m = _OPENALEX_RE.match(body) or _OPENALEX_URL_RE.match(body)
    if not m:
        raise IdentifierError(f"{raw!r} is not a valid OpenAlex work id (W<digits>)")
    return "openalex", (m.group(1) if m.groups() else body).upper()


def arxiv_doi(arxiv_id: str) -> str:
    """The DataCite DOI arXiv registers for every paper."""
    return f"10.48550/arxiv.{arxiv_id}"


def arxiv_from_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    m = _ARXIV_DOI_RE.match(doi)
    return m.group(1) if m and is_valid_arxiv_id(m.group(1)) else None
