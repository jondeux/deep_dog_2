"""LangSmith controls in the web UI, and secret redaction in failure text.

Direct-path tests call the handlers; the real launched app is covered in
tests/test_webui_e2e.py and tests/test_webui_langsmith_e2e.py. Nothing here reaches a
provider, a search service or LangSmith: models are fakes and ``langsmith.Client`` is the
recording fake from tests/test_langsmith_tracing.py.
"""

import asyncio
import inspect
import json
import os
from types import SimpleNamespace

import langsmith
import pytest

gr = pytest.importorskip("gradio")

import deep_research.integration as integration
from conftest import FakeLLM, FakeModelFactory
from deep_research.integration import ResearchResult
from test_langsmith_tracing import RecordingClient, _Factory

from webui import app as ui

REQ = SimpleNamespace(session_hash="ls-session")
LS_KEY = "lsv2_pt_0123456789abcdef0123456789abcdef_01234567"
DS_KEY = "sk-deepseek-secret"
EXA_KEY = "exa-secret-key"
OTHER_KEY = "reddit-secret-xyz"
US = "https://api.smith.langchain.com"
EU = "https://eu.api.smith.langchain.com"


@pytest.fixture(autouse=True)
def _isolation(monkeypatch):
    monkeypatch.setattr(integration, "ModelFactory", FakeModelFactory)
    for name in list(os.environ):
        if name.startswith(("LANGSMITH_", "LANGCHAIN_")) or name in dict(ui.KEY_FIELDS):
            monkeypatch.delenv(name, raising=False)
    RecordingClient.instances = []
    monkeypatch.setattr(langsmith, "Client", RecordingClient)
    ui._RUNNING.clear()
    ui._TOKENS.clear()


def keys(**by_name):
    return [by_name.get(name, "") for name, _ in ui.KEY_FIELDS]


def _args(**over):
    base = dict(
        question="What is new in sodium-ion batteries?", sup=["deepseek-v4-flash"],
        sub=["deepseek-v4-flash"], draft=["deepseek-v4-flash"], route_or=False, thinking="auto",
        minutes=5, iters=5, searches=3, concurrency=2, agents=["ResearchWeb"], web_on=True,
        use_tavily=False, use_exa=True, keys=keys(DEEPSEEK_API_KEY=DS_KEY, EXA_API_KEY=EXA_KEY),
        extra="", ls_on=False, ls_key="", ls_project="", ls_region="US")
    base.update(over)
    return base


async def _run(**over):
    a = _args(**over)
    return [out async for out in ui.run_ui(
        a["question"], a["sup"], a["sub"], a["draft"], a["route_or"], a["thinking"],
        a["minutes"], a["iters"], a["searches"], a["concurrency"], a["agents"], a["web_on"],
        a["use_tavily"], a["use_exa"], *a["keys"], a["extra"],
        a["ls_on"], a["ls_key"], a["ls_project"], a["ls_region"], request=REQ)]


def run(**over):
    return asyncio.run(_run(**over))


# ── project name, region, config and credentials builders ──────────────────

@pytest.mark.parametrize("raw, expected", [
    ("  my project  ", "my project"),
    ("", "deep-dog-2"), ("   ", "deep-dog-2"), (None, "deep-dog-2"),
    ("a" * 200, "a" * 80),
    ("x" * 79 + " " + "tail", "x" * 79),            # cut, then trimmed again
    ("line1\nline2\t\x00end", "line1 line2  end"),  # control characters never survive
])
def test_clean_project(raw, expected):
    assert ui.clean_project(raw) == expected
    assert len(ui.clean_project(raw)) <= 80


def test_region_mapping_is_fixed_and_rejects_anything_else():
    assert ui.LANGSMITH_REGIONS == {"US": US, "EU": EU}
    assert ui.langsmith_endpoint("US") == US and ui.langsmith_endpoint("EU") == EU
    for bad in ("https://evil.example", "us", "", None, "US https://evil.example"):
        with pytest.raises(ValueError):
            ui.langsmith_endpoint(bad)


