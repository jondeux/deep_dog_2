"""A usage stop must still end in a report, and the run's usage must be readable.

Free tests: the fake `claude` binary stands in, nothing touches the network. Covers the two-tier governor
(research stops early, the final report may use the reserve), salvage at the supervisor nodes, the
no-model digest fallback, and cost / plan-window usage in `ResearchResult`, the events and the CLI."""
import asyncio
import json
import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import deep_research.integration as integration
import deep_research.multi_agent_supervisor as sup
from deep_research import model_picker as mp
from deep_research.events import EventCollector
from deep_research.models import Credentials, ModelFactory
from deep_research.observer import Observer
from deep_research.providers import claude_code as cc
from deep_research.providers.claude_code.governor import Governor, GovernorStop
from deep_research.runtime import RuntimeContext, runtime_scope
from conftest import api_config

CODE = "A1-S1#1"
REGISTRY = [{"code": CODE, "title": "Source One", "url": "https://example.com/one",
             "identifier": "https://example.com/one", "ref": "relevant", "agent": 1}]


# ── governor tiers ────────────────────────────────────────────────────────────
def test_research_stops_at_the_cost_reserve_and_the_report_may_use_it():
    g = Governor(usd_cap=1.0, report_reserve=0.25)
    g.cost = 0.74
    g.check()                                                     # research still allowed
    g.cost = 0.76
    with pytest.raises(GovernorStop) as e:
        g.check()
    assert e.value.reason == "usd_cap" and e.value.phase == "research"
    g.check("report")                                             # the reserve belongs to the report
    g.cost = 1.0
    with pytest.raises(GovernorStop) as e2:
        g.check("report")                                         # the approved cap is still the ceiling
    assert e2.value.phase == "report"


def test_research_stops_below_the_plan_window_guard_and_the_report_may_use_the_gap():
    g = Governor(plan_window_max=0.8, window_reserve=0.1)
    g.util["five_hour"] = 0.69
    g.check()
    g.util["five_hour"] = 0.71
    with pytest.raises(GovernorStop) as e:
        g.check()
    assert e.value.reason == "plan_window_guard"
    g.check("report")
    g.util["seven_day"] = 0.8
    with pytest.raises(GovernorStop):
        g.check("report")                                         # 0.8 is never exceeded


def test_governor_stop_is_fatal_for_the_engine_and_carries_its_reason():
    err = GovernorStop("usd_cap", "x")
    assert sup._classify_error(err) == "fatal"
    assert sup._governor_stop_reason(err) == "usd_cap"
    assert sup._governor_stop_reason(RuntimeError("wrapped")) is None
    try:
        try:
            raise err
        except GovernorStop as inner:
            raise RuntimeError("outer") from inner
    except RuntimeError as outer:
        assert sup._governor_stop_reason(outer) == "usd_cap"      # found through __cause__


def test_report_scope_lets_a_call_through_after_research_was_stopped(fake_claude):
    fake_claude.script([{"text": "the report", "cost": 0.01}])
    g = Governor(usd_cap=1.0, report_reserve=0.25)
    g.cost = 0.8
    m = cc.build_chat_model("claudecode:haiku-5.5").model_copy(update={"governor": g})
    with pytest.raises(GovernorStop):
        m.invoke([HumanMessage("research")])
    assert fake_claude.calls() == []                              # blocked before a process was spawned
    with cc.report_scope():
        assert m.invoke([HumanMessage("write")]).content == "the report"
    assert len(fake_claude.calls()) == 1


# ── salvage through the real supervisor subgraph, real adapter, fake binary ───
def make_runtime(factory_cfg=None):
    cfg = mp.apply_preset(factory_cfg or api_config(claude_code_usd_cap=1.0), "testing-framework")
    collector = EventCollector()
    observer = Observer(run_id="r", config=cfg, event_sink=collector, console_enabled=False)
    factory = ModelFactory(cfg, Credentials({}))
    ctx = RuntimeContext(run_id="r", thread_id="t", config=cfg, models=factory, observer=observer,
                         console_enabled=False)
    return ctx, factory, observer, collector


def seeded(findings=True):
    msgs = [HumanMessage(content="brief text")]
    if findings:
        msgs += [AIMessage(content="", tool_calls=[{"id": "tc1", "name": "ResearchWeb",
                                                    "args": {"research_topic": "T"}}]),
                 ToolMessage(content=f"Finding: X is Y [{CODE}].", name="ResearchWeb", tool_call_id="tc1")]
    return {"supervisor_messages": msgs, "research_brief": "brief", "draft_report": "the initial draft",
            "start_time": time.time(), "target_language": "English",
            "source_registry": [dict(r) for r in REGISTRY] if findings else [], "curated_sources": [], "notes": []}


