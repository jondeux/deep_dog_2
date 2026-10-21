"""Offline tests for webui/model_catalog.py (httpx.MockTransport, no network)."""

import json
import logging

import httpx
import pytest

pytest.importorskip("gradio")

from webui import app as ui  # noqa: F401  (puts webui/ on sys.path)

import model_catalog as mc

KEY = "sk-live-SECRET-key-123"


@pytest.fixture(autouse=True)
def _clean_cache():
    mc.clear_cache()
    yield
    mc.clear_cache()


def transport(handler):
    return httpx.MockTransport(handler)


def json_ok(payload):
    return transport(lambda request: httpx.Response(200, json=payload))


def test_success_openai_style_prefixes_sorts_and_dedupes():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"data": [
            {"id": "zeta/model"}, {"id": "alpha/model"}, {"id": "alpha/model"},
            {"id": "deepinfra:already/prefixed"}, {"id": ""}, {"nope": 1}, 7, None, {"id": "has space"}]})

    result = mc.fetch_models("deepinfra", KEY, transport=transport(handler))
    assert result.live is True
    assert result.models == ("deepinfra:alpha/model", "deepinfra:already/prefixed",
                             "deepinfra:zeta/model")
    assert seen["url"] == "https://api.deepinfra.com/v1/openai/models"
    assert seen["auth"] == f"Bearer {KEY}"


def test_bare_list_payload_is_accepted():
    payload = [{"id": "openai/gpt-oss-120b", "type": "chat"}, {"id": "meta/x"}]
    result = mc.fetch_models("together", KEY, transport=json_ok(payload))
    assert result.live and result.models == ("together:meta/x", "together:openai/gpt-oss-120b")


def test_bare_list_of_plain_id_strings_is_accepted():
    result = mc.fetch_models("together", KEY, transport=json_ok(["b/m", "a/m"]))
    assert result.models == ("together:a/m", "together:b/m")


def test_only_clearly_marked_non_chat_models_are_skipped():
    payload = {"data": [
        {"id": "chat-a", "type": "chat"},
        {"id": "emb", "type": "embedding"},
        {"id": "img", "kind": "image"},
        {"id": "rr", "model_type": "Rerank"},
        {"id": "odd-type", "type": "something-new"},   # unknown value: kept, not guessed
        {"id": "no-type"},
    ]}
    result = mc.fetch_models("fireworks", KEY, transport=json_ok(payload))
    assert result.models == ("fireworks:chat-a", "fireworks:no-type", "fireworks:odd-type")


def test_key_is_sent_only_to_the_fixed_provider_url_and_redirects_are_not_followed():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.example/steal"})

    result = mc.fetch_models("together", KEY, transport=transport(handler))
    assert calls == ["https://api.together.ai/v1/models"]
    assert result.live is False and result.message == "provider answered HTTP 302"


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_key(status):
    result = mc.fetch_models("together", KEY, transport=transport(
        lambda r: httpx.Response(status, text=f"bad key {KEY}")))
    assert result.live is False and result.message == "key rejected"
    assert result.models == tuple(mc.FALLBACK_MODELS["together"])


def test_server_error_is_a_short_message():
    result = mc.fetch_models("fireworks", KEY, transport=transport(
        lambda r: httpx.Response(500, text=f"boom {KEY}")))
    assert result.message == "provider answered HTTP 500"


def test_timeout_falls_back():
    def handler(request):
        raise httpx.ReadTimeout(f"timed out talking to {request.url} with {KEY}")

    result = mc.fetch_models("deepinfra", KEY, transport=transport(handler))
    assert result.live is False and result.message == "timed out"
    assert result.models == tuple(mc.FALLBACK_MODELS["deepinfra"])


def test_connection_error_falls_back():
    def handler(request):
        raise httpx.ConnectError(f"refused {KEY}")

    result = mc.fetch_models("deepinfra", KEY, transport=transport(handler))
    assert result.live is False and result.message == "could not connect"


def test_malformed_json_falls_back():
    result = mc.fetch_models("together", KEY, transport=transport(
        lambda r: httpx.Response(200, content=b"<html>not json " + KEY.encode())))
    assert result.live is False and result.message == "unexpected response format"


def test_empty_or_unrecognised_payload_falls_back():
    for payload in ({"data": []}, {"unexpected": True}, [], "text", 5, {"data": "x"}):
        result = mc.fetch_models("together", KEY, transport=json_ok(payload))
        assert result.live is False and result.message == "no models found in the response"