def test_build_config_carries_the_tracing_flag_and_the_cleaned_project():
    base = (["deepseek-v4-flash"],) * 3 + (False, "auto", 5, 5, 3, 1, ["ResearchWeb"], "exa")
    off = ui.build_config(*base)
    assert off.langsmith_tracing is False
    on = ui.build_config(*base, True, "  proj  ")
    assert on.langsmith_tracing is True and on.langsmith_project == "proj"
    assert ui.build_config(*base, True, "").langsmith_project == "deep-dog-2"
    assert ui.build_config(*base, 0, "p").langsmith_tracing is False


@pytest.mark.parametrize("region, endpoint", [("US", US), ("EU", EU)])
def test_build_credentials_maps_region_and_adds_ls_keys_only_when_ticked(region, endpoint):
    creds = ui.build_credentials(keys(DEEPSEEK_API_KEY=DS_KEY), "", True, f" {LS_KEY} ", region)
    assert creds.keys["LANGSMITH_API_KEY"] == LS_KEY
    assert creds.keys["LANGSMITH_ENDPOINT"] == endpoint
    assert creds.keys["DEEPSEEK_API_KEY"] == DS_KEY
    # Unticked, or ticked without a key: no LangSmith entries at all.
    for on, key in ((False, LS_KEY), (True, ""), (True, "   ")):
        c = ui.build_credentials(keys(DEEPSEEK_API_KEY=DS_KEY), "", on, key, region)
        assert not [k for k in c.keys if k.startswith("LANGSMITH")]


def test_other_keys_box_cannot_set_the_langsmith_endpoint_or_key():
    extra = ("LANGSMITH_ENDPOINT=http://169.254.169.254; LANGSMITH_API_KEY=smuggled-key; "
             f"PERPLEXITY_API_KEY={OTHER_KEY}")
    creds = ui.build_credentials(keys(), extra, True, LS_KEY, "EU")
    assert creds.keys["LANGSMITH_ENDPOINT"] == EU            # from the region mapping only
    assert creds.keys["LANGSMITH_API_KEY"] == LS_KEY         # from the dedicated box only
    assert creds.keys["PERPLEXITY_API_KEY"] == OTHER_KEY                # other extra keys still work
    # Ticked with no key typed: the smuggled one is not accepted either.
    none = ui.build_credentials(keys(), extra, True, "", "US")
    assert "LANGSMITH_ENDPOINT" not in none.keys and "LANGSMITH_API_KEY" not in none.keys


def test_invalid_region_stops_the_run_with_a_settings_message():
    out = run(ls_on=True, ls_key=LS_KEY, ls_region="https://evil.example")
    assert len(out) == 1 and out[0][0].startswith("Settings problem")
    assert RecordingClient.instances == []
    assert LS_KEY not in repr(out)


# ── run_ui parameters and wiring ─────────────────────────────────────────

def test_run_ui_signature_order_is_unchanged_plus_the_four_ls_parameters():
    names = list(inspect.signature(ui.run_ui).parameters)
    assert names[:14] == ["question", "sup", "sub", "draft", "route_or", "thinking", "minutes",
                         "iters", "searches", "concurrency", "agents", "web_on", "use_tavily",
                         "use_exa"]
    assert names[14:24] == [f"key{i}" for i in range(1, 11)]
    assert names[24:] == ["extra", "ls_on", "ls_key", "ls_project", "ls_region", "request"]


def _run_button_fn(demo):
    return next(f for f in demo.fns.values() if f.fn is ui.run_ui)


