"""Offline tests for the Gradio web UI (no provider or search calls)."""

import asyncio
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
    ui._RUNNING.clear()
    ui._TOKENS.clear()


def _args(question="What is new in sodium-ion batteries?", **over):
    base = dict(
        question=question, sup=["deepseek-v4-flash"], sub=["deepseek-v4-flash"],
        draft=["deepseek-v4-flash"], route_or=False, thinking="auto", minutes=5,
        iters=5, searches=3, concurrency=2, agents=["ResearchWeb"], engine="exa",
        keys=["sk-deepseek-secret", "", "", "", "", "exa-secret", ""], extra="")
    base.update(over)
    return base


async def _run(**over):
    a = _args(**over)
    outputs = []
    async for out in ui.run_ui(
            a["question"], a["sup"], a["sub"], a["draft"], a["route_or"], a["thinking"],
            a["minutes"], a["iters"], a["searches"], a["concurrency"], a["agents"],
            a["engine"], *a["keys"], a["extra"], request=REQ):
        outputs.append(out)
    return outputs


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
    assert download and open(download, encoding="utf-8").read() == FINAL_TEXT
    assert not ui._RUNNING and not ui._TOKENS           # session state is cleaned up


def test_keys_never_reach_outputs():
    outputs = asyncio.run(_run())
    blob = repr(outputs)
    assert "sk-deepseek-secret" not in blob and "exa-secret" not in blob


def test_missing_key_is_reported_before_the_run_starts():
    outputs = asyncio.run(_run(keys=["", "", "", "", "", "", ""]))
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
    # run_ui takes one named parameter per key box; keep them in step.
    import inspect
    names = [n for n in inspect.signature(ui.run_ui).parameters if n.startswith("key")]
    assert len(names) == len(ui.KEY_FIELDS)


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
