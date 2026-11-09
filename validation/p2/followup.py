#!/usr/bin/env python3
"""P2 follow-up: (e) what a too-small output limit does, (c) proof WebSearch actually ran (tool_use events)."""
import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
import run_p2 as p
p.ledger[0] = 0.016          # spent by run_p2.py
p.LIMIT_USD = 0.05

d, ev, rc, err = p.raw_call("You are a writer.", "Write a 1500-word essay on the history of the bicycle.",
                            env_extra={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "150"})
r = d.result or {}
print("[e] rc", rc, "subtype", r.get("subtype"), "is_error", r.get("is_error"), "stop", r.get("stop_reason"),
      "terminal", r.get("terminal_reason"), "api_err", r.get("api_error_status"), "out_tokens",
      (r.get("usage") or {}).get("output_tokens"), "text_head", repr((r.get("result") or "")[:160]), "stderr", repr(err[-120:]))

d, ev, rc, err = p.raw_call(
    "You are a search backend. Use the WebSearch tool, then reply with JSON only: "
    '[{"title":..., "url":..., "snippet":...}] with at most 3 items.',
    "Search the web for: DeepResearch Bench leaderboard RACE score",
    extra=["--tools", "WebSearch"], flags=p.DEFAULT_FLAGS + ["--allowedTools", "WebSearch"])
uses = []
for e in ev:
    if e.get("type") == "assistant":
        for b in (e.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                uses.append({"name": b.get("name"), "input": str(b.get("input"))[:100]})
results_blocks = sum(1 for e in ev if e.get("type") == "user" and "tool_result" in json.dumps(e)[:4000])
print("[c] ok", d.ok, "tool_use", uses, "tool_result_events", results_blocks, "cost", d.cost,
      "server_tool_use", (d.result.get("usage") or {}).get("server_tool_use"), "reply_head", repr(d.text[:200]))
print("ledger", round(p.ledger[0], 4))
