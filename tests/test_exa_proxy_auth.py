"""EXA_AUTH_PROXY=1: send Exa requests with no key header so a credential-injecting proxy can add its own.

Why it exists: in a cloud session the proxy injects the Exa key only into requests that carry none; any key
header (even a dummy) is passed through and Exa answers 401. The option is read from the process environment
only, so a run with ``Credentials(env_fallback=False)`` (the public web UI) can never use it.
"""

import asyncio

import pytest

from deep_research import utils
from deep_research.agents.base import _search_tools_for
from deep_research.integration import validate_credentials
from deep_research.models import Credentials
from deep_research.run_config import RunConfig
from deep_research.runtime import RuntimeContext, runtime_scope
from deep_research.secrets import exa_proxy_auth


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.delenv("EXA_AUTH_PROXY", raising=False)


class FakeResponse:
    status = 200

    async def json(self):
        return {"results": [{"url": "https://example.org/a", "title": "A", "text": "body"}]}

    async def text(self):
        return ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    seen = []
    sessions = []

    def __init__(self, *a, **k):
        FakeSession.sessions.append(k)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, json=None, headers=None):
        FakeSession.seen.append({"url": url, "headers": dict(headers or {})})
        return FakeResponse()


@pytest.fixture
def exa_calls(monkeypatch):
    FakeSession.seen = []
    FakeSession.sessions = []
    monkeypatch.setattr(utils.aiohttp, "ClientSession", FakeSession)
    return FakeSession.seen


def web_ui_runtime():
    return RuntimeContext(run_id="t", thread_id="t", config=RunConfig(), credentials=Credentials({}, env_fallback=False))


def search():
    return asyncio.run(utils._exa_search_multiple(["q"], max_results=1))


# ── the switch ──────────────────────────────────────────────────────────

def test_off_by_default():
    assert exa_proxy_auth() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes"])
def test_on_for_cli_runs_when_the_env_var_says_so(monkeypatch, value):
    monkeypatch.setenv("EXA_AUTH_PROXY", value)
    assert exa_proxy_auth() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "maybe"])
def test_other_values_leave_it_off(monkeypatch, value):
    monkeypatch.setenv("EXA_AUTH_PROXY", value)
    assert exa_proxy_auth() is False


def test_never_on_for_a_run_that_has_no_env_fallback(monkeypatch):
    monkeypatch.setenv("EXA_AUTH_PROXY", "1")
    assert exa_proxy_auth(Credentials({}, env_fallback=False)) is False       # explicit credentials
    with runtime_scope(web_ui_runtime()):
        assert exa_proxy_auth() is False                                      # the active run (web UI)


# ── the request ─────────────────────────────────────────────────────────

def test_proxy_mode_sends_no_authorization_header(monkeypatch, exa_calls):
    monkeypatch.setenv("EXA_AUTH_PROXY", "1")
    out = search()
    assert exa_calls[0]["url"] == "https://api.exa.ai/search"
    assert not any(k.lower() in ("authorization", "x-api-key") for k in exa_calls[0]["headers"])
    assert out[0]["results"]


def test_proxy_mode_routes_through_the_environments_proxy(monkeypatch, exa_calls):
    monkeypatch.setenv("EXA_AUTH_PROXY", "1")        # aiohttp ignores HTTPS_PROXY unless trust_env is on
    search()
    assert FakeSession.sessions[0].get("trust_env") is True


def test_a_key_is_still_sent_as_a_bearer_token_and_the_switch_still_routes_through_the_proxy(monkeypatch, exa_calls):
    monkeypatch.setenv("EXA_API_KEY", "set-by-network-secret")                 # a placeholder the proxy will replace
    monkeypatch.setenv("EXA_AUTH_PROXY", "1")
    search()
    assert exa_calls[0]["headers"]["Authorization"] == "Bearer set-by-network-secret"
    assert FakeSession.sessions[0].get("trust_env") is True                    # else the placeholder goes straight to Exa


def test_a_normal_run_with_a_key_behaves_as_before(monkeypatch, exa_calls):
    monkeypatch.setenv("EXA_API_KEY", "exa-real-key-123")
    search()
    assert exa_calls[0]["headers"]["Authorization"] == "Bearer exa-real-key-123"
    assert not FakeSession.sessions[0].get("trust_env")


def test_without_a_key_or_the_switch_it_still_refuses(exa_calls):
    with pytest.raises(ValueError, match="EXA_API_KEY"):
        search()
    assert exa_calls == []


def test_a_web_ui_run_cannot_reach_exa_without_its_own_key(monkeypatch, exa_calls):
    monkeypatch.setenv("EXA_AUTH_PROXY", "1")
    with runtime_scope(web_ui_runtime()):
        with pytest.raises(ValueError):
            search()
    assert exa_calls == []


# ── the pre-flight check and the tool list ──────────────────────────────

def test_preflight_accepts_exa_without_a_key_in_proxy_mode(monkeypatch):
    cfg = RunConfig(web_search_engine="exa")
    assert "EXA_API_KEY" in validate_credentials(cfg, Credentials({})).missing_required
    monkeypatch.setenv("EXA_AUTH_PROXY", "1")
    assert "EXA_API_KEY" not in validate_credentials(cfg, Credentials({})).missing_required


def test_preflight_for_a_web_ui_run_still_asks_for_the_key(monkeypatch):
    monkeypatch.setenv("EXA_AUTH_PROXY", "1")
    check = validate_credentials(RunConfig(web_search_engine="exa"), Credentials({}, env_fallback=False))
    assert "EXA_API_KEY" in check.missing_required


def names(tools):
    return {t.name for t in tools}


def test_the_exa_tool_is_offered_in_proxy_mode_only_for_runs_that_may_use_it(monkeypatch):
    cfg = RunConfig(web_search_engine="exa").finalize()
    assert "exa_deep_search" not in names(_search_tools_for(cfg, Credentials({})))
    monkeypatch.setenv("EXA_AUTH_PROXY", "1")
    assert "exa_deep_search" in names(_search_tools_for(cfg, Credentials({})))
    assert "exa_deep_search" not in names(_search_tools_for(cfg, Credentials({}, env_fallback=False)))