def test_each_new_control_reaches_run_ui_then_config_and_credentials(monkeypatch):
    """Distinct values in every LangSmith control: a swapped parameter (key <-> project,
    region <-> project, ...) changes what the engine receives and fails this test."""
    seen = {}
    real_config, real_creds = ui.build_config, ui.build_credentials

    def spy_config(*a, **kw):
        cfg = real_config(*a, **kw)
        seen["tracing"], seen["project"] = cfg.langsmith_tracing, cfg.langsmith_project
        return cfg

    def spy_creds(*a, **kw):
        creds = real_creds(*a, **kw)
        seen["keys"] = dict(creds.keys)
        return creds

    monkeypatch.setattr(ui, "build_config", spy_config)
    monkeypatch.setattr(ui, "build_credentials", spy_creds)
    monkeypatch.setattr(ui, "preflight_text", lambda cfg, creds: "stop here")
    demo = ui.build_demo()
    bf = _run_button_fn(demo)
    by_label = {getattr(c, "label", None): c for c in bf.inputs}
    assert type(by_label["LangSmith API key"]).__name__ == "Textbox"
    wired = {"Trace this run in LangSmith": True, "LangSmith API key": "typed-ls-key-123",
             "LangSmith project": "typed-project", "LangSmith region": "EU"}
    names_by_label = {label: name for name, label in ui.KEY_FIELDS}
    values = []
    for comp in bf.inputs:
        label = getattr(comp, "label", None)
        if label in wired:
            values.append(wired[label])
        elif label in names_by_label and type(comp).__name__ == "Textbox":
            values.append("val-" + names_by_label[label])
        elif label == "Research question":
            values.append("q?")
        elif label and label.startswith("Other keys"):
            values.append("")
        else:
            values.append(comp.value)

    async def go():
        return [o async for o in ui.run_ui(*values, request=REQ)]

    assert asyncio.run(go())[-1][0] == "stop here"
    assert seen["tracing"] is True and seen["project"] == "typed-project"
    assert seen["keys"]["LANGSMITH_API_KEY"] == "typed-ls-key-123"
    assert seen["keys"]["LANGSMITH_ENDPOINT"] == EU
    assert seen["keys"]["DEEPSEEK_API_KEY"] == "val-DEEPSEEK_API_KEY"


def test_ls_controls_are_the_last_inputs_before_request_in_wiring_order():
    bf = _run_button_fn(ui.build_demo())
    labels = [getattr(c, "label", None) for c in bf.inputs]
    assert labels[-4:] == ["Trace this run in LangSmith", "LangSmith API key",
                           "LangSmith project", "LangSmith region"]
    assert labels[-5].startswith("Other keys")


# ── the controls themselves ──────────────────────────────────────────────

def _blocks(demo, type_name, label):
    return next(b for b in demo.blocks.values()
                if type(b).__name__ == type_name and getattr(b, "label", None) == label)


def test_tracing_accordion_and_controls():
    demo = ui.build_demo()
    acc = next(b for b in demo.blocks.values()
               if type(b).__name__ == "Accordion" and b.label == "Tracing (LangSmith)")
    assert acc.open is False
    on = _blocks(demo, "Checkbox", "Trace this run in LangSmith")
    assert on.value is False
    key = _blocks(demo, "Textbox", "LangSmith API key")
    assert key.type == "password" and key.max_lines == 1 and not key.value
    project = _blocks(demo, "Textbox", "LangSmith project")
    assert project.value == "deep-dog-2" and project.max_length == 80 and project.type == "text"
    region = _blocks(demo, "Dropdown", "LangSmith region")
    assert [v for _, v in region.choices] == ["US", "EU"] and len(region.choices) == 2
    assert region.allow_custom_value is False and region.value == "US"
    assert [l for l, _ in region.choices] == [f"US ({US})", f"EU ({EU})"]
    texts = " ".join(str(getattr(b, "value", "")) for b in demo.blocks.values()
                     if type(b).__name__ == "Markdown")
    for phrase in ("prompts", "search results", "final report", "LangSmith account",
                   "off unless you tick the box"):
        assert phrase in texts


