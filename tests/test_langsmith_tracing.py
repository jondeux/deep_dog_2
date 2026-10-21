"""Opt-in LangSmith tracing: offline tests with a recording fake ``langsmith.Client``.

No network and no real LangSmith account. ``langsmith.Client`` is replaced by a
subclass that records the calls the tracing path makes
(``create_run`` / ``update_run`` / ``flush`` and, should the library route
through them, ``multipart_ingest`` / ``batch_ingest_runs``) instead of sending
them. Models are scripted fakes whose calls go through a real LangChain
``Runnable`` so each model call becomes a traced run.
"""

import asyncio
import contextlib
import json
import logging
import os
import threading
import time

import langsmith
import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

import deep_research.integration as integration
import deep_research.langsmith_tracing as lst
import deep_research.multi_agent_supervisor as supervisor_mod
from deep_research.cancellation import CancellationToken
from deep_research.events import EventCollector
from deep_research.integration import validate_credentials
from deep_research.models import Credentials
from deep_research.observer import Observer
from deep_research.run_config import RunConfig
from conftest import FakeLLM, FakeModelFactory, api_config

KEY = "lsv2_pt_0123456789abcdef0123456789abcdef_01234567"
KEY_B = "lsv2_pt_ffffffffffffffffffffffffffffffff_76543210"
PROVIDER_KEY = "sk-provider-secret-xyz"


# ── fakes ────────────────────────────────────────────────────────────────

class RecordingClient(langsmith.Client):
    """Real ``Client`` object (so the library's isinstance/attribute use works)
    whose outbound calls are recorded instead of sent."""

    instances: list = []

    def __init__(self, *args, **kwargs):
        self.ctor_kwargs = dict(kwargs)
        self.calls: list = []          # (method, kwargs)
        self.lock = threading.Lock()
        super().__init__(*args, auto_batch_tracing=False, **kwargs)
        RecordingClient.instances.append(self)

    def _rec(self, method, **kw):
        with self.lock:
            self.calls.append((method, kw))

    def create_run(self, name, inputs, run_type, **kwargs):
        self._rec("create_run", name=name, inputs=inputs, run_type=run_type, **kwargs)

    def update_run(self, run_id, **kwargs):
        self._rec("update_run", run_id=run_id, **kwargs)

    def multipart_ingest(self, *args, **kwargs):
        self._rec("multipart_ingest", args=args, **kwargs)

    def batch_ingest_runs(self, *args, **kwargs):
        self._rec("batch_ingest_runs", args=args, **kwargs)

    def flush(self, timeout=None):
        self._rec("flush")

    # helpers
    def runs(self):
        return [kw for m, kw in self.calls if m == "create_run"]

    def updates(self):
        return [kw for m, kw in self.calls if m == "update_run"]

    def flushed(self):
        return any(m == "flush" for m, _ in self.calls)


class RaisingClient(RecordingClient):
    """Every call fails, with the API key inside the error text."""

    def _boom(self, *a, **k):
        raise RuntimeError(f"401 Unauthorized for key {KEY}")

    create_run = update_run = multipart_ingest = batch_ingest_runs = _boom

    def flush(self, timeout=None):
        self._boom()


class HangingFlushClient(RecordingClient):
    """flush() blocks; it takes no ``timeout`` argument (older library shape)."""

    release = threading.Event()

    def flush(self):  # noqa: D102 - intentionally no timeout parameter
        self._rec("flush")
        HangingFlushClient.release.wait(30)


