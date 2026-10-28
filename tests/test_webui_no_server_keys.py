"""Web-UI runs must NEVER use the server's own provider/search keys.

The Space is public: a visitor must not be able to spend the operator's keys. Every test
here sets a distinctive FAKE server key for every provider/search name in the process
environment and checks that the web-UI path (Credentials(env_fallback=False)) ignores
them, while CLI/library behaviour (default Credentials) is unchanged.
"""

import asyncio
import json

import pytest

pytest.importorskip("gradio")

import deep_research.integration as integration
from deep_research import config as cfg
from deep_research import utils
from deep_research.integration import validate_credentials
from deep_research.models import Credentials
from deep_research.run_config import RunConfig
from deep_research.runtime import RuntimeContext, runtime_scope
from deep_research.secrets import env_fallback_allowed, get_secret, scrub_secrets

from webui import app as ui

NAMES = sorted({n for n, _ in ui.KEY_FIELDS} | set(ui.EXTRA_KEY_NAMES)
               | {"GOOGLE_API_KEY", "DEEPSEEK_KEY", "LANGSMITH_API_KEY"})
SERVER = {n: f"SERVER-OWNER-{n}-9f3a1c" for n in NAMES}
TYPED = "typed-visitor-key-77b2"


@pytest.fixture
def server_env(monkeypatch):
    """Fake operator keys for every name (tests only, never real)."""
    for name, value in SERVER.items():
        monkeypatch.setenv(name, value)
    return SERVER


def strict(**keys):
    return Credentials(keys=dict(keys), langsmith_from_env=False, env_fallback=False)


def runtime(creds, engine="both"):
    return RuntimeContext(run_id="r", thread_id="t", credentials=creds,
                          config=RunConfig(web_search_engine=engine).finalize())


def no_server_value(*texts):
    blob = "\n".join(str(t) for t in texts)
    leaked = [n for n, v in SERVER.items() if v in blob]
    assert not leaked, f"server key values leaked: {leaked}"


# ── credential layer ────────────────────────────────────────────────────

def test_default_credentials_still_fall_back_to_the_environment(server_env):
    # CLI / library behaviour is unchanged.
    creds = Credentials()
    assert all(creds.get(n) == SERVER[n] for n in NAMES)
    assert creds.env_fallback is True
    with runtime_scope(runtime(creds)):
        assert get_secret("EXA_API_KEY") == SERVER["EXA_API_KEY"]
        assert env_fallback_allowed() is True
    assert get_secret("EXA_API_KEY") == SERVER["EXA_API_KEY"]    # no runtime at all


def test_strict_credentials_ignore_the_environment_for_every_name(server_env):
    creds = strict(EXA_API_KEY=TYPED)
    assert creds.get("EXA_API_KEY") == TYPED
    assert [n for n in NAMES if n != "EXA_API_KEY" and creds.get(n)] == []
    assert not any(creds.has(n) for n in NAMES if n != "EXA_API_KEY")


def test_get_secret_under_a_strict_runtime_uses_only_typed_keys(server_env):
    with runtime_scope(runtime(strict(TAVILY_API_KEY=TYPED))):
        assert env_fallback_allowed() is False
        assert get_secret("TAVILY_API_KEY") == TYPED
        assert [n for n in NAMES if n != "TAVILY_API_KEY" and get_secret(n)] == []


def test_scrub_secrets_hides_typed_keys_longest_first():
    creds = strict(A_KEY="abcdef", B_KEY="abcdefghij")
    with runtime_scope(runtime(creds)):
        assert scrub_secrets("x abcdefghij y abcdef") == "x *** y ***"
        assert scrub_secrets("short abc") == "short abc"


def test_validate_credentials_counts_only_typed_keys(server_env):
    run_cfg = RunConfig(web_search_engine="both").finalize()
    assert validate_credentials(run_cfg, Credentials()).ok          # server keys count in the CLI
    check = validate_credentials(run_cfg, strict())
    assert not check.ok
    assert {"DEEPSEEK_API_KEY", "EXA_API_KEY", "TAVILY_API_KEY"} <= set(check.missing_required)
    typed = strict(DEEPSEEK_API_KEY=TYPED, EXA_API_KEY=TYPED, TAVILY_API_KEY=TYPED)
    assert validate_credentials(run_cfg, typed).ok


