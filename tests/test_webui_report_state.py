"""Report download, settings kept in the browser, and temp-file cleanup.

The launched-app tests start the REAL webui/app.py (engine faked, see
tests/webui_fake_run_launcher.py) and drive it over HTTP, because direct calls once hid
wire-level bugs. TMPDIR and GRADIO_TEMP_DIR point into the test's tmp_path, so the files
the app creates and deletes can be counted.
"""

import copy
import json
import os
import uuid
from pathlib import Path

import httpx
import pytest

pytest.importorskip("gradio")

from conftest import FINAL_TEXT
from test_webui_langsmith_e2e import _launch, _stop
from webui import app as ui

DS = "sk-deepseek-STATE-secret-1"
EXA = "exa-STATE-secret-key-2"
LS = "lsv2_pt_STATE-0123456789abcdef"
OTHER = "reddit-STATE-secret-xyz"
KEYS = [DS, EXA, LS, OTHER]
TYPED = {"DeepSeek": DS, "Exa search": EXA, "LangSmith API key": LS,
         "Other keys, NAME=value separated by semicolons": f"REDDIT_CLIENT_SECRET={OTHER}",
         "Web search on (master switch)": True, "Tavily": False, "Exa": True}

GOOD = {
    "question": "Why is the sky blue?", "preset": ui.DEFAULT_PRESET,
    "sup": ["deepseek-v4-pro"], "sub": ["deepseek-v4-flash", "gpt-5.2"], "draft": ["mimo-v2.5"],
    "route_or": True, "thinking": "off", "minutes": 9, "iters": 7, "searches": 4,
    "concurrency": 2, "agents": ["ResearchWeb", "ResearchArxiv"], "web_on": True,
    "use_tavily": True, "use_exa": False, "ls_on": True, "ls_project": "my project",
    "ls_region": "EU"}
VALUES = [GOOD[n] for n in ui.SETTING_NAMES]
SKIP = {"__type__": "update"}          # what gr.skip() is: "leave this component alone"


# ── pure functions ──────────────────────────────────────────────────────

def test_sanitize_keeps_valid_values_and_drops_invalid_ones():
    assert ui.sanitize_settings(GOOD) == GOOD
    bad = {"question": 5, "preset": "nope", "sup": [], "sub": ["has space"], "draft": "x",
           "route_or": "yes", "thinking": "max", "minutes": 10 ** 6, "iters": 2,
           "searches": True, "concurrency": 2.5, "agents": ["Nope"], "web_on": 1,
           "use_tavily": None, "use_exa": [], "ls_on": "true", "ls_project": 7,
           "ls_region": "https://evil.example", "unknown": "x"}
    assert ui.sanitize_settings(bad) == {}
    for junk in (None, "text", 5, [], [GOOD], {"settings": GOOD}):
        assert ui.sanitize_settings(junk) == {}
    mixed = dict(GOOD, minutes=10 ** 6, thinking="max")
    assert ui.sanitize_settings(mixed) == {k: v for k, v in GOOD.items()
                                           if k not in ("minutes", "thinking")}
    assert ui.sanitize_settings({"minutes": 9.0})["minutes"] == 9   # a whole float is fine
    assert ui.sanitize_settings({"ls_project": "x" * 500})["ls_project"] == "x" * 80


def test_restore_applies_valid_values_and_skips_the_rest():
    payload = ui.build_payload(ui.sanitize_settings(GOOD), {"text": "# R", "sources": [["1", "t", "u"]]})
    restored = ui.restore_settings(payload)
    assert list(restored[:len(ui.SETTING_NAMES)]) == VALUES
    assert restored[-2:] == ("# R", [["1", "t", "u"]])
    skipped = ui.restore_settings({"v": 1, "settings": {"thinking": "max", "iters": 12},
                                   "report": "junk"})
    by_name = dict(zip(ui.SETTING_NAMES, skipped))
    assert by_name["iters"] == 12
    assert by_name["thinking"] == SKIP and skipped[-2:] == (SKIP, SKIP)
    for foreign in (None, 5, "x", {"v": 99, "settings": GOOD}, {"settings": GOOD}):
        assert all(v == SKIP for v in ui.restore_settings(foreign))


