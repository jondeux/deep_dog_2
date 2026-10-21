"""The Run button over HTTP against the REAL app with the engine faked.

tests/webui_fake_run_launcher.py starts webui/app.py in a subprocess with fake models (and a
recording fake ``langsmith.Client`` that writes what it saw to a file). Every byte the server
sends back during a run is captured and checked for the typed keys.
"""

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

pytest.importorskip("gradio")

REPO = Path(__file__).resolve().parents[1]
DS = "sk-deepseek-E2E-secret"
EXA = "exa-E2E-secret-key"
LS = "lsv2_pt_E2E-0123456789abcdef"
OTHER = "reddit-E2E-secret-xyz"
SECRETS = [DS, EXA, LS, OTHER]
EU = "https://eu.api.smith.langchain.com"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Launched:
    def __init__(self, url, record):
        self.url, self.record = url, record
        self.config = httpx.get(f"{url}/config", trust_env=False).json()
        self.comps = {c["id"]: c for c in self.config["components"]}
        labels = {c["id"]: c["props"].get("label") for c in self.config["components"]}
        self.dep = next(d for d in self.config["dependencies"]
                        if d["inputs"] and labels[d["inputs"][0]] == "Research question")

    def data(self, **by_label):
        """One value per Run-button input, by label; everything else keeps its default."""
        from webui import app as ui
        key_names = {label: name for name, label in ui.KEY_FIELDS}
        out = []
        for cid in self.dep["inputs"]:
            c = self.comps[cid]
            label = c["props"].get("label")
            if label in by_label:
                out.append(by_label[label])
            elif label == "Research question":
                out.append("What is new in sodium-ion batteries?")
            elif c["type"] == "textbox" and label in key_names:
                out.append("")
            else:
                out.append(c["props"].get("value"))
        return out

    def run(self, **by_label):
        """Press Run over the queue. Returns (final output data, every raw line received)."""
        session = uuid.uuid4().hex[:10]
        raw = []
        final = None
        with httpx.Client(trust_env=False, timeout=120) as http:
            http.post(f"{self.url}/gradio_api/queue/join",
                      json={"data": self.data(**by_label), "fn_index": self.dep["id"],
                            "session_hash": session}).raise_for_status()
            with http.stream("GET", f"{self.url}/gradio_api/queue/data",
                             params={"session_hash": session}) as stream:
                for line in stream.iter_lines():
                    raw.append(line)
                    if line.startswith("data:"):
                        msg = json.loads(line[5:])
                        if msg.get("msg") == "process_completed":
                            assert msg.get("success"), msg
                            final = msg["output"]["data"]
                            break
        assert final is not None, "no process_completed message"
        return final, "\n".join(raw)


