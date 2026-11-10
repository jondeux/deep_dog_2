"""Page reading through Claude's own WebFetch (the `--search claude` backend), on the fake `claude` binary.

Before this, `fetch_urls` under `--search claude` returned the search snippet (about 200 characters) in 0.0 s, so
agents never read a page. Now a snippet is not mistaken for page text, and fetch_urls reads the page through
`claude -p --tools WebFetch`."""
import asyncio
import json

import pytest
from langchain_core.messages import HumanMessage

from deep_research import model_picker as mp
from deep_research.agents import AGENT_REGISTRY
from deep_research.agents import base
from deep_research.agents.web import claude_fetch
from deep_research.agents.web.claude_search import claude_web_search
from deep_research.models import Credentials, ModelFactory
from deep_research.runtime import RuntimeContext, runtime_scope
from conftest import api_config

URL = "https://example.com/a"
PAGE = "FULL PAGE TEXT: the trial enrolled 4,158 adults. " + "More detail. " * 40


def runtime(**kw):
    cfg = mp.apply_preset(api_config(**kw), "testing-framework")
    return RuntimeContext(run_id="r", thread_id="t", config=cfg, models=ModelFactory(cfg, Credentials({})),
                          console_enabled=False)


def val(argv, flag):
    return argv[argv.index(flag) + 1]


def run_in(rt, coro_fn):
    async def go():
        with runtime_scope(rt):
            return await coro_fn()
    return asyncio.run(go())


# ── the reader itself ─────────────────────────────────────────────────────────
def test_reader_runs_webfetch_only_and_returns_title_and_page(fake_claude):
    fake_claude.script([{"text": f"TITLE: Page A\n---\n{PAGE}", "cost": 0.01}])
    title, body = run_in(runtime(), lambda: claude_fetch.claude_fetch_page(URL))
    assert title == "Page A" and "FULL PAGE TEXT" in body and URL in body and body.startswith("# Page A")
    call = fake_claude.calls()[0]
    assert val(call["argv"], "--tools") == "WebFetch"
    assert call["argv"][call["argv"].index("--allowedTools") + 1] == "WebFetch"
    assert URL in call["stdin"] and val(call["argv"], "--model") == "claude-haiku-5-5"


def test_reader_failures_are_text_for_the_agent_not_crashes(fake_claude):
    fake_claude.script([{"text": "FETCH_FAILED: HTTP 403 from the server", "cost": 0.01}])
    title, body = run_in(runtime(), lambda: claude_fetch.claude_fetch_page(URL))
    assert title == URL and body.startswith("Failed to fetch") and "403" in body
    fake_claude.script([{"text": "", "cost": 0.01}])
    _, body2 = run_in(runtime(), lambda: claude_fetch.claude_fetch_page(URL))
    assert body2.startswith("Failed to fetch") and "no text" in body2


def test_a_reply_that_is_not_page_text_is_a_failure_not_a_source(fake_claude):
    """Live probe v1 (PubMed): the model apologised that only a cookie notice came back. That is not a page."""
    fake_claude.script([{"text": "I tried to open the page, but only a cookie notice came back. What would you like me to do?",
                         "cost": 0.01}])
    title, body = run_in(runtime(), lambda: claude_fetch.claude_fetch_page(URL))
    assert title == URL and body.startswith("Failed to fetch") and "cookie notice" in body   # the agent sees why


def test_the_agents_topic_steers_what_is_extracted(fake_claude):
    fake_claude.script([{"text": f"TITLE: Page A\n---\n{PAGE}", "cost": 0.01}])
    run_in(runtime(), lambda: claude_fetch.claude_fetch_page(URL, focus="sample size of the 2022 TRE trial"))
    assert "sample size of the 2022 TRE trial" in fake_claude.calls()[0]["stdin"]


def test_reader_truncates_to_the_configured_limit(fake_claude):
    fake_claude.script([{"text": "TITLE: T\n---\n" + "x" * 5000, "cost": 0.01}])
    _, body = run_in(runtime(fetch_url_max_chars=1000), lambda: claude_fetch.claude_fetch_page(URL))
    assert body.endswith("[content truncated]") and len(body) < 1300


def test_a_usage_stop_in_the_reader_is_reported_to_the_agent_as_text(fake_claude):
    fake_claude.script([{"text": "never", "cost": 0.01}])
    rt = runtime()
    rt.models.governor.util["five_hour"] = 0.75                 # past the research limit
    _, body = run_in(rt, lambda: claude_fetch.claude_fetch_page(URL))
    assert body.startswith("Failed to fetch") and "usage" in body.lower()
    assert fake_claude.calls() == []


