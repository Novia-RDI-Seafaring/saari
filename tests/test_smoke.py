"""Smoke tests: verify the package is importable and structurally sane.

These tests do not touch the network, a database, or any model weights.
They are designed to run in a clean CI environment with no project root
(no .saaristo/ directory) so they avoid any call that triggers
paths.project_root().
"""

import importlib.metadata


def test_package_imports():
    """The top-level package must be importable and expose __version__."""
    import saari

    assert hasattr(saari, "__version__")
    assert isinstance(saari.__version__, str)
    assert saari.__version__ != ""


def test_version_matches_pyproject():
    """The installed package version must match what pyproject.toml declares."""
    import saari

    dist_version = importlib.metadata.version("saari")
    assert dist_version == saari.__version__


def test_models_importable():
    """Core Pydantic models must be importable and constructable without side effects."""
    from saari.models import Paper, Author, Location

    p = Paper(id="openalex:W123", title="Test Paper")
    assert p.id == "openalex:W123"
    assert p.status == "candidate"
    assert p.authors == []


def test_paths_importable():
    """paths module must be importable and expose expected constants/functions."""
    from saari import paths

    assert paths.MARKER == ".saaristo"
    assert callable(paths.find_project_root)
    assert callable(paths.project_root)


def test_cli_app_importable():
    """The Typer CLI app object must be importable without invoking any command."""
    from saari.cli import app
    import typer

    assert isinstance(app, typer.Typer)