def run_subgraph(ctx, state):
    async def go():
        with runtime_scope(ctx):
            out = await sup.supervisor_agent.ainvoke(state)
            await ctx.observer.flush_events()
            return out
    return asyncio.run(go())


def test_a_stop_during_research_still_writes_a_synthesised_report(fake_claude):
    fake_claude.script([{"text": f"# Report\n\nX is Y [{CODE}].", "cost": 0.02}])
    ctx, factory, observer, collector = make_runtime()
    factory.governor.util["five_hour"] = 0.75                     # above the research limit (0.7), below 0.8
    out = run_subgraph(ctx, seeded())
    assert "X is Y [1]" in out["final_report"] and "Source One" in out["final_report"]
    assert out["stopped_early"] == "plan_window_guard" and out["report_mode"] == "synthesized"
    assert not out.get("aborted")
    assert len(fake_claude.calls()) == 1                          # only the writer ran; research spawned nothing
    stop = [e for e in collector.events if e.type == "run_stopped_early"]
    assert len(stop) == 1 and stop[0].payload["reason"] == "plan_window_guard"
    assert stop[0].payload["report_mode"] == "synthesized"


def test_when_the_model_cannot_write_the_report_the_findings_are_returned_as_a_digest(fake_claude):
    fake_claude.script([{"text": "must never be called"}])
    ctx, factory, observer, collector = make_runtime()
    factory.governor.util["five_hour"] = 0.85                     # past the hard ceiling: no model call at all
    out = run_subgraph(ctx, seeded())
    report = out["final_report"]
    assert fake_claude.calls() == []
    assert out["report_mode"] == "digest" and out["stopped_early"] == "plan_window_guard"
    assert "Finding: X is Y [1]" in report and "https://example.com/one" in report
    assert "not written by the model" in report.lower()
    stop = [e for e in collector.events if e.type == "run_stopped_early"]
    assert stop and stop[0].payload["report_mode"] == "digest"


def test_a_stop_with_no_findings_aborts_loudly_instead_of_inventing_a_report(fake_claude):
    fake_claude.script([{"text": "must never be called"}])
    ctx, factory, observer, collector = make_runtime()
    factory.governor.util["five_hour"] = 0.75
    with pytest.raises(GovernorStop):
        run_subgraph(ctx, seeded(findings=False))
    assert fake_claude.calls() == []


def test_all_subagents_stopped_by_the_governor_salvages_instead_of_the_quota_abort(monkeypatch, fake_claude):
    async def boom(*a, **k):
        raise GovernorStop("usd_cap", "list-price estimate $0.80 >= cap $0.75")
    monkeypatch.setattr(sup, "_run_subagent_with_progress", boom)
    ctx, *_ = make_runtime()
    call = AIMessage(content="", tool_calls=[{"id": "r1", "name": "ResearchWeb", "args": {"research_topic": "more"}}])
    state = {**seeded(), "supervisor_messages": seeded()["supervisor_messages"] + [call], "research_iterations": 1}
    with runtime_scope(ctx):
        command = asyncio.run(sup.supervisor_tools(state))
    assert command.goto == "write_final_report"
    assert not command.update.get("aborted") and command.update["stopped_early"] == "usd_cap"
    state_none = {**seeded(findings=False), "research_iterations": 1,
                  "supervisor_messages": [HumanMessage("b"), call]}
    with runtime_scope(ctx):
        command = asyncio.run(sup.supervisor_tools(state_none))
    assert command.update["aborted"] is True                      # no findings: the old abort is unchanged


# ── usage in the result, the events and the CLI ───────────────────────────────
SCRIPT = [
    {"text": json.dumps({"research_brief": "Brief", "input_language": "English", "target_language": "English"}),
     "cost": 0.001},
    {"text": "DRAFT scaffold text", "cost": 0.002},
    {"text": json.dumps({"content": "enough", "tool_calls": [{"name": "ResearchComplete", "args": {}}]}),
     "cost": 0.003},
    {"text": "# Final report\n\nThe finished body.", "cost": 0.004, "util": {"five_hour": 0.2, "seven_day": 0.1}},
]


def run_engine(config, collector=None):
    async def go():
        return await integration.run_research(
            "What is X?", config=config,
            runtime=integration.RuntimeOptions(run_id="u1", thread_id="u1", event_sink=collector),
            credentials=Credentials({}))
    return asyncio.run(go())


