"""The CLI switch table: every named switch reaches its RunConfig field, --set and --config coerce and refuse bad
input, mixed backends build the right chains, and --show-config exposes no secrets."""
import argparse
import json

import pytest

from deep_research import cli_options as co
from deep_research import model_picker as mp
from deep_research.run_config import RunConfig


def parse(*argv):
    p = argparse.ArgumentParser()
    p.add_argument("--resume")
    co.add_arguments(p)
    return p.parse_args(list(argv))


def test_every_named_switch_maps_to_a_real_runconfig_field():
    names = {f for f in RunConfig.__dataclass_fields__}
    assert {o.field for o in co.OPTIONS} <= names
    assert len({o.flag for o in co.OPTIONS}) == len(co.OPTIONS)


def test_no_switch_means_engine_defaults():
    assert co.build_config(parse()) is None


@pytest.mark.parametrize("argv, field, value", [
    (["--time-max", "12"], "research_time_max_minutes", 12.0),
    (["--supervisor-iterations", "7"], "supervisor_max_iterations", 7),
    (["--parallel-research", "2"], "supervisor_max_concurrent_research", 2),
    (["--searches-per-agent", "4"], "subagent_max_searches", 4),
    (["--thinking", "off"], "thinking_mode", "off"),
    (["--route-openrouter"], "route_via_openrouter", True),
    (["--no-trace"], "enable_research_trace", False),
    (["--agents", "ResearchWeb,ResearchReddit"], "enabled_agents", ["ResearchWeb", "ResearchReddit"]),
    (["--langsmith", "--langsmith-project", "p"], "langsmith_project", "p"),
    (["--static-reserve"], "claude_code_dynamic_reserve", False),
    (["--no-page-reader"], "claude_page_reader", False),
    (["--page-reads", "0"], "claude_page_reads_per_agent", 0),
    (["--usd-cap", "2.5"], "claude_code_usd_cap", 2.5),
    (["--search", "tavily"], "web_search_engine", "tavily"),
])
def test_named_switches_reach_their_field(argv, field, value):
    assert getattr(co.build_config(parse(*argv)), field) == value


@pytest.mark.parametrize("argv", [
    ["--usd-cap", "0"], ["--report-budget", "-1"], ["--plan-window-max", "2"], ["--plan-window-max", "0.05"],
    ["--supervisor-iterations", "0"], ["--page-reads", "-1"], ["--agents", "ResearchNope"],
    ["--general-platforms", "nope"], ["--set", "nope=1"], ["--set", "noequals"], ["--set", "supervisor_max_iterations=x"],
])
def test_bad_values_are_refused(argv):
    with pytest.raises(co.CliError):
        co.build_config(parse(*argv))


def test_set_coerces_by_field_type_and_beats_named_switches():
    cfg = co.build_config(parse(
        "--time-max", "10", "--set", "research_time_max_minutes=20", "--set", "enable_research_trace=yes",
        "--set", "language=none", "--set", "enabled_agents=ResearchWeb,ResearchArxiv",
        "--set", "claude_code_report_budget_usd=none", "--set", 'subagent_model_chain_by_agent={"ResearchWeb": ["x"]}'))
    assert cfg.research_time_max_minutes == 20.0 and cfg.enable_research_trace is True
    assert cfg.language is None and cfg.enabled_agents == ["ResearchWeb", "ResearchArxiv"]
    assert cfg.claude_code_report_budget_usd is None and cfg.subagent_model_chain_by_agent == {"ResearchWeb": ["x"]}


def test_unknown_set_field_suggests_a_close_one():
    with pytest.raises(co.CliError, match="did you mean.*supervisor_max_iterations"):
        co.parse_set(["supervisor_max_iteration=3"])


def test_config_file_is_weaker_than_switches(tmp_path):
    f = tmp_path / "run.json"
    f.write_text(json.dumps({"research_time_max_minutes": 30, "supervisor_max_iterations": 9}))
    cfg = co.build_config(parse("--config", str(f), "--supervisor-iterations", "4"))
    assert cfg.research_time_max_minutes == 30.0 and cfg.supervisor_max_iterations == 4
    f.write_text("[1]")
    with pytest.raises(co.CliError):
        co.build_config(parse("--config", str(f)))
    with pytest.raises(co.CliError):
        co.build_config(parse("--config", str(tmp_path / "missing.json")))


def test_mixed_backends_build_the_right_chains(capsys):
    cfg = co.build_config(parse(
        "--preset", "testing-framework", "--supervisor-model", "deepinfra:deepseek-ai/DeepSeek-V3",
        "--subagent-model", "opus-5.5@low", "--agent-model", "ResearchReddit=openrouter:meta-llama/llama-3.3-70b-instruct:free"))
    assert cfg.supervisor_model_fallback_chain == ["deepinfra:deepseek-ai/DeepSeek-V3"]
    assert cfg.draft_report_model_fallback_chain == ["deepinfra:deepseek-ai/DeepSeek-V3"]      # the draft follows the supervisor
    assert cfg.subagent_model_fallback_chain == ["claudecode:opus-5.5@low"]
    assert cfg.subagent_model_chain_by_agent["ResearchReddit"] == ["openrouter:meta-llama/llama-3.3-70b-instruct:free"]
    assert cfg.disable_model_fallback is True
    assert "deepinfra:deepseek-ai/DeepSeek-V3" in capsys.readouterr().out


