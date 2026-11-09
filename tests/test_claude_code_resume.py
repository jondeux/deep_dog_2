"""Resume after a usage stop: the run's state is saved to deep-research-memory and a later run continues from it.

Free tests on the fake `claude` binary. The snapshot must survive a process boundary (JSON on disk), restore the
supervisor's history, keep the research clock where it stopped (waiting time is not counted), and never run on a
snapshot it does not understand."""
import asyncio
import json
import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import deep_research.integration as integration
from deep_research import model_picker as mp
from deep_research import resume as rs
from deep_research.events import EventCollector
from deep_research.models import Credentials
from conftest import api_config

CODE = "A1-S1#1"
REGISTRY = [{"code": CODE, "title": "Source One", "url": "https://example.com/one",
             "identifier": "https://example.com/one", "ref": "relevant", "agent": 1}]
THINK = {"content": "checking", "tool_calls": [{"name": "think_tool",
                                                "args": {"purpose": "assess", "reflection": "is this enough?"}}]}
DONE = {"content": "enough", "tool_calls": [{"name": "ResearchComplete", "args": {}}]}


def stopped_state(minutes_in=7.0):
    msgs = [HumanMessage(content="Here is the draft report: draft"), HumanMessage(content="brief"),
            AIMessage(content="", tool_calls=[{"id": "tc1", "name": "ResearchWeb", "args": {"research_topic": "T"}}]),
            ToolMessage(content=f"Finding: X is Y [{CODE}].", name="ResearchWeb", tool_call_id="tc1")]
    return {"messages": [HumanMessage(content="What is X?")], "research_brief": "brief", "draft_report": "draft",
            "supervisor_messages": msgs, "notes": [], "curated_sources": [], "source_registry": [dict(r) for r in REGISTRY],
            "start_time": time.time() - minutes_in * 60, "target_language": "English", "input_language": "English",
            "research_iterations": 2}


class Sink:
    """What a host does with artifacts: keep the resume state."""
    def __init__(self):
        self.snapshot = None

    def store(self, name, data):
        if name == "resume_state":
            self.snapshot = data


def cfg_for(**kw):
    return mp.apply_preset(api_config(**kw), "testing-framework")


def seed(cfg, **kw):
    return rs.build_snapshot("What is X?", cfg, stopped_state(**kw), run_id="r0", thread_id="t0",
                             reason="plan_window_guard", elapsed_minutes=7.0)


def go(snapshot, cfg, collector=None, sink=None, thread_id="t0-r1"):
    async def run():
        return await integration.resume_research(
            snapshot, config=cfg,
            runtime=integration.RuntimeOptions(run_id=thread_id, thread_id=thread_id, event_sink=collector,
                                               artifact_sink=sink),
            credentials=Credentials({}))
    return asyncio.run(run())


def sup_events(collector):
    return [e for e in collector.events if e.type == "supervisor_iteration"]


# ── the snapshot itself ───────────────────────────────────────────────────────
def test_snapshot_is_plain_json_and_round_trips_messages_and_tool_calls(tmp_path):
    snap = seed(cfg_for())
    path = rs.save_snapshot(tmp_path, snap)
    back = rs.load_snapshot(tmp_path, "t0")
    assert path.parent == tmp_path and back == json.loads(path.read_text())
    inputs = rs.restore_inputs(back)
    msgs = inputs["supervisor_messages"]
    assert isinstance(msgs[2], AIMessage) and msgs[2].tool_calls[0]["name"] == "ResearchWeb"
    assert isinstance(msgs[3], ToolMessage) and "Finding: X is Y" in msgs[3].content
    assert inputs["source_registry"][0]["code"] == CODE and inputs["research_brief"] == "brief"
    assert back["config"]["supervisor_model_fallback_chain"] == ["claudecode:sonnet-5.5@medium"]


def test_the_research_clock_continues_where_it_stopped():
    snap = seed(cfg_for(), minutes_in=0.0)            # state clock says "just started", the snapshot says 7 minutes in
    before = time.time()
    inputs = rs.restore_inputs(snap)
    spent = (time.time() - inputs["start_time"]) / 60
    assert 6.99 <= spent <= 7.1                      # waiting time between runs is not counted
    assert inputs["research_iterations"] == 2 and before > 0


