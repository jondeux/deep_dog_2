"""claudecode: wiring into config, ModelFactory, the picker, and a whole engine run on the fake binary."""
import asyncio
import json

import pytest

import deep_research.integration as integration
from deep_research import config as cfg
from deep_research import model_picker as mp
from deep_research.events import EventCollector
from deep_research.models import Credentials, ModelFactory
from deep_research.providers import claude_code as cc
from deep_research.run_config import RunConfig
from conftest import api_config
from test_claude_code_provider import Search


def val(argv, flag):
    return argv[argv.index(flag) + 1]


def test_get_model_routes_claudecode_and_keeps_old_effort_validation(fake_claude):
    m = cfg.get_model("claudecode:haiku-5.5@max")
    assert isinstance(m, cc.ClaudeCodeChatModel) and m.effort == "max" and m.model_id == "claude-haiku-5-5"
    assert cfg.get_model("claudecode:haiku-5.5", reasoning_effort="xhigh").effort == "xhigh"
    with pytest.raises(ValueError, match="Invalid reasoning_effort"):
        cfg.get_model("gpt-5.2", reasoning_effort="xhigh")           # unchanged for every other provider


def test_claudecode_needs_no_api_key_and_stays_in_chains(fake_claude):
    assert cfg.model_required_keys("claudecode:opus-5.5@low") == []
    assert cfg._filter_usable_chain(["claudecode:sonnet-5.5"], False, "supervisor") == ["claudecode:sonnet-5.5"]
    chk = integration.validate_credentials(mp.apply_preset(api_config(), "testing-framework"), Credentials({}))
    assert not any(k in chk.missing_required for k in ("DEEPSEEK_API_KEY", "OPENROUTER_API_KEY"))


def test_presets_are_exactly_as_specified():
    assert mp.PRESETS == {
        "economy":           {"supervisor": ("sonnet-5.5", "high"),   "subagent": ("haiku-5.5", "high")},
        "benchmark":         {"supervisor": ("opus-5.5", "medium"),   "subagent": ("sonnet-5.5", "high")},
        "custom":            {"supervisor": ("sonnet-5.5", "xhigh"),  "subagent": ("haiku-5.5", "xhigh")},
        "testing-framework": {"supervisor": ("sonnet-5.5", "medium"), "subagent": ("haiku-5.5", "medium")}}
    assert mp.DEFAULT_PRESET == "economy"


def test_apply_preset_sets_chains_overrides_and_validates():
    c = mp.apply_preset(RunConfig(), "benchmark", subagent=("haiku-5.5", "low"),
                        subagent_by_agent={"ResearchReddit": ("sonnet-5.5", "xhigh")})
    assert c.supervisor_model_fallback_chain == ["claudecode:opus-5.5@medium"]
    assert c.draft_report_model_fallback_chain == ["claudecode:opus-5.5@medium"]      # follows the supervisor
    assert c.subagent_model_fallback_chain == ["claudecode:haiku-5.5@low"]
    assert c.subagent_model_chain_by_agent["ResearchReddit"] == ["claudecode:sonnet-5.5@xhigh"]
    assert c.profile == "benchmark" and c.disable_model_fallback
    with pytest.raises(ValueError):
        mp.apply_preset(RunConfig(), "nope")
    with pytest.raises(ValueError):
        mp.apply_preset(RunConfig(), "economy", supervisor=("sonnet-5.5", "ultra"))
    with pytest.raises(ValueError):
        mp.apply_preset(RunConfig(), "economy", subagent=("gpt-5", "high"))
    assert mp.picker_catalog()["default"] == "economy" and "xhigh" in mp.picker_catalog()["efforts"]


def test_model_factory_gives_each_role_its_model_effort_and_the_run_governor(fake_claude):
    f = ModelFactory(mp.apply_preset(RunConfig(), "custom"))
    sup, draft = f.supervisor(max_tokens=1000), f.draft_report(max_tokens=1000)
    sub = f.subagent(tools=[Search])
    assert (sup.model_id, sup.effort) == ("claude-sonnet-5-5", "xhigh")
    assert (draft.model_id, draft.effort) == ("claude-sonnet-5-5", "xhigh")
    assert (sub.model_id, sub.effort) == ("claude-haiku-5-5", "xhigh") and sub.tool_specs
    assert sup.governor is f.governor is sub.governor
    assert f.governor.max_concurrent == 3 and f.governor.plan_window_max == 0.8


