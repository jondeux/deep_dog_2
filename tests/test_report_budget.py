"""Report-cost guard: the final-report call must fit a dollar budget the owner can set.

Measured: the final-report call cost $0.45 to $0.59 on small histories and $1.33 on one with 353 saved sources, which
pushed a run with a $3.00 cap to $3.25. The call's cost grows with the source text it is given. Before the call, the
engine now projects its cost from the history size and the model's own measured price, and when the projection is over
the budget it (1) trims the per-source text, then (2) falls back to the findings digest rather than overspend.

Budget = what is left under the approved cap, or `claude_code_report_budget_usd` / `--report-budget` when lower.
Free tests: the fake `claude` binary stands in; nothing touches the network.
"""
import asyncio
import sys
import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import deep_research.multi_agent_supervisor as sup
from deep_research import model_picker as mp
from deep_research.events import EventCollector
from deep_research.models import Credentials, ModelFactory
from deep_research.observer import Observer
from deep_research.providers.claude_code.governor import Governor
from deep_research.report_budget import CHARS_PER_TOKEN, OUTPUT_TOKENS, plan_report
from deep_research.runtime import RuntimeContext, runtime_scope
from conftest import api_config
from test_claude_code_resume import load_cli, seed, cfg_for

UNIT = 1e-6                    # $ per input-equivalent token used by the tests (output counts 5x)


def calibrated(g, unit=UNIT, model="claude-sonnet-5-5"):
    g.by_model[model] = {"calls": 5, "cost": unit * 10_000_000, "input_tokens": 10_000_000, "output_tokens": 0}
    return g


def project(tokens_in, tokens_out):
    return 1.25 * UNIT * (tokens_in + 5 * tokens_out)


# ── the planner (pure) ──────────────────────────────────────────────────

def test_no_budget_means_no_change():
    assert plan_report(None, project, 10_000, [50_000, 50_000]).action == "full"


def test_a_call_that_fits_is_left_alone():
    plan = plan_report(5.0, project, 10_000, [50_000, 50_000])
    assert plan.action == "full" and plan.projected_usd <= 5.0


def test_an_over_budget_call_is_trimmed_until_it_fits():
    sizes = [60_000] * 5
    full = project((10_000 + sum(sizes)) / CHARS_PER_TOKEN, OUTPUT_TOKENS)
    plan = plan_report(full * 0.6, project, 10_000, sizes)
    assert plan.action == "trimmed" and 300 <= plan.text_limit < 60_000
    kept = sum(min(s, plan.text_limit) for s in sizes)
    assert project((10_000 + kept) / CHARS_PER_TOKEN, OUTPUT_TOKENS) <= full * 0.6
    assert plan.projected_usd <= full * 0.6


def test_shorter_sources_are_kept_whole_when_trimming_the_long_ones_is_enough():
    sizes = [2_000, 2_000, 200_000]
    plan = plan_report(project((10_000 + 80_000) / CHARS_PER_TOKEN, OUTPUT_TOKENS), project, 10_000, sizes)
    assert plan.action == "trimmed" and plan.text_limit >= 2_000        # the small ones lose nothing


def test_when_no_useful_text_fits_all_source_text_is_dropped_but_the_report_is_still_written():
    sizes = [60_000] * 5
    floor = project(10_000 / CHARS_PER_TOKEN, OUTPUT_TOKENS)
    plan = plan_report(floor * 1.001, project, 10_000, sizes)
    assert plan.action == "trimmed" and plan.text_limit == 0


def test_when_even_the_bare_call_is_over_budget_it_falls_back_to_the_digest():
    floor = project(10_000 / CHARS_PER_TOKEN, OUTPUT_TOKENS)
    assert plan_report(floor * 0.5, project, 10_000, [60_000]).action == "digest"


# ── the governor's side ─────────────────────────────────────────────────

def test_the_budget_is_what_is_left_under_the_cap_unless_the_owner_set_less():
    g = Governor(usd_cap=3.0)
    g.cost = 1.9
    assert g.report_budget_now() == pytest.approx(1.1)
    assert Governor(usd_cap=3.0, report_budget_usd=0.6).report_budget_now() == pytest.approx(0.6)
    g2 = Governor(usd_cap=3.0, report_budget_usd=2.0)
    g2.cost = 2.5
    assert g2.report_budget_now() == pytest.approx(0.5)                # never past the approved cap
    assert Governor(usd_cap=None).report_budget_now() is None
    assert Governor(usd_cap=None, report_budget_usd=0.7).report_budget_now() == pytest.approx(0.7)