def test_result_usage_reports_cost_window_and_models(fake_claude):
    fake_claude.script(SCRIPT)
    collector = EventCollector()
    result = run_engine(mp.apply_preset(api_config(), "testing-framework"), collector)
    u = result.usage["claude_code"]
    assert result.status == "completed" and u["calls"] == 4 and u["est_cost_usd"] == pytest.approx(0.010)
    assert u["util"] == {"five_hour": 0.2, "seven_day": 0.1} and u["stopped"] is None
    assert u["by_model"]["claude-sonnet-5-5"]["calls"] == 4
    assert u["limits"]["usd_cap"] == 5.0 and u["limits"]["plan_window_max"] == 0.8
    assert result.usage["curated_sources"] == 0                   # the old counters are still there
    assert "stopped_early" not in result.run_metadata
    updates = [e for e in collector.events if e.type == "usage_updated"]
    assert len(updates) >= 2 and updates[-1].payload["est_cost_usd"] == pytest.approx(0.010)
    assert collector.types()[-2:] == ["usage_updated", "run_completed"]


def test_usage_is_absent_for_runs_that_never_used_claude_code():
    from conftest import FakeModelFactory
    import deep_research.integration as i
    orig = i.ModelFactory
    i.ModelFactory = FakeModelFactory
    try:
        result = run_engine(api_config())
    finally:
        i.ModelFactory = orig
    assert "claude_code" not in result.usage


def test_a_stop_before_any_findings_fails_clearly_and_still_reports_usage(fake_claude):
    fake_claude.script(SCRIPT)
    cfg = mp.apply_preset(api_config(claude_code_usd_cap=0.005), "testing-framework")
    result = run_engine(cfg)
    assert result.status in ("failed", "partial")
    assert "governor stop" in (result.failure or "")
    assert result.usage["claude_code"]["stopped"] == "usd_cap"
    assert result.run_metadata["stopped_early"]["reason"] == "usd_cap"


def test_format_usage_is_one_readable_block():
    text = cc.format_usage({
        "calls": 12, "est_cost_usd": 0.5512, "util": {"five_hour": 0.31, "seven_day": None}, "stopped": "usd_cap",
        "by_model": {"claude-sonnet-5-5": {"calls": 4, "cost": 0.4, "input_tokens": 10, "output_tokens": 5},
                     "claude-haiku-5-5+WebSearch": {"calls": 8, "cost": 0.1512, "input_tokens": 1, "output_tokens": 1}},
        "limits": {"usd_cap": 3.0, "plan_window_max": 0.8}})
    assert "$0.5512" in text and "12 calls" in text and "5-hour window 31%" in text
    assert "claude-sonnet-5-5" in text and "stopped: usd_cap" in text and "list-price estimate" in text
    assert cc.format_usage(None) == ""


# ── CLI ───────────────────────────────────────────────────────────────────────
def load_cli():
    import importlib.util
    spec = importlib.util.spec_from_file_location("rr_usage", "run_research.py")
    rr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rr)
    return rr


def test_cli_cap_flags_reach_the_config_and_need_a_picker(monkeypatch, capsys):
    import sys
    rr = load_cli()
    seen = {}

    async def fake_run(prompt, output_dir, thread_id=None, clean_output=False, config=None):
        seen["config"] = config
        return {"output_file": "x", "status": "completed"}
    monkeypatch.setattr(rr, "run_research", fake_run)
    monkeypatch.setattr(sys, "argv", ["rr", "--prompt", "q", "--preset", "testing-framework",
                                      "--usd-cap", "3", "--plan-window-max", "0.6"])
    rr.main()
    assert seen["config"].claude_code_usd_cap == 3.0 and seen["config"].claude_code_plan_window_max == 0.6
    for bad in (["--usd-cap", "3"], ["--preset", "economy", "--usd-cap", "0"],
                ["--preset", "economy", "--plan-window-max", "2"]):
        monkeypatch.setattr(sys, "argv", ["rr", "--prompt", "q", *bad])
        with pytest.raises(SystemExit):
            rr.main()


def test_cli_prints_and_saves_usage_for_a_stopped_run(tmp_path, monkeypatch, capsys, fake_claude):
    """A real run on the fake binary, ended early by the cap: the CLI says so and writes usage_*.json."""
    fake_claude.script(SCRIPT)
    rr = load_cli()
    cfg = mp.apply_preset(api_config(claude_code_usd_cap=0.005), "testing-framework")
    with pytest.raises(RuntimeError):                  # no findings before the stop: a clear failure, not a report
        asyncio.run(rr.run_research("What is X?", tmp_path, config=cfg))
    cfg2 = mp.apply_preset(api_config(), "testing-framework")
    fake_claude.script(SCRIPT)
    out = asyncio.run(rr.run_research("What is X?", tmp_path, config=cfg2))
    shown = capsys.readouterr().out
    assert out["usage"]["calls"] == 4 and "Usage (list-price estimate" in shown and "$0.0100" in shown
    saved = json.loads(open(out["usage_file"]).read())
    assert saved["status"] == "completed" and saved["usage"]["est_cost_usd"] == pytest.approx(0.01)
    assert saved["stopped_early"] is None