def test_whole_engine_run_on_the_fake_claude(fake_claude, monkeypatch):
    """Brief -> draft -> supervisor -> final report, every LLM call going through `claude -p`."""
    created = []

    class F(ModelFactory):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            created.append(self)
    monkeypatch.setattr(integration, "ModelFactory", F)
    fake_claude.script([
        {"text": json.dumps({"research_brief": "Brief of the question", "input_language": "English",
                             "target_language": "English"}), "cost": 0.001},
        {"text": "DRAFT scaffold text", "cost": 0.002},
        {"text": json.dumps({"content": "enough", "tool_calls": [{"name": "ResearchComplete", "args": {}}]}),
         "cost": 0.003},
        {"text": "# Final report\n\nThe finished body.", "cost": 0.004, "util": {"five_hour": 0.2}},
    ])
    collector = EventCollector()
    config = mp.apply_preset(api_config(), "testing-framework")

    async def run():
        return await integration.run_research(
            "What is X?", config=config,
            runtime=integration.RuntimeOptions(run_id="r1", thread_id="t1", event_sink=collector),
            credentials=Credentials({}))
    result = asyncio.run(run())

    assert result.status == "completed" and "finished body" in result.final_report
    types = collector.types()
    assert types[0] == "run_started" and types[-1] == "run_completed"
    for expected in ("scope_started", "draft_started", "supervisor_iteration", "report_started", "citations_validated"):
        assert expected in types
    calls = fake_claude.calls()
    assert len(calls) == 4
    assert "JSON Schema" in calls[0]["stdin"]                                  # brief: schema mode
    assert '"name": "ResearchComplete"' in calls[2]["stdin"]                   # supervisor: tools mode
    for c in calls:
        assert val(c["argv"], "--model") == "claude-sonnet-5-5" and val(c["argv"], "--effort") == "medium"
    gov = created[0].governor
    assert gov.calls == 4 and gov.cost == pytest.approx(0.010) and gov.util["five_hour"] == 0.2


def test_web_ui_refuses_claudecode_models():
    """The public Space must never spend the owner's plan for visitors."""
    pytest.importorskip("gradio")
    import sys
    sys.path.insert(0, "webui")
    try:
        import webui.app as ui
    except ImportError:
        import app as ui
    ok = ["deepseek-v4-flash"]
    for bad in (["claudecode:sonnet-5.5"], ["deepseek-v4-flash", " ClaudeCode:haiku-5.5@low"]):
        for slot in range(3):
            chains = [ok, ok, ok]
            chains[slot] = bad
            with pytest.raises(ValueError, match="personal use"):
                ui.build_config(*chains, False, "auto", 5, 5, 3, 1, ["ResearchWeb"], "exa")


def test_cli_picker_flags(monkeypatch, capsys):
    import runpy, sys
    seen = {}

    async def fake_run(prompt, output_dir, thread_id=None, clean_output=False, config=None):
        seen["config"] = config
        return {"output_file": "x"}
    monkeypatch.setattr(sys, "argv", ["run_research.py", "--list-presets"])
    runpy.run_path("run_research.py", run_name="__main__")
    out = capsys.readouterr().out
    assert "economy -> supervisor: sonnet-5.5 @ high" in out and "testing-framework" in out

    import importlib.util
    spec = importlib.util.spec_from_file_location("rr", "run_research.py")
    rr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rr)
    monkeypatch.setattr(rr, "run_research", fake_run)
    monkeypatch.setattr(sys, "argv", ["rr", "--prompt", "q", "--preset", "benchmark", "--subagent-effort", "xhigh"])
    rr.main()
    c = seen["config"]
    assert c.supervisor_model_fallback_chain == ["claudecode:opus-5.5@medium"]
    assert c.subagent_model_fallback_chain == ["claudecode:sonnet-5.5@xhigh"]
    monkeypatch.setattr(sys, "argv", ["rr", "--prompt", "q"])
    rr.main()
    assert seen["config"] is None                                   # no picker flag: engine defaults untouched
