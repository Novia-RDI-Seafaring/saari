"""Tests for paths.scaffold_workspace: idempotent, merge-only harness wiring."""

from __future__ import annotations

import json

from saari import paths


def test_fresh_folder_gets_all_three(tmp_path):
    changed = paths.scaffold_workspace(tmp_path)
    assert sorted(changed) == [".gitignore", ".mcp.json", "CLAUDE.md"]
    assert ".saaristo/" in (tmp_path / ".gitignore").read_text()
    data = json.loads((tmp_path / ".mcp.json").read_text())
    assert data["mcpServers"]["saari"]["args"] == ["saari-mcp"]
    assert "saari" in (tmp_path / "CLAUDE.md").read_text()


def test_second_run_is_noop(tmp_path):
    paths.scaffold_workspace(tmp_path)
    before = {
        p.name: p.read_text() for p in tmp_path.iterdir() if p.is_file()
    }
    assert paths.scaffold_workspace(tmp_path) == []
    after = {p.name: p.read_text() for p in tmp_path.iterdir() if p.is_file()}
    assert before == after


def test_mcp_json_merge_preserves_other_servers(tmp_path):
    existing = {"mcpServers": {"other": {"command": "foo", "args": []}}}
    (tmp_path / ".mcp.json").write_text(json.dumps(existing))
    changed = paths.scaffold_workspace(tmp_path)
    assert ".mcp.json" in changed
    data = json.loads((tmp_path / ".mcp.json").read_text())
    assert data["mcpServers"]["other"] == {"command": "foo", "args": []}
    assert "saari" in data["mcpServers"]


def test_existing_saari_mcp_entry_untouched(tmp_path):
    existing = {"mcpServers": {"saari": {"command": "custom", "args": ["x"]}}}
    (tmp_path / ".mcp.json").write_text(json.dumps(existing))
    changed = paths.scaffold_workspace(tmp_path)
    assert ".mcp.json" not in changed
    data = json.loads((tmp_path / ".mcp.json").read_text())
    assert data["mcpServers"]["saari"]["command"] == "custom"


def test_invalid_mcp_json_left_alone(tmp_path):
    (tmp_path / ".mcp.json").write_text("{not json")
    changed = paths.scaffold_workspace(tmp_path)
    assert ".mcp.json" not in changed
    assert (tmp_path / ".mcp.json").read_text() == "{not json"


def test_gitignore_appended_not_replaced(tmp_path):
    (tmp_path / ".gitignore").write_text("node_modules/\n")
    paths.scaffold_workspace(tmp_path)
    text = (tmp_path / ".gitignore").read_text()
    assert text.startswith("node_modules/\n")
    assert ".saaristo/" in text


def test_claude_md_mentioning_saari_untouched(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# my paper\n\nUses saaristo tooling.\n")
    changed = paths.scaffold_workspace(tmp_path)
    assert "CLAUDE.md" not in changed
    assert (tmp_path / "CLAUDE.md").read_text() == "# my paper\n\nUses saaristo tooling.\n"
