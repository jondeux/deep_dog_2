"""Hosted OpenAI-compatible providers selected by an explicit ``provider:model`` prefix.

``deepinfra:``, ``together:`` and ``fireworks:`` (case-insensitive prefix) route to
their own endpoint and key, ahead of every other rule (substring detection AND the
OpenRouter switch). The text after the FIRST colon is sent to the API untouched.

All expectations are literals written here, never read from the code under test.
Offline: ChatOpenAI & co. are replaced by recording stubs, only fake keys are used
(``"k-<ENV_NAME>"``, so ``api_key`` also shows WHICH variable was read).
tests/test_provider_routing.py is the regression guard for every pre-existing name.
"""
import logging

import langchain_deepseek
import pytest

import deep_research.config as cfg
from deep_research.integration import Credentials, validate_credentials
from deep_research.models import ModelFactory
from deep_research.run_config import RunConfig

# prefix -> (env var, base_url): literals, the contract of the three providers.
HOSTED = {
    "deepinfra": ("DEEPINFRA_API_KEY", "https://api.deepinfra.com/v1/openai"),
    "together": ("TOGETHER_API_KEY", "https://api.together.ai/v1"),
    "fireworks": ("FIREWORKS_API_KEY", "https://api.fireworks.ai/inference/v1"),
}
HOSTED_KEYS = [v[0] for v in HOSTED.values()]
EXISTING_KEYS = [
    "ZHIPUAI_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "DEEPSEEK_KEY",
    "MIMO_API_KEY", "META_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY",
]
ALL_KEYS = EXISTING_KEYS + HOSTED_KEYS

# Ids that contain substrings the pre-existing rows match on (gpt, deepseek, glm,
# gemini, meta/, muse, nemotron, nvidia/, mimo, o1, o3) plus ids that are keys of
# the OpenRouter map, and the real-world ids from the provider docs.
MODEL_IDS = [
    "openai/gpt-oss-120b",
    "deepseek-ai/DeepSeek-V3",
    "accounts/fireworks/models/llama-v3p3-70b-instruct",
    "zai-org/GLM-4.5-Air",
    "google/gemini-2.5-flash",
    "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    "meta/muse-spark-1.2",
    "nvidia/Llama-3.1-Nemotron-70B-Instruct",
    "XiaomiMiMo/MiMo-V2-Flash",
    "o1-mini",
    "o3-mini",
    "deepseek-v4-pro",          # in the OpenRouter map: route=True must NOT divert it
    "mimo-v2.5",                # in the OpenRouter map
    "deepseek-baba-singapore",  # pinned OpenRouter row
    "gemini-3-flash-preview",
    "org/model:tag",            # extra colon
]

PREFIX_FORMS = ["{p}", "{P}", "{T}"]  # lower, UPPER, Title/mixed case


def _form(prefix, form):
    return {"{p}": prefix, "{P}": prefix.upper(), "{T}": prefix[:3].upper() + prefix[3:]}[form]


class _Rec:
    """Stub client factory: records the exact call instead of building a client."""

    def __init__(self, tag):
        self.tag = tag

    def __call__(self, *args, **kwargs):
        return (self.tag, args, kwargs)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, caplog):
    for k in ALL_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(cfg, "ChatOpenAI", _Rec("ChatOpenAI"))
    monkeypatch.setattr(cfg, "init_chat_model", _Rec("init_chat_model"))
    monkeypatch.setattr(langchain_deepseek, "ChatDeepSeek", _Rec("ChatDeepSeek"))
    monkeypatch.setattr(cfg, "THINKING_MODE", "auto")
    monkeypatch.setattr(cfg, "ROUTE_VIA_OPENROUTER", False)
    caplog.set_level(logging.WARNING, logger="model_fallback")


@pytest.fixture
def set_keys(monkeypatch):
    def _set(*names):
        for n in names:
            monkeypatch.setenv(n, "k-" + n)
    return _set