def test_save_keeps_the_stored_report_and_never_takes_a_key():
    report = {"text": "# R", "sources": [["1", "t", "u"]]}
    state = ui.build_payload({}, report)
    saved = ui.save_settings(state, *VALUES)
    assert saved["settings"] == GOOD and saved["report"] == report
    assert ui.save_settings(None, *VALUES).get("report") is None
    # The handlers' signatures have no key parameter: the values list is non-secret only.
    assert len(ui.SETTING_NAMES) == len(VALUES) == 18
    assert not {"key1", "extra", "ls_key"} & set(ui.SETTING_NAMES)


def test_stored_report_is_capped_and_dropped_silently_when_too_large():
    small = {"text": "x" * 1000, "sources": [["1", "t", "u"]]}
    assert "report" in ui.build_payload({}, small)
    big = {"text": "x" * (ui.STORE_MAX_BYTES + 1), "sources": []}
    payload = ui.build_payload(GOOD, big)
    assert "report" not in payload and payload["settings"] == GOOD
    assert len(json.dumps(payload).encode()) <= ui.STORE_MAX_BYTES
    # Non-ASCII counts in bytes, not characters.
    assert "report" not in ui.build_payload({}, {"text": "é" * (ui.STORE_MAX_BYTES // 2 + 1),
                                                 "sources": []})
    assert "report" not in ui.build_payload({}, {"text": "r", "sources": [["a", "b"]]})


def test_store_report_only_after_a_completed_run():
    state = ui.build_payload(GOOD, None)
    assert ui.store_report("# R", [["1", "t", "u"]], None, state) == SKIP
    assert ui.store_report("", [], "/some/file", state) == SKIP
    stored = ui.store_report("# R", [["1", "t", "u"]], "/some/file", state)
    assert stored["report"] == {"text": "# R", "sources": [["1", "t", "u"]]}
    assert stored["settings"] == GOOD


# ── launched app ────────────────────────────────────────────────────────

@pytest.fixture
def app(tmp_path):
    tmp = tmp_path / "tmp"
    cache = tmp_path / "cache"
    tmp.mkdir()
    cache.mkdir()
    proc, launched = _launch("ok", tmp_path, extra_env={"TMPDIR": str(tmp),
                                                         "GRADIO_TEMP_DIR": str(cache)})
    launched.tmp, launched.cache = tmp, cache
    yield launched
    _stop(proc)


def call(app, dep, data):
    """Fire one dependency over the queue with ``data``; returns its output data."""
    session = uuid.uuid4().hex[:10]
    with httpx.Client(trust_env=False, timeout=120) as http:
        http.post(f"{app.url}/gradio_api/queue/join",
                  json={"data": data, "fn_index": dep["id"], "session_hash": session}
                  ).raise_for_status()
        with http.stream("GET", f"{app.url}/gradio_api/queue/data",
                         params={"session_hash": session}) as stream:
            for line in stream.iter_lines():
                if line.startswith("data:"):
                    msg = json.loads(line[5:])
                    if msg.get("msg") == "process_completed":
                        assert msg.get("success"), msg
                        return msg["output"]["data"]
    raise AssertionError("no process_completed")


def label(app, cid):
    return app.comps[cid]["props"].get("label")


def deps_by_fn(app):
    out = {}
    for d in app.config["dependencies"]:
        out.setdefault(len(d["inputs"]), []).append(d)
    return out


def state_id(app):
    return next(c["id"] for c in app.config["components"] if c["type"] == "browserstate")


def save_dep(app):
    sid = state_id(app)
    return next(d for d in app.config["dependencies"]
                if d["outputs"] == [sid] and len(d["inputs"]) == len(ui.SETTING_NAMES) + 1
                and d["inputs"][0] == sid)


def restore_dep(app):
    sid = state_id(app)
    return next(d for d in app.config["dependencies"]
                if d["inputs"] == [sid] and len(d["outputs"]) == len(ui.SETTING_NAMES) + 2)


def store_dep(app):
    sid = state_id(app)
    return next(d for d in app.config["dependencies"]
                if d["outputs"] == [sid] and len(d["inputs"]) == 4)


def run_once(app):
    final, raw = app.run(**TYPED)
    assert final[0].startswith("Completed"), final[0]
    return final, raw


def test_report_download_over_http_returns_the_exact_report_bytes(app):
    final, raw = run_once(app)
    report, file_data = final[2], final[4]
    assert report == FINAL_TEXT and file_data["orig_name"] == "report.md"
    r = httpx.get(file_data["url"], trust_env=False)
    assert r.status_code == 200
    assert r.content == FINAL_TEXT.encode("utf-8")
    assert "report.md" in r.headers.get("content-disposition", "")
    # Still served after the app removed its own source directory (Gradio's cache copy).
    assert httpx.get(file_data["url"], trust_env=False).content == FINAL_TEXT.encode("utf-8")
    assert Path(file_data["path"]).is_file()
    assert str(app.cache) in file_data["path"]


def test_no_report_temp_dirs_accumulate_across_runs(app):
    for _ in range(3):
        final, _ = run_once(app)
        assert httpx.get(final[4]["url"], trust_env=False).status_code == 200
    roots = list(app.tmp.glob("deepdog_*"))
    assert len(roots) == 1 and roots[0].name.startswith("deepdog_reports_")   # the one root
    assert list(roots[0].iterdir()) == []                                     # no run dir left


def test_failed_run_leaves_no_files_and_no_download(app):
    final, _ = app.run(**{"Research question": "   "})
    assert final[0].startswith("Enter a research question")
    assert final[4] is None
    assert not [p for r in app.tmp.glob("deepdog_reports_*") for p in r.iterdir()]


def test_settings_round_trip_over_http_and_no_typed_key_is_ever_stored(app):
    sid = state_id(app)
    labels_in = {label(app, c) for c in
                 save_dep(app)["inputs"] + restore_dep(app)["outputs"] + store_dep(app)["inputs"]}
    assert not labels_in & {"DeepSeek", "Exa search", "LangSmith API key", "OpenAI",
                            "Other keys, NAME=value separated by semicolons"}
    for cid in save_dep(app)["inputs"] + store_dep(app)["inputs"] + restore_dep(app)["outputs"]:
        assert app.comps[cid]["props"].get("type") != "password"

    # Save the page's non-secret settings, then run, then store the finished report.
    saved = call(app, save_dep(app), [None, *VALUES])[0]
    assert saved["settings"] == GOOD
    final, raw = run_once(app)
    stored = call(app, store_dep(app), [final[2], final[3], final[4], saved])[0]
    assert stored["report"]["text"] == FINAL_TEXT
    blob = json.dumps(stored)
    for secret in KEYS:
        assert secret not in blob and secret not in raw

    # A reload: restore from what the browser kept.
    out = call(app, restore_dep(app), [stored])
    assert out[:len(ui.SETTING_NAMES)] == VALUES
    assert out[-2] == FINAL_TEXT


def test_corrupt_or_foreign_stored_values_are_ignored_over_http(app):
    restore = restore_dep(app)
    n = len(ui.SETTING_NAMES)
    skip = call(app, restore, [None])
    for payload in ({"v": 99, "settings": GOOD}, "garbage", 42, ["x"],
                    {"v": 1, "settings": {"thinking": "max", "minutes": 10 ** 9,
                                          "ls_region": "http://evil.example",
                                          "agents": ["x"], "iters": 12},
                     "report": {"text": 5, "sources": "no"}}):
        out = call(app, restore, [copy.deepcopy(payload)])
        assert len(out) == n + 2
        assert "max" not in out and "http://evil.example" not in json.dumps(out)
        if isinstance(payload, dict) and payload.get("v") == 1:
            assert out[ui.SETTING_NAMES.index("iters")] == 12
        else:
            assert out == skip
    assert call(app, store_dep(app), ["", {"headers": ["Code", "Title", "URL"], "data": [], "metadata": None}, None, None])[0] is not None   # skip, no crash


def test_report_root_is_recreated_at_the_same_path(monkeypatch, tmp_path):
    import shutil
    monkeypatch.setattr(ui, "_REPORT_ROOT", None)
    monkeypatch.setattr(ui.tempfile, "mkdtemp", lambda prefix="", dir=None: str(tmp_path / "root"))
    (tmp_path / "root").mkdir()
    monkeypatch.setattr(ui.atexit, "register", lambda *a, **k: None)
    first = ui.report_root()
    shutil.rmtree(first)
    again = ui.report_root()
    assert again == first and again.is_dir()


def test_stale_run_dirs_are_swept_and_fresh_ones_kept(tmp_path):
    old, fresh = tmp_path / "deepdog_old", tmp_path / "deepdog_fresh"
    old.mkdir(); fresh.mkdir()
    long_ago = 1_000_000_000
    os.utime(old, (long_ago, long_ago))
    ui._sweep_stale_run_dirs(tmp_path)
    assert not old.exists() and fresh.exists()
