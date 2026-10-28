"""Offline tests for the Gradio web UI (no provider or search calls)."""

import asyncio
import os
from types import SimpleNamespace

import pytest

gr = pytest.importorskip("gradio")

import deep_research.integration as integration
from conftest import FINAL_TEXT, FakeModelFactory

from webui import app as ui

REQ = SimpleNamespace(session_hash="test-session")


@pytest.fixture(autouse=True)
def _fake_models(monkeypatch):
    monkeypatch.setattr(integration, "ModelFactory", FakeModelFactory)
    # The engine falls back to the process environment for keys; keep tests hermetic.
    for name, _ in ui.KEY_FIELDS:
        monkeypatch.delenv(name, raising=False)
    ui._RUNNING.clear()
    ui._TOKENS.clear()


def key_list(**by_name) -> list[str]:
    """Key box values in KEY_FIELDS order, given as NAME=value (others empty)."""
    unknown = set(by_name) - {n for n, _ in ui.KEY_FIELDS}
    assert not unknown, unknown
    return [by_name.get(name, "") for name, _ in ui.KEY_FIELDS]


def _args(question="What is new in sodium-ion batteries?", **over):
    base = dict(
        question=question, sup=["deepseek-v4-flash"], sub=["deepseek-v4-flash"],
        draft=["deepseek-v4-flash"], route_or=False, thinking="auto", minutes=5,
        iters=5, searches=3, concurrency=2, agents=["ResearchWeb"],
        web_on=True, use_tavily=False, use_exa=True,
        keys=key_list(DEEPSEEK_API_KEY="sk-deepseek-secret", EXA_API_KEY="exa-secret"),
        extra="")
    base.update(over)
    return base


async def _run(**over):
    a = _args(**over)
    outputs = []
    async for out in ui.run_ui(
            a["question"], a["sup"], a["sub"], a["draft"], a["route_or"], a["thinking"],
            a["minutes"], a["iters"], a["searches"], a["concurrency"], a["agents"],
            a["web_on"], a["use_tavily"], a["use_exa"], *a["keys"], a["extra"], request=REQ):
        outputs.append(out)
        # The report file exists only until Gradio has taken it (the generator's next step).
        if out[-1]:
            _downloads.append(open(out[-1], encoding="utf-8").read())
    return outputs


_downloads: list = []


def test_parse_extra_keys_ignores_comments_and_blanks():
    text = "# note\nREDDIT_CLIENT_ID = abc\n\nbad line\nPUBMED_EMAIL=me@example.com\nEMPTY="
    expected = {"REDDIT_CLIENT_ID": "abc", "PUBMED_EMAIL": "me@example.com"}
    assert ui.parse_extra_keys(text) == expected
    assert ui.parse_extra_keys("REDDIT_CLIENT_ID=abc; PUBMED_EMAIL=me@example.com") == expected


def test_build_config_applies_ui_choices_and_server_cap(monkeypatch):
    monkeypatch.setattr(ui, "MAX_MINUTES", 10)
    cfg = ui.build_config(["deepseek-v4-pro"], ["deepseek-v4-flash"], ["gpt-5.2"], True, "off",
                          240, 7, 4, 2, ["ResearchWeb", "ResearchArxiv"], "tavily")
    assert cfg.supervisor_model_fallback_chain == ["deepseek-v4-pro"]
    assert cfg.draft_report_model_fallback_chain == ["gpt-5.2"]
    assert cfg.route_via_openrouter is True and cfg.thinking_mode == "off"
    assert cfg.research_time_max_minutes == 10          # visitor cannot exceed the operator cap
    assert cfg.enabled_agents == ["ResearchWeb", "ResearchArxiv"]
    assert cfg.web_search_engine == "tavily"
    assert cfg.output_mode == "none" and cfg.save_report_to_file is False


def test_completed_run_streams_progress_and_returns_report(tmp_path):
    outputs = asyncio.run(_run())
    status, log, report, sources, download = outputs[-1]
    assert status.startswith("Completed")
    assert FINAL_TEXT in report
    assert "run_started" in log and "run_completed" in log
    assert download and _downloads[-1] == FINAL_TEXT
    assert not os.path.exists(download)                  # the per-run dir is removed after the yield
    assert not ui._RUNNING and not ui._TOKENS           # session state is cleaned up


def test_keys_never_reach_outputs():
    outputs = asyncio.run(_run())
    blob = repr(outputs)
    assert "sk-deepseek-secret" not in blob and "exa-secret" not in blob


def test_missing_key_is_reported_before_the_run_starts():
    outputs = asyncio.run(_run(keys=key_list()))
    assert len(outputs) == 1
    assert "Missing keys" in outputs[0][0] and "DEEPSEEK_API_KEY" in outputs[0][0]


def test_empty_question_and_empty_roles_are_rejected():
    assert "question" in asyncio.run(_run(question="  "))[0][0].lower()
    assert "model" in asyncio.run(_run(sub=[]))[0][0].lower()
    assert "agent" in asyncio.run(_run(agents=[]))[0][0].lower()


