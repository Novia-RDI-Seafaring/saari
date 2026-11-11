"""Project config at .saaristo/config.toml: hand-written TOML, read by tomllib."""

from __future__ import annotations

import tomllib

import pytest

from saari import config, paths


@pytest.fixture()
def root(tmp_path):
    return paths.init_project(tmp_path)


def test_missing_config_reads_empty(root):
    assert not config.config_path(root).exists()
    assert config.read(root) == {}
    assert config.get("a.b", default="dflt", project_root=root) == "dflt"


def test_roundtrip_scalar_types(root):
    values = {
        "s.flag": True,
        "s.off": False,
        "s.n": 25,
        "s.ratio": 0.1,
        "s.tags": ["a", "b c"],
        "s.nums": [1, 2],
        "s.name": "digital twins",
    }
    for k, v in values.items():
        config.set_value(k, v, project_root=root)
    for k, v in values.items():
        assert config.get(k, project_root=root) == v
    # the file is valid TOML on its own, not only via our reader
    tomllib.loads(config.config_path(root).read_text())


@pytest.mark.parametrize(
    "value",
    ['say "hi"', "back\\slash", "two\nlines", "tab\there", "unicode: saaristo \u00e4\u00f6", ""],
)
def test_strings_needing_escapes_survive(root, value):
    config.set_value("search.query", value, project_root=root)
    assert config.get("search.query", project_root=root) == value


def test_nested_tables_are_real_tables(root):
    config.set_value("a.b.c", 1, project_root=root)
    config.set_value("a.x", "y", project_root=root)
    assert config.read(root) == {"a": {"x": "y", "b": {"c": 1}}}
    assert config.get("a.b.c", project_root=root) == 1


def test_top_level_scalar(root):
    config.set_value("version", 2, project_root=root)
    config.set_value("t.k", "v", project_root=root)
    assert config.read(root) == {"version": 2, "t": {"k": "v"}}


def test_set_value_preserves_other_keys(root):
    config.set_value("one.a", 1, project_root=root)
    config.set_value("two.b", 2, project_root=root)
    config.set_value("one.a", 3, project_root=root)
    assert config.read(root) == {"one": {"a": 3}, "two": {"b": 2}}


def test_get_through_a_scalar_returns_default(root):
    config.set_value("a.b", 1, project_root=root)
    assert config.get("a.b.c", default="none", project_root=root) == "none"
    assert config.get("missing", project_root=root) is None


def test_none_is_rejected_not_written_as_string(root):
    with pytest.raises(TypeError):
        config.set_value("a.b", None, project_root=root)


def test_invalid_toml_raises(root):
    config.config_path(root).write_text("not = [valid\n")
    with pytest.raises(tomllib.TOMLDecodeError):
        config.read(root)