# ── model layer ─────────────────────────────────────────────────────────

def test_model_key_lookup_ignores_the_environment_in_a_strict_scope(server_env):
    for model in ("deepseek-v4-flash", "gpt-5.2", "gemini-3-flash-preview", "mimo-v2.5",
                  "together:meta/llama-x"):
        assert cfg._model_key_available(model, False) is True       # CLI: env counts
        with cfg.credential_scope(strict()):
            assert cfg._model_key_available(model, False) is False
    with cfg.credential_scope(strict(DEEPSEEK_API_KEY=TYPED)):
        assert cfg._model_key_available("deepseek-v4-flash", False) is True
        assert cfg._require_api_key("DEEPSEEK_API_KEY", "m") == TYPED


@pytest.mark.parametrize("model", ["deepseek-v4-flash", "gpt-5.2", "gemini-3-flash-preview"])
def test_get_model_fails_closed_without_a_typed_key_and_names_the_variable(server_env, model):
    with cfg.credential_scope(strict()):
        with pytest.raises(ValueError) as err:
            cfg.get_model(model)
    message = str(err.value)
    assert "_API_KEY" in message
    no_server_value(message)


def test_openai_client_gets_the_typed_key_not_the_server_key(server_env):
    with cfg.credential_scope(strict(OPENAI_API_KEY=TYPED)):
        model = cfg.get_model("gpt-5.2")
    assert model.openai_api_key.get_secret_value() == TYPED
    # CLI behaviour: the library reads the environment itself.
    legacy = cfg.get_model("gpt-5.2")
    assert legacy.openai_api_key.get_secret_value() == SERVER["OPENAI_API_KEY"]


def test_gemini_gets_the_typed_key_passed_explicitly(server_env, monkeypatch):
    seen = {}
    monkeypatch.setattr(cfg, "init_chat_model", lambda name, **kw: seen.update(name=name, **kw))
    with cfg.credential_scope(strict(GEMINI_API_KEY=TYPED)):
        cfg.get_model("gemini-3-flash-preview")
    assert seen["name"] == "google_genai:gemini-3-flash-preview" and seen["api_key"] == TYPED
    seen.clear()
    cfg.get_model("gemini-3-flash-preview")                          # CLI: nothing passed
    assert "api_key" not in seen


# ── search layer ────────────────────────────────────────────────────────

def test_search_tools_are_not_enabled_by_server_keys(server_env):
    from deep_research.agents.base import _runtime_platform_tools, _runtime_search_tools

    ctx = runtime(strict())
    with runtime_scope(ctx):
        assert _runtime_search_tools() == []
        for kind in ("web", "general"):
            names = {t.name for t in _runtime_platform_tools(ctx, kind)}
            assert not names & {"tavily_search", "exa_deep_search"}
    typed = runtime(strict(TAVILY_API_KEY=TYPED, EXA_API_KEY=TYPED))
    with runtime_scope(typed):
        assert sorted(t.name for t in _runtime_search_tools()) == [
            "exa_deep_search", "tavily_search"]


class _FakeTavily:
    """Stands in for tavily.TavilyClient; records which key each client was built with."""
    built: list = []
    searched: list = []

    def __init__(self, api_key=None):
        self.api_key = api_key
        _FakeTavily.built.append(api_key)

    def search(self, query, **kw):
        _FakeTavily.searched.append((self.api_key, query))
        return {"results": [{"url": "https://example.test", "title": "t"}]}