def test_no_free_text_endpoint_control_exists():
    demo = ui.build_demo()
    for b in demo.blocks.values():
        label = str(getattr(b, "label", "") or "").lower()
        assert "endpoint" not in label and "url" not in label, label
    free_text = [b for b in demo.blocks.values()
                 if type(b).__name__ == "Dropdown" and getattr(b, "allow_custom_value", False)]
    assert {b.label for b in free_text} == {"Supervisor", "Sub-agents", "Draft and brief"}


# ── preflight, live status ───────────────────────────────────────────────

def test_ticked_without_a_key_stops_before_the_run_and_names_the_key(monkeypatch):
    calls = []

    async def spy(*a, **kw):
        calls.append(1)
        raise AssertionError("run must not start")

    monkeypatch.setattr(ui, "run_research", spy)
    out = run(ls_on=True, ls_key="")
    assert len(out) == 1
    assert out[0][0].startswith("Missing keys for this setup")
    assert "LANGSMITH_API_KEY" in out[0][0]
    assert calls == [] and not ui._RUNNING
    # Same setup unticked is fine; the preflight only complains when tracing is requested.
    assert ui.preflight_text(ui.build_config(["deepseek-v4-flash"], ["deepseek-v4-flash"],
                                             ["deepseek-v4-flash"], False, "auto", 5, 5, 3, 1,
                                             ["ResearchWeb"], "exa"),
                             ui.build_credentials(keys(DEEPSEEK_API_KEY=DS_KEY, EXA_API_KEY=EXA_KEY),
                                                  "")) == ""


def _status_args(**over):
    a = dict(sup=["deepseek-v4-flash"], sub=["deepseek-v4-flash"], draft=["deepseek-v4-flash"],
             route_or=False, agents=["ResearchWeb"], web_on=True, tavily=False, exa=True,
             keys=keys(DEEPSEEK_API_KEY=DS_KEY, EXA_API_KEY=EXA_KEY), extra="", ls_on=False,
             ls_key="", ls_project="deep-dog-2", ls_region="US")
    a.update(over)
    return [a["sup"], a["sub"], a["draft"], a["route_or"], a["agents"], a["web_on"], a["tavily"],
            a["exa"], *a["keys"], a["extra"], a["ls_on"], a["ls_key"], a["ls_project"],
            a["ls_region"]]


def test_live_status_reports_the_missing_langsmith_key_and_clears_when_typed():
    assert ui.live_status(*_status_args())[2] == "Ready"
    assert ui.live_status(*_status_args(ls_on=True))[2] == "Missing keys: LANGSMITH_API_KEY"
    out = ui.live_status(*_status_args(ls_on=True, ls_key=LS_KEY))
    assert out[2] == "Ready" and LS_KEY not in repr(out)
    # A typed key with the box unticked changes nothing.
    assert ui.live_status(*_status_args(ls_on=False, ls_key=LS_KEY))[2] == "Ready"


def test_live_status_still_accepts_the_old_argument_list_without_ls_controls():
    old = _status_args()[:-4]
    assert ui.live_status(*old)[2] == "Ready"


def test_ls_hint_never_prints_the_key():
    assert ui.langsmith_hint(False, LS_KEY) == "Tracing is off."
    assert "no LangSmith API key" in ui.langsmith_hint(True, "")
    on = ui.langsmith_hint(True, LS_KEY)
    assert LS_KEY not in on and "key entered" in on


# ── status line after a run ──────────────────────────────────────────────

@pytest.mark.parametrize("meta, expected", [
    ({"langsmith": {"requested": True, "active": True, "project": "proj-1"}},
     "LangSmith: tracing active, project proj-1"),
    ({"langsmith": {"requested": True, "active": False, "project": None}},
     "LangSmith: tracing was requested but not active"),
    ({"langsmith": {"requested": False, "active": False, "project": None}}, ""),
    ({}, ""),
])
def test_langsmith_status_line(meta, expected):
    line = ui.langsmith_status_line(SimpleNamespace(run_metadata=meta))
    assert line == expected
    assert "received" not in line and "sent" not in line


