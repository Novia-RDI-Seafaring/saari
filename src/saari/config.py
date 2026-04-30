"""Project-scoped config at `.saaristo/config.toml`.

Tiny schema; written manually to avoid a tomli-w dep. Reads via stdlib `tomllib`.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from saari import paths


def config_path(project_root: Path | None = None) -> Path:
    return paths.saaristo_dir(project_root) / "config.toml"


def read(project_root: Path | None = None) -> dict[str, Any]:
    p = config_path(project_root)
    if not p.exists():
        return {}
    with p.open("rb") as f:
        return tomllib.load(f)


def get(path: str, default: Any = None, project_root: Path | None = None) -> Any:
    """Dot-path lookup: `get('section.key')`."""
    cfg = read(project_root)
    cur: Any = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def set_value(path: str, value: Any, project_root: Path | None = None) -> None:
    """Set a dotted-path key in the config and write it back."""
    cfg = read(project_root)
    parts = path.split(".")
    cur = cfg
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value
    _write(cfg, project_root)


def _write(cfg: dict[str, Any], project_root: Path | None = None) -> None:
    p = config_path(project_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    # Top-level scalars first (none expected today, but support them)
    scalars = {k: v for k, v in cfg.items() if not isinstance(v, dict)}
    for k, v in scalars.items():
        lines.append(f"{k} = {_format_value(v)}")
    if scalars:
        lines.append("")
    # Tables
    for table_name, table in cfg.items():
        if not isinstance(table, dict):
            continue
        lines.append(f"[{table_name}]")
        for k, v in table.items():
            lines.append(f"{k} = {_format_value(v)}")
        lines.append("")
    p.write_text("\n".join(lines).rstrip() + "\n")


def _format_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_format_value(x) for x in v) + "]"
    # default: string with double-quote escaping
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'
