"""Release guards: things that only break once a tag is pushed.

(Version consistency is covered in test_smoke.py.)
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


def test_mcp_dependency_is_capped_below_2():
    """mcp 2.x removed `mcp.server.fastmcp`; an uncapped pin breaks `uvx saari-mcp`.

    uv.lock hides this (it pins 1.x), so only a fresh install shows it.
    """
    deps = [d for d in pyproject()["project"]["dependencies"] if re.match(r"mcp\b", d)]
    assert len(deps) == 1 and "<2" in deps[0]


def test_sdist_excludes_node_modules():
    """hatchling ignores ui/.gitignore; without this the sdist packs node_modules."""
    excludes = pyproject()["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]
    assert "ui/node_modules" in excludes
