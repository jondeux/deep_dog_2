"""Free tests for the claudecode: provider. A fake `claude` binary stands in; nothing touches the network."""
import asyncio
import json
import os
import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel

from deep_research.providers import claude_code as cc
from deep_research.providers.claude_code import env as ccenv
from deep_research.providers.claude_code import transcript as tr
from deep_research.providers.claude_code.governor import Governor, GovernorStop
from deep_research.multi_agent_supervisor import _classify_error


class Search(BaseModel):
    """Search the web."""
    q: str


class Brief(BaseModel):
    research_brief: str


def mk(name="claudecode:sonnet-5.5@high", gov=None, **kw):
    m = cc.build_chat_model(name, **kw)
    return m.model_copy(update={"governor": gov or Governor()})


def argv_of(call):
    return call["argv"]


def val(argv, flag):
    return argv[argv.index(flag) + 1]


# ── names, effort, environment ─────────────────────────────────────────────
def test_parse_name_models_and_effort():
    assert cc.parse_name("claudecode:sonnet-5.5@xhigh") == ("claude-sonnet-5-5", "xhigh")
    assert cc.parse_name("claudecode:haiku-5.5", default_effort="low") == ("claude-haiku-5-5", "low")
    assert cc.parse_name("claudecode:opus-5.5@max", default_effort="low") == ("claude-opus-5-5", "max")  # suffix wins
    assert cc.parse_name("claudecode:claude-opus-5-5") == ("claude-opus-5-5", None)
    for bad in ("claudecode:gpt-5", "claudecode:", "claudecode:sonnet-5.5@ultra"):
        with pytest.raises(ValueError):
            cc.parse_name(bad)
    assert {"low", "medium", "high", "xhigh", "max"} == set(cc.EFFORTS)