def _launch(mode, tmp_path, extra_env=None, record=False):
    port = _free_port()
    rec = tmp_path / "record.json" if record else None
    cmd = [sys.executable, str(REPO / "tests" / "webui_fake_run_launcher.py"), mode]
    if rec:
        cmd += ["--record", str(rec)]
    cmd += ["--host", "127.0.0.1", "--port", str(port)]
    env = {k: v for k, v in os.environ.items()
           if not k.endswith("_API_KEY") and not k.startswith(("LANGSMITH_", "LANGCHAIN_"))}
    env.update(NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost",
               GRADIO_ANALYTICS_ENABLED="False", PYTHONUNBUFFERED="1", **(extra_env or {}))
    proc = subprocess.Popen(cmd, cwd=REPO, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 90
    while time.time() < deadline:
        if proc.poll() is not None:
            pytest.fail("app exited early:\n" + proc.stdout.read())
        try:
            if httpx.get(url, trust_env=False, timeout=2).status_code == 200:
                return proc, Launched(url, rec)
        except httpx.HTTPError:
            time.sleep(0.5)
    proc.kill()
    pytest.fail("app did not start")


def _stop(proc):
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture
def launch(tmp_path):
    started = []

    def go(mode, **kw):
        proc, app = _launch(mode, tmp_path, **kw)
        started.append(proc)
        return app

    yield go
    for proc in started:
        _stop(proc)


TYPED = {
    "DeepSeek": DS, "Exa search": EXA, "LangSmith API key": LS,
    "Other keys, NAME=value separated by semicolons": f"REDDIT_CLIENT_SECRET={OTHER}",
}


def _typed(**over):
    values = dict(TYPED)
    values.update({"Trace this run in LangSmith": True, "Web search on (master switch)": True,
                   "Tavily": False, "Exa": True})
    values.update(over)
    return values


def _assert_no_secret(*texts):
    for text in texts:
        for secret in SECRETS:
            assert secret not in text, f"{secret!r} leaked"


# ── a full fake run with tracing on ──────────────────────────────────────

def test_full_run_over_the_wire_records_under_the_typed_project(launch):
    app = launch("ok", record=True)
    final, raw = app.run(**_typed(**{"LangSmith project": "  wire project  ",
                                     "LangSmith region": "EU"}))
    status = final[0]
    assert status.startswith("Completed")
    assert status.endswith("LangSmith: tracing active, project wire project")
    assert "# Final Report" in final[2]
    _assert_no_secret(raw)

    rec = json.loads(app.record.read_text())
    assert rec["ctor"] == {"api_key": LS, "api_url": EU, "omit_traced_runtime_info": True, "workspace_id": ""}
    assert rec["sessions"] == ["wire project"]
    assert rec["runs"] > 10
    _assert_no_secret(rec["calls_blob"])


def test_unticked_run_over_the_wire_shows_no_langsmith_line_and_records_nothing(launch):
    app = launch("ok", record=True)
    final, raw = app.run(**_typed(**{"Trace this run in LangSmith": False}))
    assert final[0].startswith("Completed") and "LangSmith" not in final[0]
    assert not app.record.exists()
    _assert_no_secret(raw)


def test_a_typed_endpoint_in_other_keys_is_ignored_over_the_wire(launch):
    app = launch("ok", record=True)
    smuggled = (f"REDDIT_CLIENT_SECRET={OTHER};LANGSMITH_ENDPOINT=http://169.254.169.254/x;"
                "LANGSMITH_API_KEY=smuggled-key-value")
    final, raw = app.run(**_typed(**{
        "Other keys, NAME=value separated by semicolons": smuggled, "LangSmith region": "US"}))
    assert final[0].startswith("Completed")
    rec = json.loads(app.record.read_text())
    assert rec["ctor"] == {"api_key": LS, "api_url": "https://api.smith.langchain.com",
                           "omit_traced_runtime_info": True, "workspace_id": ""}
    assert "169.254" not in json.dumps(rec) and "smuggled-key-value" not in json.dumps(rec)


# ── failure text never carries a typed key ───────────────────────────────

@pytest.mark.parametrize("mode, prefix", [
    ("engine_fail", ("Run failed: ", "Run partial: ")),
    ("escape", ("The run failed: RuntimeError: ",)),
    ("unredacted", ("Run failed: RuntimeError: ",)),
])
def test_failure_messages_over_the_wire_hide_every_typed_key(launch, mode, prefix):
    app = launch(mode)
    final, raw = app.run(**_typed())
    status = final[0]
    assert status.startswith(prefix), status
    assert "provider rejected credentials" in status
    assert "***" in status or "[REDACTED]" in status
    _assert_no_secret(raw, json.dumps(final))


def test_escape_and_unredacted_messages_use_triple_star_for_all_four_keys(launch):
    for mode in ("escape", "unredacted"):
        app = launch(mode)
        final, _ = app.run(**_typed())
        assert final[0].count("***") == 4, (mode, final[0])


def test_settings_problem_over_the_wire_hides_every_typed_key(launch):
    app = launch("settings", extra_env={"ECHO_SECRETS": json.dumps(SECRETS)})
    final, raw = app.run(**_typed(**{"LangSmith project": "boom"}))
    assert final[0] == "Settings problem: invalid setup *** *** *** ***"
    _assert_no_secret(raw)
