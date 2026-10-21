"""End-to-end checks against the REAL app in a subprocess (real Gradio server, real wire
format) with a fake local model-list provider. No real provider or search is contacted.

Unit tests that call handlers directly once hid a request-injection bug and a
host-binding bug, so the live-model-list, search-status and key-box wiring are all
exercised here through HTTP and gradio_client.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

pytest.importorskip("gradio")
gradio_client = pytest.importorskip("gradio_client")

REPO = Path(__file__).resolve().parents[1]
GOOD = {"deepinfra": "di-good-SECRET", "together": "tg-good-SECRET", "fireworks": "fw-good-SECRET"}
PAYLOADS = {
    "deepinfra": {"object": "list", "data": [{"id": "deepseek-ai/DeepSeek-V3"},
                                              {"id": "meta/llama-x"}, {"id": "bge-embed", "type": "embedding"}]},
    "together": [{"id": "openai/gpt-oss-120b", "type": "chat"}, {"id": "acme/new-chat"},
                 {"id": "acme/image-gen", "type": "image"}],
    "fireworks": {"data": [{"id": "accounts/fireworks/models/llama-v3p3-70b-instruct"},
                           {"id": "accounts/fireworks/models/qwen-z"}]},
}


class FakeProvider(BaseHTTPRequestHandler):
    seen: list = []

    def do_GET(self):  # noqa: N802
        provider = self.path.strip("/").split("/")[0]
        auth = self.headers.get("Authorization", "")
        FakeProvider.seen.append((self.path, auth))
        if not self.path.endswith("/models") or provider not in PAYLOADS:
            self.send_response(404); self.end_headers(); return
        if auth != f"Bearer {GOOD[provider]}":
            self.send_response(401); self.end_headers(); self.wfile.write(b"bad key " + auth.encode()); return
        body = json.dumps(PAYLOADS[provider]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def fake_provider():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(scope="module")
def app_url(fake_provider):
    port = free_port()
    bases = {p: f"{fake_provider}/{p}" for p in GOOD}
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    env.update(NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost",
               GRADIO_ANALYTICS_ENABLED="False", PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(
        [sys.executable, str(REPO / "tests" / "webui_launcher.py"), json.dumps(bases),
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 90
    try:
        while time.time() < deadline:
            if proc.poll() is not None:
                pytest.fail("app exited early:\n" + proc.stdout.read())
            try:
                if httpx.get(url, trust_env=False, timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        else:
            pytest.fail("app did not start")
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="module")
def config(app_url):
    return httpx.get(f"{app_url}/config", trust_env=False).json()


@pytest.fixture(scope="module")
def client(app_url):
    return gradio_client.Client(app_url, verbose=False)


@pytest.fixture
def fresh_client(app_url):
    """A new gradio session per test: refreshes are throttled per session (3 s)."""
    return gradio_client.Client(app_url, verbose=False)


def components(config):
    return {c["id"]: c for c in config["components"]}


def label_of(c):
    return c["props"].get("label")


# ── page and key boxes ──────────────────────────────────────────────────

def test_page_serves_200_and_has_every_key_box_as_a_password_field(app_url, config):
    from webui import app as ui
    assert httpx.get(app_url, trust_env=False).status_code == 200
    boxes = {label_of(c): c for c in config["components"] if c["type"] == "textbox"}
    for _, label in ui.KEY_FIELDS:
        assert label in boxes, label
        assert boxes[label]["props"]["type"] == "password"
    for label in ("DeepInfra", "Together", "Fireworks"):
        assert label in boxes
    labels = {label_of(c) for c in config["components"]}
    assert {"Tavily", "Exa", "Web search on (master switch)", "Supervisor", "Sub-agents",
            "Draft and brief"} <= labels
    assert any(c["type"] == "button" and c["props"].get("value") == "Refresh model lists"
               for c in config["components"])


def test_provider_key_boxes_trigger_the_refresh_on_blur_and_button_click(config):
    comps = components(config)
    refresh = [d for d in config["dependencies"] if d.get("api_name") == "refresh_models"]
    assert len(refresh) == 1
    inputs = [label_of(comps[i]) for i in refresh[0]["inputs"]]
    assert inputs == ["DeepInfra", "Together", "Fireworks"]
    blur_targets = {label_of(comps[t[0]]) for d in config["dependencies"]
                    if d["inputs"] == refresh[0]["inputs"] and d["outputs"] == refresh[0]["outputs"]
                    for t in d["targets"] if t[1] == "blur"}
    assert blur_targets == {"DeepInfra", "Together", "Fireworks"}
    outputs = [label_of(comps[i]) for i in refresh[0]["outputs"]][:3]
    assert outputs == ["Supervisor", "Sub-agents", "Draft and brief"]


# ── live model lists ────────────────────────────────────────────────────

def test_refresh_with_good_keys_returns_live_prefixed_choices(fresh_client):
    FakeProvider.seen.clear()
    sup, sub, draft, status = fresh_client.predict(
        GOOD["deepinfra"], GOOD["together"], GOOD["fireworks"], api_name="/refresh_models")
    for update in (sup, sub, draft):
        choices = [c[0] if isinstance(c, (list, tuple)) else c for c in update["choices"]]
        assert "deepinfra:meta/llama-x" in choices and "together:acme/new-chat" in choices
        assert "fireworks:accounts/fireworks/models/qwen-z" in choices
        assert "deepseek-v4-flash" in choices            # existing choices are kept
        assert "deepinfra:bge-embed" not in choices and "together:acme/image-gen" not in choices
        assert "value" not in update or update["value"] in (None, ...)  # selection untouched
    assert "DeepInfra: live list (3 models)" not in status      # embedding model was dropped
    assert "DeepInfra: live list (2 models)" in status
    assert "Together: live list (2 models)" in status
    assert "Fireworks: live list (2 models)" in status
    assert ("/deepinfra/models", f"Bearer {GOOD['deepinfra']}") in FakeProvider.seen
    for secret in GOOD.values():
        assert secret not in repr((sup, sub, draft, status))


def test_refresh_rejects_bad_key_and_falls_back_without_echoing_it(fresh_client):
    bad = "totally-wrong-SECRET"
    sup, _, _, status = fresh_client.predict(bad, GOOD["together"], "", api_name="/refresh_models")
    assert "DeepInfra: fallback list (key rejected)" in status
    assert "Together: live list" in status
    assert "Fireworks: fallback list (no key)" in status
    choices = [c[0] if isinstance(c, (list, tuple)) else c for c in sup["choices"]]
    assert "deepinfra:deepseek-ai/DeepSeek-V3" in choices        # curated fallback
    assert "fireworks:accounts/fireworks/models/llama-v3p3-70b-instruct" in choices
    assert bad not in repr((sup, status))


def test_refresh_with_no_keys_uses_the_fallback_lists(fresh_client):
    sup, _, _, status = fresh_client.predict("", "", "", api_name="/refresh_models")
    assert status.count("fallback list (no key)") == 3
    choices = [c[0] if isinstance(c, (list, tuple)) else c for c in sup["choices"]]
    assert "together:openai/gpt-oss-120b" in choices


def test_second_refresh_from_one_session_is_throttled_over_the_wire(app_url, fresh_client):
    FakeProvider.seen.clear()
    first = fresh_client.predict(GOOD["deepinfra"], "", "", api_name="/refresh_models")
    assert "DeepInfra: live list" in first[3]
    n = len(FakeProvider.seen)
    second = fresh_client.predict(GOOD["deepinfra"], "", "", api_name="/refresh_models")
    assert second[3] == "Please wait a few seconds before refreshing again"
    assert len(FakeProvider.seen) == n                           # no outbound request
    # Another session is not affected by this one's throttle.
    other = gradio_client.Client(app_url, verbose=False)
    assert other.predict("", "", "", api_name="/refresh_models")[3].startswith("Model lists: ")


# ── search-tool status ──────────────────────────────────────────────────

def status_args(**over):
    from webui import app as ui
    a = dict(sup=["deepseek-v4-flash"], sub=["deepseek-v4-flash"], draft=["deepseek-v4-flash"],
             route_or=False, agents=["ResearchWeb"], web_on=True, tavily=False, exa=True,
             keys={}, extra="", ls_on=False, ls_key="", ls_project="deep-dog-2", ls_region="US")
    a.update(over)
    return [a["sup"], a["sub"], a["draft"], a["route_or"], a["agents"], a["web_on"], a["tavily"],
            a["exa"], *[a["keys"].get(n, "") for n, _ in ui.KEY_FIELDS], a["extra"],
            a["ls_on"], a["ls_key"], a["ls_project"], a["ls_region"]]


def test_search_status_endpoint_reports_key_states_and_ready_line(client):
    secret_keys = {"DEEPSEEK_API_KEY": "ds-SECRET", "EXA_API_KEY": "exa-SECRET"}
    out = client.predict(*status_args(keys=secret_keys), api_name="/search_status")
    assert tuple(out) == ("no key", "key entered", "Ready")
    out = client.predict(*status_args(tavily=True, keys={"DEEPSEEK_API_KEY": "ds-SECRET"}),
                         api_name="/search_status")
    assert out[0] == "no key" and out[1] == "no key"
    assert out[2].startswith("Missing keys:") and "TAVILY_API_KEY" in out[2]
    out = client.predict(*status_args(web_on=False, keys={"DEEPSEEK_API_KEY": "ds-SECRET"}),
                         api_name="/search_status")
    assert out[2] == "Ready (web search is off)"
    assert "SECRET" not in repr(out)


# ── the Run button over the real queue (private endpoint) ───────────────

def _run_dependency(config):
    comps = components(config)
    return next(d for d in config["dependencies"]
                if d["inputs"] and label_of(comps[d["inputs"][0]]) == "Research question")


def _queue_call(app_url, fn_index, data):
    session = uuid.uuid4().hex[:10]
    with httpx.Client(trust_env=False, timeout=60) as http:
        http.post(f"{app_url}/gradio_api/queue/join",
                  json={"data": data, "fn_index": fn_index, "session_hash": session}
                  ).raise_for_status()
        with http.stream("GET", f"{app_url}/gradio_api/queue/data",
                         params={"session_hash": session}) as stream:
            for line in stream.iter_lines():
                if line.startswith("data:"):
                    msg = json.loads(line[5:])
                    if msg.get("msg") == "process_completed":
                        assert msg.get("success"), msg
                        return msg["output"]["data"]
    pytest.fail("no process_completed message")


def test_run_button_maps_each_key_box_over_the_wire_without_starting_a_run(app_url, config):
    """Only one provider key typed, three roles needing three providers: the reply must name
    exactly the two keys that were not typed. A misaligned box would change that set."""
    comps = components(config)
    dep = _run_dependency(config)
    from webui import app as ui
    names_by_label = {label: name for name, label in ui.KEY_FIELDS}
    roles = {"Supervisor": ["deepinfra:m/x"], "Sub-agents": ["together:m/x"],
             "Draft and brief": ["fireworks:m/x"]}
    expect_missing = {"DEEPINFRA_API_KEY", "TOGETHER_API_KEY", "FIREWORKS_API_KEY"}
    for typed in ("DEEPINFRA_API_KEY", "TOGETHER_API_KEY", "FIREWORKS_API_KEY"):
        data = []
        for cid in dep["inputs"]:
            c, label = comps[cid], label_of(comps[cid])
            if label == "Research question":
                data.append("What is new?")
            elif label in roles:
                data.append(roles[label])
            elif label == "Web search on (master switch)":
                data.append(False)
            elif c["type"] == "textbox" and names_by_label.get(label) == typed:
                data.append("TYPED-SECRET-VALUE")
            elif c["type"] == "textbox" and label in names_by_label:
                data.append("")
            else:
                data.append(c["props"].get("value"))
        out = _queue_call(app_url, dep["id"], data)
        status = out[0]
        assert status.startswith("Missing keys for this setup"), status
        for name in expect_missing - {typed}:
            assert name in status
        assert typed not in status
        assert "TYPED-SECRET-VALUE" not in json.dumps(out)


# ── LangSmith controls ──────────────────────────────────────────────────

LS_SECRET = "lsv2_pt_E2E-SECRET-0123456789"


def test_langsmith_controls_are_served_with_a_password_key_and_a_two_choice_region(config):
    by_label = {label_of(c): c for c in config["components"]}
    assert by_label["Trace this run in LangSmith"]["type"] == "checkbox"
    assert by_label["Trace this run in LangSmith"]["props"]["value"] is False
    key = by_label["LangSmith API key"]
    assert key["type"] == "textbox" and key["props"]["type"] == "password"
    assert key["props"]["max_lines"] == 1 and not key["props"].get("value")
    project = by_label["LangSmith project"]
    assert project["props"]["value"] == "deep-dog-2" and project["props"]["max_length"] == 80
    region = by_label["LangSmith region"]
    assert region["type"] == "dropdown"
    assert region["props"]["choices"] == [
        ["US (https://api.smith.langchain.com)", "US"],
        ["EU (https://eu.api.smith.langchain.com)", "EU"]]
    assert region["props"]["allow_custom_value"] is False
    accordion = next(c for c in config["components"]
                     if c["type"] == "accordion" and label_of(c) == "Tracing (LangSmith)")
    assert accordion["props"]["open"] is False
    # No free-text endpoint box anywhere on the page.
    for c in config["components"]:
        label = (label_of(c) or "").lower()
        assert "endpoint" not in label and "url" not in label


def test_run_button_sends_the_langsmith_controls_after_the_other_keys_box(config):
    comps = components(config)
    labels = [label_of(comps[i]) for i in _run_dependency(config)["inputs"]]
    assert labels[-4:] == ["Trace this run in LangSmith", "LangSmith API key",
                           "LangSmith project", "LangSmith region"]
    assert labels[-5].startswith("Other keys")


def test_search_status_names_the_missing_langsmith_key_and_never_echoes_a_key(client):
    base = {"DEEPSEEK_API_KEY": "ds-SECRET", "EXA_API_KEY": "exa-SECRET"}
    out = client.predict(*status_args(keys=base, ls_on=True), api_name="/search_status")
    assert out[2] == "Missing keys: LANGSMITH_API_KEY"
    out = client.predict(*status_args(keys=base, ls_on=True, ls_key=LS_SECRET, ls_region="EU"),
                         api_name="/search_status")
    assert tuple(out) == ("no key", "key entered", "Ready")
    assert LS_SECRET not in repr(out)


def test_search_status_refuses_an_unknown_region_over_the_wire(client):
    with pytest.raises(Exception):
        client.predict(*status_args(ls_on=True, ls_key=LS_SECRET,
                                    ls_region="https://evil.example"), api_name="/search_status")


def test_run_button_with_tracing_ticked_and_no_key_reports_it_and_starts_nothing(app_url, config):
    from webui import app as ui
    key_labels = {label for _, label in ui.KEY_FIELDS}
    comps = components(config)
    dep = _run_dependency(config)
    data = []
    for cid in dep["inputs"]:
        c, label = comps[cid], label_of(comps[cid])
        if label == "Research question":
            data.append("What is new?")
        elif label == "Trace this run in LangSmith":
            data.append(True)
        elif label == "LangSmith API key":
            data.append("")
        elif label == "Web search on (master switch)":
            data.append(False)
        elif c["type"] == "textbox" and label in key_labels:
            data.append("")
        else:
            data.append(c["props"].get("value"))
    out = _queue_call(app_url, dep["id"], data)
    assert out[0].startswith("Missing keys for this setup")
    assert "LANGSMITH_API_KEY" in out[0]


# ── Space bundle ────────────────────────────────────────────────────────

def test_space_bundle_contains_every_module_app_imports(tmp_path):
    out = tmp_path / "space"
    subprocess.run(["bash", str(REPO / "webui" / "build_space.sh"), str(out)], check=True,
                   capture_output=True, text=True)
    assert (out / "model_catalog.py").exists() and (out / "app.py").exists()
    out2 = tmp_path / "space-docker"
    subprocess.run(["bash", str(REPO / "webui" / "build_space.sh"), str(out2), "docker"],
                   check=True, capture_output=True, text=True)
    assert (out2 / "model_catalog.py").exists()
    assert "model_catalog.py" in (REPO / "webui" / "Dockerfile").read_text()