# ── where the plan window started (a final number alone cannot say how much the run itself used) ──────────
def test_governor_remembers_the_first_window_reading():
    from deep_research.providers.claude_code.events import Digest
    g = Governor()
    d1, d2 = Digest(), Digest()
    d1.util = {"five_hour": 0.42, "seven_day": None}
    d2.util = {"five_hour": 0.70, "seven_day": 0.18}
    g.ingest(d1)
    g.ingest(d2)
    st = g.stats()
    assert st["util"] == {"five_hour": 0.70, "seven_day": 0.18}
    assert st["util_first"] == {"five_hour": 0.42, "seven_day": 0.18}       # each window keeps its own first reading


def test_usage_text_shows_where_the_window_started():
    text = cc.format_usage({"calls": 3, "est_cost_usd": 0.1, "util": {"five_hour": 0.70, "seven_day": 0.18},
                            "util_first": {"five_hour": 0.42, "seven_day": 0.18}, "by_model": {}, "limits": {}})
    assert "5-hour window 70% (42% at the first call)" in text and "7-day window 18%" in text
    assert "(18% at the first call)" not in text                              # unchanged windows add no noise


def test_the_result_carries_the_first_reading(fake_claude):
    fake_claude.script([{**SCRIPT[0], "util": {"five_hour": 0.11}}] + SCRIPT[1:3] + [{**SCRIPT[3], "util": {"five_hour": 0.2}}])
    result = run_engine(mp.apply_preset(api_config(), "testing-framework"))
    assert result.usage["claude_code"]["util_first"]["five_hour"] == 0.11


def test_the_cli_memory_folder_follows_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPDOG_MEMORY_DIR", str(tmp_path / "elsewhere"))
    assert load_cli().DEFAULT_MEMORY_DIR == tmp_path / "elsewhere"


def test_cost_is_split_into_research_and_report_so_the_reserve_can_be_sized():
    from deep_research.providers.claude_code.events import Digest
    g = Governor()
    d, d2 = Digest(), Digest()
    d.result = {"total_cost_usd": 0.30}
    d2.result = {"total_cost_usd": 0.20}
    g.ingest(d, model="m")
    with cc.report_scope():
        g.ingest(d2, model="m")
    by = g.stats()["by_phase"]
    assert by["research"] == {"calls": 1, "cost": pytest.approx(0.30)}
    assert by["report"] == {"calls": 1, "cost": pytest.approx(0.20)}
    assert "report phase: $0.2000" in cc.format_usage({**g.stats()})


# ── dollar floor on the report reserve (measured: the report call cost $0.45 at a $3 cap) ─────────────────────
def test_the_report_reserve_has_a_dollar_floor_but_never_swallows_the_run():
    big = Governor(usd_cap=3.0, report_reserve=0.25, report_reserve_floor_usd=0.5)
    assert big.limits()["usd_research_limit"] == pytest.approx(2.25)          # 25% ($0.75) already above the floor
    small = Governor(usd_cap=1.0, report_reserve=0.25, report_reserve_floor_usd=0.5)
    assert small.limits()["usd_research_limit"] == pytest.approx(0.5)         # the floor beats 25% ($0.25)
    tiny = Governor(usd_cap=0.6, report_reserve=0.25, report_reserve_floor_usd=0.5)
    assert tiny.limits()["usd_research_limit"] == pytest.approx(0.24)         # never reserve more than 60% of the cap
    off = Governor(usd_cap=1.0, report_reserve=0.25, report_reserve_floor_usd=0.0)
    assert off.limits()["usd_research_limit"] == pytest.approx(0.75)
    assert Governor(usd_cap=None, report_reserve_floor_usd=0.5).limits()["usd_research_limit"] is None


def test_the_floor_is_what_stops_research_early_at_a_small_cap():
    g = Governor(usd_cap=1.0, report_reserve=0.25, report_reserve_floor_usd=0.5)
    g.cost = 0.55
    with pytest.raises(GovernorStop):
        g.check()                                  # research stops at $0.50, leaving $0.45 for the report call
    g.check("report")


def test_the_config_default_floor_reaches_the_governor():
    cfg = api_config()
    assert cfg.claude_code_report_reserve_floor_usd == 0.5
    # The floor belongs to the fixed reserve; the dynamic reserve (the default) replaces it with a projection.
    f = ModelFactory(mp.apply_preset(api_config(claude_code_usd_cap=1.0, claude_code_dynamic_reserve=False),
                                     "testing-framework"))
    assert f.governor.limits()["usd_research_limit"] == pytest.approx(0.5)