def _call(model, **kwargs):
    try:
        return cfg.get_model(model, **kwargs)
    except Exception as e:  # compared as data
        return ("ERR", type(e).__name__, str(e))


def _expect(prefix, model_id, temperature=0, max_tokens=None):
    key, url = HOSTED[prefix]
    kw = {"model": model_id, "api_key": "k-" + key, "base_url": url, "temperature": temperature}
    if max_tokens is not None:
        kw["max_tokens"] = max_tokens
    return ("ChatOpenAI", (), kw)


# ── model_required_keys ────────────────────────────────────────────────────

@pytest.mark.parametrize("route", [False, True])
@pytest.mark.parametrize("model_id", MODEL_IDS)
@pytest.mark.parametrize("prefix", list(HOSTED))
def test_required_keys_is_the_providers_key(prefix, model_id, route):
    assert cfg.model_required_keys(f"{prefix}:{model_id}", route) == [HOSTED[prefix][0]]


@pytest.mark.parametrize("form", PREFIX_FORMS)
@pytest.mark.parametrize("prefix", list(HOSTED))
def test_required_keys_prefix_is_case_insensitive(prefix, form):
    name = _form(prefix, form) + ":deepseek-v4-pro"
    assert cfg.model_required_keys(name, True) == [HOSTED[prefix][0]]
    assert cfg.model_required_keys(name, False) == [HOSTED[prefix][0]]
    assert cfg.model_required_keys(name) == [HOSTED[prefix][0]]


def test_required_keys_literals():
    assert cfg.model_required_keys("deepinfra:deepseek-ai/DeepSeek-V3", True) == ["DEEPINFRA_API_KEY"]
    assert cfg.model_required_keys("together:openai/gpt-oss-120b", False) == ["TOGETHER_API_KEY"]
    assert cfg.model_required_keys(
        "fireworks:accounts/fireworks/models/llama-v3p3-70b-instruct", True) == ["FIREWORKS_API_KEY"]


def test_required_keys_returns_a_fresh_list():
    a = cfg.model_required_keys("together:x")
    a.append("MUTATED")
    assert cfg.model_required_keys("together:x") == ["TOGETHER_API_KEY"]


# ── get_model: full recorded call ──────────────────────────────────────────

# (route_via_openrouter kwarg, module ROUTE_VIA_OPENROUTER): the prefix wins in all.
ROUTES = [(None, False), (None, True), (True, False), (True, True), (False, True), (False, False)]


@pytest.mark.parametrize("route,grt", ROUTES)
@pytest.mark.parametrize("model_id", MODEL_IDS)
@pytest.mark.parametrize("prefix", list(HOSTED))
def test_get_model_builds_chatopenai_for_prefix(monkeypatch, set_keys, prefix, model_id, route, grt):
    set_keys(*ALL_KEYS)  # every key present: the right one must still be picked
    monkeypatch.setattr(cfg, "ROUTE_VIA_OPENROUTER", grt)
    assert _call(f"{prefix}:{model_id}", route_via_openrouter=route) == _expect(prefix, model_id)


@pytest.mark.parametrize("form", PREFIX_FORMS)
@pytest.mark.parametrize("prefix", list(HOSTED))
def test_get_model_prefix_case_insensitive_id_case_kept(set_keys, prefix, form):
    set_keys(*HOSTED_KEYS)
    name = _form(prefix, form) + ":Org/Mixed-Case_Model-V3"
    assert _call(name) == _expect(prefix, "Org/Mixed-Case_Model-V3")


def test_get_model_literals_deepinfra(set_keys):
    set_keys("DEEPINFRA_API_KEY")
    assert _call("deepinfra:deepseek-ai/DeepSeek-V3") == (
        "ChatOpenAI", (),
        {"model": "deepseek-ai/DeepSeek-V3", "api_key": "k-DEEPINFRA_API_KEY",
         "base_url": "https://api.deepinfra.com/v1/openai", "temperature": 0})