def test_tavily_client_captured_at_import_is_never_used_in_web_mode(server_env, monkeypatch):
    # The module-level client was built at import and may hold the server's TAVILY key.
    _FakeTavily.built, _FakeTavily.searched = [], []
    at_import = _FakeTavily(api_key=SERVER["TAVILY_API_KEY"])
    monkeypatch.setattr(utils, "tavily_client", at_import)
    monkeypatch.setattr(utils, "TavilyClient", _FakeTavily)
    _FakeTavily.searched.clear()

    with runtime_scope(runtime(strict())):
        docs = asyncio.run(utils.tavily_search_multiple(["q1", "q2"]))
    assert docs == [{"results": []}, {"results": []}]
    assert _FakeTavily.searched == []                                # nothing was searched

    with runtime_scope(runtime(strict(TAVILY_API_KEY=TYPED))):
        docs = asyncio.run(utils.tavily_search_multiple(["q1"]))
    assert _FakeTavily.searched == [(TYPED, "q1")]                   # only the typed key

    # CLI behaviour: with no per-run key the import-time client is still the fallback.
    _FakeTavily.searched.clear()
    monkeypatch.delenv("TAVILY_API_KEY")
    asyncio.run(utils.tavily_search_multiple(["q3"]))
    assert _FakeTavily.searched == [(SERVER["TAVILY_API_KEY"], "q3")]


def test_exa_refuses_without_a_typed_key_and_never_names_the_server_key(server_env):
    with runtime_scope(runtime(strict())):
        with pytest.raises(ValueError) as err:
            asyncio.run(utils._exa_search_multiple(["q"]))
    no_server_value(err.value)


# ── the web UI layer (direct calls) ─────────────────────────────────────

def _page(**typed):
    return [typed.get(name, "") for name, _ in ui.KEY_FIELDS]


def test_build_credentials_is_strict_and_status_never_reports_a_server_key(server_env):
    creds = ui.build_credentials(_page(DEEPSEEK_API_KEY=TYPED), "")
    assert creds.env_fallback is False and creds.langsmith_from_env is False
    assert creds.get("EXA_API_KEY") is None
    assert ui.search_key_status("EXA_API_KEY", "") == "no key"
    tavily, exa, line = ui.live_status(
        ["deepseek-v4-flash"], ["deepseek-v4-flash"], ["deepseek-v4-flash"], False,
        ["ResearchWeb"], True, True, True, *_page(DEEPSEEK_API_KEY=TYPED), "")
    assert (tavily, exa) == ("no key", "no key")
    assert line == "Missing keys: EXA_API_KEY, TAVILY_API_KEY"
    no_server_value(tavily, exa, line)


def _drive(monkeypatch, **typed):
    """Press Run with the page state ``typed``; returns (outputs, whether the engine was called)."""
    called = []

    async def fake_run_research(*a, **kw):
        called.append(kw["credentials"])
        return integration.ResearchResult(status="completed", final_report="# R\n\nbody")

    monkeypatch.setattr(ui, "run_research", fake_run_research)
    monkeypatch.setattr(ui, "_POLL_SECONDS", 0.01)

    async def go():
        outs = []
        async for out in ui.run_ui(
                "A question?", ["deepseek-v4-flash"], ["deepseek-v4-flash"],
                ["deepseek-v4-flash"], False, "auto", 5, 5, 3, 1, ["ResearchWeb"], True, False,
                True, *_page(**typed), "", request=None):
            outs.append(out)
        return outs

    return asyncio.run(go()), called


def test_run_ui_refuses_to_start_on_server_keys_alone(server_env, monkeypatch):
    outs, called = _drive(monkeypatch)
    assert called == []                                            # the engine never started
    assert outs[-1][0].startswith("Missing keys for this setup")
    no_server_value(json.dumps(outs, default=str))


def test_run_ui_runs_with_typed_keys_and_hands_the_engine_strict_credentials(server_env,
                                                                         monkeypatch):
    outs, called = _drive(monkeypatch, DEEPSEEK_API_KEY=TYPED, EXA_API_KEY=TYPED + "-exa")
    assert outs[-1][0].startswith("Completed")
    (creds,) = called
    assert creds.env_fallback is False
    assert [n for n in NAMES if creds.get(n) and n not in ("DEEPSEEK_API_KEY", "EXA_API_KEY")] == []
    no_server_value(json.dumps(outs, default=str))
