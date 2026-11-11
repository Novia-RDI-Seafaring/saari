"""Central logging setup for the `saari` logger namespace.

Modules log through `logging.getLogger(__name__)`; this module only decides
where those records go. Entry points (the `saari` CLI, `saari-mcp`, and the
HTTP app) call `setup()` once at start.

Output goes to stderr, never stdout. In stdio mode the MCP server speaks
JSON-RPC over stdout, so a single log line there would corrupt the protocol.
`SAARI_LOG_FILE` adds a file handler on top of stderr.

Level precedence: explicit `level` argument (from the CLI `-v` flag), then
`SAARI_LOG_LEVEL`, then WARNING. WARNING keeps normal runs quiet; INFO shows
one line per pipeline stage; DEBUG shows per-batch detail.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

LOGGER_NAME = "saari"
ENV_LEVEL = "SAARI_LOG_LEVEL"
ENV_FILE = "SAARI_LOG_FILE"
DEFAULT_LEVEL = logging.WARNING

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"
# Tag on handlers we own, so repeated setup() calls replace them instead of
# stacking duplicates (and never touch handlers someone else attached).
_OWNED = "_saari_owned"


class _StderrHandler(logging.StreamHandler):
    """Writes to whatever `sys.stderr` is at emit time, not at setup time.

    Same idea as logging's own last-resort handler. Keeps working when a
    test runner or host swaps sys.stderr after setup() ran.
    """

    def __init__(self) -> None:
        super().__init__(sys.stderr)

    @property  # type: ignore[override]
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, _value) -> None:
        pass


def _parse_level(value: str | int | None) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    v = value.strip().upper()
    if v.isdigit():
        return int(v)
    level = logging.getLevelName(v)
    return level if isinstance(level, int) else None


def verbosity_to_level(verbose: int) -> int | None:
    """Map a count of `-v` flags to a level. 0 means "not set"."""
    if verbose <= 0:
        return None
    return logging.INFO if verbose == 1 else logging.DEBUG


def setup(
    level: str | int | None = None, *, propagate: bool = True
) -> logging.Logger:
    """Configure the `saari` logger. Safe to call more than once.

    `propagate=False` stops records also reaching root handlers. The MCP
    server needs that: constructing FastMCP installs a root handler, so
    propagating would print every line twice. Elsewhere propagation stays on
    so host apps and pytest's `caplog` still see saari records.

    Returns the configured logger. An unknown level name in `SAARI_LOG_LEVEL`
    falls back to the default instead of crashing the entry point.
    """
    resolved = _parse_level(level)
    if resolved is None:
        resolved = _parse_level(os.environ.get(ENV_LEVEL))
    if resolved is None:
        resolved = DEFAULT_LEVEL

    logger = logging.getLogger(LOGGER_NAME)
    for h in list(logger.handlers):
        if getattr(h, _OWNED, False):
            logger.removeHandler(h)
            h.close()

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)
    stream = _StderrHandler()
    stream.setFormatter(formatter)
    setattr(stream, _OWNED, True)
    logger.addHandler(stream)

    log_file = os.environ.get(ENV_FILE)
    if log_file:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setFormatter(formatter)
        setattr(fh, _OWNED, True)
        logger.addHandler(fh)

    logger.setLevel(resolved)
    logger.propagate = propagate
    return logger