def test_get_model_literals_together_with_max_tokens(set_keys):
    set_keys("TOGETHER_API_KEY")
    assert _call("TOGETHER:openai/gpt-oss-120b", temperature=0.3, max_tokens=4096) == (
        "ChatOpenAI", (),
        {"model": "openai/gpt-oss-120b", "api_key": "k-TOGETHER_API_KEY",
         "base_url": "https://api.together.ai/v1", "temperature": 0.3, "max_tokens": 4096})


def test_get_model_literals_fireworks(set_keys):
    set_keys("FIREWORKS_API_KEY")
    assert _call("Fireworks:accounts/fireworks/models/llama-v3p3-70b-instruct",
                 route_via_openrouter=True) == (
        "ChatOpenAI", (),
        {"model": "accounts/fireworks/models/llama-v3p3-70b-instruct",
         "api_key": "k-FIREWORKS_API_KEY",
         "base_url": "https://api.fireworks.ai/inference/v1", "temperature": 0})


@pytest.mark.parametrize("prefix", list(HOSTED))
def test_max_tokens_only_when_not_none(set_keys, prefix):
    set_keys(*HOSTED_KEYS)
    assert "max_tokens" not in _call(f"{prefix}:m", max_tokens=None)[2]
    assert _call(f"{prefix}:m", max_tokens=0) == _expect(prefix, "m", max_tokens=0)
    assert _call(f"{prefix}:m", max_tokens=777) == _expect(prefix, "m", max_tokens=777)


@pytest.mark.parametrize("tmode", [None, "auto", "on", "off"])
@pytest.mark.parametrize("prefix", list(HOSTED))
def test_no_thinking_or_extra_body_for_any_mode(set_keys, prefix, tmode):
    set_keys(*HOSTED_KEYS)
    res = _call(f"{prefix}:deepseek-ai/DeepSeek-V3", thinking_mode=tmode)
    assert res == _expect(prefix, "deepseek-ai/DeepSeek-V3")
    assert "extra_body" not in res[2]


@pytest.mark.parametrize("name,model_id", [
    ("together:org/model:tag", "org/model:tag"),
    ("deepinfra:a:b:c", "a:b:c"),
    ("fireworks::leading", ":leading"),
    ("together: spaced ", " spaced "),
    ("DeepInfra:x/y ", "x/y "),
    ("together:google_genai:gemini-3-flash-preview", "google_genai:gemini-3-flash-preview"),
    ("together:openai:gpt-5", "openai:gpt-5"),
])
def test_model_id_is_everything_after_first_colon_verbatim(set_keys, name, model_id):
    set_keys(*HOSTED_KEYS)
    prefix = name.split(":", 1)[0].lower()
    assert _call(name) == _expect(prefix, model_id)


# ── keys: only that key set, wrong key, missing key ───────────────────────

@pytest.mark.parametrize("model_id", ["openai/gpt-oss-120b", "deepseek-ai/DeepSeek-V3"])
@pytest.mark.parametrize("prefix", list(HOSTED))
def test_only_the_providers_own_key_is_needed(set_keys, prefix, model_id):
    set_keys(HOSTED[prefix][0])
    for route in (None, True, False):
        assert _call(f"{prefix}:{model_id}", route_via_openrouter=route) == _expect(prefix, model_id)


@pytest.mark.parametrize("prefix", list(HOSTED))
def test_other_keys_alone_do_not_satisfy(set_keys, prefix):
    own = HOSTED[prefix][0]
    set_keys(*[k for k in ALL_KEYS if k != own])
    for route in (None, True, False):
        err = _call(f"{prefix}:openai/gpt-oss-120b", route_via_openrouter=route)
        assert err == ("ERR", "ValueError",
                       f"{own} not found in .env for model '{prefix}:openai/gpt-oss-120b'. "
                       "Add the key or set the model chain to a provider you have a key for.")


@pytest.mark.parametrize("prefix", list(HOSTED))
def test_openrouter_key_only_does_not_satisfy_with_route_true(set_keys, prefix):
    set_keys("OPENROUTER_API_KEY")
    err = _call(f"{prefix}:deepseek-v4-pro", route_via_openrouter=True)
    assert err[:2] == ("ERR", "ValueError")
    assert err[2].startswith(f"{HOSTED[prefix][0]} not found in .env")
    assert "OPENROUTER" not in err[2]