def test_agent_env_allowlist_and_opt_in():
    env = {"PATH": "/bin", "HOME": "/h", "ANTHROPIC_API_KEY": "sk-1", "GITHUB_TOKEN": "t", "RANDOM": "r",
           "CLAUDE_CODE_FOO": "1", "CLAUDE_CODE_OAUTH_TOKEN": "tok", "DEEPDOG_ENV_PASS": "MY_VAR", "MY_VAR": "v"}
    out = ccenv.agent_env(env, extra={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": 100})
    assert {"PATH", "HOME", "CLAUDE_CODE_FOO", "MY_VAR", "CLAUDE_CODE_MAX_OUTPUT_TOKENS"} <= set(out)
    for banned in ("ANTHROPIC_API_KEY", "GITHUB_TOKEN", "RANDOM", "CLAUDE_CODE_OAUTH_TOKEN", "DEEPDOG_ENV_PASS"):
        assert banned not in out


# ── transcript and envelope ────────────────────────────────────────────────
def test_render_and_envelope_parsing():
    msgs = [SystemMessage("SYS"), HumanMessage("hello"),
            AIMessage("", tool_calls=[{"id": "t1", "name": "Search", "args": {"q": "a"}}]),
            ToolMessage("result text", name="Search", tool_call_id="t1")]
    system, transcript = tr.render(msgs)
    assert system == "SYS"
    assert "[user]\nhello" in transcript and "[tool result name=Search id=t1]" in transcript
    specs = [convert_to_openai_tool(Search)]
    reply = 'Sure.\n```json\n{"content": "hi", "tool_calls": [{"name": "Search", "args": {"q": "x"}}]}\n```'
    content, calls = tr.parse_envelope(reply, specs)
    assert content == "hi" and calls[0]["name"] == "Search" and calls[0]["id"].startswith("call_")
    for bad in ("no json here", '{"tool_calls": [{"name": "Nope", "args": {}}]}',
                '{"tool_calls": [{"name": "Search", "args": {}}]}', '{"tool_calls": "x"}'):
        with pytest.raises(tr.ProtocolError):
            tr.parse_envelope(bad, specs)


# ── calls through the fake binary ──────────────────────────────────────────
def test_plain_call_argv_stdin_cost_and_isolation(fake_claude, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-reach-child")
    fake_claude.script([{"text": "hello", "cost": 0.002, "util": {"five_hour": 0.1, "seven_day": 0.2}}])
    gov = Governor()
    res = mk("claudecode:sonnet-5.5@xhigh", gov).invoke([SystemMessage("SYS"), HumanMessage("hi there")])
    assert res.content == "hello"
    call = fake_claude.calls()[0]
    a = argv_of(call)
    assert val(a, "--model") == "claude-sonnet-5-5" and val(a, "--effort") == "xhigh"
    assert val(a, "--system-prompt") == "SYS" and val(a, "--tools") == ""
    assert "--bare" not in a and "-p" in a
    assert "hi there" in call["stdin"] and "hi there" not in a           # prompt travels on stdin, not argv
    assert "ddcc-" in call["cwd"]                                         # empty temp dir, not the repo
    assert "ANTHROPIC_API_KEY" not in call["env_names"]
    assert gov.cost == pytest.approx(0.002) and gov.util == {"five_hour": 0.1, "seven_day": 0.2}
    assert res.usage_metadata["total_tokens"] == 355 and res.response_metadata["cost_usd"] == 0.002


def test_async_call(fake_claude):
    fake_claude.script([{"text": "async hello"}])
    res = asyncio.run(mk().ainvoke([HumanMessage("hi")]))
    assert res.content == "async hello"


def test_tools_mode_returns_tool_calls(fake_claude):
    fake_claude.script([{"text": json.dumps({"content": "plan", "tool_calls": [
        {"name": "Search", "args": {"q": "a"}}, {"name": "Search", "args": {"q": "b"}}]})}])
    res = mk().bind_tools([Search]).invoke([HumanMessage("go")])
    assert [c["args"]["q"] for c in res.tool_calls] == ["a", "b"] and res.content == "plan"
    assert '"name": "Search"' in fake_claude.calls()[0]["stdin"]


def test_malformed_reply_is_repaired_once_then_a_protocol_error(fake_claude):
    good = json.dumps({"content": "", "tool_calls": [{"name": "Search", "args": {"q": "a"}}]})
    fake_claude.script([{"text": "not json"}, {"text": good}])
    res = mk().bind_tools([Search]).invoke([HumanMessage("go")])
    assert res.tool_calls[0]["name"] == "Search"
    calls = fake_claude.calls()
    assert len(calls) == 2 and "rejected" in calls[1]["stdin"]
    fake_claude.script([{"text": "still not json"}])
    import shutil, pathlib  # fresh counter and log for the second scenario
    pathlib.Path(os.environ["DEEPDOG_CC_FAKE_LOG"]).unlink()
    with pytest.raises(tr.ProtocolError):
        mk().bind_tools([Search]).invoke([HumanMessage("go")])
    assert len(fake_claude.calls()) == 2


def test_structured_output_sync_and_async(fake_claude):
    fake_claude.script([{"text": '{"research_brief": "B"}'}])
    m = mk().with_structured_output(Brief, method="json_mode")
    assert m.invoke([HumanMessage("q")]) == Brief(research_brief="B")
    assert asyncio.run(m.ainvoke([HumanMessage("q")])) == Brief(research_brief="B")
    assert "JSON Schema" in fake_claude.calls()[0]["stdin"]


def test_large_system_prompt_goes_through_stdin(fake_claude):
    fake_claude.script([{"text": "ok"}])
    mk().invoke([SystemMessage("S" * 150_000), HumanMessage("hi")])
    call = fake_claude.calls()[0]
    assert len(val(call["argv"], "--system-prompt")) < 200 and "[system]\n" + "S" * 100 in call["stdin"]


# ── governor: plan window, cap, hard limit, errors ─────────────────────────
def test_plan_window_guard_stops_before_spawning(fake_claude):
    fake_claude.script([{"text": "a", "util": {"five_hour": 0.85}}])
    gov = Governor(plan_window_max=0.8)
    m = mk(gov=gov)
    m.invoke([HumanMessage("1")])
    with pytest.raises(GovernorStop) as e:
        m.invoke([HumanMessage("2")])
    assert len(fake_claude.calls()) == 1 and gov.stopped == "plan_window_guard"
    assert _classify_error(e.value) == "fatal"


def test_usd_cap_stops(fake_claude):
    fake_claude.script([{"text": "a", "cost": 0.004}])
    gov = Governor(usd_cap=0.005)
    m = mk(gov=gov)
    m.invoke([HumanMessage("1")]); m.invoke([HumanMessage("2")])
    with pytest.raises(GovernorStop):
        m.invoke([HumanMessage("3")])
    assert len(fake_claude.calls()) == 2


def test_plan_limit_reply_is_fatal_and_server_error_is_retryable(fake_claude):
    fake_claude.script([{"text": "You've hit your limit", "is_error": True, "subtype": "error_during_execution",
                         "api_error_status": 429}])
    with pytest.raises(GovernorStop) as e:
        mk().invoke([HumanMessage("x")])
    assert _classify_error(e.value) == "fatal"
    fake_claude.script([{"text": "boom", "is_error": True, "subtype": "error_during_execution",
                         "api_error_status": 500}])
    with pytest.raises(cc.ClaudeCodeCallError) as e2:
        mk().invoke([HumanMessage("x")])
    assert _classify_error(e2.value) == "retryable"


def test_no_result_event_is_an_error(fake_claude):
    fake_claude.script([{"no_result": True, "exit": 1}])
    with pytest.raises(cc.ClaudeCodeCallError):
        mk().invoke([HumanMessage("x")])


def test_hooks_in_children_are_counted(fake_claude):
    fake_claude.script([{"text": "a", "hooks": 2}])
    gov = Governor()
    mk(gov=gov).invoke([HumanMessage("x")])
    assert gov.stats()["hooks_seen"] == 2


# ── process control ────────────────────────────────────────────────────────
def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _wait_dead(pid, s=3.0):
    end = time.time() + s
    while time.time() < end:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def test_timeout_kills_the_process_group(fake_claude):
    fake_claude.script([{"text": "slow", "delay": 30}])
    with pytest.raises(TimeoutError):
        mk(gov=Governor(timeout_s=0.6)).invoke([HumanMessage("x")])
    assert _wait_dead(fake_claude.calls()[0]["pid"])


def test_async_cancellation_kills_the_process_group(fake_claude):
    fake_claude.script([{"text": "slow", "delay": 30}])

    async def go():
        t = asyncio.ensure_future(mk().ainvoke([HumanMessage("x")]))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if fake_claude.calls():
                break
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
    asyncio.run(go())
    assert _wait_dead(fake_claude.calls()[0]["pid"])


def test_concurrency_limit_is_enforced(fake_claude):
    fake_claude.script([{"text": "a", "delay": 0.4}])
    m = mk(gov=Governor(max_concurrent=2))

    async def go():
        await asyncio.gather(*[m.ainvoke([HumanMessage(str(i))]) for i in range(5)])
    asyncio.run(go())
    spans = [(c["start"], c["end"]) for c in fake_claude.calls()]
    assert len(spans) == 5
    peak = max(sum(1 for s, e in spans if s <= t < e) for t in [s for s, _ in spans])
    assert peak <= 2


# ── preflight ──────────────────────────────────────────────────────────────
def test_preflight_reports_missing_binary_and_signed_out(fake_claude, monkeypatch):
    monkeypatch.setenv("DEEPDOG_CC_FAKE_LOGGED_OUT", "1")
    with pytest.raises(cc.ClaudeCodeUnavailable, match="not signed in"):
        cc.ensure_ready(force=True)
    monkeypatch.delenv("DEEPDOG_CC_FAKE_LOGGED_OUT")
    monkeypatch.delenv("DEEPDOG_CLAUDE_BIN")
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(cc.ClaudeCodeUnavailable, match="not found"):
        cc.ensure_ready(force=True)


def test_governor_breaks_cost_down_by_model(fake_claude):
    fake_claude.script([{"text": "a", "cost": 0.01}])
    gov = Governor()
    mk("claudecode:sonnet-5.5@low", gov).invoke([HumanMessage("x")])
    mk("claudecode:haiku-5.5@low", gov).invoke([HumanMessage("x")])
    mk("claudecode:haiku-5.5@low", gov).invoke([HumanMessage("x")])
    bm = gov.stats()["by_model"]
    assert bm["claude-sonnet-5-5"]["calls"] == 1 and bm["claude-haiku-5-5"]["calls"] == 2
    assert bm["claude-haiku-5-5"]["cost"] == pytest.approx(0.02) and bm["claude-haiku-5-5"]["output_tokens"] == 100
