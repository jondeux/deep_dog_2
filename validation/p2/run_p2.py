#!/usr/bin/env python3
"""P2 live contract probes for the claudecode: provider (haiku-5.5, medium effort, one call each).

Spend guard: stops before any probe once the list-price ledger reaches LIMIT_USD. Writes
validation/p2/p2_results.json (no raw init events: only selected fields) and prints a summary.
Run: python3 validation/p2/run_p2.py"""
import asyncio, json, os, subprocess, sys, tempfile, time, shutil
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pydantic import BaseModel
from langchain_core.messages import HumanMessage, SystemMessage
from deep_research.providers import claude_code as cc
from deep_research.providers.claude_code.chat_model import DEFAULT_FLAGS
from deep_research.providers.claude_code.env import agent_env
from deep_research.providers.claude_code.events import Digest
from deep_research.providers.claude_code.governor import Governor

LIMIT_USD = 0.05
OUT = os.path.join(os.path.dirname(__file__), "p2_results.json")
MODEL = "claudecode:haiku-5.5@medium"
results, ledger = {}, [0.0]


class Search(BaseModel):
    """Search the web for a query."""
    q: str


class Calc(BaseModel):
    """Evaluate an arithmetic expression."""
    expression: str


def guard():
    if ledger[0] >= LIMIT_USD:
        raise SystemExit(f"spend guard: ${ledger[0]:.4f} >= ${LIMIT_USD}")


def raw_call(system, prompt, *, extra=None, flags=None, env_extra=None, model="claude-haiku-5-5", effort="medium"):
    """One real `claude -p` call, returning (digest, all events, rc, stderr tail)."""
    guard()
    binary = cc.ensure_ready()["binary"]
    argv = [binary, "-p", "--model", model, "--effort", effort, "--output-format", "stream-json", "--verbose",
            "--system-prompt", system]
    argv += (extra if extra is not None else ["--tools", ""]) + (DEFAULT_FLAGS if flags is None else flags)
    cwd = tempfile.mkdtemp(prefix="ddcc-p2-")
    try:
        p = subprocess.run(argv, input=prompt, capture_output=True, text=True, timeout=240, cwd=cwd,
                           env=agent_env(extra=env_extra))
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    events = []
    for line in p.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    d = Digest().feed_all(p.stdout)
    ledger[0] += d.cost
    return d, events, p.returncode, p.stderr[-200:]


def init_of(events):
    e = next((x for x in events if x.get("type") == "system" and x.get("subtype") == "init"), {})
    return {"tools": e.get("tools"), "mcp_servers": e.get("mcp_servers"), "slash_commands_n": len(e.get("slash_commands") or []),
            "agents": e.get("agents"), "model": e.get("model"), "permissionMode": e.get("permissionMode"),
            "apiKeySource": e.get("apiKeySource"), "version": e.get("claude_code_version")}


def record(name, **kw):
    results[name] = kw
    print(f"[{name}] " + json.dumps(kw, default=str)[:600], flush=True)


def probe_a():
    """a) clean model: no tools, own system prompt honoured."""
    d, ev, rc, err = raw_call("You are a parrot. Whatever the user says, reply with exactly the single word SQUAWK.",
                              "What is the capital of France?")
    ini = init_of(ev)
    record("a_clean_model", ok=d.ok, reply=d.text[:80], init=ini, cost=d.cost, rc=rc,
           pass_=bool(d.ok and "SQUAWK" in d.text.upper() and not (ini.get("tools") or [])))


def probe_b():
    """b) tool-call envelope reliability through the adapter (haiku medium)."""
    gov = Governor()
    m = cc.build_chat_model(MODEL).model_copy(update={"governor": gov}).bind_tools([Search, Calc])
    r = m.invoke([SystemMessage("You are a research assistant."),
                  HumanMessage("Look up the population of Lisbon and the height of Mount Kosciuszko in two separate "
                                "searches, and also compute 17*23. Call every needed tool in one reply.")])
    names = [c["name"] for c in r.tool_calls]
    record("b_tool_calls", names=names, args=[c["args"] for c in r.tool_calls], cost=gov.cost, calls=gov.calls,
           pass_=sorted(names) == ["Calc", "Search", "Search"])
    ledger[0] += gov.cost


def probe_b2():
    """b2) schema mode (the engine's research-brief shape)."""
    from deep_research.state_scope import ResearchQuestion
    gov = Governor()
    m = cc.build_chat_model(MODEL).model_copy(update={"governor": gov})
    out = m.with_structured_output(ResearchQuestion, method="json_mode").invoke(
        [HumanMessage("Turn this into a research brief: how do sodium-ion batteries compare to lithium-ion for home storage?")])
    record("b2_schema_mode", brief=out.research_brief[:120], target_language=out.target_language, cost=gov.cost,
           calls=gov.calls, pass_=bool(out.research_brief and out.target_language))
    ledger[0] += gov.cost