@pytest.mark.parametrize("prefix", list(HOSTED))
def test_each_provider_reads_its_own_env_var_among_all_keys(set_keys, prefix):
    set_keys(*ALL_KEYS)
    assert _call(f"{prefix}:x")[2]["api_key"] == "k-" + HOSTED[prefix][0]


def test_per_run_credentials_resolver_is_honoured(monkeypatch):
    # No env var at all: the key comes from the run's Credentials (credential_scope).
    creds = Credentials({"TOGETHER_API_KEY": "run-key"})
    factory = ModelFactory(RunConfig(), creds)
    assert factory.get("together:openai/gpt-oss-120b") == (
        "ChatOpenAI", (),
        {"model": "openai/gpt-oss-120b", "api_key": "run-key",
         "base_url": "https://api.together.ai/v1", "temperature": 0})
    with pytest.raises(ValueError, match="DEEPINFRA_API_KEY not found"):
        factory.get("deepinfra:x")


# ── empty model after the prefix ──────────────────────────────────────────

@pytest.mark.parametrize("name", ["together:", "TOGETHER:", "deepinfra:", "Fireworks:", "together:   "])
def test_empty_model_after_prefix_is_a_clear_error(set_keys, name):
    prefix = name.split(":", 1)[0].lower()
    expected = ("ERR", "ValueError",
                f"Model name {name!r} has no model after the {prefix!r} prefix. "
                f"Use '{prefix}:<model-id>'.")
    set_keys(*HOSTED_KEYS)
    assert _call(name) == expected
    assert _call(name, route_via_openrouter=True) == expected


def test_empty_model_error_comes_before_the_missing_key_error():
    # No keys at all: the empty model is reported, not the key.
    assert _call("together:")[2].startswith("Model name 'together:' has no model")


# ── reasoning_effort ──────────────────────────────────────────────────────

def test_invalid_reasoning_effort_still_fails_fast(set_keys):
    set_keys(*HOSTED_KEYS)
    for name in ("together:x", "deepinfra:", "fireworks:y"):
        assert _call(name, reasoning_effort="extreme") == (
            "ERR", "ValueError", "Invalid reasoning_effort: 'extreme'. Use 'low', 'medium', or 'high'.")


@pytest.mark.parametrize("prefix", list(HOSTED))
def test_valid_reasoning_effort_is_ignored_with_a_warning(set_keys, caplog, prefix):
    set_keys(*HOSTED_KEYS)
    res = _call(f"{prefix}:m", reasoning_effort=" High ")
    assert res == _expect(prefix, "m")
    msgs = [r.getMessage() for r in caplog.records if r.name == "model_fallback"]
    assert msgs == [f"reasoning_effort ignored for {prefix}:m: not sent to {prefix} (thinking "
                    "parameters are not enabled for hosted providers yet)."]


# ── unknown prefixes and pre-existing names behave exactly as before ──────

_UNSUPPORTED_TAIL = (
    "Supported providers: OpenAI (gpt-*), Google (gemini-*), Z.AI GLM (glm-*), DeepSeek "
    "(deepseek-*), MiMo (mimo-*), Meta Muse (muse-* / meta/*), OpenRouter Nemotron "
    "(nemotron-* / nvidia/*)")


@pytest.mark.parametrize("name", [
    "foo:bar", "deepinfra", "deepinfra-x", "together/x", " deepinfra:x", "deepinfra :x",
    "deepinfras:x", "together.ai:x", "fire:x", "fireworksai:x",
    # heads of length 8/9 that pass the length gate but are not exact prefixes
    "together :x", " together:x", "togethers:x",
])
def test_unknown_or_malformed_prefix_is_not_a_hosted_provider(set_keys, name):
    set_keys(*ALL_KEYS)
    assert cfg.model_required_keys(name, False) == []
    assert cfg.model_required_keys(name, True) == []
    assert _call(name, route_via_openrouter=False) == (
        "ERR", "ValueError", f"Cannot determine provider for model: {name}. {_UNSUPPORTED_TAIL}")


