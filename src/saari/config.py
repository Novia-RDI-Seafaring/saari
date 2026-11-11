"""Project-scoped config at `.saaristo/config.toml`.

Tiny schema; written manually to avoid a tomli-w dep. Reads via stdlib `tomllib`.
"""

from __future__ import annotations

import json
import logging
import tomllib
from pathlib import Path
from typing import Any

from saari import paths

log = logging.getLogger(__name__)


def config_path(project_root: Path | None = None) -> Path:
    return paths.saaristo_dir(project_root) / "config.toml"


def read(project_root: Path | None = None) -> dict[str, Any]:
    p = config_path(project_root)
    if not p.exists():
        return {}
    try:
        with p.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError:
        log.error("invalid TOML in %s", p)
        raise


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
    _emit_table(cfg, [], lines)
    p.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _emit_table(table: dict[str, Any], prefix: list[str], lines: list[str]) -> None:
    """Scalars of `table` first, then each sub-table under its dotted header.

    Recursive, so `set_value("a.b.c", 1)` writes a real `[a.b]` table rather
    than the Python repr of a dict.
    """
    scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
    subtables = {k: v for k, v in table.items() if isinstance(v, dict)}
    if prefix and (scalars or not subtables):
        lines.append("[" + ".".join(_format_key(k) for k in prefix) + "]")
    for k, v in scalars.items():
        lines.append(f"{_format_key(k)} = {_format_value(v)}")
    if scalars or (prefix and not subtables):
        lines.append("")
    for k, v in subtables.items():
        _emit_table(v, [*prefix, k], lines)


def _format_key(k: str) -> str:
    if k and all(c.isascii() and (c.isalnum() or c in "-_") for c in k):
        return k
    return _format_value(str(k))


def _format_value(v: Any) -> str:
    if v is None:
        raise TypeError("TOML has no null; remove the key instead of setting None")
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_format_value(x) for x in v) + "]"
    # JSON string escapes (\", \\, \n, \t, \uXXXX) are valid TOML basic-string
    # escapes; a raw newline or control character is not.
    return json.dumps(str(v), ensure_ascii=False)
