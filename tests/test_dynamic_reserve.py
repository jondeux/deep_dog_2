"""Dynamic reserve: stop researching when spend plus the cheapest useful report reaches the cap.

The fixed reserve (25 % of the cap, at least $0.50) was too small for a history of 149 Exa sources: live5 stopped
research at $0.52 on a $1.00 cap and the report call then projected $0.92 against $0.48 of room, so it returned the
findings digest. The reserve is now measured: before each supervisor turn the engine projects the report call for the
history it has (source text cut to a useful minimum), and research stops when

    spent + next round (what the last round cost) + that projected report  >=  the cap.

An owner's --report-budget caps the report term. The old static limit stays as a backstop at 90 % of the cap, and
--static-reserve (claude_code_dynamic_reserve=False) restores the old behaviour entirely.
Free tests: fake `claude` binary, nothing touches the network.
"""
import asyncio
import json
import sys

import pytest

import deep_research.multi_agent_supervisor as sup
from deep_research.providers.claude_code.governor import Governor
from deep_research.report_budget import CHARS_PER_TOKEN, OUTPUT_TOKENS, TEXT_FLOOR_CHARS, minimal_report_cost
from deep_research.run_config import RunConfig
from deep_research.runtime import runtime_scope
from test_report_budget import big_state, calibrated, load_cli, make_runtime, project, run_cli

DONE = {"text": json.dumps({"content": "enough", "tool_calls": [{"name": "ResearchComplete", "args": {}}]}),
        "cost": 0.01}


# ── the projection of the cheapest useful report ────────────────────────

def test_the_minimal_report_keeps_only_a_floor_of_text_per_source():
    sizes = [60_000, 200, 5_000]
    kept = sum(min(s, TEXT_FLOOR_CHARS) for s in sizes)
    assert minimal_report_cost(project, 10_000, sizes) == pytest.approx(
        project((10_000 + kept) / CHARS_PER_TOKEN, OUTPUT_TOKENS))


def test_the_minimal_report_costs_less_than_the_full_one_when_sources_are_long():
    sizes = [60_000] * 20
    full = project((10_000 + sum(sizes)) / CHARS_PER_TOKEN, OUTPUT_TOKENS)
    assert minimal_report_cost(project, 10_000, sizes) < full


# ── the governor's rule ─────────────────────────────────────────────────

def test_the_static_limit_becomes_a_backstop_at_90_percent_in_dynamic_mode():
    assert Governor(usd_cap=1.0, report_reserve_floor_usd=0.5).limits()["usd_research_limit"] == pytest.approx(0.5)  # old
    lim = Governor(usd_cap=1.0, dynamic_reserve=True).limits()
    assert lim["usd_research_limit"] == pytest.approx(0.9) and lim["dynamic_reserve"] is True


def test_it_keeps_researching_while_the_report_still_fits():
    g = Governor(usd_cap=1.0, dynamic_reserve=True)
    g.cost = 0.3
    assert g.dynamic_reserve_check(0.2) is None


def test_it_stops_when_spend_plus_the_report_reaches_the_cap():
    g = Governor(usd_cap=1.0, dynamic_reserve=True)
    g.cost = 0.9
    msg = g.dynamic_reserve_check(0.2)
    assert msg and "cap" in msg and g.stats()["reserve_check"]["stopped"] is True


def test_it_allows_for_the_next_round_using_what_the_last_one_cost():
    g = Governor(usd_cap=1.0, dynamic_reserve=True)
    g.cost = 0.1
    assert g.dynamic_reserve_check(0.2) is None                    # first look: a default allowance (15 % of the cap)
    g.cost = 0.5                                                   # the round cost $0.40
    assert 0.5 + 0.2 < 1.0                                         # spend + report alone would still fit...
    assert g.dynamic_reserve_check(0.2) is not None                # ...but another $0.40 round would not


def test_the_owners_report_budget_caps_the_report_term():
    g = Governor(usd_cap=1.0, dynamic_reserve=True)
    g.cost = 0.7
    assert g.dynamic_reserve_check(0.5) is not None                # 0.7 + 0.1 + 0.5 > 1.0
    g2 = Governor(usd_cap=1.0, dynamic_reserve=True, report_budget_usd=0.1)
    g2.cost = 0.7
    assert g2.dynamic_reserve_check(0.5) is None                   # 0.7 + 0.1 + 0.1 < 1.0: the owner accepts a $0.10 report