def test_pre_existing_colon_names_unchanged(set_keys):
    set_keys(*ALL_KEYS)
    assert _call("google_genai:gemini-3-flash-preview") == (
        "init_chat_model", ("google_genai:gemini-3-flash-preview",),
        {"temperature": 0, "max_retries": 1,
         "generation_config": {"thinking_config": {"thinking_budget": 1}}})
    assert _call("openai:gpt-5", max_tokens=9) == (
        "init_chat_model", ("openai:gpt-5",), {"temperature": 0, "max_tokens": 9})
    # "deepseek:" is NOT a hosted prefix: the old substring row still takes it.
    assert _call("deepseek:foo", route_via_openrouter=False) == (
        "ChatDeepSeek", (),
        {"model": "deepseek:foo", "api_key": "k-DEEPSEEK_API_KEY",
         "api_base": "https://api.deepseek.com/v1", "temperature": 0})
    assert cfg.model_required_keys("google_genai:gemini-3-flash-preview") == ["GEMINI_API_KEY", "GOOGLE_API_KEY"]
    assert cfg.model_required_keys("openai:gpt-5", True) == ["OPENAI_API_KEY"]
    assert cfg.model_required_keys("deepseek:foo") == ["DEEPSEEK_API_KEY", "DEEPSEEK_KEY"]


def test_pre_existing_plain_names_unchanged(set_keys):
    set_keys(*ALL_KEYS)
    assert _call("gpt-5.2", route_via_openrouter=False) == (
        "init_chat_model", ("openai:gpt-5.2",), {"temperature": 0})
    assert _call("glm-5.2", route_via_openrouter=False) == (
        "ChatOpenAI", (),
        {"model": "glm-5.2", "api_key": "k-ZHIPUAI_API_KEY",
         "base_url": "https://api.z.ai/api/paas/v4/", "temperature": 0})
    assert _call("deepseek-v4-flash", route_via_openrouter=False) == (
        "ChatDeepSeek", (),
        {"model": "deepseek-v4-flash", "api_key": "k-DEEPSEEK_API_KEY",
         "api_base": "https://api.deepseek.com/v1", "temperature": 0})
    assert _call("deepseek-v4-flash", route_via_openrouter=True) == (
        "ChatOpenAI", (),
        {"model": "deepseek/deepseek-v4-flash", "api_key": "k-OPENROUTER_API_KEY",
         "base_url": "https://openrouter.ai/api/v1", "temperature": 0})
    assert cfg.model_required_keys("deepseek-v4-flash", True) == ["OPENROUTER_API_KEY"]
    assert cfg.model_required_keys("glm-5.2", True) == ["ZHIPUAI_API_KEY"]
    assert cfg.model_required_keys("mimo-v2.5-pro", False) == ["MIMO_API_KEY"]
    assert cfg.model_required_keys("nvidia/nemotron-3.5-lightning") == ["OPENROUTER_API_KEY"]


def test_hosted_rows_are_not_in_the_scan_table():
    # The prefix rows live in their own dict; the ordered scan table is untouched.
    assert [p.name for p in cfg._PROVIDERS] == [
        "glm", "nemotron", "deepseek-baba-singapore", "deepseek", "mimo",
        "meta-openrouter", "muse", "gemini", "openai"]
    assert sorted(cfg._HOSTED_PROVIDERS) == ["deepinfra", "fireworks", "together"]


# ── validate_credentials / chain filtering with prefixed models ───────────

def _cfg_with(sup, draft=None, sub=None, **kw):
    return RunConfig(
        supervisor_model_fallback_chain=list(sup),
        draft_report_model_fallback_chain=list(draft or sup),
        subagent_model_fallback_chain=list(sub or sup),
        web_search_engine="exa",
        **kw,
    )