@pytest.fixture(autouse=True)
def _isolation(monkeypatch):
    RecordingClient.instances = []
    HangingFlushClient.release = threading.Event()
    monkeypatch.setattr(integration, "ModelFactory", _Factory)
    monkeypatch.setattr(_Factory, "delay", 0.0)
    # No ambient LangSmith configuration may leak into (or out of) a test.
    for name in list(os.environ):
        if name.startswith(("LANGSMITH_", "LANGCHAIN_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(langsmith, "Client", RecordingClient)
    yield
    HangingFlushClient.release.set()


class TracedLLM(FakeLLM):
    """FakeLLM whose call is a real LangChain run (a traced 'model call')."""

    delay = 0.0

    def with_structured_output(self, schema, **kwargs):
        bound = type(self)(self.kind)
        bound._schema = schema
        bound.delay = self.delay
        return bound

    async def ainvoke(self, messages):
        return await RunnableLambda(self._call, name=f"fake_{self.kind}_model").ainvoke(messages)

    async def _call(self, messages):
        if self.delay:
            await asyncio.sleep(self.delay)
        return await FakeLLM.ainvoke(self, messages)


class ScriptedSupervisor(TracedLLM):
    """First turn dispatches two parallel sub-agents, then completes."""

    def _respond(self, messages):
        if self.calls == 1:
            return AIMessage(content="dispatch", tool_calls=[
                {"id": "tc-a", "name": "ResearchWeb", "args": {"research_topic": "topic A"}},
                {"id": "tc-b", "name": "ResearchWeb", "args": {"research_topic": "topic B"}},
            ])
        return AIMessage(content="done", tool_calls=[
            {"id": "tc-done", "name": "ResearchComplete", "args": {}}])


class _Factory(FakeModelFactory):
    delay = 0.0
    supervisor_cls = ScriptedSupervisor

    def _mk(self, cls, kind):
        model = cls(kind)
        model.delay = self.delay
        return model

    def supervisor(self, tools=None, *, max_tokens=None, **kw):
        if max_tokens == 40000:
            return self._mk(TracedLLM, "writer")
        return self._mk(self.supervisor_cls, "supervisor")

    def draft_report(self, **kw):
        return self._mk(TracedLLM, "draft")

    def subagent(self, tools=None, **kw):
        return self._mk(TracedLLM, "subagent")

    def get(self, model_name, **kw):
        return self._mk(TracedLLM, "subagent")


def _cfg(**kw):
    return api_config(langsmith_tracing=True, **kw)


def _run(coro):
    return asyncio.run(coro)


def _go(*, config=None, creds=None, run_id="run-ls", token=None, collector=None):
    return integration.run_research(
        "What is new in LLM evaluation?",
        config=config if config is not None else _cfg(),
        runtime=integration.RuntimeOptions(
            run_id=run_id, thread_id=run_id, cancellation=token,
            event_sink=collector, console_enabled=False),
        credentials=creds if creds is not None else Credentials({"LANGSMITH_API_KEY": KEY}),
    )


def _all_calls(clients=None):
    clients = RecordingClient.instances if clients is None else clients
    return [c for client in clients for c in client.calls]


def _blob(obj) -> str:
    return json.dumps(obj, default=repr)


def _absent(needle, haystack):
    """Assert ``needle`` is nowhere in ``haystack`` without pytest's slow diff of huge blobs."""
    if not isinstance(haystack, (str, bytes)):
        haystack = _blob(haystack)
    if isinstance(haystack, bytes) and isinstance(needle, str):
        needle = needle.encode()
    if needle in haystack:
        pytest.fail(f"secret/forbidden value found in {len(haystack)} characters of output")


def _ancestry(client):
    """Map run id -> (name, parent id) from recorded create_run calls."""
    return {str(r["id"]): (r["name"], str(r["parent_run_id"]) if r.get("parent_run_id") else None)
            for r in client.runs()}


def _chain(runs, run_id):
    names = []
    while run_id is not None:
        name, run_id = runs[run_id]
        names.append(name)
    return names


# ── nesting / propagation ────────────────────────────────────────────────

def test_enabled_run_records_one_root_with_graph_and_subagent_runs_nested():
    cfg = _cfg(langsmith_project="my-research")
    result = _run(_go(config=cfg, run_id="run-nest"))
    assert result.status == "completed", result.failure

    assert len(RecordingClient.instances) == 1
    client = RecordingClient.instances[0]
    assert client.ctor_kwargs == {"api_key": KEY, "omit_traced_runtime_info": True}          # library-default endpoint

    runs = _ancestry(client)
    roots = [rid for rid, (_, parent) in runs.items() if parent is None]
    assert len(roots) == 1, "exactly one root run per research run"
    assert runs[roots[0]][0] == "deep_dog_2:run-nest"
    assert len(runs) > 10, "graph nodes, sub-agents and model calls must be traced"

    # Every run belongs to the chosen project and is a descendant of the root.
    for kw in client.runs():
        assert kw["session_name"] == "my-research"
        assert _chain(runs, str(kw["id"]))[-1] == "deep_dog_2:run-nest"
        meta = kw["extra"]["metadata"]
        assert meta["run_id"] == "run-nest"
        assert "deep_dog_2" in kw["tags"]
    for kw in client.updates():
        assert kw["session_name"] == "my-research" if "session_name" in kw else True

    # Children from the supervisor, the sub-agent graphs and the model calls.
    names = {name for name, _ in runs.values()}
    assert "supervisor_tools" in names
    assert "fake_subagent_model" in names
    assert "fake_supervisor_model" in names
    assert "fake_writer_model" in names

    # The root was closed and the client flushed.
    assert any(u["run_id"] == roots[0] or str(u["run_id"]) == roots[0] for u in client.updates())
    assert client.flushed()

    # Two sub-agents ran concurrently (asyncio.gather inside supervisor_tools):
    # each sub-agent model call sits under supervisor_tools -> sub-agent graph,
    # in two distinct branches that share the same root and project.
    sub_calls = [rid for rid, (name, _) in runs.items() if name == "fake_subagent_model"]
    assert len(sub_calls) >= 2
    branch_parents = set()
    for rid in sub_calls:
        chain = _chain(runs, rid)
        assert "supervisor_tools" in chain
        assert chain[-1] == "deep_dog_2:run-nest"
        branch_parents.add(runs[rid][1])
    assert len(branch_parents) >= 2, "concurrent branches must be separate subtrees"

    # metadata also lands in the result snapshot, without the key.
    assert result.run_metadata["langsmith"] == {
        "requested": True, "active": True, "project": "my-research"}


def test_default_project_and_endpoint_from_credentials():
    creds = Credentials({"LANGSMITH_API_KEY": KEY,
                         "LANGSMITH_ENDPOINT": "https://eu.api.smith.langchain.com"})
    result = _run(_go(creds=creds))
    assert result.status == "completed"
    client = RecordingClient.instances[0]
    assert client.ctor_kwargs == {"api_key": KEY, "api_url": "https://eu.api.smith.langchain.com",
                                  "omit_traced_runtime_info": True}
    assert {kw["session_name"] for kw in client.runs()} == {"deep-dog-2"}
    meta = client.runs()[0]["extra"]["metadata"]
    for field in ("run_id", "profile", "supervisor_model_chain", "draft_report_model_chain",
                  "subagent_model_chain", "web_search_engine", "thinking_mode"):
        assert field in meta
    _absent("eu.api.smith", _blob(_all_calls()))


def test_context_reaches_to_thread_and_concurrent_tasks():
    """The mechanism: contextvars. to_thread, create_task and gather copy them."""
    cfg = _cfg(langsmith_project="ctx")

    async def main():
        async with lst.langsmith_scope(cfg, Credentials({"LANGSMITH_API_KEY": KEY}), "ctx-run"):
            def sync_work(x):
                return RunnableLambda(lambda v: v, name=f"threaded_{x}").invoke(x)

            async def task_work(x):
                await asyncio.sleep(0.01)
                return await RunnableLambda(_ident, name=f"task_{x}").ainvoke(x)

            await asyncio.gather(
                asyncio.to_thread(sync_work, 1),
                asyncio.create_task(task_work(2)),
                task_work(3),
            )

    async def _ident(v):
        return v

    _run(main())
    client = RecordingClient.instances[0]
    runs = _ancestry(client)
    for name in ("threaded_1", "task_2", "task_3"):
        rid = next(r for r, (n, _) in runs.items() if n == name)
        assert _chain(runs, rid) == [name, "deep_dog_2:ctx-run"]


# ── off / no key ─────────────────────────────────────────────────────────

def test_flag_off_records_nothing_even_with_a_key():
    cfg = api_config()                                  # langsmith_tracing False
    assert cfg.langsmith_tracing is False
    result = _run(_go(config=cfg))
    assert result.status == "completed"
    assert RecordingClient.instances == [], "no client may even be constructed"
    assert result.run_metadata["langsmith"] == {
        "requested": False, "active": False, "project": None}


def test_flag_off_leaves_operator_environment_tracing_untouched(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    from langsmith.run_helpers import get_tracing_context

    async def main():
        before = get_tracing_context()
        async with lst.langsmith_scope(api_config(), Credentials({"LANGSMITH_API_KEY": KEY}), "r"):
            inside = get_tracing_context()
        return before, inside, get_tracing_context()

    before, inside, after = _run(main())
    assert before == inside == after
    assert RecordingClient.instances == []


def test_flag_on_without_key_runs_untraced_and_preflight_reports_it(caplog):
    creds = Credentials({"DEEPSEEK_API_KEY": PROVIDER_KEY})
    cfg = _cfg()
    check = validate_credentials(cfg, creds)
    assert "LANGSMITH_API_KEY" in check.missing_required
    assert not check.ok

    collector = EventCollector()
    with caplog.at_level(logging.WARNING):
        result = _run(_go(config=cfg, creds=creds, collector=collector))
    assert result.status == "completed"
    assert RecordingClient.instances == []
    validated = next(e for e in collector.events if e.type == "config_validated")
    assert "LANGSMITH_API_KEY" in validated.payload["missing_required"]
    warnings = [r for r in caplog.records if r.name == lst.__name__]
    assert len(warnings) == 1 and "LANGSMITH_API_KEY is not set" in warnings[0].getMessage()
    assert result.run_metadata["langsmith"]["active"] is False


def test_preflight_ok_when_key_present_or_flag_off():
    assert "LANGSMITH_API_KEY" not in validate_credentials(
        _cfg(), Credentials({"LANGSMITH_API_KEY": KEY})).missing_required
    assert "LANGSMITH_API_KEY" not in validate_credentials(
        api_config(), Credentials()).missing_required


# ── secrecy ──────────────────────────────────────────────────────────────

def test_key_is_in_no_payload_event_log_trace_or_snapshot(caplog):
    collector = EventCollector()
    creds = Credentials({"LANGSMITH_API_KEY": KEY, "DEEPSEEK_API_KEY": PROVIDER_KEY,
                         "LANGSMITH_ENDPOINT": "https://user:pw-secret@ls.example.invalid"})
    with caplog.at_level(logging.DEBUG):
        result = _run(_go(config=_cfg(langsmith_project="p"), creds=creds, collector=collector))
    assert result.status == "completed"

    client = RecordingClient.instances[0]
    assert client.ctor_kwargs["api_key"] == KEY           # the only place the key goes
    recorded = _blob([c for c in client.calls])
    _absent(KEY, recorded)
    _absent("pw-secret", recorded)
    _absent(PROVIDER_KEY, recorded)
    _absent(KEY, _blob([e.to_dict() for e in collector.events]))
    _absent(KEY, _blob(result.to_dict()))
    _absent(KEY, _blob(_cfg().to_dict()))
    _absent(KEY, "\n".join(r.getMessage() for r in caplog.records))
    _absent(KEY, _blob(result.logs))


def test_failing_client_error_text_is_redacted_in_logs_and_trace_records(monkeypatch, caplog):
    monkeypatch.setattr(langsmith, "Client", RaisingClient)
    with caplog.at_level(logging.DEBUG, logger=lst.__name__):
        result = _run(_go())
    assert result.status == "completed"
    ours = [r for r in caplog.records if r.name == lst.__name__ and r.levelno >= logging.WARNING]
    assert len(ours) == 1, "exactly one short warning per run"
    _absent(KEY, ours[0].getMessage())
    assert "[REDACTED]" in ours[0].getMessage()
    assert len(ours[0].getMessage()) < 400
    # The trace record (observer path) carries the same redacted text.
    warns = [r for r in result.logs if r["kind"] == "langsmith_warning"]
    assert len(warns) == 1
    _absent(KEY, _blob(result.logs))
    _absent(KEY, "\n".join(r.getMessage() for r in caplog.records if r.name == lst.__name__))


def test_observer_redacts_langsmith_key_in_trace_content_and_error_paths(monkeypatch):
    creds = Credentials({"LANGSMITH_API_KEY": KEY})
    obs = Observer("r", api_config(), credentials=creds, console_enabled=False)
    obs.emit_trace("x", error=f"HTTPError: 403 for key {KEY}",
                   payload={"nested": [f"Bearer {KEY}"]})
    blob = _blob(obs.get_trace_records())
    _absent(KEY, blob)
    assert "[REDACTED]" in blob

    # CLI path: the key only exists in the environment.
    monkeypatch.setenv("LANGSMITH_API_KEY", KEY_B)
    env_creds = Credentials.from_env()
    assert env_creds.get("LANGSMITH_API_KEY") == KEY_B
    assert validate_credentials(_cfg(), env_creds).missing_required.count("LANGSMITH_API_KEY") == 0
    obs2 = Observer("r2", api_config(), credentials=env_creds, console_enabled=False)
    obs2.emit_trace("x", error=f"boom {KEY_B}")
    _absent(KEY_B, _blob(obs2.get_trace_records()))


def test_cli_env_key_is_used_when_credentials_come_from_env(monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", KEY_B)
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://eu.api.smith.langchain.com")
    result = _run(_go(creds=Credentials.from_env()))
    assert result.status == "completed"
    assert RecordingClient.instances[0].ctor_kwargs == {
        "api_key": KEY_B, "api_url": "https://eu.api.smith.langchain.com",
        "omit_traced_runtime_info": True}


def test_server_env_key_and_endpoint_are_ignored_when_credentials_forbid_env(monkeypatch):
    """Web UI rule: a visitor who ticks Trace without a key never gets the operator's account."""
    monkeypatch.setenv("LANGSMITH_API_KEY", KEY_B)
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://operator.example.invalid")
    creds = Credentials(keys={"DEEPSEEK_API_KEY": PROVIDER_KEY}, langsmith_from_env=False)
    assert creds.get("LANGSMITH_API_KEY") is None and not creds.has("LANGSMITH_API_KEY")
    assert creds.get("LANGSMITH_ENDPOINT") is None
    assert creds.get("DEEPSEEK_API_KEY") == PROVIDER_KEY
    check = validate_credentials(_cfg(), creds)
    assert "LANGSMITH_API_KEY" in check.missing_required and not check.ok
    result = _run(_go(config=_cfg(), creds=creds))
    assert result.status == "completed"
    assert RecordingClient.instances == []
    assert result.run_metadata["langsmith"]["active"] is False
    # A typed per-run key is still honoured under the same rule.
    typed = Credentials(keys={"LANGSMITH_API_KEY": KEY}, langsmith_from_env=False)
    assert typed.get("LANGSMITH_API_KEY") == KEY and typed.get("LANGSMITH_ENDPOINT") is None


def test_process_environment_is_byte_identical_around_a_run():
    before = dict(os.environ)
    creds = Credentials({"LANGSMITH_API_KEY": KEY,
                         "LANGSMITH_ENDPOINT": "https://eu.api.smith.langchain.com"})
    result = _run(_go(config=_cfg(langsmith_project="envcheck"), creds=creds))
    assert result.status == "completed"
    assert RecordingClient.instances
    assert dict(os.environ) == before
    assert not any(k.startswith(("LANGSMITH_", "LANGCHAIN_")) for k in os.environ)


# ── config ───────────────────────────────────────────────────────────────

def test_runconfig_fields_roundtrip_and_never_hold_a_key():
    default = RunConfig()
    assert default.langsmith_tracing is False and default.langsmith_project is None
    cfg = RunConfig(langsmith_tracing=True, langsmith_project="proj").finalize()
    snap = json.loads(json.dumps(cfg.to_dict()))
    assert snap["langsmith_tracing"] is True and snap["langsmith_project"] == "proj"
    assert RunConfig.from_dict(snap) == cfg
    assert not any("key" in k.lower() or "secret" in k.lower() for k in snap
                   if k.startswith("langsmith"))
    assert lst.effective_project(RunConfig(langsmith_project="  ")) == "deep-dog-2"


# ── failures never affect the run ────────────────────────────────────────

def test_client_that_raises_on_every_call_does_not_fail_the_run(monkeypatch):
    monkeypatch.setattr(langsmith, "Client", RaisingClient)
    started = time.monotonic()
    result = _run(_go())
    assert result.status == "completed", result.failure
    assert result.final_report
    assert time.monotonic() - started < 11
    assert result.run_metadata["langsmith"]["active"] is False


def test_client_constructor_failure_does_not_fail_the_run(monkeypatch):
    def broken(**kwargs):
        raise ValueError(f"bad key {kwargs['api_key']}")

    monkeypatch.setattr(langsmith, "Client", broken)
    result = _run(_go())
    assert result.status == "completed", result.failure
    _absent(KEY, _blob(result.logs))


def test_flush_error_does_not_fail_the_run(monkeypatch):
    class FlushRaises(RecordingClient):
        def flush(self, timeout=None):
            raise OSError(f"connection reset {KEY}")

    monkeypatch.setattr(langsmith, "Client", FlushRaises)
    result = _run(_go())
    assert result.status == "completed"
    _absent(KEY, _blob(result.logs))
    assert any(r["kind"] == "langsmith_warning" for r in result.logs)


def test_hanging_flush_is_bounded_by_the_timeout(monkeypatch):
    monkeypatch.setattr(langsmith, "Client", HangingFlushClient)
    monkeypatch.setattr(lst, "FLUSH_TIMEOUT_SECONDS", 0.4)
    started = time.monotonic()
    result = _run(_go())
    elapsed = time.monotonic() - started
    assert result.status == "completed"
    assert HangingFlushClient.instances[0].flushed()
    assert any(r["kind"] == "langsmith_warning" for r in result.logs)
    assert elapsed < 8, "a hung flush must not hold the run for its full duration"


def test_hanging_flush_with_default_timeout_adds_at_most_eleven_seconds(monkeypatch):
    monkeypatch.setattr(langsmith, "Client", HangingFlushClient)
    assert lst.FLUSH_TIMEOUT_SECONDS == 10.0
    started = time.monotonic()
    result = _run(_go())
    elapsed = time.monotonic() - started
    assert result.status == "completed"
    assert 9.5 <= elapsed <= 11.5, elapsed


def test_hanging_flush_does_not_block_the_event_loop(monkeypatch):
    monkeypatch.setattr(langsmith, "Client", HangingFlushClient)
    monkeypatch.setattr(lst, "FLUSH_TIMEOUT_SECONDS", 0.6)

    async def main():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        t = asyncio.create_task(ticker())
        async with lst.langsmith_scope(_cfg(), Credentials({"LANGSMITH_API_KEY": KEY}), "loop"):
            pass
        t.cancel()
        return ticks

    assert _run(main()) >= 15, "the loop must keep running while the flush is waited on"


# ── isolation between concurrent runs ────────────────────────────────────

def test_two_concurrent_runs_do_not_mix_projects_keys_or_runs(monkeypatch):
    monkeypatch.setattr(_Factory, "delay", 0.02)        # force interleaving

    async def main():
        return await asyncio.gather(
            _go(config=_cfg(langsmith_project="proj-a"), run_id="run-a",
                creds=Credentials({"LANGSMITH_API_KEY": KEY})),
            _go(config=_cfg(langsmith_project="proj-b"), run_id="run-b",
                creds=Credentials({"LANGSMITH_API_KEY": KEY_B})),
            _go(config=api_config(), run_id="run-off",
                creds=Credentials({"LANGSMITH_API_KEY": KEY})),
        )

    ra, rb, roff = _run(main())
    assert {ra.status, rb.status, roff.status} == {"completed"}
    clients = {c.ctor_kwargs["api_key"]: c for c in RecordingClient.instances}
    assert set(clients) == {KEY, KEY_B}, "no client for the run with the flag off"
    for key, project, run_id in ((KEY, "proj-a", "run-a"), (KEY_B, "proj-b", "run-b")):
        client = clients[key]
        assert client.runs()
        assert {kw["session_name"] for kw in client.runs()} == {project}
        assert {kw["extra"]["metadata"]["run_id"] for kw in client.runs()} == {run_id}
        runs = _ancestry(client)
        assert [n for n, p in runs.values() if p is None] == [f"deep_dog_2:{run_id}"]
        assert client.flushed()
    # Neither client ever saw the other run's id anywhere.
    _absent("run-b", clients[KEY].calls)
    _absent("run-a", clients[KEY_B].calls)
    _absent("run-off", _blob(_all_calls()))


# ── cancellation and failure still flush ─────────────────────────────────

def test_host_cancelled_run_still_closes_root_and_flushes():
    token = CancellationToken()
    token.cancel("stop")
    result = _run(_go(token=token))
    assert result.status in ("cancelled", "partial")
    client = RecordingClient.instances[0]
    assert client.runs() and client.flushed()
    root = next(str(r["id"]) for r in client.runs() if not r.get("parent_run_id"))
    assert any(str(u["run_id"]) == root for u in client.updates())


def test_failed_run_still_closes_root_and_flushes(monkeypatch):
    import deep_research.research_agent_full as full

    real_build = full.build_research_agent

    class ExplodingGraph:
        """Runs the real graph, then fails mid-stream like a crashed node."""

        def __init__(self, graph):
            self._graph = graph

        async def astream(self, *args, **kwargs):
            async for chunk in self._graph.astream(*args, **kwargs):
                yield chunk
                raise RuntimeError("graph exploded")

    monkeypatch.setattr(full, "build_research_agent",
                        lambda **kw: ExplodingGraph(real_build(**kw)))
    result = _run(_go())
    assert result.status in ("failed", "partial")
    assert "graph exploded" in (result.failure or "")
    client = RecordingClient.instances[0]
    assert client.runs() and client.flushed()
    root = next(str(r["id"]) for r in client.runs() if not r.get("parent_run_id"))
    assert any(str(u["run_id"]) == root for u in client.updates())


def test_task_cancellation_propagates_and_still_flushes(monkeypatch):
    monkeypatch.setattr(_Factory, "delay", 0.3)

    async def main():
        task = asyncio.create_task(_go())
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    _run(main())
    client = RecordingClient.instances[0]
    assert client.runs() and client.flushed()
    root = next(str(r["id"]) for r in client.runs() if not r.get("parent_run_id"))
    errored = [u for u in client.updates() if str(u["run_id"]) == root]
    assert errored and errored[-1].get("error"), "the cancelled root run records an error"


# ── real Client, stubbed transport ───────────────────────────────────────

def test_real_client_over_stub_session_sends_only_to_endpoint_with_key_only_in_header(monkeypatch):
    """The real batching path (not the recording fake): runs go out as one
    multipart POST to the configured endpoint, the key only as ``x-api-key``."""
    import requests

    class Session(requests.Session):
        def __init__(self):
            super().__init__()
            self.sent = []

        def request(self, method, url, **kwargs):
            self.sent.append((method, url, kwargs))
            response = requests.Response()
            response.status_code = 200
            response._content = b"{}"
            response.headers["content-type"] = "application/json"
            response.url = url
            return response

    session = Session()
    real_client = RecordingClient.__mro__[1]          # the genuine langsmith.Client
    monkeypatch.setattr(langsmith, "Client", lambda **kw: real_client(session=session, **kw))
    creds = Credentials({"LANGSMITH_API_KEY": KEY,
                         "LANGSMITH_ENDPOINT": "https://ls.example.invalid"})

    async def main():
        async with lst.langsmith_scope(_cfg(langsmith_project="wire"), creds, "wire-run"):
            async def work(x):
                return x
            await RunnableLambda(work, name="unit_of_work").ainvoke("hello")

    _run(main())
    assert session.sent, "flush on exit must have delivered the runs"
    assert all(url.startswith("https://ls.example.invalid/") for _, url, _ in session.sent)
    posts = [(url, kw) for method, url, kw in session.sent if method == "POST"]
    assert posts and all(url.endswith("/runs/multipart") for url, _ in posts)
    for _, _, kw in session.sent:
        assert kw["headers"]["x-api-key"] == KEY
        body = kw.get("data")
        if isinstance(body, bytes):
            _absent(KEY.encode(), body)
    body = b"".join(kw["data"] for _, kw in posts)
    for needle in (b"deep_dog_2:wire-run", b"unit_of_work", b"wire", b"wire-run"):
        assert needle in body


def test_operator_langsmith_env_vars_are_not_copied_into_run_metadata(monkeypatch):
    """The client's runtime-info collection would otherwise leak LANGSMITH_*/LANGCHAIN_* values."""
    import requests

    class Session(requests.Session):
        def __init__(self):
            super().__init__()
            self.sent = []

        def request(self, method, url, **kwargs):
            self.sent.append((method, url, kwargs))
            response = requests.Response()
            response.status_code = 200
            response._content = b"{}"
            response.headers["content-type"] = "application/json"
            response.url = url
            return response

    session = Session()
    real_client = RecordingClient.__mro__[1]
    monkeypatch.setattr(langsmith, "Client", lambda **kw: real_client(session=session, **kw))
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://operator-endpoint.example.invalid")
    monkeypatch.setenv("LANGSMITH_PROJECT", "operator-private-project")
    creds = Credentials({"LANGSMITH_API_KEY": KEY,
                         "LANGSMITH_ENDPOINT": "https://ls.example.invalid"},
                        langsmith_from_env=False)

    async def main():
        async with lst.langsmith_scope(_cfg(langsmith_project="wire"), creds, "env-run"):
            async def work(x):
                return x
            await RunnableLambda(work, name="unit_of_work").ainvoke("hello")

    _run(main())
    posts = [kw for method, _, kw in session.sent if method == "POST"]
    assert posts
    body = b"".join(kw["data"] for kw in posts if isinstance(kw.get("data"), bytes))
    assert b"unit_of_work" in body
    _absent(b"operator-endpoint", body)
    _absent(b"operator-private-project", body)
    _absent(b"LANGSMITH_ENDPOINT", body)


# ── web-UI hardening: operator environment must not reach a visitor's run ──

def _redaction_straddle(prefix="sk-"):
    """A fake key whose text straddles char 200 of the exception message."""
    key = prefix + "".join(chr(ord("a") + (i * 7) % 26) for i in range(60))
    return key


@pytest.mark.parametrize("filler", [150, 170, 185])
def test_secret_straddling_the_warning_cut_leaves_no_fragment(monkeypatch, caplog, filler):
    # The cut is at 200 chars of str(exc); the key starts `filler` chars in, so the
    # first 200-filler chars of it would survive a slice-then-redact.
    long_key = "lsv2_pt_" + "".join(chr(ord("a") + (i * 7) % 26) for i in range(60)) + "_ZZ"
    text = "x" * filler + long_key

    class Boom(RecordingClient):
        def __init__(self, *a, **k):
            raise RuntimeError(text)

    monkeypatch.setattr(langsmith, "Client", Boom)
    creds = Credentials({"LANGSMITH_API_KEY": long_key})
    with caplog.at_level(logging.DEBUG, logger=lst.__name__):
        result = _run(_go(creds=creds))
    assert result.status == "completed"
    ours = [r.getMessage() for r in caplog.records if r.name == lst.__name__]
    assert len(ours) == 1 and "[REDACTED]" in ours[0]
    traces = [r for r in result.logs if r["kind"] == "langsmith_warning"]
    assert len(traces) == 1
    blob = ours[0] + _blob(traces)
    for i in range(0, len(long_key) - 7):
        assert long_key[i:i + 8] not in blob, f"fragment {i} of the key is visible"


def test_redact_is_longest_first():
    short, long_ = "abcdefghij", "abcdefghij-and-more-secret"
    assert lst._redact(f"see {long_} end", [short, long_]) == "see [REDACTED] end"
    assert lst._redact(f"see {long_} end", [long_, short]) == "see [REDACTED] end"


class _Resp:
    status_code = 200
    content = b"{}"
    text = "{}"
    headers: dict = {}
    ok = True

    def json(self):
        return {}

    def raise_for_status(self):
        return None


@pytest.fixture
def wire(monkeypatch):
    """Real langsmith Clients (incl. LangChain's env-built one) with a stub Session.send
    that records every request instead of sending it."""
    import requests
    from langsmith import utils as ls_utils

    requests_seen: list = []

    def send(self, request, **kwargs):
        requests_seen.append((request.method, request.url, dict(request.headers)))
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"{}"
        resp.url = request.url
        resp.request = request
        return resp

    monkeypatch.setattr(requests.Session, "send", send)
    monkeypatch.setattr(langsmith, "Client", RecordingClient.__mro__[1])  # the real Client
    for cached in (ls_utils.get_env_var, ls_utils.tracing_is_enabled):
        with contextlib.suppress(AttributeError):
            cached.cache_clear()
    import langsmith.run_trees as rt
    monkeypatch.setattr(rt, "_CLIENT", None)      # LangChain's env-built client, per test
    return requests_seen


def _operator_env(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", KEY_B)
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://operator.example.invalid")
    from langsmith import utils as ls_utils
    ls_utils.get_env_var.cache_clear()
    with contextlib.suppress(AttributeError):
        ls_utils.tracing_is_enabled.cache_clear()


def _flush_default_clients():
    """Flush LangChain's env-built client if the run created one (it does so only when
    its env tracer fired); creating it here would itself make a request."""
    import langsmith.run_trees as rt
    if rt._CLIENT is not None:
        rt._CLIENT.flush()


def test_web_ui_unticked_run_sends_nothing_even_with_operator_env_tracing(monkeypatch, wire):
    _operator_env(monkeypatch)
    creds = Credentials(keys={"DEEPSEEK_API_KEY": PROVIDER_KEY}, langsmith_from_env=False)
    result = _run(_go(config=api_config(langsmith_tracing=False), creds=creds))
    assert result.status == "completed"
    _flush_default_clients()
    assert wire == [], f"requests leaked to the operator account: {wire[:2]}"


def test_web_ui_builders_with_ls_off_send_nothing_with_operator_env_tracing(monkeypatch, wire):
    ui = pytest.importorskip("webui.app")
    _operator_env(monkeypatch)
    cfg = ui.build_config(["deepseek-v4-flash"], ["deepseek-v4-flash"], ["deepseek-v4-flash"],
                          False, "auto", 5, 5, 3, 2, ["ResearchWeb"], "none", ls_on=False)
    creds = ui.build_credentials([""] * len(ui.KEY_FIELDS), "", ls_on=False)
    assert cfg.langsmith_tracing is False and creds.langsmith_from_env is False
    result = _run(_go(config=cfg, creds=creds))
    assert result.status == "completed"
    _flush_default_clients()
    assert wire == []


def test_cli_unticked_run_keeps_operator_env_tracing(monkeypatch, wire):
    """Documented: from the command line the operator's own env tracing is not interfered with."""
    _operator_env(monkeypatch)
    result = _run(_go(config=api_config(langsmith_tracing=False), creds=Credentials.from_env()))
    assert result.status == "completed"
    _flush_default_clients()
    assert wire, "operator env tracing should still trace a command-line run"
    assert all("operator.example.invalid" in url for _, url, _ in wire)


def test_web_ui_client_does_not_take_workspace_id_from_the_operator_env(monkeypatch, wire):
    _operator_env(monkeypatch)
    monkeypatch.setenv("LANGSMITH_WORKSPACE_ID", "operator-workspace")
    monkeypatch.delenv("LANGSMITH_ENDPOINT")
    created = []
    real = langsmith.Client

    class Spy(real):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            created.append(self)

    monkeypatch.setattr(langsmith, "Client", Spy)
    creds = Credentials(keys={"LANGSMITH_API_KEY": KEY, "LANGSMITH_ENDPOINT": "https://visitor.example.invalid"},
                        langsmith_from_env=False)
    result = _run(_go(creds=creds))
    assert result.status == "completed"
    assert created and all(c._workspace_id is None for c in created)
    # ... while a command-line run (env credentials) still uses the operator's workspace.
    created.clear()
    _run(_go(creds=Credentials.from_env(), run_id="run-cli"))
    assert created and all(c._workspace_id == "operator-workspace" for c in created)