def test_oversized_declared_body_is_refused(monkeypatch):
    monkeypatch.setattr(mc, "MAX_RESPONSE_BYTES", 1000)
    body = json.dumps({"data": [{"id": f"m{i}"} for i in range(500)]}).encode()
    result = mc.fetch_models("together", KEY, transport=transport(
        lambda r: httpx.Response(200, content=body)))
    assert result.live is False and result.message == "response too large"


def test_oversized_streamed_body_without_content_length_is_refused(monkeypatch):
    monkeypatch.setattr(mc, "MAX_RESPONSE_BYTES", 1000)

    class Endless(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(100):
                yield b" " * 100

    result = mc.fetch_models("together", KEY, transport=transport(
        lambda r: httpx.Response(200, stream=Endless())))
    assert result.live is False and result.message == "response too large"


def test_no_key_uses_fallback_without_any_request():
    def handler(request):
        raise AssertionError("no request expected")

    for blank in (None, "", "   "):
        result = mc.fetch_models("fireworks", blank, transport=transport(handler))
        assert result.live is False and result.message == "no key"
        assert result.models == tuple(mc.FALLBACK_MODELS["fireworks"])


def test_fallback_lists_use_engine_ready_names():
    assert mc.FALLBACK_MODELS == {
        "deepinfra": ["deepinfra:deepseek-ai/DeepSeek-V3"],
        "together": ["together:openai/gpt-oss-120b"],
        "fireworks": ["fireworks:accounts/fireworks/models/llama-v3p3-70b-instruct"],
    }


def test_unknown_provider_never_raises():
    assert mc.fetch_models("nope", KEY).live is False


def test_key_never_appears_in_any_result_or_log_record(caplog):
    caplog.set_level(logging.DEBUG)
    handlers = {
        "ok": lambda r: httpx.Response(200, json={"data": [{"id": "a/b"}]}),
        "401": lambda r: httpx.Response(401, text=KEY),
        "500": lambda r: httpx.Response(500, text=KEY),
        "timeout": lambda r: (_ for _ in ()).throw(httpx.ReadTimeout(KEY)),
        "connect": lambda r: (_ for _ in ()).throw(httpx.ConnectError(KEY)),
        "boom": lambda r: (_ for _ in ()).throw(RuntimeError(KEY)),
        "json": lambda r: httpx.Response(200, content=KEY.encode()),
    }
    blob = []
    for name, handler in handlers.items():
        mc.clear_cache()
        results = mc.fetch_all({p: KEY for p in mc.PROVIDERS}, transport=transport(handler))
        blob.append(repr(results))
        blob.append(mc.status_line(results))
        blob.append(repr(mc.merged_choices(ui.MODEL_CHOICES, results)))
    blob.append(caplog.text)
    blob.extend(r.getMessage() + repr(r.args) for r in caplog.records)
    assert KEY not in "\n".join(blob)
    assert "SECRET" not in "\n".join(blob)


def test_cache_is_keyed_by_hash_expires_and_never_stores_the_key(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(mc, "_now", lambda: clock[0])
    hits = []

    def handler(request):
        hits.append(1)
        return httpx.Response(200, json={"data": [{"id": "a/b"}]})

    t = transport(handler)
    mc.fetch_models("together", KEY, transport=t)
    mc.fetch_models("together", KEY, transport=t)
    assert len(hits) == 1                                     # second call served from cache
    mc.fetch_models("together", KEY + "-other", transport=t)
    assert len(hits) == 2                                     # different key, different entry
    assert all(KEY not in repr(k) for k in mc._CACHE)         # only a digest is stored
    assert mc.CACHE_TTL_SECONDS <= 600
    clock[0] += mc.CACHE_TTL_SECONDS + 1
    mc.fetch_models("together", KEY, transport=t)
    assert len(hits) == 3                                     # expired


def test_cache_never_exceeds_its_cap_after_many_distinct_keys():
    t = transport(lambda r: httpx.Response(200, json={"data": [{"id": "a/b"}]}))
    for i in range(mc._CACHE_MAX_ENTRIES * 3):
        mc.fetch_models("together", f"key-{i:05d}-abcdef", transport=t)
        assert len(mc._CACHE) <= mc._CACHE_MAX_ENTRIES
    assert mc._CACHE_MAX_ENTRIES == 64
    assert len(mc._CACHE) == 64                               # full, not emptied


def test_failures_are_cached_for_30_seconds_then_retried(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(mc, "_now", lambda: clock[0])
    state = {"fail": True, "hits": 0}

    def handler(request):
        state["hits"] += 1
        if state["fail"]:
            return httpx.Response(500)
        return httpx.Response(200, json={"data": [{"id": "a/b"}]})

    t = transport(handler)
    first = mc.fetch_models("together", KEY, transport=t)
    assert first.live is False and state["hits"] == 1
    state["fail"] = False
    clock[0] += mc.FAILURE_TTL_SECONDS - 1
    again = mc.fetch_models("together", KEY, transport=t)
    assert again == first and state["hits"] == 1              # no new outbound request
    assert mc.FAILURE_TTL_SECONDS == 30.0
    clock[0] += 2                                             # 30 s have passed
    assert mc.fetch_models("together", KEY, transport=t).live is True
    assert state["hits"] == 2


def test_failure_entries_are_keyed_by_hash_and_never_hold_the_key(monkeypatch):
    t = transport(lambda r: httpx.Response(401))
    result = mc.fetch_models("together", KEY, transport=t)
    assert result.message == "key rejected"
    assert len(mc._CACHE) == 1
    assert all(KEY not in repr(k) for k in mc._CACHE)
    assert KEY not in repr(mc._CACHE.values())


def test_success_ttl_is_unchanged_by_failure_caching(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(mc, "_now", lambda: clock[0])
    hits = []

    def handler(request):
        hits.append(1)
        return httpx.Response(200, json={"data": [{"id": "a/b"}]})

    t = transport(handler)
    mc.fetch_models("together", KEY, transport=t)
    clock[0] += 31                                            # past the failure TTL only
    mc.fetch_models("together", KEY, transport=t)
    assert len(hits) == 1                                     # success still cached
    clock[0] += mc.CACHE_TTL_SECONDS
    mc.fetch_models("together", KEY, transport=t)
    assert len(hits) == 2


def test_cache_cap_holds_with_failure_entries_included():
    ok = transport(lambda r: httpx.Response(200, json={"data": [{"id": "a/b"}]}))
    bad = transport(lambda r: httpx.Response(500))
    total = mc._CACHE_MAX_ENTRIES * 2
    for i in range(total):
        # the last 10 are failures, so they are the newest entries when the cap bites
        mc.fetch_models("together", f"key-{i:05d}-abcdef", transport=bad if i >= total - 10 else ok)
        assert len(mc._CACHE) <= mc._CACHE_MAX_ENTRIES
    assert len(mc._CACHE) == mc._CACHE_MAX_ENTRIES
    # Failures expire first, so they are the first evicted; the newest one is still cached.
    hits = []
    last = transport(lambda r: hits.append(1) or httpx.Response(500))
    assert mc.fetch_models("together", f"key-{total - 1:05d}-abcdef", transport=last).live is False
    assert hits == []


def test_declared_oversize_content_length_is_refused_before_the_body_is_iterated():
    class Tripwire(httpx.SyncByteStream):
        def __iter__(self):
            raise AssertionError("body stream was iterated")
            yield b""

    def handler(request):
        return httpx.Response(200, headers={"Content-Length": str(mc.MAX_RESPONSE_BYTES + 1)},
                              stream=Tripwire())

    result = mc.fetch_models("together", KEY, transport=transport(handler))
    assert result.live is False and result.message == "response too large"


def test_merged_choices_keep_existing_names_and_add_provider_models():
    results = mc.fallback_results()
    results["together"] = mc.CatalogResult("together", ("together:a/b",), True, "1 models")
    merged = mc.merged_choices(["deepseek-v4-flash", "gpt-5.2"], results)
    assert merged[:2] == ["deepseek-v4-flash", "gpt-5.2"]
    assert "together:a/b" in merged and "together:openai/gpt-oss-120b" not in merged
    assert "deepinfra:deepseek-ai/DeepSeek-V3" in merged
    assert len(merged) == len(set(merged))


def test_status_line_says_live_or_fallback():
    results = mc.fallback_results()
    results["together"] = mc.CatalogResult("together", ("together:a",), True, "1 models")
    results["fireworks"] = mc.fallback_result("fireworks", "key rejected")
    line = mc.status_line(results)
    assert "Together: live list (1 models)" in line
    assert "Fireworks: fallback list (key rejected)" in line
    assert "DeepInfra: fallback list (no key)" in line


def test_base_urls_are_fixed_literals_not_environment_driven(monkeypatch):
    monkeypatch.setenv("DEEPINFRA_BASE_URL", "https://evil.example")
    monkeypatch.setenv("TOGETHER_BASE_URL", "https://evil.example")
    import importlib
    importlib.reload(mc)
    try:
        assert mc.BASE_URLS == {
            "deepinfra": "https://api.deepinfra.com/v1/openai",
            "together": "https://api.together.ai/v1",
            "fireworks": "https://api.fireworks.ai/inference/v1"}
    finally:
        monkeypatch.undo()
        importlib.reload(mc)
