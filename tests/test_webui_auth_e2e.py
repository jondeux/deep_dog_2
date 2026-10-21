"""The optional ACCESS_PASSWORD gate, against the REAL app in a subprocess.

A non-ASCII password (or attempt) used to make hmac.compare_digest raise TypeError, so
/login answered 500."""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

pytest.importorskip("gradio")

REPO = Path(__file__).resolve().parents[1]
PASSWORD = "pässwörd✓"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def app_url():
    port = free_port()
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    env.update(NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost",
               GRADIO_ANALYTICS_ENABLED="False", PYTHONUNBUFFERED="1",
               ACCESS_PASSWORD=PASSWORD, PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(REPO / "tests" / "webui_launcher.py"), json.dumps({}),
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 90
    try:
        while time.time() < deadline:
            if proc.poll() is not None:
                pytest.fail("app exited early:\n" + proc.stdout.read())
            try:
                if httpx.get(url, trust_env=False, timeout=2, follow_redirects=False).status_code in (200, 307):
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


def login(app_url, password):
    return httpx.post(f"{app_url}/login", data={"username": "anyone", "password": password},
                      trust_env=False, timeout=10, follow_redirects=False)


def test_non_ascii_password_logs_in_and_sets_the_session_cookie(app_url):
    resp = login(app_url, PASSWORD)
    assert resp.status_code == 200, resp.text
    assert any("access-token" in name for name in resp.cookies.keys()) or "set-cookie" in resp.headers


@pytest.mark.parametrize("attempt", ["pässwörd✗", "wrong", "päss", "✓" * 40, "pässwörd✓ "])
def test_wrong_attempts_get_the_normal_failure_status_not_500(app_url, attempt):
    resp = login(app_url, attempt)
    assert resp.status_code in (400, 401), (resp.status_code, resp.text)
    assert "set-cookie" not in resp.headers