_EXA = {"EXA_API_KEY": "e"}


@pytest.mark.parametrize("route", [False, True])
@pytest.mark.parametrize("prefix", list(HOSTED))
def test_validate_reports_the_providers_key_when_missing(prefix, route):
    cfg_ = _cfg_with([f"{prefix}:deepseek-ai/DeepSeek-V3"], route_via_openrouter=route)
    check = validate_credentials(cfg_, Credentials(dict(_EXA)))
    assert not check.ok
    assert check.missing_required == [HOSTED[prefix][0]]


@pytest.mark.parametrize("route", [False, True])
@pytest.mark.parametrize("prefix", list(HOSTED))
def test_validate_ok_with_only_the_providers_key(prefix, route):
    cfg_ = _cfg_with([f"{prefix.upper()}:openai/gpt-oss-120b"],
                     supervisor_route_via_openrouter=route,
                     draft_route_via_openrouter=route,
                     subagent_route_via_openrouter=route)
    check = validate_credentials(cfg_, Credentials({**_EXA, HOSTED[prefix][0]: "x"}))
    assert check.ok and check.missing_required == []


def test_validate_openrouter_key_does_not_cover_a_prefixed_model():
    cfg_ = _cfg_with(["together:deepseek-v4-pro"], route_via_openrouter=True)
    check = validate_credentials(cfg_, Credentials({**_EXA, "OPENROUTER_API_KEY": "or"}))
    assert check.missing_required == ["TOGETHER_API_KEY"]


def test_validate_per_role_chains_report_each_missing_key():
    cfg_ = _cfg_with(["deepinfra:a"], draft=["together:b"], sub=["fireworks:c"])
    check = validate_credentials(cfg_, Credentials(dict(_EXA)))
    assert check.missing_required == ["DEEPINFRA_API_KEY", "FIREWORKS_API_KEY", "TOGETHER_API_KEY"]
    only_ft = validate_credentials(cfg_, Credentials({**_EXA, "DEEPINFRA_API_KEY": "x"}))
    assert only_ft.missing_required == ["FIREWORKS_API_KEY", "TOGETHER_API_KEY"]


def test_validate_chain_with_one_usable_prefixed_entry_is_ok():
    cfg_ = _cfg_with(["together:a", "fireworks:b"])
    ok = validate_credentials(cfg_, Credentials({**_EXA, "FIREWORKS_API_KEY": "x"}))
    assert ok.ok
    dead = validate_credentials(cfg_, Credentials(dict(_EXA)))
    assert dead.missing_required == ["TOGETHER_API_KEY"]  # first entry's key is reported


def test_validate_prefixed_entry_mixed_with_existing_models():
    cfg_ = _cfg_with(["deepseek-v4-flash", "together:openai/gpt-oss-120b"])
    assert validate_credentials(cfg_, Credentials({**_EXA, "TOGETHER_API_KEY": "t"})).ok
    assert validate_credentials(cfg_, Credentials({**_EXA, "DEEPSEEK_API_KEY": "d"})).ok
    dead = validate_credentials(cfg_, Credentials(dict(_EXA)))
    assert dead.missing_required == ["DEEPSEEK_API_KEY", "DEEPSEEK_KEY"]


def test_chain_factories_skip_a_prefixed_entry_whose_key_is_missing(set_keys):
    set_keys("DEEPINFRA_API_KEY")
    model = cfg.get_supervisor_model(chain=["together:a/b", "deepinfra:c/d"])
    # together is filtered out (no key); the single survivor is returned directly.
    assert model == _expect("deepinfra", "c/d", max_tokens=None)
    assert cfg._model_key_available("together:a/b", True) is False
    assert cfg._model_key_available("deepinfra:c/d", True) is True


def test_chain_factories_raise_when_no_prefixed_key(set_keys):
    with pytest.raises(ValueError, match="Supervisor model chain is unusable"):
        cfg.get_supervisor_model(chain=["together:a/b", "fireworks:c/d"])