def test_it_does_nothing_without_a_cap_or_when_switched_off():
    g = Governor(usd_cap=None, dynamic_reserve=True)
    g.cost = 99.0
    assert g.dynamic_reserve_check(5.0) is None
    off = Governor(usd_cap=1.0, dynamic_reserve=False)
    off.cost = 0.99
    assert off.dynamic_reserve_check(5.0) is None


# ── the supervisor node ─────────────────────────────────────────────────

def supervisor_turn(ctx, state):
    async def go():
        with runtime_scope(ctx):
            return await sup.supervisor(state)
    return asyncio.run(go())


def test_the_supervisor_stops_research_and_writes_from_its_findings_when_the_report_would_no_longer_fit(fake_claude):
    fake_claude.script([{"text": "must never be called"}])
    ctx, factory, _ = make_runtime(claude_code_usd_cap=1.0)
    calibrated(factory.governor)
    factory.governor.cost = 0.85
    command = supervisor_turn(ctx, big_state(n=3, size=4_000))
    assert command.goto == "write_final_report"
    assert command.update["stopped_early"] == "report_reserve"
    assert fake_claude.calls() == []                               # no supervisor turn was spent
    assert "final report" in sup._STOP_TEXT["report_reserve"]


def test_the_supervisor_carries_on_while_there_is_room(fake_claude):
    fake_claude.script([DONE])
    ctx, factory, _ = make_runtime(claude_code_usd_cap=5.0)
    calibrated(factory.governor)
    factory.governor.cost = 0.2
    command = supervisor_turn(ctx, big_state(n=3, size=4_000))
    assert command.goto == "supervisor_tools" and len(fake_claude.calls()) == 1


def test_without_findings_it_never_stops_for_the_reserve(fake_claude):
    fake_claude.script([DONE])
    ctx, factory, _ = make_runtime(claude_code_usd_cap=1.0)
    calibrated(factory.governor)
    factory.governor.cost = 0.85
    state = {**big_state(n=3, size=4_000), "source_registry": [], "curated_sources": []}
    command = supervisor_turn(ctx, state)
    assert command.goto != "write_final_report" or command.update.get("stopped_early") != "report_reserve"


def test_the_stop_leaves_room_for_a_model_written_report_where_the_old_reserve_did_not(fake_claude):
    """live5 in miniature: 149 long sources at a $1 cap. The old rule stopped at $0.50 with a history too big to write."""
    fake_claude.script([{"text": "# Report\n\nX is Y [A1-S1#1].", "cost": 0.02}])
    ctx, factory, _ = make_runtime(claude_code_usd_cap=1.0)
    calibrated(factory.governor, unit=2.6e-6)
    state = big_state(n=12, size=20_000)
    factory.governor.cost = 0.20
    assert supervisor_turn(ctx, state).goto != "write_final_report"          # plenty of room: keep researching
    factory.governor.cost = 0.40                                             # a $0.20 round later
    command = supervisor_turn(ctx, state)
    assert command.update["stopped_early"] == "report_reserve"
    out = asyncio.run(_write(ctx, {**state, **command.update}))
    assert "X is Y" in out["final_report"] and out["report_mode"] == "synthesized"     # a report, not the digest
    assert factory.governor.cost + 0.02 < 1.0


async def _write(ctx, state):
    with runtime_scope(ctx):
        return await sup.write_final_report(state)


# ── config and CLI ──────────────────────────────────────────────────────

def test_it_is_on_by_default_and_reaches_the_governor():
    assert RunConfig().claude_code_dynamic_reserve is True
    ctx, factory, _ = make_runtime(claude_code_usd_cap=1.0)
    assert factory.governor.dynamic_reserve is True
    ctx2, factory2, _ = make_runtime(claude_code_usd_cap=1.0, claude_code_dynamic_reserve=False)
    assert factory2.governor.dynamic_reserve is False


def test_cli_static_reserve_switches_it_off(monkeypatch):
    seen = run_cli(monkeypatch, "--prompt", "q", "--preset", "economy", "--static-reserve")
    assert seen["config"].claude_code_dynamic_reserve is False
    seen = run_cli(monkeypatch, "--prompt", "q", "--preset", "economy")
    assert seen["config"].claude_code_dynamic_reserve is True


def test_cli_static_reserve_needs_a_claude_plan_run(monkeypatch):
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, "--prompt", "q", "--static-reserve")


