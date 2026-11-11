"""Logging setup: level precedence, stderr/file only, and MCP stdio safety."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys

import pytest

from saari import log


@pytest.fixture(autouse=True)
def restore_saari_logger(monkeypatch):
    monkeypatch.delenv(log.ENV_LEVEL, raising=False)
    monkeypatch.delenv(log.ENV_FILE, raising=False)
    logger = logging.getLogger(log.LOGGER_NAME)
    saved = (list(logger.handlers), logger.level, logger.propagate)
    yield
    for h in list(logger.handlers):
        if h not in saved[0]:
            logger.removeHandler(h)
            h.close()
    logger.setLevel(saved[1])
    logger.propagate = saved[2]


def owned(logger):
    return [h for h in logger.handlers if getattr(h, log._OWNED, False)]


def test_default_level_is_warning():
    assert log.setup().level == logging.WARNING


def test_env_level(monkeypatch):
    monkeypatch.setenv(log.ENV_LEVEL, "debug")
    assert log.setup().level == logging.DEBUG
    monkeypatch.setenv(log.ENV_LEVEL, "15")
    assert log.setup().level == 15


def test_bad_env_level_falls_back_instead_of_crashing(monkeypatch):
    monkeypatch.setenv(log.ENV_LEVEL, "chatty")
    assert log.setup().level == logging.WARNING


def test_explicit_level_beats_env(monkeypatch):
    monkeypatch.setenv(log.ENV_LEVEL, "ERROR")
    assert log.setup(logging.INFO).level == logging.INFO
    # "not set" (0 -v flags) defers to the env var
    assert log.setup(log.verbosity_to_level(0)).level == logging.ERROR


def test_verbosity_mapping():
    assert log.verbosity_to_level(0) is None
    assert log.verbosity_to_level(1) == logging.INFO
    assert log.verbosity_to_level(3) == logging.DEBUG


def test_setup_is_idempotent_and_writes_to_stderr_only():
    logger = log.setup()
    log.setup()
    hs = owned(logger)
    assert len(hs) == 1
    assert hs[0].stream is sys.stderr


def test_log_file(monkeypatch, tmp_path):
    target = tmp_path / "logs" / "saari.log"
    monkeypatch.setenv(log.ENV_FILE, str(target))
    logger = log.setup("INFO")
    logging.getLogger("saari.embed").info("embedded %d papers", 3)
    for h in owned(logger):
        h.flush()
    assert "saari.embed: embedded 3 papers" in target.read_text()


def test_cli_verbose_flag_sets_level(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from saari.cli import app

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["-vv", "init", str(tmp_path / "p")])
    assert logging.getLogger("saari").level == logging.DEBUG
    runner.invoke(app, ["init", str(tmp_path / "q")])
    assert logging.getLogger("saari").level == logging.WARNING


def test_mcp_stdio_stdout_carries_only_jsonrpc(tmp_path):
    """With DEBUG logging on, stdout must still be pure JSON-RPC.

    Runs the real `saari-mcp` entry point over stdio and calls tools that log
    (study_set at DEBUG, refresh at INFO on an empty project, so no model is
    loaded). Every stdout line must parse as JSON-RPC; the logs must show up
    on stderr.
    """
    from saari import paths

    root = paths.init_project(tmp_path / "proj")
    env = {**os.environ, "SAARI_PROJECT_ROOT": str(root), log.ENV_LEVEL: "DEBUG"}
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "study_set", "arguments": {"question": "RQ?"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "refresh", "arguments": {}}},
    ]
    proc = subprocess.Popen(
        [sys.executable, "-c", "from saari.mcp_server import main; main()"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, text=True,
    )
    responses = {}
    try:
        for req in requests:
            proc.stdin.write(json.dumps(req) + "\n")
            proc.stdin.flush()
            if "id" not in req:
                continue
            while True:
                line = proc.stdout.readline()
                assert line, f"server closed stdout; stderr:\n{proc.stderr.read()}"
                msg = json.loads(line)  # a log line here would fail to parse
                assert msg.get("jsonrpc") == "2.0"
                if msg.get("id") == req["id"]:
                    responses[req["id"]] = msg
                    break
        proc.stdin.close()
        rest = proc.stdout.read()
        stderr = proc.stderr.read()
    finally:
        proc.kill()
        proc.wait(timeout=10)

    for line in rest.splitlines():
        if line.strip():
            json.loads(line)
    assert "error" not in responses[2] and "error" not in responses[3]
    assert "saari.study: updated" in stderr
    assert "saari.projection: project: skipped" in stderr