# ── a snippet is not a page ───────────────────────────────────────────────────
def test_search_items_are_marked_snippet_only_when_the_page_reader_is_on(fake_claude):
    reply = json.dumps([{"title": "Page A", "url": URL, "snippet": "short snippet"}])
    for reader, expected in ((True, True), (False, False)):
        fake_claude.script([{"text": reply, "cost": 0.01}])
        out = run_in(runtime(claude_page_reader=reader),
                     lambda: claude_web_search.ainvoke({"query": "q", "max_results": 3}))
        assert out["items"][0].get("snippet_only", False) is expected


def test_the_search_text_cache_skips_snippets():
    results = {"S1": {"items": [{"id": URL, "title": "A", "full_text": "snippet", "snippet_only": True},
                                {"id": "https://example.com/b", "title": "B", "full_text": "page text"}]}}
    assert base._lookup_search_full_text(results, URL) is None
    assert base._lookup_search_full_text(results, "https://example.com/b") == ("B", "page text")


# ── whole sub-agent: search, then read the page, then save ────────────────────
def decision(*calls):
    return {"text": json.dumps({"content": "", "tool_calls": [{"name": n, "args": a} for n, a in calls]}),
            "cost": 0.01}


def subagent_script(read_reply):
    return [
        decision(("claude_web_search", {"query": "trial size"})),
        {"text": json.dumps([{"title": "Page A", "url": URL, "snippet": "short snippet"}]), "cost": 0.01},
        decision(("fetch_urls", {"urls": [URL]})),
        *read_reply,
        decision(("batch_save_selected", {"items": [{"ref": "S1", "index": 1, "reason": "primary trial report"}]})),
        decision(("finish_research", {"summary": "done"})),
    ]


def run_subagent(rt):
    async def go():
        return await AGENT_REGISTRY["ResearchWeb"].ainvoke({
            "researcher_messages": [HumanMessage(content="How many adults were in the trial?")],
            "research_topic": "How many adults were in the trial?", "target_language": "English",
            "discovery": False, "console_agent_id": 1, "chinese_supervisor_international_subagent": False})
    return run_in(rt, go)


def webfetch_calls(fake_claude):
    return [c for c in fake_claude.calls() if val(c["argv"], "--tools") == "WebFetch"]


def test_the_agent_reads_the_real_page_and_the_saved_source_carries_it(fake_claude):
    fake_claude.script(subagent_script([{"text": f"TITLE: Page A\n---\n{PAGE}", "cost": 0.01}]))
    out = run_subagent(runtime())
    assert len(webfetch_calls(fake_claude)) == 1
    assert "How many adults were in the trial?" in webfetch_calls(fake_claude)[0]["stdin"]   # the agent's topic
    saved = out.get("curated_sources") or []
    assert saved and "4,158 adults" in saved[0]["full_text"]          # page text, not the 13-character snippet
    assert "short snippet" not in saved[0]["full_text"] or len(saved[0]["full_text"]) > 200


def test_with_the_page_reader_off_the_agent_gets_the_snippet_as_before(fake_claude):
    fake_claude.script(subagent_script([]))
    out = run_subagent(runtime(claude_page_reader=False))
    assert webfetch_calls(fake_claude) == []
    saved = out.get("curated_sources") or []
    assert saved and saved[0]["full_text"].strip() == "short snippet"


def test_other_search_engines_never_use_the_claude_reader(fake_claude):
    cfg = api_config(web_search_engine="tavily")
    rt = RuntimeContext(run_id="r", thread_id="t", config=cfg, models=ModelFactory(cfg, Credentials({})),
                        console_enabled=False)
    assert claude_fetch.page_reader_enabled(rt.config) is False
    assert claude_fetch.page_reader_enabled(runtime().config) is True
    assert claude_fetch.page_reader_enabled(runtime(claude_page_reader=False).config) is False


def test_cli_switch_turns_the_page_reader_off(monkeypatch):
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location("rr_fetch", "run_research.py")
    rr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rr)
    seen = {}

    async def fake_run(prompt, output_dir, thread_id=None, clean_output=False, config=None, **kw):
        seen["config"] = config
        return {"output_file": "x", "status": "completed"}
    monkeypatch.setattr(rr, "run_research", fake_run)
    monkeypatch.setattr(sys, "argv", ["rr", "--prompt", "q", "--preset", "economy"])
    rr.main()
    assert seen["config"].claude_page_reader is True and seen["config"].web_search_engine == "claude"
    monkeypatch.setattr(sys, "argv", ["rr", "--prompt", "q", "--preset", "economy", "--no-page-reader"])
    rr.main()
    assert seen["config"].claude_page_reader is False
    monkeypatch.setattr(sys, "argv", ["rr", "--prompt", "q", "--no-page-reader"])      # no picker needed any more
    rr.main()
    assert seen["config"].claude_page_reader is False


