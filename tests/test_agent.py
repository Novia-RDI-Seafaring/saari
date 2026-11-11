"""The pydantic-ai agent: offline behavior, streaming, and real tool calls.

No LLM is contacted. `FunctionModel` scripts the model's turns so the agent
drives the actual saari tools against a temp project; `TestModel` covers the
streaming path. The embedding model is the stub from test_pipeline.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.messages import (  # noqa: E402
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402
from pydantic_ai.models.test import TestModel  # noqa: E402

from saari import agent as agent_mod, paths  # noqa: E402
from test_pipeline import fake_model, insert, load_seed  # noqa: E402,F401


def collect(prompt: str) -> list[dict]:
    async def run():
        return [ev async for ev in agent_mod.stream_chat(prompt)]

    return asyncio.run(run())


@pytest.fixture()
def no_keys(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture()
def project(tmp_path, monkeypatch):
    root = paths.init_project(tmp_path / "proj")
    monkeypatch.setenv("SAARI_PROJECT_ROOT", str(root))
    return root


def test_offline_without_api_key(no_keys):
    events = collect("hello")
    assert [e["event"] for e in events] == ["message", "done"]
    msg = json.loads(events[0]["data"])
    assert msg["role"] == "assistant"
    assert "OPENAI_API_KEY" in msg["content"]


def test_build_failure_is_reported_as_error_event(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    def broken():
        raise RuntimeError("bad model id")

    monkeypatch.setattr(agent_mod, "_build_agent", broken)
    events = collect("hello")
    assert events == [{"event": "error", "data": json.dumps({"error": "bad model id"})}]


def test_stream_emits_deltas_then_done(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(
        agent_mod,
        "_build_agent",
        lambda: Agent(TestModel(call_tools=[], custom_output_text="three papers included")),
    )
    events = collect("summarize")
    assert events[-1] == {"event": "done", "data": "{}"}
    deltas = [json.loads(e["data"])["delta"] for e in events if e["event"] == "delta"]
    assert "".join(deltas) == "three papers included"


def _scripted(calls: list[tuple[str, dict]]):
    """A FunctionModel that issues `calls` one per turn, then answers 'done'."""

    def fn(messages, info: AgentInfo) -> ModelResponse:
        n_returns = sum(
            isinstance(p, ToolReturnPart) for m in messages for p in getattr(m, "parts", [])
        )
        if n_returns < len(calls):
            name, args = calls[n_returns]
            return ModelResponse(parts=[ToolCallPart(name, args)])
        return ModelResponse(parts=[TextPart("done")])

    return FunctionModel(fn)


def _tool_returns(result) -> dict[str, object]:
    return {
        p.tool_name: p.content
        for m in result.all_messages()
        for p in getattr(m, "parts", [])
        if isinstance(p, ToolReturnPart)
    }


def test_agent_tools_drive_the_pipeline(project, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    insert(project, load_seed())
    agent = agent_mod._build_agent()
    model = _scripted([
        ("study_set", {"question": "How are digital twins used in shipping?"}),
        ("refresh", {}),
        ("screen_paper", {"paper_id": "10.1680/jmaen.2022.027", "decision": "include"}),
        ("project_info", {}),
    ])
    with agent.override(model=model):
        result = agent.run_sync("set up the review")
    assert result.output == "done"
    out = _tool_returns(result)

    assert out["study_set"]["question"].startswith("How are digital twins")
    assert out["refresh"]["embed"] == {"n_embedded": 11, "n_skipped": 0}
    assert out["refresh"]["project"]["n_points"] == 11
    assert (paths.papers_dir(project) / "landscape.html").exists()
    assert out["screen_paper"] == {
        "paper_id": "10.1680/jmaen.2022.027", "ok": True, "status": "included",
    }
    info = out["project_info"]
    assert info["n_papers"] == 11
    assert info["by_status"] == {"candidate": 10, "included": 1}
    assert info["pipeline"] == {"embedded": 11, "projected": 11}


def test_agent_refresh_on_empty_project_reports_note(project, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    agent = agent_mod._build_agent()
    with agent.override(model=_scripted([("refresh", {})])):
        result = agent.run_sync("refresh")
    out = _tool_returns(result)["refresh"]
    assert "Not enough embeddings" in out["project"]["note"]
    assert not (paths.papers_dir(project) / "landscape.canvas").exists()