def _with_recording_factory(monkeypatch):
    monkeypatch.setattr(integration, "ModelFactory", _Factory)


def test_full_fake_run_with_tracing_records_under_the_typed_project(monkeypatch):
    _with_recording_factory(monkeypatch)
    out = run(ls_on=True, ls_key=LS_KEY, ls_project="  my ui project  ", ls_region="EU",
              extra=f"PERPLEXITY_API_KEY={OTHER_KEY}")
    status = out[-1][0]
    assert status.startswith("Completed")
    assert status.endswith("LangSmith: tracing active, project my ui project")
    assert "received" not in status

    assert len(RecordingClient.instances) == 1
    client = RecordingClient.instances[0]
    # The key is only the client's api_key; the endpoint is the EU mapping.
    assert client.ctor_kwargs == {"api_key": LS_KEY, "api_url": EU,
                                  "omit_traced_runtime_info": True, "workspace_id": ""}
    runs = client.runs()
    assert len(runs) > 10
    assert {kw["session_name"] for kw in runs} == {"my ui project"}
    assert client.flushed()
    blob = json.dumps(client.calls, default=repr)
    for secret in (LS_KEY, DS_KEY, EXA_KEY, OTHER_KEY):
        assert secret not in blob, "a secret reached a recorded LangSmith payload"
        assert secret not in repr(out)


def test_full_fake_run_default_project_us_region(monkeypatch):
    _with_recording_factory(monkeypatch)
    out = run(ls_on=True, ls_key=LS_KEY, ls_project="", ls_region="US")
    assert out[-1][0].endswith("LangSmith: tracing active, project deep-dog-2")
    client = RecordingClient.instances[0]
    assert client.ctor_kwargs == {"api_key": LS_KEY, "api_url": US,
                                  "omit_traced_runtime_info": True, "workspace_id": ""}
    assert {kw["session_name"] for kw in client.runs()} == {"deep-dog-2"}


def test_unticked_run_records_nothing_and_shows_no_langsmith_line(monkeypatch):
    _with_recording_factory(monkeypatch)
    out = run(ls_on=False, ls_key=LS_KEY, ls_project="ignored-project")
    assert out[-1][0].startswith("Completed") and "LangSmith" not in out[-1][0]
    assert RecordingClient.instances == []


def test_requested_but_failing_tracing_says_not_active_and_never_claims_receipt(monkeypatch):
    _with_recording_factory(monkeypatch)

    class Broken(RecordingClient):
        def __init__(self, *a, **kw):
            raise RuntimeError(f"cannot connect with {kw.get('api_key')}")

    monkeypatch.setattr(langsmith, "Client", Broken)
    out = run(ls_on=True, ls_key=LS_KEY)
    status = out[-1][0]
    assert status.startswith("Completed")                       # the research still finished
    assert status.endswith("LangSmith: tracing was requested but not active")
    assert LS_KEY not in repr(out)


def test_langsmith_key_is_not_put_in_the_environment_or_written_to_disk(monkeypatch, tmp_path):
    _with_recording_factory(monkeypatch)
    monkeypatch.chdir(tmp_path)
    before = dict(os.environ)
    out = run(ls_on=True, ls_key=LS_KEY, ls_region="EU")
    assert out[-1][0].startswith("Completed")
    assert dict(os.environ) == before
    assert not any(LS_KEY in v for v in os.environ.values())
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert LS_KEY not in path.read_text(errors="ignore")


# ── redaction of failure text ────────────────────────────────────────────

ALL_SECRETS = [DS_KEY, EXA_KEY, LS_KEY, OTHER_KEY]


def _secret_args():
    return dict(keys=keys(DEEPSEEK_API_KEY=DS_KEY, EXA_API_KEY=EXA_KEY), ls_on=True, ls_key=LS_KEY,
                extra=f"REDDIT_CLIENT_SECRET={OTHER_KEY}")


def _echo(creds):
    return "provider said no: " + " / ".join(creds.keys.values())


