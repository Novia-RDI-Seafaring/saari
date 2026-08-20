"""Project-root path resolution.

A saaristo project is any directory containing a `.saaristo/` folder.
`saari init` creates one. Commands walk up from cwd to find the nearest root,
or honor `SAARI_PROJECT_ROOT` when set.
"""

from __future__ import annotations

import json
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
            "- `raw/<source>/<id>.json` — raw API responses, immutable ground truth.\n"
            "- `config.toml` — project config.\n\n"
            "Safe to `.gitignore` if you don't want the corpus committed; "
            "safe to commit if you want collaborators to get it instantly.\n"
        )

    return root


_CLAUDE_SNIPPET = """\
## saari (literature review)

This folder is a saari project: tool state lives in `.saaristo/`, outputs in
`papers/`. The `saari` MCP server (see `.mcp.json`) exposes the operations:
search, snowball, screen, refresh, query, export. CLI equivalent: `saari
--help`. If the `litreview` skill is installed, follow it for the systematic
review workflow (protocol before searching, screen with recorded reasons,
export PRISMA at the end). Never fabricate papers, findings, or citations.
"""


def scaffold_workspace(root: Path) -> list[str]:
    """Wire a project folder into an agent harness. Idempotent, merge-only.

    Creates or extends `.gitignore`, `.mcp.json`, and `CLAUDE.md` at `root`,
    never overwriting existing content. Returns relative paths it touched.
    """
    changed: list[str] = []

    gitignore = root / ".gitignore"
    text = gitignore.read_text() if gitignore.exists() else ""
    if MARKER not in text:
        block = (
            "# saari tool state (local working state; remove this line to commit the corpus)\n"
            f"{MARKER}/\n"
        )
        sep = "" if not text or text.endswith("\n") else "\n"
        gitignore.write_text(text + sep + block)
        changed.append(".gitignore")

    mcp_json = root / ".mcp.json"
    entry = {"command": "uvx", "args": ["saari-mcp"]}
    if mcp_json.exists():
        try:
            data = json.loads(mcp_json.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = None  # unreadable: leave the user's file alone
        if isinstance(data, dict):
            servers = data.setdefault("mcpServers", {})
            if isinstance(servers, dict) and "saari" not in servers:
                servers["saari"] = entry
                mcp_json.write_text(json.dumps(data, indent=2) + "\n")
                changed.append(".mcp.json")
    else:
        mcp_json.write_text(
            json.dumps({"mcpServers": {"saari": entry}}, indent=2) + "\n"
        )
        changed.append(".mcp.json")

    claude_md = root / "CLAUDE.md"
    text = claude_md.read_text() if claude_md.exists() else ""
    if "saari" not in text.lower():
        sep = "" if not text else ("\n" if text.endswith("\n") else "\n\n")
        claude_md.write_text(text + sep + _CLAUDE_SNIPPET)
        changed.append("CLAUDE.md")

    return changed
