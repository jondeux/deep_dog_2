"""claude_web_search: Claude's WebSearch as the engine's search backend, on the fake binary."""
import asyncio
import json

import pytest

from deep_research import model_picker as mp
from deep_research.agents import base as agents_base
from deep_research.agents.web.claude_search import _items_from, claude_web_search
from deep_research.models import Credentials, ModelFactory
from deep_research.runtime import RuntimeContext, runtime_scope
from conftest import api_config


def val(argv, flag):
    return argv[argv.index(flag) + 1]


def runtime_for(config):
    return RuntimeContext(run_id="r", thread_id="t", config=config, credentials=Credentials({}), models=ModelFactory(config))


GOOD = json.dumps([
    {"title": "A", "url": "https://a.example/x", "snippet": "alpha"},
    {"title": "dup", "url": "https://a.example/x", "snippet": "again"},
    {"title": "no url", "url": "", "snippet": "x"},
    {"title": "B", "url": "https://b.example/y", "snippet": "beta"}])


def test_items_are_deduped_filtered_and_capped():
    items = _items_from("Here:\n```json\n" + GOOD + "\n```", 5)
    assert [i["url"] for i in items] == ["https://a.example/x", "https://b.example/y"]
    assert items[0] == {"id": "https://a.example/x", "title": "A", "url": "https://a.example/x", "full_text": "alpha"}
    assert len(_items_from(GOOD, 1)) == 1


def test_tool_runs_websearch_in_a_plan_call_and_returns_the_engine_contract(fake_claude):
    fake_claude.script([{"text": GOOD, "cost": 0.013}])
    rt = runtime_for(mp.apply_preset(api_config(), "testing-framework"))
    with runtime_scope(rt):
        out = asyncio.run(claude_web_search.ainvoke({"query": "sodium-ion storage", "max_results": 5}))
    assert set(out) == {"display", "items"} and len(out["items"]) == 2
    assert "https://b.example/y" in out["display"] and "fetch_urls" in out["display"]
    a = fake_claude.calls()[0]["argv"]
    assert val(a, "--tools") == "WebSearch" and a[-2:] == ["--allowedTools", "WebSearch"]   # variadic flag last
    assert val(a, "--model") == "claude-haiku-5-5" and val(a, "--effort") == "low"
    assert "sodium-ion storage" in fake_claude.calls()[0]["stdin"]
    assert rt.models.governor.calls == 1 and rt.models.governor.cost == pytest.approx(0.013)


def test_unparseable_reply_gives_an_empty_result_not_a_crash(fake_claude):
    fake_claude.script([{"text": "I could not search."}])
    rt = runtime_for(mp.apply_preset(api_config(), "testing-framework"))
    with runtime_scope(rt):
        out = asyncio.run(claude_web_search.ainvoke({"query": "q"}))
    assert out["items"] == [] and "no parseable results" in out["display"]
    assert len(fake_claude.calls()) == 2                                 # one repair retry


def test_engine_picks_the_tool_only_for_the_claude_engine(fake_claude):
    names = lambda cfg: {t.name for t in agents_base._runtime_search_tools_for(cfg)}  # noqa: E731
    claude_cfg = mp.apply_preset(api_config(), "economy")
    assert claude_cfg.web_search_engine == "claude"
    assert "claude_web_search" in names(claude_cfg)
    exa_cfg = mp.apply_preset(api_config(), "economy", search="exa")
    assert exa_cfg.web_search_engine == "exa" and "claude_web_search" not in names(exa_cfg)
    assert agents_base.SEARCH_TOOL_NAMES >= {"claude_web_search"}        # counts against the search budget
    assert "claude_web_search" in agents_base.SEARCH_INDEXABLE_TOOLS
    with pytest.raises(ValueError):
        mp.apply_preset(api_config(), "economy", search="bing")