def test_scrub_replaces_longest_first_and_ignores_short_values():
    assert ui.scrub("a abcdef-long b abcdef c", ["abcdef", "abcdef-long"]) == "a *** b *** c"
    assert ui.scrub("pin 12345 here", ["12345"]) == "pin 12345 here"      # under 6 chars
    assert ui.scrub("nothing", ["secret-value"]) == "nothing"
    assert ui.typed_secrets(["short", "abcdef", ""], "A=12345;B=longvalue", " lskey-123 ") == [
        "abcdef", "longvalue", "lskey-123"]


def test_run_failed_message_is_redacted_for_every_typed_key(monkeypatch):
    async def exploding(prompt, config=None, runtime=None, credentials=None):
        raise RuntimeError(_echo(credentials))

    monkeypatch.setattr(ui, "run_research", exploding)
    out = run(**_secret_args())
    status = out[-1][0]
    assert status.startswith("The run failed: RuntimeError: provider said no")
    assert status.count("***") == 4
    for secret in ALL_SECRETS:
        assert secret not in repr(out)


def test_run_status_message_is_redacted_even_if_the_engine_text_is_not(monkeypatch):
    async def returning(prompt, config=None, runtime=None, credentials=None):
        return ResearchResult(status="failed", failure="RuntimeError: " + _echo(credentials))

    monkeypatch.setattr(ui, "run_research", returning)
    out = run(**_secret_args())
    assert out[-1][0].startswith("Run failed: RuntimeError: provider said no")
    assert out[-1][0].count("***") == 4
    for secret in ALL_SECRETS:
        assert secret not in repr(out)


def test_settings_problem_message_is_redacted(monkeypatch):
    def broken(*a, **kw):
        raise ValueError("bad setup " + " ".join(ALL_SECRETS))

    monkeypatch.setattr(ui, "build_config", broken)
    out = run(**_secret_args())
    assert out[-1][0] == "Settings problem: bad setup *** *** *** ***"
    # the live status line uses the same scrubbing
    live = ui.live_status(*_status_args(keys=keys(DEEPSEEK_API_KEY=DS_KEY, EXA_API_KEY=EXA_KEY),
                                        ls_on=True, ls_key=LS_KEY,
                                        extra=f"REDDIT_CLIENT_SECRET={OTHER_KEY}"))
    assert live[2].startswith("Settings problem: bad setup ***")
    for secret in ALL_SECRETS:
        assert secret not in repr((out, live))


class _EchoSupervisor(FakeLLM):
    def __init__(self, text):
        super().__init__("supervisor")
        self.text = text

    def with_structured_output(self, schema, **kw):
        return self

    async def ainvoke(self, messages):
        raise RuntimeError(self.text)


def _echo_factory(monkeypatch):
    class Factory(FakeModelFactory):
        def supervisor(self, tools=None, **kw):
            return _EchoSupervisor(_echo(self.credentials))

    monkeypatch.setattr(integration, "ModelFactory", Factory)


def test_engine_redacts_result_failure_for_every_typed_key(monkeypatch):
    _echo_factory(monkeypatch)
    creds = ui.build_credentials(keys(DEEPSEEK_API_KEY=DS_KEY, EXA_API_KEY=EXA_KEY),
                                 f"REDDIT_CLIENT_SECRET={OTHER_KEY}", True, LS_KEY, "US")
    cfg = ui.build_config(["deepseek-v4-flash"], ["deepseek-v4-flash"], ["deepseek-v4-flash"],
                          False, "auto", 5, 5, 3, 1, ["ResearchWeb"], "exa", True, "p")
    result = asyncio.run(integration.run_research("q", config=cfg, credentials=creds))
    assert result.status in ("failed", "partial")     # partial: the draft was salvaged
    assert "RuntimeError: provider said no" in result.failure
    # four typed secrets plus the endpoint value, which the echo also prints
    assert result.failure.count("[REDACTED]") == 5
    for secret in ALL_SECRETS:
        assert secret not in result.failure
        assert secret not in json.dumps(result.to_dict(), default=repr)


