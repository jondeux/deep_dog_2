"""Test helper (not a test): start the real webui/app.py with the research engine replaced by
fakes, so the Run button can be driven over HTTP without any provider, search or LangSmith
network call.

    python tests/webui_fake_run_launcher.py MODE [--record PATH] --host 127.0.0.1 --port N

MODE
  ok          fake models complete a run; ``langsmith.Client`` is a recording fake that
              writes what it saw to PATH (json) on every flush.
  engine_fail the supervisor model raises an error that echoes every key of the run.
  escape      ``run_research`` itself raises an error that echoes every key (it bypasses
              the engine's own redaction).
  unredacted  ``run_research`` returns a failed result whose text echoes every key.
  settings    building the config fails with a message that echoes ECHO_SECRETS (a JSON
              list in the environment), only for a LangSmith project named "boom".

The patches are made in this process before app.py is run, so the app code under test is
unchanged.
"""
import json
import os
import runpy
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

mode = sys.argv[1]
args = sys.argv[2:]
record = None
if "--record" in args:
    i = args.index("--record")
    record = Path(args[i + 1])
    del args[i:i + 2]

import deep_research.integration as integration  # noqa: E402
from deep_research.integration import ResearchResult  # noqa: E402


def _echo(creds) -> str:
    return "provider rejected credentials " + " | ".join(
        str(v) for v in (getattr(creds, "keys", {}) or {}).values())


if mode == "ok":
    import json as _json

    import langsmith
    from test_langsmith_tracing import RecordingClient, _Factory

    class FileClient(RecordingClient):
        def flush(self, timeout=None):
            super().flush(timeout=timeout)
            with self.lock:
                calls = [{"method": m, "kw": kw} for m, kw in self.calls if m != "flush"]
            sessions = sorted({str(c["kw"].get("session_name")) for c in calls
                               if c["method"] == "create_run"})
            record.write_text(_json.dumps(
                {"ctor": self.ctor_kwargs, "sessions": sessions, "runs": len(calls),
                 "calls_blob": _json.dumps(calls, default=repr)}))

    langsmith.Client = FileClient
    integration.ModelFactory = _Factory

elif mode == "engine_fail":
    from conftest import FakeLLM, FakeModelFactory

    class Boom(FakeLLM):
        def __init__(self, kind, secrets_text):
            super().__init__(kind)
            self.text = secrets_text

        async def ainvoke(self, messages):
            raise RuntimeError(self.text)

        def with_structured_output(self, schema, **kw):
            return self

    class EchoFactory(FakeModelFactory):
        def supervisor(self, tools=None, **kw):
            return Boom("supervisor", _echo(self.credentials))

    integration.ModelFactory = EchoFactory

elif mode == "escape":
    async def run_research(prompt, config=None, runtime=None, credentials=None):
        raise RuntimeError(_echo(credentials))
    integration.run_research = run_research

elif mode == "unredacted":
    async def run_research(prompt, config=None, runtime=None, credentials=None):
        return ResearchResult(status="failed", failure="RuntimeError: " + _echo(credentials))
    integration.run_research = run_research

elif mode == "settings":
    from deep_research.run_config import RunConfig

    real_finalize = RunConfig.finalize

    def finalize(self, *a, **kw):
        if self.langsmith_project == "boom":
            raise ValueError("invalid setup " + " ".join(json.loads(os.environ["ECHO_SECRETS"])))
        return real_finalize(self, *a, **kw)

    RunConfig.finalize = finalize
else:
    raise SystemExit(f"unknown mode {mode!r}")

webui = REPO / "webui"
sys.path.insert(0, str(webui))
sys.argv = [str(webui / "app.py"), *args]
runpy.run_path(str(webui / "app.py"), run_name="__main__")
