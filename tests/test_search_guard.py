"""Fail-fast guard: a search backend that keeps failing ends the run early and clearly.

A run once finished "completed" with a report written from memory after all 80 Exa searches returned 401. Now
`search_failure_limit` consecutive failures (default 6; 0 turns the guard off) stop new searches, and the supervisor
either salvages a report from the findings it already has or fails with a message that names the backend.
Free tests: fake HTTP session, fake `claude` binary, no network.
"""
import asyncio
import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import deep_research.multi_agent_supervisor as sup
from deep_research import model_picker as mp
from deep_research import utils
from deep_research.agents.web.tools import exa_deep_search
from deep_research.events import EventCollector
from deep_research.models import Credentials, ModelFactory
from deep_research.observer import Observer
from deep_research.run_config import RunConfig
from deep_research.runtime import RuntimeContext, runtime_scope
from deep_research.search_health import (SearchBackendUnavailable, SearchHealth, record_search,
                                         search_unavailable)
from conftest import api_config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.delenv("EXA_AUTH_PROXY", raising=False)


def runtime(**cfg):
    config = RunConfig(**cfg).finalize()
    return RuntimeContext(run_id="r", thread_id="t", config=config, credentials=Credentials({"EXA_API_KEY": "k-123456"}),
                          console_enabled=False)


# ── the counter ─────────────────────────────────────────────────────────

def test_it_trips_after_the_limit_in_a_row_and_names_the_backend():
    h = SearchHealth(limit=3)
    for _ in range(2):
        h.record("Exa", False, "HTTP 401 INVALID_API_KEY")
    assert h.tripped is None
    h.record("Exa", False, "HTTP 401 INVALID_API_KEY")
    assert "Exa" in h.tripped and "3" in h.tripped and "401" in h.tripped


def test_a_success_resets_the_streak():
    h = SearchHealth(limit=3)
    h.record("Exa", False, "x")
    h.record("Exa", False, "x")
    h.record("Exa", True)
    h.record("Exa", False, "x")
    h.record("Exa", False, "x")
    assert h.tripped is None


def test_a_limit_of_zero_turns_it_off():
    h = SearchHealth(limit=0)
    for _ in range(50):
        h.record("Exa", False, "x")
    assert h.tripped is None


def test_the_message_is_short_and_never_carries_a_run_secret():
    h = SearchHealth(limit=1, secrets=["sk-very-secret-value"])
    h.record("Exa", False, "bad key sk-very-secret-value " + "x" * 2000)
    assert "sk-very-secret-value" not in h.tripped and len(h.tripped) < 600


# ── tracking happens only inside a run ──────────────────────────────────

def test_nothing_is_tracked_outside_a_run():
    for _ in range(20):
        record_search("Exa", False, "x")
    assert search_unavailable() is None


def test_inside_a_run_failures_trip_it_using_the_runs_own_limit():
    with runtime_scope(runtime(search_failure_limit=2)):
        record_search("Exa", False, "boom")
        assert search_unavailable() is None
        record_search("Exa", False, "boom")
        assert "Exa" in search_unavailable()


def test_two_runs_do_not_share_a_streak():
    a, b = runtime(search_failure_limit=2), runtime(search_failure_limit=2)
    with runtime_scope(a):
        record_search("Exa", False, "x")
    with runtime_scope(b):
        record_search("Exa", False, "x")
        assert search_unavailable() is None


# ── the Exa request path feeds it ───────────────────────────────────────

class Resp:
    def __init__(self, status):
        self.status = status

    async def json(self):
        return {"results": [{"url": "https://example.org/a", "title": "A", "text": "body"}]}

    async def text(self):
        return '{"error":"Invalid API key","tag":"INVALID_API_KEY"}'

    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        return False


class Session:
    status = 401
    posts = 0

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        return False

    def post(self, url, json=None, headers=None):
        Session.posts += 1
        return Resp(Session.status)


@pytest.fixture
def exa(monkeypatch):
    Session.posts, Session.status = 0, 401
    monkeypatch.setattr(utils.aiohttp, "ClientSession", Session)
    return Session