def test_engine_failure_through_the_ui_never_shows_a_key(monkeypatch):
    _echo_factory(monkeypatch)
    out = run(**_secret_args())
    status = out[-1][0]
    assert status.startswith(("Run failed: ", "Run partial: "))
    assert "RuntimeError: provider said no" in status and "[REDACTED]" in status
    assert status.endswith("LangSmith: tracing active, project deep-dog-2")
    for secret in ALL_SECRETS:
        assert secret not in repr(out)


# ── server-side LangSmith environment never reaches a visitor's run ────────

def test_build_credentials_forbids_langsmith_env_fallback(monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "operator-env-key-123456")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://operator.example.invalid")
    creds = ui.build_credentials(keys(), "", True, "", "US")
    assert creds.get("LANGSMITH_API_KEY") is None and creds.get("LANGSMITH_ENDPOINT") is None
    typed = ui.build_credentials(keys(), "", True, LS_KEY, "EU")
    assert typed.get("LANGSMITH_API_KEY") == LS_KEY and typed.get("LANGSMITH_ENDPOINT") == EU


def test_ticked_without_key_ignores_the_operator_env_in_status_and_run(monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "operator-env-key-123456")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://operator.example.invalid")
    live = ui.live_status(*_status_args(keys=keys(DEEPSEEK_API_KEY=DS_KEY, EXA_API_KEY=EXA_KEY),
                                        ls_on=True, ls_key=""))
    assert live[2].startswith("Missing keys:") and "LANGSMITH_API_KEY" in live[2]
    out = run(ls_on=True, ls_key="")
    assert "LANGSMITH_API_KEY" in out[-1][0] and "Completed" not in out[-1][0]
    assert RecordingClient.instances == []


# ── "Other keys": allow-list, size and entry caps ─────────────────────────

def test_other_keys_accepts_every_allowed_name():
    text = ";".join(f"{name}=value-of-{i}" for i, name in enumerate(ui.EXTRA_KEY_NAMES))
    assert ui.parse_extra_keys(text) == {
        name: f"value-of-{i}" for i, name in enumerate(ui.EXTRA_KEY_NAMES)}
    assert {"REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USERNAME", "REDDIT_PASSWORD",
            "PUBMED_EMAIL", "SEC_EDGAR_CONTACT_EMAIL", "PERPLEXITY_API_KEY", "DEEPSEEK_KEY",
            "GOOGLE_API_KEY", "ZHIPUAI_API_KEY", "META_API_KEY"} == set(ui.EXTRA_KEY_NAMES)


@pytest.mark.parametrize("bad", ["OPENROUTER_BASE_URL", "HTTP_PROXY", "X_OTHER", "META_BASE_URL"])
def test_other_keys_rejects_unknown_names_without_echoing_the_value(bad):
    secret = "hunter2-secret-value"
    with pytest.raises(ValueError, match=f"unsupported key name {bad}") as err:
        ui.build_credentials(keys(), f"PUBMED_EMAIL=me@example.com;{bad}={secret}")
    assert secret not in str(err.value)
    out = run(extra=f"{bad}={secret}")
    assert out[-1][0] == f"Settings problem: unsupported key name {bad}"
    assert secret not in repr(out)
    live = ui.live_status(*_status_args(extra=f"{bad}={secret}"))
    assert live[2] == f"Settings problem: unsupported key name {bad}"


def test_other_keys_still_silently_drops_langsmith_names():
    parsed = ui.parse_extra_keys("LANGSMITH_API_KEY=x-smuggled;LANGSMITH_PROJECT=p;"
                                 "PUBMED_EMAIL=me@example.com")
    assert parsed == {"PUBMED_EMAIL": "me@example.com"}


