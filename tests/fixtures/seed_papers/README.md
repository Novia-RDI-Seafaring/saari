# seed_papers

Eleven paper records (title, abstract, first author, year, venue) from the
predecessor tool's maritime / digital-twin corpus. File name is the DOI with
`/` replaced by `_`. One record (`10.1016_j.heliyon.2023.e14534`) has an empty
abstract on purpose: keep it, it covers the title-only path.

Loaded by `tests/test_pipeline.py`, which maps them onto `saari.models.Paper`.