def test_draft_can_differ_and_fallbacks_turn_fallback_on():
    cfg = co.build_config(parse("--supervisor-model", "haiku-5.5", "--draft-model", "sonnet-5.5@low",
                                "--subagent-fallback", "haiku-5.5@low,together:openai/gpt-oss-120b"))
    assert cfg.draft_report_model_fallback_chain == ["claudecode:sonnet-5.5@low"]
    assert cfg.subagent_model_fallback_chain == ["claudecode:haiku-5.5@high", "claudecode:haiku-5.5@low", "together:openai/gpt-oss-120b"]
    assert cfg.disable_model_fallback is False


def test_effort_on_a_third_party_model_is_refused():
    with pytest.raises(co.CliError, match="Claude-plan"):
        co.build_config(parse("--supervisor-model", "fireworks:x", "--supervisor-effort", "high"))
    with pytest.raises(co.CliError, match="unknown|Unknown"):
        co.build_config(parse("--supervisor-model", "gpt-9"))
    with pytest.raises(co.CliError, match="AGENT=MODEL"):
        co.build_config(parse("--agent-model", "ResearchReddit"))
    with pytest.raises(co.CliError, match="unknown agent"):
        co.build_config(parse("--agent-model", "Nope=haiku-5.5"))


def test_spec_parsing():
    assert mp.split_spec("sonnet-5.5@high") == ("sonnet-5.5", "high")
    assert mp.split_spec("claudecode:opus-5.5") == ("opus-5.5", None)
    assert mp.split_spec("DeepInfra:a/b:c") == ("deepinfra:a/b:c", None)
    assert mp.is_hosted("openrouter:x/y") and not mp.is_hosted("openrouter:") and not mp.is_hosted("sonnet-5.5")
    assert mp.chain_name("haiku-5.5@low") == "claudecode:haiku-5.5@low"
    assert mp.chain_name("together:openai/gpt-oss-120b") == "together:openai/gpt-oss-120b"


def test_resume_allows_only_the_cost_flags(monkeypatch):
    from deep_research import resume
    snap = {"config": RunConfig().to_dict()}
    monkeypatch.setattr(resume, "config_from_snapshot", lambda s: RunConfig.from_dict(s["config"]))
    assert co.build_config(parse("--usd-cap", "2"), snap).claude_code_usd_cap == 2.0
    assert co.build_config(parse(), snap) is None
    for bad in (["--time-max", "5"], ["--supervisor-model", "haiku-5.5"], ["--search", "exa"], ["--set", "language=fr"]):
        with pytest.raises(co.CliError):
            co.build_config(parse(*bad), snap)


def test_show_config_has_no_secrets_and_lists_missing_key_names(monkeypatch):
    monkeypatch.setenv("DEEPINFRA_API_KEY", "sk-secret-value-123456")
    monkeypatch.delenv("TOGETHER_API_KEY", raising=False)
    cfg = co.build_config(parse("--supervisor-model", "deepinfra:m", "--subagent-model", "together:n"))
    out = co.show_config(cfg)
    assert "sk-secret-value-123456" not in out
    keys = json.loads(out)["keys"]
    assert keys["deepinfra:m"] == {"needs": ["DEEPINFRA_API_KEY"], "present": True}
    assert keys["together:n"] == {"needs": ["TOGETHER_API_KEY"], "present": False}


def test_listings_name_keys_without_values(monkeypatch):
    monkeypatch.setenv("FIREWORKS_API_KEY", "sk-fw-secret-999999")
    text = co.models_report()
    assert "FIREWORKS_API_KEY: set" in text and "sk-fw-secret-999999" not in text
    assert "ResearchReddit" in co.agents_report()


def test_the_manual_switch_tables_match_the_option_table():
    from pathlib import Path
    manual = Path("docs/DEEP_DOG_RESEARCH_MANUAL.md")
    assert co.sync_manual(manual), "run: python -m deep_research.cli_options docs/DEEP_DOG_RESEARCH_MANUAL.md"
    text = manual.read_text(encoding="utf-8")
    for o in co.OPTIONS:
        assert f"`{o.flag}" in text


def test_every_switch_is_in_the_help_and_documented():
    import subprocess, sys
    out = subprocess.run([sys.executable, "run_research.py", "--help"], capture_output=True, text=True, check=True).stdout
    for o in co.OPTIONS:
        assert o.flag in out
    for flag in ("--show-config", "--set", "--config", "--list-models", "--list-agents", "--agent-model", "--price"):
        assert flag in out


def test_language_sets_the_fallback_the_engine_reads():
    assert co.build_config(parse("--language", "French")).target_language_fallback == "French"
