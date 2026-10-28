"""Search-tools panel: checkbox -> engine mapping, key states, and the Ready line.

Also pins how the engine treats web_search_engine="none" (web search off), which the
panel relies on and which needed no engine change.
"""

import pytest

pytest.importorskip("gradio")

from deep_research.integration import validate_credentials
from deep_research.models import Credentials
from deep_research.run_config import RunConfig
from deep_research.runtime import RuntimeContext, runtime_scope

from webui import app as ui

DS = "sk-deepseek-secret"


@pytest.fixture(autouse=True)
def _no_server_keys(monkeypatch):
    for name, _ in ui.KEY_FIELDS:
        monkeypatch.delenv(name, raising=False)


def keys(**by_name):
    return [by_name.get(name, "") for name, _ in ui.KEY_FIELDS]


# ── Engine behaviour for "none" (no engine change was needed) ───────────

def test_finalize_accepts_none_and_keeps_it():
    assert RunConfig(web_search_engine="none").finalize().web_search_engine == "none"


def test_validate_credentials_asks_for_no_search_key_when_web_search_is_off():
    creds = Credentials({"DEEPSEEK_API_KEY": DS})
    off = validate_credentials(RunConfig(web_search_engine="none"), creds)
    assert off.ok and off.missing_required == []
    # The same setup with a search engine selected does need its key.
    assert validate_credentials(RunConfig(web_search_engine="exa"), creds).missing_required == [
        "EXA_API_KEY"]
    assert validate_credentials(RunConfig(web_search_engine="both"), creds).missing_required == [
        "EXA_API_KEY", "TAVILY_API_KEY"]


def _search_tool_names(engine, creds):
    from deep_research.agents.base import _runtime_platform_tools, _runtime_search_tools

    ctx = RuntimeContext(run_id="r", thread_id="t", credentials=creds,
                         config=RunConfig(web_search_engine=engine).finalize())
    with runtime_scope(ctx):
        direct = sorted(t.name for t in _runtime_search_tools())
        web = {t.name for t in _runtime_platform_tools(ctx, "web")}
        general = {t.name for t in _runtime_platform_tools(ctx, "general")}
    assert {"tavily_search", "exa_deep_search"} & web == set(direct)
    assert {"tavily_search", "exa_deep_search"} & general == set(direct)
    return direct


@pytest.mark.parametrize("engine,expected", [
    ("none", []), ("", []), ("exa", ["exa_deep_search"]), ("tavily", ["tavily_search"]),
    ("both", ["exa_deep_search", "tavily_search"])])
def test_runtime_search_tools_follow_the_engine_even_with_both_keys_present(engine, expected):
    creds = Credentials({"TAVILY_API_KEY": "t", "EXA_API_KEY": "e"})
    assert _search_tool_names(engine, creds) == expected


# ── Checkbox -> engine mapping ──────────────────────────────────────────

@pytest.mark.parametrize("web_on,tavily,exa,engine", [
    (True, True, True, "both"),
    (True, True, False, "tavily"),
    (True, False, True, "exa"),
    (True, False, False, "none"),
    (False, True, True, "none"),       # master switch wins
    (False, False, False, "none"),
])
def test_engine_from_selection(web_on, tavily, exa, engine):
    assert ui.engine_from_selection(web_on, tavily, exa) == engine


@pytest.mark.parametrize("engine", ["both", "tavily", "exa", "none"])
def test_engine_and_selection_round_trip(engine):
    assert ui.engine_from_selection(*ui.engine_to_selection(engine)) == engine


def test_every_preset_sets_the_checkboxes_for_its_engine():
    for name, preset in ui.PRESETS.items():
        *_, web_on, tavily, exa = ui.apply_preset(name)
        assert ui.engine_from_selection(web_on, tavily, exa) == preset["engine"]
    assert ui.apply_preset(ui.DEFAULT_PRESET)[-3:] == (True, False, True)   # engine="exa"


def test_build_config_still_takes_the_engine_string():
    for engine in ("exa", "tavily", "both", "none"):
        cfg = ui.build_config(["deepseek-v4-flash"], ["deepseek-v4-flash"],
                              ["deepseek-v4-flash"], False, "auto", 5, 5, 3, 2,
                              ["ResearchWeb"], engine)
        assert cfg.web_search_engine == engine


# ── Key state next to each tool ─────────────────────────────────────────

