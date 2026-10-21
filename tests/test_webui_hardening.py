"""Hardening checks for webui/app.py: secret scrubbing before truncation, key-name echo,
and the per-session refresh throttle. Offline; no provider or LangSmith is contacted."""

import asyncio
import os
from types import SimpleNamespace

import pytest

pytest.importorskip("gradio")

import deep_research.integration as integration
from conftest import FakeModelFactory
from deep_research.events import ResearchEvent
from deep_research.integration import ResearchResult

from webui import app as ui

FAKE_KEY = "sk-" + "".join(chr(ord("a") + (i * 11) % 26) for i in range(92)) + "Q"   # 96 chars
assert len(FAKE_KEY) >= 95


def windows(secret, n=8):
    return [secret[i:i + n] for i in range(len(secret) - n + 1)]


def assert_no_fragment(secret, text):
    for w in windows(secret):
        assert w not in text, f"fragment {w!r} of the key is visible"


# ── 1. progress log: scrub the whole value, then cut ────────────────────────

def make_event(value):
    return ResearchEvent(type="tool_call", agent="sub", iteration=2, payload={"detail": value})


def test_format_event_scrubs_before_cutting_a_key_that_straddles_the_cut():
    value = "f" * 60 + FAKE_KEY            # the key starts at char 60, the cut is at 80
    line = ui.format_event(make_event(value), 0.0, [FAKE_KEY])
    assert_no_fragment(FAKE_KEY, line)
    assert "***" in line and "detail=" in line
    assert len(line.split("detail=")[1]) <= 80 + 1


def test_format_event_without_secrets_still_cuts_at_80():
    line = ui.format_event(make_event("y" * 200), 0.0)
    assert line.split("detail=")[1] == "y" * 80


def test_run_ui_progress_log_shows_no_fragment_of_a_straddling_key(monkeypatch):
    monkeypatch.setattr(integration, "ModelFactory", FakeModelFactory)
    ui._RUNNING.clear()
    ui._TOKENS.clear()

    async def fake(question, config=None, credentials=None, runtime=None, **kw):
        runtime.event_sink(make_event("f" * 60 + FAKE_KEY))
        return ResearchResult(status="completed", final_report="# report")

    monkeypatch.setattr(ui, "run_research", fake)
    keys = [""] * len(ui.KEY_FIELDS)
    keys[[n for n, _ in ui.KEY_FIELDS].index("DEEPSEEK_API_KEY")] = FAKE_KEY

    async def go():
        return [o async for o in ui.run_ui(
            "q?", ["deepseek-v4-flash"], ["deepseek-v4-flash"], ["deepseek-v4-flash"], False,
            "auto", 5, 5, 3, 2, ["ResearchWeb"], False, False, False, *keys, "",
            False, "", "", "US", request=SimpleNamespace(session_hash="h1"))]

    outs = asyncio.run(go())
    assert any("detail=" in str(o[1]) for o in outs), "the event line should reach the log"
    assert_no_fragment(FAKE_KEY, repr(outs))


# ── 3. Other keys: unsupported-name message ─────────────────────────────────

@pytest.mark.parametrize("pasted", [
    "sk-ant-api03-AbCdEf0123456789+/AbCdEf0123456789AbCdEf0123456789==",   # base64-ish, ends in ==
    "c2VjcmV0LWtleS1ieXRlcw==",                                          # base64 padding
    "lower_case_name",                                                   # not NAME-shaped
    "A" * 41,                                                            # too long
])
def test_unsupported_key_name_message_does_not_echo_odd_names(pasted):
    secret = "hunter2-secret-value"
    with pytest.raises(ValueError) as err:
        ui.parse_extra_keys(f"{pasted}={secret}")
    assert str(err.value) == "unsupported key name"
    assert pasted[:8] not in str(err.value) and secret not in str(err.value)


def test_unsupported_key_name_still_echoes_a_plain_upper_case_name():
    with pytest.raises(ValueError) as err:
        ui.parse_extra_keys("HTTP_PROXY=http://x")
    assert str(err.value) == "unsupported key name HTTP_PROXY"


# ── 6. refresh throttle ─────────────────────────────────────────────────────

@pytest.fixture
def refresh_env(monkeypatch):
    clock = [5000.0]
    monkeypatch.setattr(ui.model_catalog, "_now", lambda: clock[0])
    ui._REFRESH_LAST.clear()
    calls = []

    def fake_fetch_all(keys, *, transport=None):
        calls.append(dict(keys))
        return ui.model_catalog.fallback_results()

    monkeypatch.setattr(ui.model_catalog, "fetch_all", fake_fetch_all)
    yield clock, calls
    ui._REFRESH_LAST.clear()


def req(sid):
    return SimpleNamespace(session_hash=sid)


def test_second_refresh_inside_three_seconds_makes_no_request_and_keeps_choices(refresh_env):
    clock, calls = refresh_env
    first = ui.refresh_models("k1", "", "", req("a"))
    assert len(calls) == 1 and first[3].startswith("Model lists: ")
    clock[0] += 2.9
    second = ui.refresh_models("k1", "", "", req("a"))
    assert len(calls) == 1                                  # no outbound request
    assert second[3] == "Please wait a few seconds before refreshing again"
    assert second[0]["choices"] == first[0]["choices"]      # previous choices unchanged
    assert second[1]["choices"] == first[1]["choices"] and second[2]["choices"] == first[2]["choices"]
    assert "k1" not in repr(second)


def test_refresh_is_allowed_again_after_three_seconds(refresh_env):
    clock, calls = refresh_env
    ui.refresh_models("", "", "", req("a"))
    clock[0] += 3.0
    out = ui.refresh_models("", "", "", req("a"))
    assert len(calls) == 2 and out[3].startswith("Model lists: ")


def test_throttle_is_per_session(refresh_env):
    clock, calls = refresh_env
    ui.refresh_models("", "", "", req("a"))
    ui.refresh_models("", "", "", req("b"))
    assert len(calls) == 2
    ui.refresh_models("", "", "", req("b"))
    assert len(calls) == 2


def test_throttled_refresh_while_first_is_unfinished_leaves_dropdowns_alone(refresh_env):
    clock, calls = refresh_env
    allowed, _ = ui._refresh_gate("c")                      # a fetch has started, nothing stored yet
    assert allowed
    out = ui.refresh_models("", "", "", req("c"))
    assert len(calls) == 0 and out[3] == "Please wait a few seconds before refreshing again"
    assert "choices" not in out[0] or out[0].get("choices") is None


def test_session_table_is_bounded(refresh_env):
    for i in range(ui._REFRESH_SESSIONS_MAX + 50):
        ui.refresh_models("", "", "", req(f"s{i}"))
    assert len(ui._REFRESH_LAST) <= ui._REFRESH_SESSIONS_MAX