def probe_c():
    """c) WebSearch headless on the plan sign-in."""
    d, ev, rc, err = raw_call(
        "You are a search backend. Use the WebSearch tool, then reply with JSON only: "
        '[{"title":..., "url":..., "snippet":...}] with at most 3 items.',
        "Search the web for: sodium-ion battery home energy storage 2026",
        extra=["--tools", "WebSearch"], flags=DEFAULT_FLAGS + ["--allowedTools", "WebSearch"])
    u = (d.result or {}).get("usage") or {}
    n = ((u.get("server_tool_use") or {}).get("web_search_requests"))
    record("c_websearch", ok=d.ok, web_search_requests=n, reply_head=d.text[:300], cost=d.cost, rc=rc,
           denials=len((d.result or {}).get("permission_denials") or []), init_tools=init_of(ev).get("tools"),
           pass_=bool(d.ok and n))


def probe_e():
    """e) CLAUDE_CODE_MAX_OUTPUT_TOKENS is honoured (tiny limit, long ask)."""
    d, ev, rc, err = raw_call("You are a writer.", "Write a 1500-word essay on the history of the bicycle.",
                              env_extra={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "150"})
    u = (d.result or {}).get("usage") or {}
    record("e_max_output_tokens", ok=d.ok, output_tokens=u.get("output_tokens"), stop_reason=(d.result or {}).get("stop_reason"),
           chars=len(d.text), cost=d.cost, rc=rc, pass_=bool(u.get("output_tokens") and u["output_tokens"] <= 400))


def probe_f():
    """f) rate_limit_event present headless + g) hooks in children, default flags vs control."""
    d1, ev1, *_ = raw_call("Reply with the word OK.", "ping")
    no_disable = [f for f in DEFAULT_FLAGS if f not in ("--settings", json.dumps({"disableAllHooks": True}))]
    d2, ev2, *_ = raw_call("Reply with the word OK.", "ping", flags=no_disable)
    record("f_rate_limit_event", util=d1.util, pass_=any(v is not None for v in d1.util.values()))
    record("g_hooks_in_children", hooks_default_flags=d1.hooks_seen, hooks_control_no_disable=d2.hooks_seen,
           slash_default=init_of(ev1)["slash_commands_n"], slash_control=init_of(ev2)["slash_commands_n"],
           costs=[d1.cost, d2.cost], pass_=(d1.hooks_seen == 0))


def probe_i():
    """i) 3 concurrent async calls through the governor (live async path)."""
    gov = Governor(max_concurrent=3)
    m = cc.build_chat_model(MODEL).model_copy(update={"governor": gov})

    async def go():
        t0 = time.time()
        rs = await asyncio.gather(*[m.ainvoke([HumanMessage(f"Reply with the number {i} only.")]) for i in range(3)])
        return [r.content.strip() for r in rs], time.time() - t0
    out, secs = asyncio.run(go())
    record("i_concurrent_async", replies=out, seconds=round(secs, 1), cost=gov.cost, util=gov.util,
           pass_=sorted(out) == ["0", "1", "2"] or all(x in "012" for x in "".join(out)))
    ledger[0] += gov.cost


def probe_d():
    """d) can this environment fetch arbitrary URLs with httpx (the engine's fetch_urls path)?"""
    import httpx
    res = {}
    for url in ("https://example.com", "https://en.wikipedia.org/wiki/Sodium-ion_battery", "https://arxiv.org/abs/1706.03762"):
        try:
            r = httpx.get(url, timeout=20, follow_redirects=True)
            res[url] = r.status_code
        except Exception as e:
            res[url] = f"{type(e).__name__}"
    record("d_httpx_fetch", results=res, pass_=all(v == 200 for v in res.values()))


if __name__ == "__main__":
    info = cc.ensure_ready()
    print("ready:", {k: info[k] for k in ("version", "auth_method")})
    for fn in (probe_d, probe_a, probe_b, probe_b2, probe_f, probe_e, probe_c, probe_i):
        try:
            fn()
        except SystemExit as e:
            print("STOPPED:", e)
            break
        except Exception as e:
            record(fn.__name__, error=f"{type(e).__name__}: {str(e)[:200]}", pass_=False)
    results["_ledger_usd"] = round(ledger[0], 6)
    json.dump(results, open(OUT, "w"), indent=1, default=str)
    print("ledger $%.4f" % ledger[0], "->", OUT)