def test_search_key_status(monkeypatch):
    assert ui.search_key_status("EXA_API_KEY", "") == "no key"
    assert ui.search_key_status("EXA_API_KEY", "   ") == "no key"
    assert ui.search_key_status("EXA_API_KEY", "typed") == "key entered"
    # A server-side key is neither used nor reported: web runs use typed keys only.
    monkeypatch.setenv("EXA_API_KEY", "server")
    assert ui.search_key_status("EXA_API_KEY", "") == "no key"
    assert ui.search_key_status("EXA_API_KEY", "typed") == "key entered"


# ── Ready / Missing keys line ───────────────────────────────────────────

def status(**over):
    args = dict(sup=["deepseek-v4-flash"], sub=["deepseek-v4-flash"], draft=["deepseek-v4-flash"],
                route_or=False, agents=["ResearchWeb"], web_on=True, use_tavily=False,
                use_exa=True, key_values=keys(DEEPSEEK_API_KEY=DS, EXA_API_KEY="exa-secret"),
                extra="")
    args.update(over)
    return ui.live_status(args["sup"], args["sub"], args["draft"], args["route_or"],
                          args["agents"], args["web_on"], args["use_tavily"], args["use_exa"],
                          *args["key_values"], args["extra"])


def test_ready_when_all_needed_keys_are_present():
    tavily, exa, line = status()
    assert (tavily, exa, line) == ("no key", "key entered", "Ready")


def test_missing_keys_are_named_for_the_current_selection():
    _, _, line = status(use_tavily=True, key_values=keys(DEEPSEEK_API_KEY=DS))
    assert line == "Missing keys: EXA_API_KEY, TAVILY_API_KEY"
    _, _, line = status(key_values=keys())
    # The engine lists alternative spellings too (DEEPSEEK_KEY); only names, never values.
    assert line.startswith("Missing keys: DEEPSEEK_API_KEY") and line.endswith("EXA_API_KEY")


def test_web_search_off_needs_no_search_key():
    for kwargs in (dict(web_on=False), dict(use_exa=False)):
        _, _, line = status(key_values=keys(DEEPSEEK_API_KEY=DS), **kwargs)
        assert line == "Ready (web search is off)"


def test_hosted_provider_models_ask_for_their_own_key():
    chain = ["fireworks:accounts/fireworks/models/llama-v3p3-70b-instruct"]
    base = keys(DEEPSEEK_API_KEY=DS, EXA_API_KEY="e")
    assert status(sup=chain, key_values=base)[2] == "Missing keys: FIREWORKS_API_KEY"
    with_key = keys(DEEPSEEK_API_KEY=DS, EXA_API_KEY="e", FIREWORKS_API_KEY="fw")
    assert status(sup=chain, key_values=with_key)[2] == "Ready"


def test_a_server_side_key_does_not_count(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "server-exa")
    tavily, exa, line = status(key_values=keys(DEEPSEEK_API_KEY=DS))
    assert (tavily, exa, line) == ("no key", "no key", "Missing keys: EXA_API_KEY")
    # Typing the key makes the same setup ready.
    assert status(key_values=keys(DEEPSEEK_API_KEY=DS, EXA_API_KEY="typed-exa"))[2] == "Ready"


def test_bad_selections_give_a_short_message_not_an_error():
    assert "model" in status(sup=[])[2].lower()
    assert "Settings problem" in status(agents=[])[2]


def test_status_never_contains_key_values():
    secrets = {name: f"SECRET-{name}" for name, _ in ui.KEY_FIELDS}
    out = status(key_values=keys(**secrets), extra="REDDIT_CLIENT_ID=SECRET-REDDIT")
    assert "SECRET" not in repr(out)
    out = status(use_tavily=True, sup=["together:x"], key_values=keys(DEEPSEEK_API_KEY="SECRET-1"),
                 extra="PUBMED_EMAIL=SECRET-2")
    assert "SECRET" not in repr(out)


def test_status_inputs_are_wired_to_the_live_events():
    """The Ready line updates on blur of every key box and on change of the other controls."""
    demo = ui.build_demo()
    status_fns = [f for f in demo.fns.values() if f.fn is ui.live_status]
    triggers = {(t[0], t[1]) for f in status_fns for t in f.targets}
    blurred = {demo.blocks[i].label for i, ev in triggers if ev == "blur"}
    changed = {demo.blocks[i].label for i, ev in triggers if ev == "change"}
    assert {label for _, label in ui.KEY_FIELDS} <= blurred
    assert {"Supervisor", "Sub-agents", "Draft and brief", "Agents", "Tavily", "Exa",
            "Web search on (master switch)"} <= changed
