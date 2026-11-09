"""Shared fakes for the Deep Dog test suite.

No test in this suite calls a real LLM/search provider. Model construction and
invocation are faked so individual stages and graph wiring can be exercised
cheaply and deterministically.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import pytest

from langchain_core.messages import AIMessage

from deep_research.models import Credentials
from deep_research.run_config import RunConfig

FINAL_TEXT = "# Final Report\n\nA verified body of findings with citations preserved in code."
DRAFT_TEXT = "DRAFT REPORT TEXT"


def _fill_schema(schema) -> Any:
    """Instantiate a pydantic structured-output schema with canned values."""
    vals: dict[str, Any] = {}
    for name, field_ in getattr(schema, "model_fields", {}).items():
        if not field_.is_required():
            continue
        ann = str(field_.annotation)
        if name == "research_brief":
            vals[name] = "RESEARCH BRIEF: analyse the test question."
        elif name in ("input_language", "target_language"):
            vals[name] = "English"
        elif "bool" in ann:
            vals[name] = False
        elif "int" in ann:
            vals[name] = 0
        elif "float" in ann:
            vals[name] = 0.0
        else:
            vals[name] = "x"
    return schema(**vals)


class FakeLLM:
    """A scripted fake chat model that never touches the network."""

    def __init__(self, kind: str):
        self.kind = kind
        self.calls = 0
        self._schema = None

    def bind_tools(self, tools):
        return self

    def with_fallbacks(self, fallbacks, **kwargs):
        return self

    def with_structured_output(self, schema, **kwargs):
        # Mirrors real models: returns a NEW bound runnable, never mutating the
        # cached base model (the cached draft model is reused by other nodes).
        bound = FakeLLM(self.kind)
        bound._schema = schema
        return bound

    async def ainvoke(self, messages):
        self.calls += 1
        return self._respond(messages)

    def invoke(self, messages):
        self.calls += 1
        if self._schema is not None:
            return _fill_schema(self._schema)
        return AIMessage(content="ok")

    def _respond(self, messages):
        if self._schema is not None:
            return _fill_schema(self._schema)
        if self.kind == "supervisor":
            return AIMessage(
                content="supervisor decision: research complete",
                tool_calls=[{"id": "tc-research-complete", "name": "ResearchComplete", "args": {}}],
            )
        if self.kind == "writer":
            return AIMessage(content=FINAL_TEXT)
        if self.kind == "draft":
            return AIMessage(content=DRAFT_TEXT)
        return AIMessage(content="subagent deliverable")


class FakeModelFactory:
    """Drop-in replacement for ModelFactory that returns FakeLLM instances."""

    def __init__(self, config: RunConfig, credentials: Credentials | None = None):
        self.config = config
        self.credentials = credentials or Credentials()

    def supervisor(self, tools=None, *, max_tokens=None, temperature=0.0, chain=None,
                   reasoning_effort=None):
        kind = "writer" if max_tokens == 40000 else "supervisor"
        return FakeLLM(kind)

    def draft_report(self, *, max_tokens=None, temperature=0.0, chain=None, reasoning_effort=None):
        return FakeLLM("draft")

    def subagent(self, tools=None, *, max_tokens=None, temperature=0.0, chain=None,
                 reasoning_effort=None):
        return FakeLLM("subagent")

    def get(self, model_name, *, temperature=0.0, max_tokens=None, reasoning_effort=None,
            route_via_openrouter=None):
        return FakeLLM("subagent")


def api_config(**overrides) -> RunConfig:
    """A run config safe for API-style tests (no file output, fast)."""
    base = dict(
        output_mode="none",
        log_mode="none",
        save_report_to_file=False,
        save_subagent_reports_to_file=False,
        enable_source_log=False,
        enable_subtopic_generation=False,
        enable_research_trace=False,
        disable_model_fallback=True,
    )
    base.update(overrides)
    return RunConfig(**base).finalize()


@pytest.fixture(autouse=True)
def _fast_ui_poll(monkeypatch):
    """Shorten the web UI's progress refresh in tests; the app's own 0.7 s is unchanged."""
    app = sys.modules.get("webui.app")
    if app is not None:
        monkeypatch.setattr(app, "_POLL_SECONDS", 0.05)


# ── Free tests must never launch a real `claude` ───────────────────────────────
# A stub `claude` goes first on PATH. If a test reaches it, it writes a sentinel and the session fails.
# Tests that exercise the claudecode: provider use the `fake_claude` fixture (DEEPDOG_CLAUDE_BIN) instead.
@pytest.fixture(autouse=True, scope="session")
def _no_real_claude(tmp_path_factory):
    import os, stat
    d = tmp_path_factory.mktemp("claude-net")
    sentinel = d / "claude_was_called"
    stub = d / "claude"
    stub.write_text(f"#!/bin/sh\necho called > {sentinel}\nexit 99\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{d}{os.pathsep}{old}"
    os.environ.pop("DEEPDOG_CLAUDE_BIN", None)
    yield
    os.environ["PATH"] = old
    assert not sentinel.exists(), "a free test launched a real `claude` (it reached the PATH stub)"


# Resume state must never land in the real deep-research-memory/ folder from a test run.
@pytest.fixture(autouse=True, scope="session")
def _memory_dir_in_tmp(tmp_path_factory):
    import os
    old = os.environ.get("DEEPDOG_MEMORY_DIR")
    os.environ["DEEPDOG_MEMORY_DIR"] = str(tmp_path_factory.mktemp("deep-research-memory"))
    yield
    if old is None:
        os.environ.pop("DEEPDOG_MEMORY_DIR", None)
    else:
        os.environ["DEEPDOG_MEMORY_DIR"] = old


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """Point the provider at tests/fake_claude.py. Returns a helper: ``fc.script([...])``, ``fc.calls()``."""
    import json, os, stat, sys
    from deep_research.providers.claude_code import preflight
    from deep_research.providers.claude_code.runtime import _governor_var
    wrapper = tmp_path / "claude"
    wrapper.write_text(f'#!/bin/sh\nexec {sys.executable} {os.path.join(os.path.dirname(__file__), "fake_claude.py")} "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    paths = {"script": tmp_path / "script.json", "state": tmp_path / "state", "log": tmp_path / "log.jsonl"}
    monkeypatch.setenv("DEEPDOG_CLAUDE_BIN", str(wrapper))
    monkeypatch.setenv("DEEPDOG_CC_FAKE_SCRIPT", str(paths["script"]))
    monkeypatch.setenv("DEEPDOG_CC_FAKE_STATE", str(paths["state"]))
    monkeypatch.setenv("DEEPDOG_CC_FAKE_LOG", str(paths["log"]))
    preflight._cache.clear()

    class FC:
        bin = str(wrapper)

        def script(self, responses):
            paths["script"].write_text(json.dumps(responses))
            paths["state"].write_text("0")

        def calls(self):
            if not paths["log"].exists():
                return []
            rows = [json.loads(l) for l in paths["log"].read_text().splitlines() if l.strip()]
            starts = [r for r in rows if "argv" in r]
            ends = {r["pid"]: r["end"] for r in rows if "end" in r}
            for r in starts:
                r["end"] = ends.get(r["pid"])
            return starts

    yield FC()
    preflight._cache.clear()