# ── mid-round: the per-call research limit follows the projection ────────
from deep_research.providers import claude_code as cc
from deep_research.providers.claude_code.governor import GovernorStop
from langchain_core.messages import HumanMessage


def seen_one_round(**kw):
    """A governor that has checked once (spend $0.10, cheapest report $0.50) and seen 4 research calls at $0.05."""
    g = Governor(usd_cap=1.0, dynamic_reserve=True, max_concurrent=3, **kw)
    g.cost = 0.10
    g.dynamic_reserve_check(0.50)
    g.by_phase["research"] = {"calls": 4, "cost": 0.20}
    return g


def test_before_any_check_only_the_backstop_limits_research_calls():
    g = Governor(usd_cap=1.0, dynamic_reserve=True)
    g.cost = 0.85
    g.check("research")                                            # below the 90 % backstop: allowed
    g.cost = 0.91
    with pytest.raises(GovernorStop):
        g.check("research")


def test_after_a_check_research_calls_stop_where_the_report_would_no_longer_fit():
    g = seen_one_round()
    g.cost = 0.30
    g.check("research")                                            # 0.30 < 1.0 - 0.50 - 3 * 0.05 = 0.35
    g.cost = 0.36
    with pytest.raises(GovernorStop) as e:
        g.check("research")
    assert e.value.reason == "usd_cap" and "cap" in str(e.value)


def test_the_margin_is_the_average_research_call_times_the_calls_in_flight():
    g = seen_one_round()
    assert g.limits()["dynamic_research_limit"] == pytest.approx(1.0 - 0.50 - 3 * 0.05)
    g.max_concurrent = 1
    assert g.limits()["dynamic_research_limit"] == pytest.approx(1.0 - 0.50 - 1 * 0.05)


def test_the_report_phase_is_not_held_to_the_research_limit():
    g = seen_one_round()
    g.cost = 0.60
    g.check("report")                                              # the report may spend what research left


def test_the_fixed_reserve_mode_is_unchanged():
    g = Governor(usd_cap=1.0, dynamic_reserve=False, report_reserve_floor_usd=0.5)
    g.cost = 0.10
    g.dynamic_reserve_check(0.50)
    g.cost = 0.40
    g.check("research")
    assert g.limits()["dynamic_research_limit"] is None


def test_the_first_round_allowance_grows_with_the_parallel_sub_agents():
    one = Governor(usd_cap=1.0, dynamic_reserve=True, max_concurrent=1)
    three = Governor(usd_cap=1.0, dynamic_reserve=True, max_concurrent=3)
    for g in (one, three):
        g.dynamic_reserve_check(0.1)
    assert three.reserve_check["next_round_usd"] == pytest.approx(3 * one.reserve_check["next_round_usd"])


def test_the_check_reports_the_research_limit_it_set():
    g = seen_one_round()
    assert g.stats()["reserve_check"]["research_limit_usd"] == pytest.approx(0.5)      # cap - cheapest report


def test_a_real_adapter_call_is_refused_mid_round_but_the_report_call_goes_through(fake_claude):
    fake_claude.script([{"text": "the report", "cost": 0.01}])
    g = seen_one_round()
    g.cost = 0.40
    m = cc.build_chat_model("claudecode:haiku-5.5").model_copy(update={"governor": g})
    with pytest.raises(GovernorStop):
        m.invoke([HumanMessage("research")])
    assert fake_claude.calls() == []                               # refused before a process was spawned
    with cc.report_scope():
        assert m.invoke([HumanMessage("write")]).content == "the report"


def test_the_live6t_round_would_have_been_cut_short(fake_claude):
    """live6t: the first check came at $0.24; the round then spent $0.85 and ended at $1.10 on a $1.00 cap."""
    g = Governor(usd_cap=1.0, dynamic_reserve=True, max_concurrent=3)
    g.cost = 0.24
    g.dynamic_reserve_check(0.57)
    g.by_phase["research"] = {"calls": 3, "cost": 0.14}            # the three brief/draft calls so far
    spent_when_refused = None
    for _ in range(40):                                            # haiku sub-agent calls at about $0.047
        try:
            g.check("research")
        except GovernorStop:
            spent_when_refused = g.cost
            break
        g.cost += 0.047
    assert spent_when_refused is not None and spent_when_refused < 0.40
    assert spent_when_refused + 3 * 0.047 + 0.57 <= 1.0 + 0.05     # in-flight calls plus the report still fit the cap