def test_open_tool_calls_are_closed_so_the_history_stays_valid():
    open_call = AIMessage(content="", tool_calls=[{"id": "x1", "name": "ResearchWeb", "args": {"research_topic": "T"}},
                                                  {"id": "x2", "name": "think_tool", "args": {}}])
    answered = ToolMessage(content="done", name="think_tool", tool_call_id="x2")
    out = rs.close_open_tool_calls([HumanMessage(content="b"), open_call, answered])
    ids = [m.tool_call_id for m in out if isinstance(m, ToolMessage)]
    assert sorted(ids) == ["x1", "x2"] and "usage limit" in next(
        m.content for m in out if isinstance(m, ToolMessage) and m.tool_call_id == "x1")
    assert rs.close_open_tool_calls(out) == out       # nothing left open: unchanged


@pytest.mark.parametrize("breakage", [{"version": 99}, {"state": None}, {"prompt": ""}])
def test_a_snapshot_it_does_not_understand_is_refused(breakage):
    snap = {**seed(cfg_for()), **breakage}
    with pytest.raises(ValueError):
        rs.restore_inputs(snap)
    with pytest.raises(ValueError):
        rs.load_snapshot("/nonexistent-dir-xyz", "nope")


def test_snapshot_ids_cannot_escape_the_memory_folder(tmp_path):
    with pytest.raises(ValueError):
        rs.load_snapshot(tmp_path, "../../etc/passwd")
    snap = {**seed(cfg_for()), "root_id": "../evil"}
    with pytest.raises(ValueError):
        rs.save_snapshot(tmp_path, snap)


# ── stop, save, resume: through the real engine, real adapter, fake binary ────
def test_a_stopped_run_saves_its_state_and_a_resume_finishes_the_research(fake_claude, tmp_path):
    cfg = cfg_for(claude_code_usd_cap=1.0)
    fake_claude.script([{**{"text": json.dumps(THINK)}, "util": {"five_hour": 0.75}, "cost": 0.01},
                        {"text": f"# Report\n\nX is Y [{CODE}].", "cost": 0.02}])
    sink, events = Sink(), EventCollector()
    first = go(seed(cfg), cfg, events, sink)
    assert first.status == "completed" and first.run_metadata["stopped_early"]["reason"] == "plan_window_guard"
    snap = sink.snapshot
    assert snap and snap["root_id"] == "t0" and snap["resumes"] == 1 and snap["stopped_early"] == "plan_window_guard"
    assert 7.0 <= snap["elapsed_minutes"] < 8.5 and snap["state"]["research_iterations"] == 3
    assert any(m["type"] == "tool" for m in snap["state"]["supervisor_messages"][-2:])   # the think_tool exchange
    stop = [e for e in events.events if e.type == "run_stopped_early"][0]
    assert stop.payload["resumable"] is True

    # a later process: the file is all it has
    rs.save_snapshot(tmp_path, snap)
    loaded = rs.load_snapshot(tmp_path, "t0")
    fake_claude.script([{"text": json.dumps(DONE), "cost": 0.01},
                        {"text": f"# Final\n\nX is Y after more work [{CODE}].", "cost": 0.02}])
    events2 = EventCollector()
    second = go(loaded, cfg, events2, Sink(), thread_id="t0-r2")
    assert second.status == "completed" and "after more work" in second.final_report
    assert "stopped_early" not in second.run_metadata
    resumed_first = fake_claude.calls()[-2]               # the log spans both runs; the resumed run made two calls
    assert "Finding: X is Y" in resumed_first["stdin"] and "is this enough?" in resumed_first["stdin"]   # history restored
    it = sup_events(events2)[0]
    assert it.iteration == 4 and 7.0 <= it.payload["elapsed_minutes"] < 8.5          # counter and clock continue
    assert events2.types()[0] == "run_started" and "draft_started" not in events2.types()   # no scoping redone
    assert [e for e in events2.events if e.type == "run_started"][0].payload["resumed_from"] == "t0-r1"   # the run whose state this one continues


def test_resume_with_the_same_stop_again_chains_the_snapshot(fake_claude):
    cfg = cfg_for(claude_code_usd_cap=1.0)
    fake_claude.script([{"text": json.dumps(THINK), "util": {"five_hour": 0.75}, "cost": 0.01},
                        {"text": f"# Report [{CODE}]", "cost": 0.02}])
    sink = Sink()
    go(seed(cfg), cfg, sink=sink)
    s1 = sink.snapshot
    fake_claude.script([{"text": json.dumps(THINK), "util": {"five_hour": 0.76}, "cost": 0.01},
                        {"text": f"# Report 2 [{CODE}]", "cost": 0.02}])
    sink2 = Sink()
    go(s1, cfg, sink=sink2, thread_id="t0-r2")
    s2 = sink2.snapshot
    assert s2["root_id"] == "t0" and s2["resumes"] == 2 and s2["thread_id"] == "t0-r2"