def test_failed_exa_requests_trip_the_guard_and_the_tool_stops_calling_the_network(exa):
    with runtime_scope(runtime(search_failure_limit=3)):
        for _ in range(3):
            asyncio.run(utils._exa_search_multiple(["q"], max_results=1))
        assert exa.posts == 3 and "Exa" in search_unavailable()
        text = asyncio.run(exa_deep_search.ainvoke({"query": "more", "max_results": 1}))
        assert "unavailable" in str(text).lower() and "finish" in str(text).lower()
        assert exa.posts == 3                                       # no fourth request


def test_a_200_with_no_results_is_not_a_failure(exa):
    exa.status = 200

    class Empty(Resp):
        async def json(self):
            return {"results": []}

    Session.post = lambda self, url, json=None, headers=None: Empty(200)
    with runtime_scope(runtime(search_failure_limit=2)):
        for _ in range(5):
            asyncio.run(utils._exa_search_multiple(["q"], max_results=1))
        assert search_unavailable() is None


# ── the supervisor ──────────────────────────────────────────────────────

CODE = "A1-S1#1"
REGISTRY = [{"code": CODE, "title": "Source One", "url": "https://example.com/one",
             "identifier": "https://example.com/one", "ref": "relevant", "agent": 1}]


def make_ctx():
    cfg = mp.apply_preset(api_config(claude_code_usd_cap=5.0), "testing-framework")
    collector = EventCollector()
    observer = Observer(run_id="r", config=cfg, event_sink=collector, console_enabled=False)
    ctx = RuntimeContext(run_id="r", thread_id="t", config=cfg, models=ModelFactory(cfg, Credentials({})),
                         observer=observer, console_enabled=False)
    return ctx, collector


def seeded(findings):
    msgs = [HumanMessage(content="brief text")]
    if findings:
        msgs += [AIMessage(content="", tool_calls=[{"id": "tc1", "name": "ResearchWeb", "args": {"research_topic": "T"}}]),
                 ToolMessage(content=f"Finding: X is Y [{CODE}].", name="ResearchWeb", tool_call_id="tc1")]
    return {"supervisor_messages": msgs, "research_brief": "brief", "draft_report": "the initial draft",
            "start_time": time.time(), "target_language": "English",
            "source_registry": [dict(r) for r in REGISTRY] if findings else [], "curated_sources": [], "notes": []}


def trip(ctx):
    with runtime_scope(ctx):
        for _ in range(ctx.config.search_failure_limit):
            record_search("Exa", False, "HTTP 401 INVALID_API_KEY")
        assert search_unavailable()


def test_with_findings_a_tripped_guard_writes_the_report_from_them_and_can_resume(fake_claude):
    fake_claude.script([{"text": f"# Report\n\nX is Y [{CODE}].", "cost": 0.02}])
    ctx, collector = make_ctx()
    trip(ctx)

    async def go():
        with runtime_scope(ctx):
            out = await sup.supervisor_agent.ainvoke(seeded(findings=True))
            await ctx.observer.flush_events()
            return out
    out = asyncio.run(go())
    assert out["stopped_early"] == "search_unavailable" and "X is Y [1]" in out["final_report"]
    assert len(fake_claude.calls()) == 1                          # the writer only: no supervisor turn was spent
    stop = [e for e in collector.events if e.type == "run_stopped_early"]
    assert stop and stop[0].payload["reason"] == "search_unavailable" and stop[0].payload["resumable"] is True


def test_without_findings_it_fails_clearly_before_any_model_call(fake_claude):
    fake_claude.script([{"text": "must never be called"}])
    ctx, _ = make_ctx()
    trip(ctx)

    async def go():
        with runtime_scope(ctx):
            return await sup.supervisor_agent.ainvoke(seeded(findings=False))
    with pytest.raises(SearchBackendUnavailable, match="Exa"):
        asyncio.run(go())
    assert fake_claude.calls() == []


def test_a_healthy_run_is_untouched(fake_claude):
    fake_claude.script([{"text": f"# Report\n\nX is Y [{CODE}].", "cost": 0.02}])
    ctx, _ = make_ctx()
    ctx.config.search_failure_limit  # the default exists
    assert ctx.config.search_failure_limit == 6
    with runtime_scope(ctx):
        record_search("Exa", True)
        assert search_unavailable() is None