def test_second_run_in_same_session_is_refused():
    ui._RUNNING.add(REQ.session_hash)
    outputs = asyncio.run(_run())
    assert "already in progress" in outputs[0][0]


def test_stop_cancels_the_session_token():
    assert ui.stop_run(REQ) == "Nothing is running."
    token = ui.CancellationToken()
    ui._TOKENS[REQ.session_hash] = token
    ui.stop_run(REQ)
    assert token.is_cancelled()


def test_key_parameters_match_key_fields():
    # run_ui takes one named parameter per key box, numbered in KEY_FIELDS order.
    import inspect
    names = [n for n in inspect.signature(ui.run_ui).parameters if n.startswith("key")]
    assert names == [f"key{i}" for i in range(1, len(ui.KEY_FIELDS) + 1)]


def test_key_fields_include_the_hosted_providers_and_search_keys():
    names = [n for n, _ in ui.KEY_FIELDS]
    assert names == [
        "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
        "MIMO_API_KEY", "DEEPINFRA_API_KEY", "TOGETHER_API_KEY", "FIREWORKS_API_KEY",
        "TAVILY_API_KEY", "EXA_API_KEY"]
    assert len(set(names)) == len(names)


def _run_button_fn(demo):
    return next(f for f in demo.fns.values() if f.fn is ui.run_ui)


def _values_for_wiring(bf, **overrides):
    """One value per input component of the Run button, in the order Gradio will send them."""
    names_by_label = {label: name for name, label in ui.KEY_FIELDS}
    values = []
    for comp in bf.inputs:
        label = getattr(comp, "label", None)
        if label in overrides:
            values.append(overrides[label])
        elif label in names_by_label and type(comp).__name__ == "Textbox":
            values.append("val-" + names_by_label[label])
        elif label == "Research question":
            values.append("q?")
        elif label and label.startswith("Other keys"):
            values.append("")
        else:
            values.append(comp.value)
    return values


def test_run_button_wiring_maps_each_key_box_to_its_credential(monkeypatch):
    """Drives run_ui with the values in the order the real Run button wires them."""
    seen = {}
    real_build = ui.build_credentials

    def spy_credentials(key_values, extra, *ls):
        creds = real_build(key_values, extra, *ls)
        seen["keys"] = dict(creds.keys)
        return creds

    def stop_at_preflight(cfg, creds):
        seen["engine"] = cfg.web_search_engine
        return "stop here"

    monkeypatch.setattr(ui, "build_credentials", spy_credentials)
    monkeypatch.setattr(ui, "preflight_text", stop_at_preflight)
    bf = _run_button_fn(ui.build_demo())

    async def go(values):
        return [o async for o in ui.run_ui(*values, request=REQ)]

    out = asyncio.run(go(_values_for_wiring(bf)))
    assert out[-1][0] == "stop here"
    assert seen["keys"] == {name: "val-" + name for name, _ in ui.KEY_FIELDS}
    assert seen["engine"] == "exa"                      # the default preset

    # Each search checkbox reaches the engine through its own parameter.
    asyncio.run(go(_values_for_wiring(bf, **{"Tavily": True, "Exa": False})))
    assert seen["engine"] == "tavily"
    asyncio.run(go(_values_for_wiring(bf, **{"Tavily": True, "Exa": True})))
    assert seen["engine"] == "both"
    asyncio.run(go(_values_for_wiring(bf, **{"Web search on (master switch)": False})))
    assert seen["engine"] == "none"


def test_key_boxes_are_password_fields_in_the_right_groups():
    demo = ui.build_demo()
    boxes = {b.label: b for b in demo.blocks.values()
             if type(b).__name__ == "Textbox" and b.label in dict((l, n) for n, l in ui.KEY_FIELDS)}
    assert set(boxes) == {label for _, label in ui.KEY_FIELDS}
    assert all(b.type == "password" and b.max_lines == 1 for b in boxes.values())


def test_hosted_provider_key_reaches_the_engine_credentials():
    outputs = asyncio.run(_run(
        sup=["together:openai/gpt-oss-120b"], sub=["together:openai/gpt-oss-120b"],
        draft=["together:openai/gpt-oss-120b"],
        keys=key_list(TOGETHER_API_KEY="tg-secret", EXA_API_KEY="exa-secret")))
    assert outputs[-1][0].startswith("Completed")
    assert "tg-secret" not in repr(outputs)


def test_request_param_is_a_gradio_injectable_request():
    import inspect
    import typing
    p = inspect.signature(ui.run_ui).parameters["request"]
    assert p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert typing.get_type_hints(ui.run_ui)["request"] is gr.Request


def test_demo_builds_with_all_controls():
    demo = ui.build_demo()
    labels = {getattr(b, "label", None) for b in demo.blocks.values()}
    assert {"Supervisor", "Sub-agents", "Draft and brief", "Research question"} <= labels
