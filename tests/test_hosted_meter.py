"""Third-party models are metered and count against the run's cap, together with Claude-plan calls."""
import argparse

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from deep_research import cli_options as co
from deep_research import config as cfg
from deep_research.providers import hosted_meter as hm
from deep_research.providers.claude_code.governor import Governor, GovernorStop
from deep_research.run_config import RunConfig


def msg(inp=1000, out=500, **meta):
    m = AIMessage(content="hi", usage_metadata={"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out})
    if meta:
        m.response_metadata = {"token_usage": meta}
    return m


def run_call(callback, message):
    model = GenericFakeChatModel(messages=iter([message]), callbacks=[callback])
    return model.invoke([HumanMessage(content="q")])


def governor(**kw):
    g = Governor(usd_cap=kw.pop("usd_cap", 1.0), dynamic_reserve=False, **kw)
    g.set_price("deepinfra:m", 1.0 / 1e6, 2.0 / 1e6)          # $1 / $2 per million tokens
    return g


def test_a_priced_call_is_estimated_and_counted():
    g = governor()
    run_call(hm.HostedMeterCallback("deepinfra:m", g), msg(1_000_000, 500_000))
    s = g.stats()
    assert s["est_cost_usd"] == pytest.approx(2.0) and s["calls"] == 1
    row = s["by_model"]["deepinfra:m"]
    assert row["source"] == "estimated" and row["input_tokens"] == 1_000_000 and row["output_tokens"] == 500_000


def test_a_provider_reported_cost_wins_over_the_price_table():
    g = governor()
    run_call(hm.HostedMeterCallback("deepinfra:m", g), msg(1000, 1000, cost=0.0123))
    assert g.stats()["est_cost_usd"] == pytest.approx(0.0123)
    assert g.stats()["by_model"]["deepinfra:m"]["source"] == "reported"


def test_an_unpriced_call_is_tokens_only_and_marked():
    g = Governor(usd_cap=1.0)
    run_call(hm.HostedMeterCallback("fireworks:x", g), msg())
    row = g.stats()["by_model"]["fireworks:x"]
    assert row["source"] == "unpriced" and row["cost"] == 0 and row["input_tokens"] == 1000


def test_a_call_past_the_cap_does_not_start():
    g = governor(usd_cap=0.05)
    g.cost = 0.05
    with pytest.raises(GovernorStop) as e:
        run_call(hm.HostedMeterCallback("deepinfra:m", g), msg())
    assert e.value.reason == "usd_cap" and "RESOURCE_EXHAUSTED" in str(e.value)


def test_claude_plan_and_third_party_spend_share_one_cap():
    g = governor(usd_cap=1.0, report_reserve=0.0)
    g.cost = 0.9                                            # a Claude-plan spend counted earlier
    run_call(hm.HostedMeterCallback("deepinfra:m", g), msg(50_000, 25_000))      # $0.10 estimated
    assert g.cost == pytest.approx(1.0)
    with pytest.raises(GovernorStop):
        g.check("report")


def test_the_report_projection_uses_the_known_price():
    g = governor()
    assert g.project_report_cost("deepinfra:m", 100_000, 20_000) == pytest.approx(1.25 * (0.1 + 0.04))


def test_callbacks_only_exist_inside_the_scope_and_need_a_price():
    assert hm.callbacks_for("deepinfra:m") == []
    g = governor()
    with hm.meter_scope(g):
        assert len(hm.callbacks_for("DeepInfra:m")) == 1
        assert len(hm.callbacks_for("openrouter:any/slug")) == 1       # OpenRouter reports its own cost
        with pytest.raises(ValueError, match="--price"):
            hm.callbacks_for("together:unpriced")


def test_get_model_attaches_the_meter_to_a_hosted_model(monkeypatch):
    monkeypatch.setenv("DEEPINFRA_API_KEY", "k-test-123456")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k-or-123456")
    g = governor()
    with hm.meter_scope(g):
        m = cfg.get_model("deepinfra:m")
        o = cfg.get_model("openrouter:vendor/model:free")
    assert any(isinstance(c, hm.HostedMeterCallback) for c in m.callbacks)
    assert o.model_name == "vendor/model:free" and o.extra_body == {"usage": {"include": True}}
    assert str(o.openai_api_base).startswith("https://openrouter.ai/api/v1")
    assert not getattr(cfg.get_model("deepinfra:m"), "callbacks", None)          # outside the scope: unmetered


def test_model_factory_meters_only_when_asked(monkeypatch):
    from deep_research.models import ModelFactory
    monkeypatch.setenv("DEEPINFRA_API_KEY", "k-test-123456")
    base = RunConfig(supervisor_model_fallback_chain=["deepinfra:m"], disable_model_fallback=True,
                     hosted_prices_per_mtok={"deepinfra:m": [1.0, 2.0]})
    off = ModelFactory(base)
    off.get("deepinfra:m")
    assert off.usage() is None                                               # web UI behaviour: untouched
    on = ModelFactory(RunConfig(**{**base.to_dict(), "hosted_metering": True}))
    m = on.get("deepinfra:m")
    assert any(isinstance(c, hm.HostedMeterCallback) for c in m.callbacks)
    assert on.governor.prices["deepinfra:m"] == pytest.approx((1e-6, 2e-6))
    assert on.report_cost_estimator(None).project(100_000, 0) == pytest.approx(1.25 * 0.1)


def test_usage_of_reads_langchain_results():
    res = LLMResult(generations=[[ChatGeneration(message=msg(7, 3))]], llm_output={"token_usage": {"cost": "0.5"}})
    assert hm._usage_of(res) == (7, 3, 0.5)
    assert hm._usage_of(LLMResult(generations=[[]])) == (0, 0, None)


# ── start-up: prices and keys ──────────────────────────────────────────────
def parse(*argv):
    p = argparse.ArgumentParser()
    p.add_argument("--resume")
    co.add_arguments(p)
    return p.parse_args(list(argv))


def test_price_flag_parsing():
    assert co.parse_prices(["DeepInfra:a/b=0.38/0.89"]) == {"deepinfra:a/b": [0.38, 0.89]}
    for bad in ("deepinfra:a", "deepinfra:a=1", "deepinfra:a=x/y", "sonnet-5.5=1/2", "deepinfra:a=-1/2"):
        with pytest.raises(co.CliError):
            co.parse_prices([bad])


def test_a_third_party_chain_turns_metering_on_and_carries_the_prices():
    c = co.build_config(parse("--supervisor-model", "deepinfra:m", "--price", "deepinfra:m=1/2"))
    assert c.hosted_metering is True and c.hosted_prices_per_mtok == {"deepinfra:m": [1.0, 2.0]}
    assert co.build_config(parse("--supervisor-model", "haiku-5.5")).hosted_metering is False


def test_prices_come_from_the_owner_first_then_the_provider_list(monkeypatch):
    monkeypatch.setenv("DEEPINFRA_API_KEY", "k-test-123456")
    monkeypatch.setenv("TOGETHER_API_KEY", "k-tg-123456")
    c = co.build_config(parse("--supervisor-model", "deepinfra:m", "--subagent-model", "together:n",
                              "--price", "deepinfra:m=1/2"))
    seen = []

    def lookup(name):
        seen.append(name)
        return (0.3, 0.6)
    out, problems = co.prepare_hosted(c, lookup)
    assert seen == ["together:n"] and problems == []
    assert out.hosted_prices_per_mtok == {"deepinfra:m": [1.0, 2.0], "together:n": [0.3, 0.6]}


def test_a_model_with_no_price_or_key_is_refused_before_any_spend(monkeypatch):
    monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
    c = co.build_config(parse("--subagent-model", "fireworks:x"))
    with pytest.raises(co.CliError) as e:
        co.prepare_hosted(c, lambda name: None)
    assert "--price 'fireworks:x=" in str(e.value) and "FIREWORKS_API_KEY" in str(e.value)
    out, problems = co.prepare_hosted(c, lambda name: None, strict=False)          # --show-config only reports them
    assert len(problems) == 2


def test_openrouter_needs_no_price(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k-or-123456")
    c = co.build_config(parse("--subagent-model", "openrouter:vendor/m"))
    out, problems = co.prepare_hosted(c, lambda name: None)
    assert problems == [] and out.hosted_metering is True


def test_price_lists_are_read_from_the_providers_formats():
    assert hm._price_of("deepinfra", {"metadata": {"pricing": {"input_tokens": 0.3, "output_tokens": 0.9}}}) == (0.3, 0.9)
    assert hm._price_of("openrouter", {"pricing": {"prompt": "0.000001", "completion": "0.000002"}}) == pytest.approx((1.0, 2.0))
    assert hm._price_of("together", {"pricing": {"input": 0.2, "output": 0.4}}) == (0.2, 0.4)
    assert hm._price_of("deepinfra", {"metadata": {}}) is None


def test_usage_text_labels_third_party_rows():
    from deep_research.providers.claude_code import format_usage
    g = governor()
    run_call(hm.HostedMeterCallback("deepinfra:m", g), msg(10, 10))
    text = format_usage(g.stats())
    assert "deepinfra:m: 1 calls" in text and "(estimated)" in text and "plan window" not in text