# ── hybrid reading: a few pages per agent are read in full, the rest stay summaries ─────────────────────────
URL_B = "https://example.com/b"
TWO = json.dumps([{"title": "Page A", "url": URL, "snippet": "summary of A"},
                  {"title": "Page B", "url": URL_B, "snippet": "summary of B"}])


def hybrid_script(*middle):
    return [decision(("claude_web_search", {"query": "q"})), {"text": TWO, "cost": 0.01}, *middle,
            decision(("batch_save_selected", {"items": [{"ref": "S1", "index": 1, "reason": "a"},
                                                        {"ref": "S1", "index": 2, "reason": "b"}]})),
            decision(("finish_research", {"summary": "done"}))]


def texts(out):
    return {c["url"]: c["full_text"] for c in (out.get("curated_sources") or [])}


def test_the_budget_reads_the_first_pages_and_leaves_the_rest_as_summaries(fake_claude):
    fake_claude.script(hybrid_script(decision(("fetch_urls", {"urls": [URL, URL_B]})),
                                     {"text": f"TITLE: Page A\n---\n{PAGE}", "cost": 0.01}))
    out = run_subagent(runtime(claude_page_reads_per_agent=1))
    assert len(webfetch_calls(fake_claude)) == 1                       # only one page was read through WebFetch
    t = texts(out)
    assert "4,158 adults" in t[URL] and "summary of B" in t[URL_B]     # A in depth, B still saved (breadth kept)


def test_a_failed_read_does_not_use_up_the_budget(fake_claude):
    fake_claude.script(hybrid_script(decision(("fetch_urls", {"urls": [URL]})),
                                     {"text": "FETCH_FAILED: HTTP 403", "cost": 0.01},
                                     decision(("fetch_urls", {"urls": [URL_B]})),
                                     {"text": f"TITLE: Page B\n---\nPAGE B TEXT {PAGE}", "cost": 0.01}))
    out = run_subagent(runtime(claude_page_reads_per_agent=1))
    assert len(webfetch_calls(fake_claude)) == 2                       # the 403 on A left the slot free for B
    assert "PAGE B TEXT" in texts(out)[URL_B]


def test_zero_means_every_read_goes_through_the_reader(fake_claude):
    same = {"text": f"TITLE: A page\n---\nPAGE TEXT {PAGE}", "cost": 0.01}       # two reads run at once: identical replies
    fake_claude.script(hybrid_script(decision(("fetch_urls", {"urls": [URL, URL_B]})), same, same))
    out = run_subagent(runtime(claude_page_reads_per_agent=0))
    assert len(webfetch_calls(fake_claude)) == 2
    t = texts(out)
    assert "PAGE TEXT" in t[URL] and "PAGE TEXT" in t[URL_B]


def test_the_agent_is_told_when_a_read_was_only_a_summary(fake_claude):
    fake_claude.script(hybrid_script(decision(("fetch_urls", {"urls": [URL, URL_B]})),
                                     {"text": f"TITLE: Page A\n---\n{PAGE}", "cost": 0.01}))
    out = run_subagent(runtime(claude_page_reads_per_agent=1))
    seen = json.dumps([str(m.content) for m in out["researcher_messages"]])
    assert "page-read budget" in seen                                  # the agent can see why B is thin


def test_the_search_results_tell_the_agent_about_the_budget(fake_claude):
    fake_claude.script([{"text": TWO, "cost": 0.01}])
    out = run_in(runtime(claude_page_reads_per_agent=3),
                 lambda: claude_web_search.ainvoke({"query": "q", "max_results": 3}))
    assert "3 pages" in out["display"] and "primary" in out["display"]
    fake_claude.script([{"text": TWO, "cost": 0.01}])
    out2 = run_in(runtime(claude_page_reader=False), lambda: claude_web_search.ainvoke({"query": "q", "max_results": 3}))
    assert "primary" not in out2["display"]                              # reader off: the old, plain footer


def test_cli_page_reads_flag(monkeypatch):
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location("rr_reads", "run_research.py")
    rr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rr)
    seen = {}

    async def fake_run(prompt, output_dir, thread_id=None, clean_output=False, config=None, **kw):
        seen["config"] = config
        return {"output_file": "x", "status": "completed"}
    monkeypatch.setattr(rr, "run_research", fake_run)
    monkeypatch.setattr(sys, "argv", ["rr", "--prompt", "q", "--preset", "economy"])
    rr.main()
    assert seen["config"].claude_page_reads_per_agent == 3
    monkeypatch.setattr(sys, "argv", ["rr", "--prompt", "q", "--preset", "economy", "--page-reads", "0"])
    rr.main()
    assert seen["config"].claude_page_reads_per_agent == 0
    for bad in (["--preset", "economy", "--page-reads", "-1"],):
        monkeypatch.setattr(sys, "argv", ["rr", "--prompt", "q", *bad])
        with pytest.raises(SystemExit):
            rr.main()