def test_the_owners_budget_sets_how_much_research_leaves_for_the_report():
    assert Governor(usd_cap=3.0).limits()["usd_research_limit"] == pytest.approx(2.25)        # 25 % reserve, unchanged
    lim = Governor(usd_cap=3.0, report_budget_usd=1.2).limits()
    assert lim["usd_research_limit"] == pytest.approx(1.8) and lim["report_budget_usd"] == 1.2
    assert Governor(usd_cap=3.0, report_budget_usd=9.0).limits()["usd_research_limit"] == pytest.approx(1.2)  # 60 % ceiling


def test_the_price_is_measured_from_the_models_own_calls_with_a_safe_default():
    g = calibrated(Governor(usd_cap=3.0))
    assert g.project_report_cost("claude-sonnet-5-5", 100_000, 20_000) == pytest.approx(project(100_000, 20_000))
    unknown = Governor(usd_cap=3.0).project_report_cost("claude-opus-5-5", 100_000, 20_000)
    assert unknown > project(100_000, 20_000)                          # no data yet: assume a dearer price than sonnet's


def test_a_calibration_from_a_handful_of_tokens_is_not_trusted():
    g = Governor(usd_cap=3.0)
    g.by_model["claude-sonnet-5-5"] = {"calls": 1, "cost": 0.000001, "input_tokens": 10, "output_tokens": 1}
    assert g.project_report_cost("claude-sonnet-5-5", 100_000, 20_000) > project(100_000, 20_000)


# ── the engine, through the real supervisor subgraph and the fake binary ─

def make_runtime(**cfg):
    config = mp.apply_preset(api_config(**cfg), "testing-framework")
    collector = EventCollector()
    observer = Observer(run_id="r", config=config, event_sink=collector, console_enabled=False)
    factory = ModelFactory(config, Credentials({}))
    ctx = RuntimeContext(run_id="r", thread_id="t", config=config, models=factory, observer=observer,
                         console_enabled=False)
    return ctx, factory, collector


def big_state(n=5, size=60_000):
    reg = [{"code": f"A1-S1#{i}", "title": f"Source {i}", "url": f"https://example.com/{i}",
            "identifier": f"https://example.com/{i}", "ref": "relevant", "agent": 1,
            "full_text": f"TEXT{i} " + "x" * size} for i in range(1, n + 1)]
    msgs = [HumanMessage(content="brief text"),
            AIMessage(content="", tool_calls=[{"id": "tc1", "name": "ResearchWeb", "args": {"research_topic": "T"}}]),
            ToolMessage(content="Finding: X is Y [A1-S1#1].", name="ResearchWeb", tool_call_id="tc1")]
    return {"supervisor_messages": msgs, "research_brief": "brief", "draft_report": "the initial draft",
            "start_time": time.time(), "target_language": "English", "source_registry": reg,
            "curated_sources": [], "notes": []}


def run_subgraph(ctx, state):
    """Run the final-report node itself (the supervisor node before it would make its own model call)."""
    async def go():
        with runtime_scope(ctx):
            out = await sup.write_final_report(state)
            await ctx.observer.flush_events()
            return out
    return asyncio.run(go())


REPORT = {"text": "# Report\n\nX is Y [A1-S1#1].", "cost": 0.02}


def stdin_of(fake_claude):
    calls = fake_claude.calls()
    assert len(calls) == 1
    return calls[0]["stdin"]


def test_a_call_that_fits_sends_every_source_in_full(fake_claude):
    fake_claude.script([REPORT])
    ctx, factory, _ = make_runtime(claude_code_usd_cap=5.0)
    calibrated(factory.governor)
    out = run_subgraph(ctx, big_state())
    assert "X is Y" in out["final_report"] and not out.get("stopped_early")
    assert stdin_of(fake_claude).count("x") > 290_000
    assert factory.governor.stats()["report_plan"]["action"] == "full"