def test_a_run_that_finishes_normally_saves_nothing(fake_claude):
    cfg = cfg_for()
    fake_claude.script([{"text": json.dumps(DONE), "cost": 0.01}, {"text": f"# Report [{CODE}]", "cost": 0.02}])
    sink = Sink()
    result = go(seed(cfg), cfg, sink=sink)
    assert result.status == "completed" and sink.snapshot is None


def test_config_comes_from_the_snapshot_when_none_is_given(fake_claude):
    cfg = mp.apply_preset(api_config(claude_code_usd_cap=2.5), "benchmark")
    fake_claude.script([{"text": json.dumps(DONE), "cost": 0.01}, {"text": f"# Report [{CODE}]", "cost": 0.02}])
    async def run():
        return await integration.resume_research(seed(cfg), runtime=integration.RuntimeOptions(run_id="x", thread_id="x"),
                                                 credentials=Credentials({}))
    result = asyncio.run(run())
    assert result.status == "completed"
    argv = fake_claude.calls()[0]["argv"]
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5" and argv[argv.index("--effort") + 1] == "medium"
    assert result.usage["claude_code"]["limits"]["usd_cap"] == 2.5


# ── CLI ───────────────────────────────────────────────────────────────────────
def load_cli():
    import importlib.util
    spec = importlib.util.spec_from_file_location("rr_resume", "run_research.py")
    rr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rr)
    return rr


SCRIPT = [
    {"text": json.dumps({"research_brief": "Brief", "input_language": "English", "target_language": "English"}), "cost": 0.001},
    {"text": "DRAFT scaffold text", "cost": 0.002},
    {"text": json.dumps(DONE), "cost": 0.003},
    {"text": "# Final report\n\nThe finished body.", "cost": 0.004},
]


def test_cli_saves_a_resume_file_when_a_cap_stops_the_run_and_resumes_from_it(tmp_path, capsys, fake_claude):
    rr = load_cli()
    mem = tmp_path / "deep-research-memory"
    fake_claude.script(SCRIPT)
    cfg = mp.apply_preset(api_config(claude_code_usd_cap=0.005), "testing-framework")
    with pytest.raises(RuntimeError):
        asyncio.run(rr.run_research("What is X?", tmp_path / "out", thread_id="cli1", config=cfg, memory_dir=mem))
    shown = capsys.readouterr().out
    assert "--resume cli1" in shown
    files = list(mem.glob("*.json"))
    assert [f.stem for f in files] == ["cli1"]

    fake_claude.script(SCRIPT[2:])
    snap = rs.load_snapshot(mem, "cli1")
    out = asyncio.run(rr.run_research(None, tmp_path / "out", config=None, memory_dir=mem, resume=snap))
    assert out["status"] == "completed" and "finished body" in out["final_report"]
    assert out["usage"]["calls"] == 2                      # the scoping calls were not repeated


def test_cli_resume_flag_loads_the_file_and_needs_no_prompt(tmp_path, monkeypatch):
    import sys
    rr = load_cli()
    mem = tmp_path / "mem"
    rs.save_snapshot(mem, seed(cfg_for()))
    seen = {}

    async def fake_run(prompt, output_dir, thread_id=None, clean_output=False, config=None, resume=None, memory_dir=None):
        seen.update(prompt=prompt, resume=resume, config=config, memory_dir=memory_dir, thread_id=thread_id)
        return {"output_file": "x", "status": "completed"}
    monkeypatch.setattr(rr, "run_research", fake_run)
    monkeypatch.setattr(sys, "argv", ["rr", "--resume", "t0", "--memory-dir", str(mem)])
    rr.main()
    assert seen["resume"]["root_id"] == "t0" and seen["prompt"] is None and seen["config"] is None
    monkeypatch.setattr(sys, "argv", ["rr", "--resume", "missing", "--memory-dir", str(mem)])
    with pytest.raises(SystemExit):
        rr.main()
    monkeypatch.setattr(sys, "argv", ["rr", "--resume", "t0", "--prompt", "q", "--memory-dir", str(mem)])
    with pytest.raises(SystemExit):
        rr.main()                                          # a prompt and a resume together make no sense
