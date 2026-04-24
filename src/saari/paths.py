"""Project-root path resolution.

A saaristo project is any directory containing a `.saaristo/` folder.
`saari init` creates one. Commands walk up from cwd to find the nearest root,
or honor `SAARI_PROJECT_ROOT` when set.
"""

from __future__ import annotations

import os
from pathlib import Path

MARKER = ".saaristo"


class ProjectNotFoundError(RuntimeError):
    """Raised when a command needs a project root but none is found."""


def find_project_root(start: Path | None = None) -> Path | None:
    """Walk upward from `start` (or cwd) looking for a directory containing `.saaristo/`."""
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / MARKER).is_dir():
            return candidate
    return None


def project_root(start: Path | None = None) -> Path:
    """Resolve the project root or raise ProjectNotFoundError.

    Precedence:
      1. SAARI_PROJECT_ROOT env var (must point to a directory containing .saaristo/)
      2. Walk upward from `start` or cwd
    """
    env = os.environ.get("SAARI_PROJECT_ROOT")
    if env:
        root = Path(env).expanduser().resolve()
        if not (root / MARKER).is_dir():
            raise ProjectNotFoundError(
                f"SAARI_PROJECT_ROOT={root} does not contain {MARKER}/. "
                f"Run `saari init` there first."
            )
        return root

    found = find_project_root(start)
    if found is None:
        raise ProjectNotFoundError(
            "Not inside a saaristo project. "
            "Run `saari init` in your paper's working directory."
        )
    return found


def saaristo_dir(root: Path | None = None) -> Path:
    return (root or project_root()) / MARKER


def db_path(root: Path | None = None) -> Path:
    return saaristo_dir(root) / "saari.db"


def raw_dir(root: Path | None = None, source: str | None = None) -> Path:
    base = saaristo_dir(root) / "raw"
    return base / source if source else base


def papers_dir(root: Path | None = None) -> Path:
    return (root or project_root()) / "papers"


def init_project(path: Path) -> Path:
    """Create .saaristo/ and papers/ at `path`. Returns the project root."""
    root = path.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / MARKER).mkdir(exist_ok=True)
    (root / MARKER / "raw").mkdir(exist_ok=True)
    (root / "papers").mkdir(exist_ok=True)

    readme = root / MARKER / "README.md"
    if not readme.exists():
        readme.write_text(
            "# .saaristo\n\n"
            "Tool-owned state for the `saari` CLI / MCP server.\n\n"
            "- `saari.db` — SQLite index (papers, searches, embeddings).\n"
            "- `raw/<source>/<id>.json` — raw API responses, immutable ground truth.\n\n"
            "Safe to `.gitignore` if you don't want the corpus committed; "
            "safe to commit if you want collaborators to get it instantly.\n"
        )
    return root