def test_an_owner_budget_below_the_projection_trims_the_source_text_and_still_writes_the_report(fake_claude):
    fake_claude.script([REPORT])
    ctx, factory, _ = make_runtime(claude_code_usd_cap=5.0, claude_code_report_budget_usd=0.2)
    calibrated(factory.governor)
    out = run_subgraph(ctx, big_state())
    sent = stdin_of(fake_claude)
    assert "X is Y [1]" in out["final_report"] and not out.get("stopped_early")
    assert 20_000 < sent.count("x") < 120_000 and "TEXT1" in sent and "TEXT5" in sent    # every source kept, each shortened
    plan = factory.governor.stats()["report_plan"]
    assert plan["action"] == "trimmed" and plan["projected_usd"] <= 0.2 and plan["budget_usd"] == pytest.approx(0.2)


def test_without_an_owner_budget_the_room_left_under_the_cap_is_the_budget(fake_claude):
    fake_claude.script([REPORT])
    ctx, factory, _ = make_runtime(claude_code_usd_cap=0.25)
    calibrated(factory.governor)
    run_subgraph(ctx, big_state())
    plan = factory.governor.stats()["report_plan"]
    assert plan["action"] == "trimmed" and plan["budget_usd"] == pytest.approx(0.25) and plan["projected_usd"] <= 0.25


def test_a_budget_too_small_for_any_model_call_returns_the_digest_and_makes_no_call(fake_claude):
    fake_claude.script([{"text": "must never be called"}])
    ctx, factory, collector = make_runtime(claude_code_usd_cap=5.0, claude_code_report_budget_usd=0.05)
    calibrated(factory.governor)
    out = run_subgraph(ctx, big_state())
    assert fake_claude.calls() == []
    assert out["report_mode"] == "digest" and out["stopped_early"] == "report_budget"
    assert "not written by the model" in out["final_report"].lower() and "budget" in out["final_report"].lower()
    stop = [e for e in collector.events if e.type == "run_stopped_early"]
    assert stop and stop[0].payload["reason"] == "report_budget" and stop[0].payload["resumable"] is True


def test_an_earlier_stop_reason_is_kept_when_the_budget_also_blocks_the_report(fake_claude):
    fake_claude.script([{"text": "must never be called"}])
    ctx, factory, _ = make_runtime(claude_code_usd_cap=5.0, claude_code_report_budget_usd=0.05)
    calibrated(factory.governor)
    out = run_subgraph(ctx, {**big_state(), "stopped_early": "plan_window_guard"})
    assert out["report_mode"] == "digest" and out["stopped_early"] == "plan_window_guard"
    assert fake_claude.calls() == []


def test_runs_without_a_governor_are_untouched():
    class Plain:                                     # a model factory that has no cost data (non-claudecode runs)
        pass
    assert sup._report_cost_estimator(Plain(), "m") is None


# ── the CLI flag ────────────────────────────────────────────────────────

def run_cli(monkeypatch, *argv):
    rr = load_cli()
    seen = {}

    async def fake_run(prompt, output_dir, thread_id=None, clean_output=False, config=None, resume=None, memory_dir=None):
        seen.update(config=config, resume=resume)
        return {"output_file": "x", "status": "completed"}
    monkeypatch.setattr(rr, "run_research", fake_run)
    monkeypatch.setattr(sys, "argv", ["rr", *argv])
    rr.main()
    return seen


def test_cli_report_budget_reaches_the_run_config(monkeypatch):
    seen = run_cli(monkeypatch, "--prompt", "q", "--preset", "economy", "--usd-cap", "3", "--report-budget", "0.8")
    assert seen["config"].claude_code_report_budget_usd == 0.8 and seen["config"].claude_code_usd_cap == 3.0


def test_cli_report_budget_must_be_positive(monkeypatch):
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, "--prompt", "q", "--preset", "economy", "--report-budget", "0")


def test_cli_report_budget_may_change_on_resume(monkeypatch, tmp_path):
    from deep_research import resume as rs
    mem = tmp_path / "mem"
    rs.save_snapshot(mem, seed(cfg_for()))
    seen = run_cli(monkeypatch, "--resume", "t0", "--memory-dir", str(mem), "--report-budget", "1.5")
    assert seen["config"].claude_code_report_budget_usd == 1.5


def test_cli_report_budget_needs_a_claude_plan_run(monkeypatch):
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, "--prompt", "q", "--report-budget", "1")