def test_other_keys_entry_cap():
    ok = ";".join(f"PUBMED_EMAIL=v{i:05d}" for i in range(ui.EXTRA_KEYS_MAX_ENTRIES))
    assert len(ui.parse_extra_keys(ok)) == 1                      # same name, so one key, 20 entries
    too_many = ok + ";PUBMED_EMAIL=one-more-value"
    with pytest.raises(ValueError, match="more than 20 entries"):
        ui.parse_extra_keys(too_many)
    out = run(extra=too_many)
    assert out[-1][0].startswith("Settings problem: Other keys has more than 20 entries")
    assert "one-more-value" not in repr(out)


def test_other_keys_size_cap_and_scrubbing_stays_bounded():
    big = "PUBMED_EMAIL=" + "x" * ui.EXTRA_KEYS_MAX_CHARS
    with pytest.raises(ValueError, match="longer than 4096 characters"):
        ui.parse_extra_keys(big)
    out = run(extra=big)
    assert out[-1][0].startswith("Settings problem: Other keys is longer than 4096")
    assert "xxxxxxxx" not in repr(out)
    # The non-raising path used for scrubbing is capped too (100k entries must stay cheap).
    huge = ";".join(f"A{i}=value{i:07d}" for i in range(100_000))
    secrets = ui.typed_secrets([], huge)
    assert len(secrets) <= ui.EXTRA_KEYS_MAX_ENTRIES
    assert len(ui.parse_extra_keys(huge, strict=False)) <= ui.EXTRA_KEYS_MAX_ENTRIES


def test_scrub_still_hides_values_of_unsupported_names_typed_in_the_box():
    assert ui.typed_secrets([], "X_OTHER=longvalue-1") == ["longvalue-1"]


def test_semicolon_value_is_not_split_into_a_separately_scrubbed_fragment():
    # The value ends at the ";": the part after it is a separate (dropped) entry, and the
    # floor is applied to the whole parsed value only.
    assert ui.parse_extra_keys("PUBMED_EMAIL=abcdef;ab") == {"PUBMED_EMAIL": "abcdef"}
    assert ui.typed_secrets([], "PUBMED_EMAIL=abc;PERPLEXITY_API_KEY=abcdefgh") == ["abcdefgh"]


# ── overlapping secrets ──────────────────────────────────────────────────

def test_scrub_hides_the_whole_longer_secret_when_a_shorter_one_is_its_prefix():
    text = "x abcdef-SECRET-TAIL y"
    assert ui.scrub(text, ["abcdef", "abcdef-SECRET-TAIL"]) == "x *** y"
    assert ui.scrub(text, ["abcdef-SECRET-TAIL", "abcdef"]) == "x *** y"
    # every prefix of one secret: no set/iteration order may leave a tail behind
    full = "abcdef-SECRET-TAIL"
    prefixes = [full[:n] for n in range(6, len(full) + 1)]
    assert ui.scrub(f"x {full} y", prefixes) == "x *** y"
    assert ui.scrub(f"x {full} y", prefixes[::-1]) == "x *** y"


# ── progress log ─────────────────────────────────────────────────────────

def test_progress_log_lines_are_scrubbed_of_typed_keys(monkeypatch):
    from deep_research.events import ResearchEvent

    async def fake(prompt, config=None, runtime=None, credentials=None):
        runtime.event_sink(ResearchEvent(
            type="tool_call", agent="web", payload={"note": f"provider echoed {DS_KEY} and {OTHER_KEY}"}))
        await asyncio.sleep(1.0)                                  # let at least one poll render
        return ResearchResult(status="completed", final_report="# report")

    monkeypatch.setattr(ui, "run_research", fake)
    out = run(extra=f"REDDIT_CLIENT_SECRET={OTHER_KEY}")
    logs = [o[1] for o in out if o[1]]
    assert len(logs) >= 2, "both the polling log and the final log must be checked"
    for log in logs:
        assert "***" in log and DS_KEY not in log and OTHER_KEY not in log
    assert DS_KEY not in repr(out) and OTHER_KEY not in repr(out)
